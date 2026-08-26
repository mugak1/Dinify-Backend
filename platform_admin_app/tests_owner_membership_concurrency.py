"""
Step 2E.1 — membership mutations must serialize with onboarding decisions.

THE FINDING (Codex P1 on PR #305), stated exactly
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    "When a tenant-plane PUT .../employees changes the current owner's roles or
    active state concurrently, that path locks only the RestaurantEmployee row and
    does not participate in this service's Restaurant lock. It can therefore commit
    immediately after assert_owner_consistency() reads a valid membership but before
    the invitation is inserted, causing this endpoint to return a live claim
    credential for an owner relationship that is already inconsistent."

The remediation is the finding's own option (b): every production membership writer
takes the parent ``Restaurant`` row, which is the point every onboarding writer
already takes first.

WHY LOCKING THE MEMBERSHIP ROWS WOULD NOT HAVE BEEN ENOUGH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Under READ COMMITTED a ``SELECT ... FOR UPDATE`` takes no predicate lock, so locking
the rows the assertion READ cannot stop a row it could not have read from appearing.
REACTIVATION is that case and it is not hypothetical: a soft-deleted owner membership
is invisible to ``assert_owner_consistency`` (which filters ``deleted=False``), and
``create_employee_from_existing_user`` brings it back with an UPDATE.

MEASURED POSTGRESQL BEHAVIOUR, which decides which halves were already safe
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

While one transaction holds the ``restaurants`` row ``FOR UPDATE``, a concurrent
statement on ``restaurant_employees`` is blocked only when referential integrity
makes it touch the parent row:

    INSERT referencing that restaurant      -> BLOCKED (FK takes FOR KEY SHARE)
    UPDATE that changes the FK to it        -> BLOCKED (same RI re-check)
    UPDATE that leaves the FK alone         -> NOT blocked
    DELETE of the child row                 -> NOT blocked

So the membership INSERT was already serialized — incidentally, by one database's RI
triggers rather than by anything this repository states. Everything the finding names
(``roles``, ``active``) and everything adjacent to it (soft-delete, reactivation) was
not. This suite pins all of them through the barrier rather than through RI.

``restaurants_app/tests_table_allocation_lock.py`` records the same trap from the
other side: a test written there to prove table-allocation locking passed against the
buggy code, because ``bulk_create``'s FK lock stalled the allocator regardless. Every
positive assertion here is therefore paired with a negative control that removes the
barrier and shows the proof collapse.

THE HARNESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``TransactionTestCase``, because the whole subject is what two real transactions on
two real connections do to each other.

Blocking is proved POSITIVELY, not by absence of progress: the customer connection
sets ``lock_timeout`` and PostgreSQL raises ``OperationalError`` when the barrier
holds it. A test that merely watched a thread fail to finish would pass for any
reason at all, including a bug that made the write fail.

Synchronisation is ``threading.Event``, released by the code under test reaching a
named seam — never a ``sleep``. Every wait carries a timeout so a genuine deadlock
fails loudly instead of hanging CI.
"""
import threading
from contextlib import ExitStack, contextmanager
from unittest import mock

from django.db import OperationalError, connections
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, RESTAURANT_MANAGER, RESTAURANT_OWNER,
)
from platform_admin_app import (
    onboarding_adoption, onboarding_creation, onboarding_invitations,
)
from platform_admin_app.models import OwnerInvitation, RestaurantOnboarding
from platform_admin_app.onboarding import OwnerConsistencyError
from platform_admin_app.onboarding_creation import ExistingOwner
from restaurants_app.controllers import create_employee as create_employee_controller
from restaurants_app.controllers.employees import (
    create_employee as reactivation_controller,
)
from restaurants_app.endpoints import restaurant_setup
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

REASON = 'Rotating the claim credential while a membership write is in flight.'
WAIT = 30
LOCK_TIMEOUT_MS = 1500

# A phone range distinct from every other suite in this app.
_PHONE = iter(f'0772{n:06d}' for n in range(841000, 849999))

_POSTGRES = connections['default'].vendor == 'postgresql'
_SKIP = 'Row-lock semantics are only meaningful on PostgreSQL.'

_FACTORY = APIRequestFactory()


class _Seam:
    """Two events: the admin reached the window, and the customer is done probing."""

    def __init__(self):
        self.inside = threading.Event()
        self.resume = threading.Event()


def _api(user, method, payload, config_detail='employees'):
    """
    Drive the real restaurant-setup endpoint.

    A genuine bearer token, not ``force_authenticate``: the endpoint calls
    ``decode_jwt_token(request)`` directly and needs the header, and going through
    the real authentication stack is what makes this a proof about the shipped path
    rather than about a view method called in isolation.
    """
    token = str(RefreshToken.for_user(user).access_token)
    request = getattr(_FACTORY, method)(
        f'/api/v1/restaurant-setup/{config_detail}/', payload, format='json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    return RestaurantSetupEndpoint.as_view()(request, config_detail=config_detail)


class _RaceHarness:
    """
    The two-thread race, shared by the invitation and adoption suites.

    Kept separate from the fixtures because the two suites need different tenants —
    one admin-created, one legacy — but exactly the same harness. Calling an unbound
    method across unrelated classes would have worked and would have been a trap for
    the next reader.
    """

    def race(self, admin_call, customer_call, patch_module):
        """
        Run ``admin_call`` parked inside its owner-consistency window while
        ``customer_call`` tries to mutate a membership on another connection.

        Returns ``(admin_outcome, customer_outcome)``, each ``('ok', value)`` or
        ``('error', exception)``.

        The park is the REAL seam the finding names: ``assert_owner_consistency``
        runs for real, and only then does the admin thread stop — so the customer
        write is attempted in exactly the window between the check and the
        consequential write, not merely somewhere inside the transaction.
        """
        seam = _Seam()
        outcomes = {}
        real = patch_module.assert_owner_consistency

        def parked(restaurant):
            result = real(restaurant)
            seam.inside.set()
            assert seam.resume.wait(timeout=WAIT), 'customer probe never finished'
            return result

        def admin_runner():
            try:
                with mock.patch.object(
                    patch_module, 'assert_owner_consistency', parked,
                ):
                    outcomes['admin'] = ('ok', admin_call())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcomes['admin'] = ('error', exc)
            finally:
                seam.inside.set()      # never strand the customer on a failure
                connections.close_all()

        def customer_runner():
            try:
                assert seam.inside.wait(timeout=WAIT), 'admin never reached the seam'
                with connections['default'].cursor() as cursor:
                    cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
                outcomes['customer'] = ('ok', customer_call())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcomes['customer'] = ('error', exc)
            finally:
                seam.resume.set()
                connections.close_all()

        threads = [
            threading.Thread(target=admin_runner),
            threading.Thread(target=customer_runner),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=WAIT)
            self.assertFalse(thread.is_alive(), 'a thread deadlocked')
        return outcomes.get('admin'), outcomes.get('customer')

    def assertBlockedByBarrier(self, outcome):
        """The customer write was stopped by a lock, positively and by name."""
        self.assertIsNotNone(outcome, 'the customer thread produced no outcome')
        kind, value = outcome
        self.assertEqual(
            kind, 'error',
            f'the membership write was NOT blocked; it returned {value!r}',
        )
        self.assertIsInstance(value, OperationalError, repr(value))
        self.assertIn('lock timeout', str(value).lower(), str(value))


class _MembershipRaceBase(_RaceHarness, TransactionTestCase):
    """
    One admin-created restaurant, its owner, and a manager who may edit the team.

    The OWNER is the actor for every customer-plane write, and necessarily so:
    ``employees`` maps to ``team``, which is off-grid and owner-only (Decision 1), so
    a manager is refused 403 and cannot exercise these paths at all. The subject of
    each mutation is the owner's own membership — which is exactly the shape the
    finding describes, since it is the owner's ``roles``/``active`` that
    ``assert_owner_consistency`` reads.
    """

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if not _POSTGRES:
            self.skipTest(_SKIP)
        self.admin = User.objects.create_user(
            first_name='Ada', last_name='Min', email='memrace-admin@t.com',
            username='memrace-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        # An EXISTING owner, deliberately. `mode=existing` never touches
        # `customer_access_state`, so this owner stays `established` while holding a
        # pending invitation — an ordinary supported state (it is exactly what a
        # second restaurant for an already-claimed owner produces) and the only one
        # in which the customer plane can actually be driven. A `mode=new` owner is
        # `pending_initial_claim` and `CustomerJWTAuthentication` refuses their
        # token, so no customer-plane write could be raced against at all.
        phone = next(_PHONE)
        self.owner = User.objects.create_user(
            first_name='Owen', last_name='Ner', email='memrace-owner@t.com',
            phone_number=f'256{phone[1:]}', username=f'256{phone[1:]}',
            country='UG', password='x', roles=[],
        )
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Barrier Cafe', location='Kamwokya', is_test=False,
            owner=ExistingOwner(self.owner.id),
            actor=self.admin, reason='Creating the membership-race fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.invitation = self.creation.invitation
        self.owner_membership = RestaurantEmployee.objects.get(
            restaurant=self.restaurant, user=self.owner,
        )

    # ------------------------------------------------------------------ helpers

    def reissue(self):
        return onboarding_invitations.reissue_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=self.invitation.id,
            actor=self.admin, reason=REASON,
        )

    def deactivate_owner(self):
        return _api(self.owner, 'put', {
            'id': str(self.owner_membership.id), 'active': 'false',
        })

    def strip_owner_role(self):
        return _api(self.owner, 'put', {
            'id': str(self.owner_membership.id), 'roles': [RESTAURANT_MANAGER],
        })

    def soft_delete_owner(self):
        return _api(self.owner, 'delete', {
            'id': str(self.owner_membership.id),
            'deletion_reason': 'Removing the owner membership mid-race.',
        })

    def insert_second_owner(self):
        """
        A genuine membership INSERT, through ``POST create-employee``.

        NOT through ``POST employees``: that branch runs the reactivation shortcut
        first and, when there is no soft-deleted row to revive, falls through to
        ``Secretary.create()`` with ``restaurant`` read_only and no ``server_values``
        — an unavoidable NOT NULL violation. That is pre-existing on ``origin/main``
        and is reported, not fixed, here.
        """
        phone = next(_PHONE)
        return _api(self.owner, 'post', {
            'first_name': 'Second', 'last_name': 'Owner',
            'email': f'second-{phone}@t.com', 'phone_number': phone,
            'restaurant': str(self.restaurant.id), 'roles': [RESTAURANT_OWNER],
        }, config_detail='create-employee')

    def reactivate_second_owner(self, user):
        """The reactivation shortcut — an UPDATE, and the case RI does not cover."""
        return _api(self.owner, 'post', {
            'user': str(user.id), 'restaurant': str(self.restaurant.id),
            'roles': [RESTAURANT_OWNER],
        })

    def spare_user(self):
        phone = next(_PHONE)
        return User.objects.create_user(
            first_name='Spare', last_name='User',
            email=f'memrace-{phone}@t.com',
            phone_number=f'256{phone[1:]}', username=f'256{phone[1:]}',
            country='UG', password='x', roles=[],
        )

    def unresolved_invitations(self):
        return list(OwnerInvitation.objects.filter(
            onboarding=self.onboarding,
            consumed_at__isnull=True,
            cancelled_at__isnull=True,
            superseded_at__isnull=True,
        ))

    def owner_memberships(self):
        return [
            row for row in RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True, deleted=False,
            )
            if RESTAURANT_OWNER in (row.roles or [])
        ]

# ═══════════════════════════════════════════════════════════════════════════════
# Case A — the onboarding writer holds the barrier first
# ═══════════════════════════════════════════════════════════════════════════════

class ReissueHoldsTheBarrierTests(_MembershipRaceBase):
    """
    Reissue takes the ``Restaurant`` row, passes the consistency check, and the
    membership mutation cannot cross that point until the credential is committed.

    The tenant may of course drift AFTERWARDS — that is allowed, and the read
    projection reports it honestly. What must not happen is a mutation landing
    INSIDE the decision, which is what would let a credential be minted for an
    ownership the platform had already stopped believing in.
    """

    def test_deactivation_cannot_commit_inside_the_reissue_decision(self):
        admin, customer = self.race(
            self.reissue, self.deactivate_owner, onboarding_invitations,
        )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)

        # The credential the admin minted is real and the owner authority it was
        # minted against was still intact at that moment.
        self.assertEqual(len(self.unresolved_invitations()), 1)
        self.assertTrue(admin[1].claim_token)
        self.assertEqual(len(self.owner_memberships()), 1)

    def test_role_removal_cannot_commit_inside_the_reissue_decision(self):
        # Codex named `roles` OR `active`; this is the `roles` half, and it is the
        # one an owner-reassignment UI is most likely to send.
        admin, customer = self.race(
            self.reissue, self.strip_owner_role, onboarding_invitations,
        )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)
        self.assertEqual(len(self.owner_memberships()), 1)

    def test_the_membership_write_is_blocked_not_broken(self):
        """
        DELAYED, NOT DENIED. The barrier must be a queue, not a refusal: once the
        onboarding decision commits, the same membership write goes through
        unchanged. A test that only proved "it did not commit" would also pass for a
        fix that simply broke the endpoint.

        The tenant is then genuinely inconsistent — and that is ALLOWED. The
        invariant this PR protects is that no mutation lands INSIDE a decision, not
        that a tenant can never drift afterwards; the read projection reports the
        drift honestly and the credential stays cancellable.
        """
        admin, customer = self.race(
            self.reissue, self.strip_owner_role, onboarding_invitations,
        )
        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)

        retried = self.strip_owner_role()
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(self.owner_memberships(), [])

    def test_soft_delete_cannot_commit_inside_the_reissue_decision(self):
        # A soft-delete removes the row from the set the assertion counts just as
        # surely as a role change does, and a child DELETE/UPDATE takes no parent
        # lock of its own — so this half depends entirely on the new barrier.
        admin, customer = self.race(
            self.reissue, self.soft_delete_owner, onboarding_invitations,
        )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)
        self.assertEqual(len(self.owner_memberships()), 1)

    def test_a_second_owner_insert_cannot_commit_inside_the_decision(self):
        """
        The predicate-insert half.

        On PostgreSQL this one is ALSO blocked by referential integrity, so it is not
        evidence that the barrier works — the reactivation case below is. It is
        pinned because the outcome is what the invariant requires, however it is
        obtained.

        The assertion reads the LOG rather than an exception because
        ``handle_create_employee`` wraps the controller in ``except Exception`` and
        answers 500. The lock timeout is therefore proved positively by name, and the
        500 alone is never treated as proof of blocking — a 500 has many causes.
        """
        with self.assertLogs(
            'restaurants_app.endpoints.restaurant_setup', level='ERROR',
        ) as captured:
            admin, customer = self.race(
                self.reissue, self.insert_second_owner, onboarding_invitations,
            )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertEqual(customer[0], 'ok', repr(customer))
        self.assertEqual(customer[1].status_code, 500)
        self.assertTrue(
            any('lock timeout' in line.lower() for line in captured.output),
            f'the insert was not blocked by a lock: {captured.output}',
        )
        self.assertEqual(len(self.owner_memberships()), 1)

    def test_a_reactivated_owner_cannot_commit_inside_the_decision(self):
        """
        THE CASE THAT ONLY THE BARRIER CLOSES.

        A soft-deleted owner membership is invisible to ``assert_owner_consistency``
        and unreachable by any row lock the assertion could have taken; reactivating
        it is an UPDATE that never touches the FK, so referential integrity does not
        block it either. Without the parent barrier this commits straight through a
        held ``Restaurant`` lock and the reissue mints a credential for a restaurant
        that now has two live owners.
        """
        spare = self.spare_user()
        dormant = RestaurantEmployee.objects.create(
            user=spare, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=False, deleted=True,
        )
        self.assertEqual(len(self.owner_memberships()), 1)

        admin, customer = self.race(
            self.reissue, lambda: self.reactivate_second_owner(spare),
            onboarding_invitations,
        )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)
        dormant.refresh_from_db()
        self.assertTrue(dormant.deleted)
        self.assertEqual(len(self.owner_memberships()), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Case B — the membership mutation gets there first
# ═══════════════════════════════════════════════════════════════════════════════

class MembershipMutationWinsTests(_MembershipRaceBase):
    """
    When the customer plane commits first, the onboarding writer sees the committed
    drift under its own lock and REFUSES. No credential is minted, nothing is
    repaired, and the tenant is left exactly as the customer plane left it.
    """

    def test_reissue_refuses_after_a_committed_deactivation(self):
        response = self.deactivate_owner()
        # A restaurant must keep one active owner, so deactivating the only one is
        # the 409 guard, not a mutation. Strip the role instead to produce real
        # drift through a path the endpoint does allow.
        self.assertEqual(response.status_code, 409)

        response = self.strip_owner_role()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.owner_memberships(), [])

        before = OwnerInvitation.objects.filter(onboarding=self.onboarding).count()
        with self.assertRaises(OwnerConsistencyError) as caught:
            self.reissue()
        self.assertEqual(caught.exception.code, 'missing_owner_membership')

        # NO CREDENTIAL. The refusal is the whole point: an invitation instructs one
        # specific person to take control, and the platform no longer agrees who that is.
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), before,
        )
        self.assertEqual(len(self.unresolved_invitations()), 1)
        self.assertEqual(self.unresolved_invitations()[0].id, self.invitation.id)

    def test_reissue_refuses_after_a_committed_second_owner(self):
        self.assertEqual(self.insert_second_owner().status_code, 200)
        self.assertEqual(len(self.owner_memberships()), 2)

        with self.assertRaises(OwnerConsistencyError) as caught:
            self.reissue()
        self.assertEqual(caught.exception.code, 'multiple_owner_memberships')
        self.assertEqual(self.unresolved_invitations()[0].id, self.invitation.id)

    def test_reissue_refuses_after_a_committed_soft_delete(self):
        self.assertEqual(self.soft_delete_owner().status_code, 200)

        with self.assertRaises(OwnerConsistencyError) as caught:
            self.reissue()
        self.assertEqual(caught.exception.code, 'missing_owner_membership')
        self.assertEqual(self.unresolved_invitations()[0].id, self.invitation.id)


# ═══════════════════════════════════════════════════════════════════════════════
# The negative control
# ═══════════════════════════════════════════════════════════════════════════════

class BarrierRemovedTests(_MembershipRaceBase):
    """
    Remove the barrier and the proofs must collapse. Without this, every assertion
    above could be passing for a reason that has nothing to do with the fix — which
    is exactly what happened once already in this repository (see the module
    docstring's note on ``tests_table_allocation_lock``).

    The barrier is neutralised at the endpoint's own import site, which is the only
    honest way to do it: patching the helper's definition would leave the endpoint
    holding the name it imported at module load.
    """

    @contextmanager
    def _without_barrier(self):
        """
        Neutralise the barrier at EVERY import site.

        Each writer did ``from ... import lock_restaurant_for_membership_mutation``,
        so it holds its own reference and patching the helper's definition would
        leave those references intact — a negative control that quietly failed to
        remove the thing it was testing would be worse than none at all.
        """
        with ExitStack() as stack:
            for module in (
                restaurant_setup, create_employee_controller, reactivation_controller,
            ):
                stack.enter_context(mock.patch.object(
                    module, 'lock_restaurant_for_membership_mutation',
                    lambda restaurant_id: None,
                ))
            yield

    def test_role_removal_interleaves_when_the_barrier_is_gone(self):
        with self._without_barrier():
            admin, customer = self.race(
            self.reissue, self.strip_owner_role, onboarding_invitations,
        )

        # The membership write sails through the reissue's decision window...
        self.assertEqual(customer[0], 'ok', repr(customer))
        self.assertEqual(customer[1].status_code, 200, customer[1].data)
        # ...and the credential is minted anyway, for an ownership that no longer holds.
        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertTrue(admin[1].claim_token)
        self.assertEqual(
            self.owner_memberships(), [],
            'the exact defect: a live claim credential issued for a restaurant with '
            'no owner authority',
        )

    def test_reactivation_interleaves_when_the_barrier_is_gone(self):
        spare = self.spare_user()
        dormant = RestaurantEmployee.objects.create(
            user=spare, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=False, deleted=True,
        )

        with self._without_barrier():
            admin, customer = self.race(
                self.reissue, lambda: self.reactivate_second_owner(spare),
                onboarding_invitations,
            )

        self.assertEqual(customer[0], 'ok', repr(customer))
        self.assertEqual(customer[1].status_code, 200, customer[1].data)
        dormant.refresh_from_db()
        self.assertFalse(dormant.deleted)
        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertEqual(
            len(self.owner_memberships()), 2,
            'the predicate-insert hole: a credential minted while a second owner '
            'membership was reactivated inside the decision',
        )

    def test_soft_delete_interleaves_when_the_barrier_is_gone(self):
        with self._without_barrier():
            admin, customer = self.race(
            self.reissue, self.soft_delete_owner, onboarding_invitations,
        )

        self.assertEqual(customer[0], 'ok', repr(customer))
        self.assertEqual(customer[1].status_code, 200, customer[1].data)
        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertEqual(self.owner_memberships(), [])


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy adoption inherits the guarantee
# ═══════════════════════════════════════════════════════════════════════════════

class AdoptionInheritsTheBarrierTests(_RaceHarness, TransactionTestCase):
    """
    The exposure predates Step 2E. ``adopt_existing_restaurant`` has done
    ``Restaurant`` lock -> ``assert_owner_consistency`` -> insert since Step 2B, in
    shipped code, with the same open window. It needed no change of its own: once the
    membership writers take the same row, adoption inherits the barrier.

    Deliberately NOT redesigned here — these are proofs, not a rewrite.
    """

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if not _POSTGRES:
            self.skipTest(_SKIP)
        self.admin = User.objects.create_user(
            first_name='Ada', last_name='Opt', email='adopt-race-admin@t.com',
            username='adopt-race-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        phone = next(_PHONE)
        self.owner = User.objects.create_user(
            first_name='Leg', last_name='Acy', email='adopt-race-owner@t.com',
            phone_number=f'256{phone[1:]}', username=f'256{phone[1:]}',
            country='UG', password='x', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Adoption Race', location='Ntinda', owner=self.owner,
        )
        self.owner_membership = RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )

    def adopt(self):
        return onboarding_adoption.adopt_existing_restaurant(
            restaurant_id=self.restaurant.id, actor=self.admin,
            reason='Adopting while a membership write is in flight.',
        )

    def strip_owner_role(self):
        return _api(self.owner, 'put', {
            'id': str(self.owner_membership.id), 'roles': [RESTAURANT_MANAGER],
        })

    def test_membership_mutation_cannot_commit_inside_the_adoption_decision(self):
        admin, customer = self.race(
            self.adopt, self.strip_owner_role, onboarding_adoption,
        )

        self.assertEqual(admin[0], 'ok', repr(admin))
        self.assertBlockedByBarrier(customer)
        self.assertEqual(
            RestaurantOnboarding.objects.filter(restaurant=self.restaurant).count(), 1,
        )

    def test_adoption_refuses_after_a_committed_membership_mutation(self):
        self.assertEqual(self.strip_owner_role().status_code, 200)

        with self.assertRaises(OwnerConsistencyError) as caught:
            self.adopt()
        self.assertEqual(caught.exception.code, 'missing_owner_membership')
        # Adoption never repairs: the drift is left for a human, and no provenance
        # row was invented on the way past.
        self.assertFalse(
            RestaurantOnboarding.objects.filter(restaurant=self.restaurant).exists()
        )
