"""
Step 2F.2 — the redemption transaction against everything that races it.

WHAT THESE PROVE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

§30  redeem vs redeem              — exactly one may succeed
§31  redeem vs reissue             — a superseded credential can never be consumed
§32  redeem vs cancel              — never both cancelled AND consumed
§33  redeem vs membership mutation — the PR #306 barrier is actually CONSUMED
§34  redeem vs phone change        — a stale factor cannot be spent

THE HARNESS, AND WHY IT LOOKS LIKE THIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``TransactionTestCase``, because the whole subject is what two real transactions on two
real connections do to each other. ``TestCase`` wraps everything in one transaction and
would prove nothing.

Blocking is proved POSITIVELY, never by absence of progress: the second connection sets
``lock_timeout`` and PostgreSQL raises ``OperationalError`` by name when the barrier
holds it. A test that merely watched a thread fail to finish would pass for any reason,
including a bug that made the write fail on its own.

Synchronisation is ``threading.Event``, released by the code under test reaching a NAMED
SEAM — never a ``sleep``. Every wait carries a timeout so a genuine deadlock fails loudly
instead of hanging CI.

THE SEAM IS INSIDE THE DECISION. The parked point is ``assert_owner_consistency``, which
runs FOR REAL and only then blocks — so the competing write is attempted in exactly the
window between the check and the consume it guards, which is the window
``tests_owner_membership_concurrency`` was built to close and this suite is built to
spend.
"""
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.db import OperationalError, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_OWNER,
)
from platform_admin_app import (
    onboarding_creation, onboarding_invitations, owner_claim_redemption,
)
from platform_admin_app.models import OwnerInvitation
from platform_admin_app.onboarding_creation import NewOwner
from platform_admin_app.onboarding_reads import onboarding_summary
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint
from restaurants_app.models import RestaurantEmployee
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

WAIT = 30
LOCK_TIMEOUT_MS = 1500
# How long an invitation stays live in the decision-clock tests, and how far past that
# the row is held. Small enough to keep the suite quick, large enough that a pre-lock and
# a post-lock clock land on opposite sides of the deadline on a loaded CI runner.
EXPIRY_WINDOW = 1.5
LOCK_HOLD_MARGIN = 1.0
DEV_OTP = '1234'
GOOD_PASSWORD = 'Kabalagala-Sunrise-7'
REASON = 'Acting on this credential while a redemption is in flight.'

# A phone range distinct from every other suite in this app.
_PHONE = iter(f'0772{n:06d}' for n in range(871000, 879999))

_POSTGRES = connections['default'].vendor == 'postgresql'
_SKIP = 'Row-lock semantics are only meaningful on PostgreSQL.'

_FACTORY = APIRequestFactory()


def _api_call(user, method, payload, config_detail='employees'):
    """
    A zero-argument callable that drives the REAL restaurant-setup endpoint.

    Not ``force_authenticate``: the endpoint calls ``decode_jwt_token(request)`` directly
    and needs the header, and going through the real authentication stack is what makes
    this a proof about the shipped path.

    ━━ THE TOKEN IS MINTED NOW, AND THE REQUEST IS BUILT NOW ━━━━━━━━━━━━━━━━━━━━━

    ``RefreshToken.for_user`` INSERTs an ``OutstandingToken`` row, whose FK to ``users``
    makes PostgreSQL take ``FOR KEY SHARE`` on that user's row. Redemption holds the
    OWNER's ``users`` row ``FOR UPDATE``, so minting a token for the owner INSIDE the race
    blocks on ``users`` — a real lock, but not the one under test, and it would have made
    every membership proof below pass for the wrong reason.

    (That interaction is real production behaviour, not a test artefact: while a
    redemption is in flight, anything that INSERTs a row referencing that owner — a login
    by the same owner, say — waits for it. The transaction performs no I/O, so the wait
    is bounded by database work.)
    """
    token = str(RefreshToken.for_user(user).access_token)
    request = getattr(_FACTORY, method)(
        f'/api/v1/restaurant-setup/{config_detail}/', payload, format='json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    view = RestaurantSetupEndpoint.as_view()
    return lambda: view(request, config_detail=config_detail)


def _api(user, method, payload, config_detail='employees'):
    """Build and immediately dispatch — for the sequential (non-raced) cases."""
    return _api_call(user, method, payload, config_detail)()


class RedemptionRaceBase(TransactionTestCase):
    """One admin-created restaurant, a brand-new owner, and a live claim credential."""

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if not _POSTGRES:
            self.skipTest(_SKIP)
        self.staff = User.objects.create_user(
            first_name='Ada', last_name='Min',
            email=f'ocrc-admin-{next(_PHONE)}@t.com',
            username=f'ocrc-admin-{next(_PHONE)}', country='UG', password='x',
            roles=[], account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Race Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Owen', 'Ner', next(_PHONE), None),
            actor=self.staff, reason='Creating the redemption race fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.invitation = self.creation.invitation
        self.owner = self.creation.owner
        self.token = self.creation.claim_token
        self.issue_challenge()

    def tearDown(self):
        connections.close_all()
        super().tearDown()

    # --- fixtures ---

    def issue_challenge(self):
        """Deliver a real owner-claim OTP, bound to the owner's canonical phone."""
        self.assertTrue(
            OtpManager().make_otp(
                user=self.owner, msisdn=self.owner.phone_number,
                purpose=owner_claim_redemption.OWNER_CLAIM_OTP_PURPOSE,
            )
        )

    def redeem(self, token=None, password=GOOD_PASSWORD):
        return owner_claim_redemption.redeem_owner_claim(
            raw_token=token or self.token, otp=DEV_OTP,
            encoded_password=make_password(password) if password else None,
        )

    # --- the race ---

    def race(self, first, second, park_at=owner_claim_redemption):
        """
        Run ``first`` parked inside its owner-consistency window while ``second`` runs
        on another connection.

        Returns ``(first_outcome, second_outcome)``, each ``('ok', value)`` or
        ``('error', exception)``. ``park_at`` names the module whose
        ``assert_owner_consistency`` is the seam, so a test can park EITHER side.
        """
        inside, resume = threading.Event(), threading.Event()
        outcomes = {}
        real = park_at.assert_owner_consistency

        def parked(restaurant):
            result = real(restaurant)
            inside.set()
            assert resume.wait(timeout=WAIT), 'the second thread never finished'
            return result

        def first_runner():
            try:
                with mock.patch.object(
                    park_at, 'assert_owner_consistency', parked,
                ):
                    outcomes['first'] = ('ok', first())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcomes['first'] = ('error', exc)
            finally:
                inside.set()      # never strand the second thread on a failure
                connections.close_all()

        def second_runner():
            try:
                assert inside.wait(timeout=WAIT), 'the first thread never parked'
                with connections['default'].cursor() as cursor:
                    cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
                outcomes['second'] = ('ok', second())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcomes['second'] = ('error', exc)
            finally:
                resume.set()
                connections.close_all()

        threads = [
            threading.Thread(target=first_runner),
            threading.Thread(target=second_runner),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=WAIT)
            self.assertFalse(thread.is_alive(), 'a thread deadlocked')
        return outcomes.get('first'), outcomes.get('second')

    # --- assertions ---

    def assertBlocked(self, outcome):
        """The competing write was stopped by a LOCK, positively and by name."""
        self.assertIsNotNone(outcome, 'the second thread produced no outcome')
        kind, value = outcome
        self.assertEqual(
            kind, 'error', f'it was NOT blocked; it returned {value!r}',
        )
        self.assertIsInstance(value, OperationalError, repr(value))
        self.assertIn('lock timeout', str(value).lower(), str(value))

    def assertSucceeded(self, outcome):
        self.assertIsNotNone(outcome)
        kind, value = outcome
        self.assertEqual(kind, 'ok', f'expected success, got {value!r}')
        return value

    def assertRefused(self, outcome, expected=None):
        self.assertIsNotNone(outcome)
        kind, value = outcome
        self.assertEqual(kind, 'error', f'expected a refusal, got {value!r}')
        if expected is not None:
            self.assertIsInstance(value, expected, repr(value))
        return value

    def fresh(self):
        return OwnerInvitation.objects.get(pk=self.invitation.pk)

    def fresh_owner(self):
        return User.objects.get(pk=self.owner.pk)


# ═══════════════════════════════════════════════════════════════════════════════
# §30 — redeem vs redeem
# ═══════════════════════════════════════════════════════════════════════════════

class RedeemVsRedeemTests(RedemptionRaceBase):
    """
    §30. Two callers, the same raw token, the same valid code. EXACTLY ONE succeeds.

    Both hold everything they need. What decides the outcome is the ``Restaurant`` row:
    the loser waits, then re-reads a consumed invitation under the lock and refuses.
    """

    def test_the_second_redemption_blocks_on_the_restaurant_row(self):
        first, second = self.race(self.redeem, self.redeem)

        self.assertIsInstance(
            self.assertSucceeded(first), owner_claim_redemption.RedemptionResult,
        )
        self.assertBlocked(second)

    def test_only_one_consume_and_one_transition_happen(self):
        self.race(self.redeem, self.redeem)

        invitation = self.fresh()
        self.assertIsNotNone(invitation.consumed_at)
        self.assertEqual(
            OwnerInvitation.objects.filter(
                onboarding=self.onboarding, consumed_at__isnull=False,
            ).count(), 1,
        )
        self.assertEqual(
            self.fresh_owner().customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
        )

    def test_only_one_session_is_minted(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        self.race(self.redeem, self.redeem)
        self.assertEqual(
            OutstandingToken.objects.filter(user=self.owner).count(), 1,
        )

    def test_the_loser_refuses_cleanly_once_the_winner_commits(self):
        """
        SEQUENTIAL, not raced. This is what the blocked caller sees when it is allowed
        to proceed rather than timing out: a generic refusal, and no second state change.
        """
        self.assertIsInstance(
            self.redeem(), owner_claim_redemption.RedemptionResult,
        )
        consumed_at = self.fresh().consumed_at
        password = User.objects.values_list('password', flat=True).get(
            pk=self.owner.pk,
        )

        with self.assertRaises(owner_claim_redemption.RedemptionRefused):
            self.redeem()

        self.assertEqual(self.fresh().consumed_at, consumed_at)
        self.assertEqual(
            User.objects.values_list('password', flat=True).get(pk=self.owner.pk),
            password,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §31 — redeem vs reissue
# ═══════════════════════════════════════════════════════════════════════════════

class RedeemVsReissueTests(RedemptionRaceBase):
    """§31. Both operations take the ``Restaurant`` row, so one strictly precedes."""

    def reissue(self):
        return onboarding_invitations.reissue_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.staff, reason=REASON,
        )

    def test_case_a_redeem_wins_and_reissue_waits(self):
        first, second = self.race(self.redeem, self.reissue)

        self.assertIsInstance(
            self.assertSucceeded(first), owner_claim_redemption.RedemptionResult,
        )
        self.assertBlocked(second)
        self.assertIsNotNone(self.fresh().consumed_at)

    def test_case_a_reissue_then_refuses_because_control_is_established(self):
        """SEQUENTIAL. Once the claim lands there is nothing left to issue."""
        self.redeem()

        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as caught:
            self.reissue()

        self.assertEqual(
            caught.exception.code,
            onboarding_invitations.OWNER_CONTROL_ALREADY_ESTABLISHED,
        )
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_case_b_reissue_wins_and_the_old_token_can_never_redeem(self):
        """
        The one that matters most. A superseded credential must be dead forever —
        redemption asks the canonical head selector rather than looking the token hash
        up directly, which is what makes this true rather than hopeful.
        """
        result = self.reissue()
        self.assertIsNotNone(self.fresh().superseded_at)

        with self.assertRaises(owner_claim_redemption.RedemptionRefused):
            self.redeem(token=self.token)

        old = self.fresh()
        self.assertIsNone(old.consumed_at)
        self.assertIsNotNone(old.superseded_at)
        self.assertEqual(
            self.fresh_owner().customer_access_state,
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        # ...and the replacement works, with its own OTP.
        self.issue_challenge()
        self.assertIsInstance(
            self.redeem(token=result.claim_token),
            owner_claim_redemption.RedemptionResult,
        )

    def test_case_b_reissue_holds_the_row_against_a_concurrent_redeem(self):
        first, second = self.race(
            self.reissue, self.redeem, park_at=onboarding_invitations,
        )
        self.assertSucceeded(first)
        self.assertBlocked(second)


# ═══════════════════════════════════════════════════════════════════════════════
# §32 — redeem vs cancel
# ═══════════════════════════════════════════════════════════════════════════════

class RedeemVsCancelTests(RedemptionRaceBase):
    """§32. Never both cancelled AND consumed — the last backstop is a DB constraint."""

    def cancel(self):
        return onboarding_invitations.cancel_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.staff, reason=REASON,
        )

    def test_case_a_redeem_wins_and_cancel_waits(self):
        first, second = self.race(self.redeem, self.cancel)
        self.assertSucceeded(first)
        self.assertBlocked(second)

    def test_case_a_cancel_then_refuses_as_resolved(self):
        self.redeem()
        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as caught:
            self.cancel()
        self.assertEqual(
            caught.exception.code,
            onboarding_invitations.OWNER_INVITATION_ALREADY_RESOLVED,
        )

    def test_case_b_cancel_wins_and_the_token_cannot_redeem(self):
        self.cancel()
        with self.assertRaises(owner_claim_redemption.RedemptionRefused):
            self.redeem()
        invitation = self.fresh()
        self.assertIsNotNone(invitation.cancelled_at)
        self.assertIsNone(invitation.consumed_at)

    def test_neither_ordering_can_produce_both_stamps(self):
        self.race(self.redeem, self.cancel)
        invitation = self.fresh()
        self.assertFalse(
            invitation.consumed_at is not None
            and invitation.cancelled_at is not None,
            'an invitation was both consumed and cancelled',
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §33 — redeem vs owner-membership mutation
# ═══════════════════════════════════════════════════════════════════════════════

class RedeemVsMembershipTests(RedemptionRaceBase):
    """
    §33. THE POINT OF PR #306, finally spent by the transaction it was built for.

    Redemption takes the ``Restaurant`` row and then asserts owner consistency. Every
    production ``RestaurantEmployee`` writer now takes that same row, so a role removal,
    deactivation, soft-delete, insert or REACTIVATION cannot commit between the assertion
    and the consume it guards.

    The customer-plane actor is necessarily the OWNER: ``employees`` maps to ``team``,
    which is off-grid and owner-only, so nobody else can exercise these paths at all.
    """

    def setUp(self):
        super().setUp()
        # The owner must be able to authenticate on the customer plane to drive the real
        # endpoint. Establishing them here does not weaken the proof: what is being
        # tested is the LOCK, and the redemption under test is then the established-owner
        # transition (which still consumes the invitation under the same barrier).
        User.objects.filter(pk=self.owner.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            password=make_password('Owner-Passw0rd!'),
        )
        self.owner.refresh_from_db()
        self.membership = RestaurantEmployee.objects.get(
            restaurant=self.restaurant, user=self.owner,
        )

    def redeem(self, token=None, password=None):
        # An ESTABLISHED owner supplies no password.
        return super().redeem(token=token, password=None)

    def remove_the_owner_role(self):
        return _api_call(
            self.owner, 'put',
            {'id': str(self.membership.pk), 'roles': ['manager']},
        )

    def deactivate_the_membership(self):
        return _api_call(
            self.owner, 'put',
            {'id': str(self.membership.pk), 'active': 'false'},
        )

    def soft_delete_the_membership(self):
        return _api_call(self.owner, 'delete', {'id': str(self.membership.pk)})

    def test_case_a_role_removal_cannot_interleave(self):
        first, second = self.race(self.redeem, self.remove_the_owner_role())
        self.assertSucceeded(first)
        self.assertBlocked(second)

    def test_case_a_deactivation_cannot_interleave(self):
        first, second = self.race(self.redeem, self.deactivate_the_membership())
        self.assertSucceeded(first)
        self.assertBlocked(second)

    def test_case_a_soft_delete_cannot_interleave(self):
        first, second = self.race(self.redeem, self.soft_delete_the_membership())
        self.assertSucceeded(first)
        self.assertBlocked(second)

    def insert_a_second_owner(self):
        """
        A genuine membership INSERT, through the real ``POST create-employee``.

        NOT ``POST employees``: that branch runs the reactivation shortcut first and,
        with no soft-deleted row to revive, falls through to ``Secretary.create()`` with
        ``restaurant`` read_only and no ``server_values`` — an unavoidable NOT NULL
        violation. Pre-existing on ``origin/main``, reported in
        ``tests_owner_membership_concurrency`` and not fixed here.
        """
        phone = next(_PHONE)
        return _api_call(self.owner, 'post', {
            'first_name': 'Second', 'last_name': 'Owner',
            'email': f'ocrc-second-{phone}@t.com', 'phone_number': phone,
            'restaurant': str(self.restaurant.id), 'roles': [RESTAURANT_OWNER],
        }, config_detail='create-employee')

    def reactivate_a_second_owner(self, user):
        """
        The reactivation shortcut — a real production UPDATE, and the case referential
        integrity does NOT cover.
        """
        return _api_call(self.owner, 'post', {
            'user': str(user.id), 'restaurant': str(self.restaurant.id),
            'roles': [RESTAURANT_OWNER],
        })

    def dormant_owner(self):
        """A soft-deleted owner membership: invisible to the assertion, and revivable."""
        phone = next(_PHONE)
        user = User.objects.create_user(
            first_name='Dor', last_name='Mant',
            email=f'ocrc-dor-{phone}@t.com', phone_number=f'256{phone[1:]}',
            username=f'256{phone[1:]}', country='UG', password='x', roles=[],
        )
        RestaurantEmployee.objects.create(
            restaurant=self.restaurant, user=user,
            roles=[RESTAURANT_OWNER], active=False, deleted=True,
        )
        return user

    def test_case_a_a_second_owner_insertion_cannot_interleave(self):
        """
        A second live owner membership is exactly what would make
        ``assert_owner_consistency`` fail.

        NOT EVIDENCE THAT THE BARRIER WORKS, and said so plainly: on PostgreSQL an
        INSERT is ALSO blocked by referential integrity (the FK takes ``FOR KEY SHARE``
        on the parent row), so this would pass with the barrier removed. It is pinned
        because the outcome is what the invariant requires however it is obtained. The
        REACTIVATION case below is the one that proves the barrier.

        ``handle_create_employee`` wraps its controller in ``except Exception`` and answers
        500, so the assertion is on the OUTCOME — the invariant the barrier exists to
        protect — rather than on a status code, which has many causes.
        """
        first, second = self.race(self.redeem, self.insert_a_second_owner())

        self.assertSucceeded(first)
        response = self.assertSucceeded(second)
        self.assertNotEqual(
            response.status_code, 200,
            'the second owner membership was created inside the decision',
        )
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True, deleted=False,
                roles__contains=[RESTAURANT_OWNER],
            ).count(), 1,
            'a second live owner membership committed inside the decision',
        )

    def test_case_a_a_reactivation_cannot_interleave(self):
        """
        THE CASE THAT PROVES THE BARRIER. A soft-deleted owner membership is invisible
        to ``assert_owner_consistency`` (it filters ``deleted=False``), so a row lock
        over the rows the assertion READ could never have covered it — and the
        production reactivation path is an UPDATE that leaves the FK alone, which
        PostgreSQL's referential integrity does NOT block against a held parent row.

        Driven through the REAL endpoint: a bare ``objects.update()`` bypasses the
        barrier by construction and would prove only that raw SQL is raw SQL.
        """
        spare = self.dormant_owner()

        first, second = self.race(
            self.redeem, self.reactivate_a_second_owner(spare),
        )

        self.assertSucceeded(first)
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True, deleted=False,
                roles__contains=[RESTAURANT_OWNER],
            ).count(), 1,
            'a membership was reactivated inside the decision',
        )

    def test_case_b_a_committed_drift_refuses_before_the_consume(self):
        """
        The other ordering. The membership mutation lands FIRST; redemption then reads
        the committed drift under its own lock and refuses — WITHOUT consuming the
        credential and WITHOUT establishing anything.
        """
        response = self.remove_the_owner_role()()
        self.assertEqual(response.status_code, 200, response.data)

        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem()

        self.assertEqual(
            caught.exception.code,
            owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
        )
        self.assertIsNone(self.fresh().consumed_at)
        self.assertEqual(
            onboarding_summary(self.restaurant)['owner_control']['status'],
            'not_established',
        )

    def test_case_b_a_committed_deactivation_refuses_too(self):
        response = self.deactivate_the_membership()()
        # The last-active-owner guard answers 409 rather than allowing it, which is
        # itself the correct behaviour — so drift is forced through the role path.
        self.assertIn(response.status_code, (200, 409), response.data)
        if response.status_code == 409:
            RestaurantEmployee.objects.filter(pk=self.membership.pk).update(
                active=False,
            )

        with self.assertRaises(owner_claim_redemption.RedemptionRefused):
            self.redeem()
        self.assertIsNone(self.fresh().consumed_at)


# ═══════════════════════════════════════════════════════════════════════════════
# §34 — redeem vs phone change
# ═══════════════════════════════════════════════════════════════════════════════

class RedeemVsPhoneChangeTests(RedemptionRaceBase):
    """
    §34. A code delivered to a superseded number is not evidence of current control.

    ON THE WRITER, stated rather than invented: **no production path mutates an existing
    ``User.phone_number``.** ``self_update_user_profile`` REFUSES a change with a 400
    ("Phone number cannot be changed here"), and every other site is a CREATE. So there
    is no real writer to race, and the tests below use a direct ``UPDATE`` — which is
    what operator SQL, a data migration, or a future writer would do.

    That is why the binding is built the way it is: it compares against the ``users`` row
    this transaction LOCKS, not against anything remembered from the challenge, so it
    holds whatever that future writer turns out to be.
    """

    def test_the_real_profile_writer_still_refuses_a_phone_change(self):
        """The premise, asserted rather than assumed."""
        from users_app.controllers.update_user_profile import self_update_user_profile
        User.objects.filter(pk=self.owner.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
        )
        result = self_update_user_profile(
            user_id=str(self.owner.pk), phone_number=f'256{next(_PHONE)[1:]}',
        )
        self.assertEqual(result['status'], 400)
        self.assertIn('cannot be changed', result['message'])
        self.owner.refresh_from_db()
        self.assertEqual(
            UserOtp.objects.get(user=self.owner).msisdn, self.owner.phone_number,
        )

    def test_a_committed_phone_change_kills_the_old_factor(self):
        new_phone = f'256{next(_PHONE)[1:]}'
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=new_phone, username=new_phone,
        )

        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem()

        self.assertEqual(
            caught.exception.code, owner_claim_redemption.OTP_DESTINATION_STALE,
        )
        # §17: THE INVITATION IS UNTOUCHED — including its attempt budget. The claimant
        # did not cause the phone change and must not be charged a guess for it.
        invitation = self.fresh()
        self.assertIsNone(invitation.consumed_at)
        self.assertEqual(invitation.claim_failed_attempts, 0)
        self.assertEqual(
            self.fresh_owner().customer_access_state,
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        # The stale code was not even tested against.
        stale = UserOtp.objects.get(user=self.owner)
        self.assertIsNone(stale.consumed_at)
        self.assertEqual(stale.attempts, 0)

    def test_a_phone_change_cannot_interleave_with_a_redemption(self):
        """
        The ``users`` row is locked by redemption before the factor check, so a
        concurrent UPDATE of it waits — the redemption's view of the destination cannot
        be invalidated between the check and the commit.
        """
        new_phone = f'256{next(_PHONE)[1:]}'

        def change_phone():
            return User.objects.filter(pk=self.owner.pk).update(
                phone_number=new_phone,
            )

        first, second = self.race(self.redeem, change_phone)
        self.assertSucceeded(first)
        self.assertBlocked(second)

    def test_a_fresh_challenge_to_the_new_phone_redeems(self):
        """The owner is not stranded: they request a new code and it works."""
        new_phone = f'256{next(_PHONE)[1:]}'
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=new_phone, username=new_phone,
        )
        self.owner.refresh_from_db()
        self.issue_challenge()
        # BOTH rows are live: `make_otp` deletes by (user, msisdn), so the row bound to
        # the OLD number survives beside the new one. That coexistence is exactly why
        # the stale-destination refusal is narrow — "something outstanding, and none of
        # it went where it should go now" rather than "a stale row exists".
        self.assertTrue(
            UserOtp.objects.filter(user=self.owner, msisdn=new_phone).exists(),
        )
        self.assertEqual(UserOtp.objects.filter(user=self.owner).count(), 2)

        self.assertIsInstance(
            self.redeem(), owner_claim_redemption.RedemptionResult,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# The decision clock is captured AFTER the serialization lock
# ═══════════════════════════════════════════════════════════════════════════════

class DecisionClockTests(RedemptionRaceBase):
    """
    ``now`` is read once the ``Restaurant`` row is HELD, not before reaching for it.

    ━━ THE DEFECT THIS CLOSES (Codex P2 on PR #309) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ``select_for_update`` BLOCKS while a competing redemption, reissue or cancel holds
    the row. A ``now`` captured before that call is stale by the entire wait, and two
    things follow:

      * an invitation whose ``expires_at`` falls INSIDE the wait still compares as live,
        so an expired credential is redeemed;
      * ``consumed_at`` is stamped earlier than the moment the claim actually happened,
        putting ``owner_control.evidence_at`` before the event it is evidence of.

    It is the same rule the rest of the transaction already follows for every other fact
    — authoritative values are read once the serialization point is held. The clock is a
    fact like any other.

    ━━ WHY THESE ARE NOT VACUOUS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Both drive a REAL lock wait from a second connection rather than patching
    ``timezone.now``: a mocked clock would prove only that the mock was applied. The
    blocker holds the row, the test moves the clock-relevant state while redemption is
    parked in ``select_for_update``, and then releases.
    """

    def hold_the_restaurant_row(self, during):
        """
        Hold the ``Restaurant`` row on a second connection, run ``during()`` while a
        redemption is blocked on it, then release and return the redemption's outcome.
        """
        holding, mutate_done = threading.Event(), threading.Event()
        outcome = {}

        def blocker():
            try:
                from django.db import transaction as tx
                with tx.atomic():
                    # Take the row this redemption will queue behind.
                    list(
                        type(self.restaurant).objects
                        .select_for_update().filter(pk=self.restaurant.pk)
                    )
                    holding.set()
                    assert mutate_done.wait(timeout=WAIT), 'mutation never ran'
                    # ...and release by leaving the block.
            finally:
                connections.close_all()

        def redeemer():
            try:
                assert holding.wait(timeout=WAIT), 'the blocker never took the row'
                # The mutation lands while THIS call is parked inside select_for_update.
                outcome['result'] = ('ok', self.redeem())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcome['result'] = ('error', exc)
            finally:
                connections.close_all()

        blocker_thread = threading.Thread(target=blocker)
        redeem_thread = threading.Thread(target=redeemer)
        blocker_thread.start()
        self.assertTrue(holding.wait(timeout=WAIT), 'the blocker never took the row')
        redeem_thread.start()

        during()
        mutate_done.set()

        for thread in (blocker_thread, redeem_thread):
            thread.join(timeout=WAIT)
            self.assertFalse(thread.is_alive(), 'a thread deadlocked')
        return outcome.get('result')

    def test_an_invitation_that_expires_during_the_lock_wait_cannot_redeem(self):
        """
        THE ONE THE FIX EXISTS FOR. The invitation is live when the redemption starts
        queueing and expired by the time it holds the row.

        THE WINDOW HAS TO STRADDLE THE WAIT, and getting that wrong is how this test
        first passed against the buggy code: expiring the invitation RETROACTIVELY makes
        it already expired at the pre-lock instant too, so both readings refuse and the
        test proves nothing. ``expires_at`` is therefore set into the near FUTURE before
        the redeemer starts, and the row is then held past it — so a pre-lock clock sees
        a live credential and a post-lock clock sees an expired one.
        """
        import time

        # Live when the redeemer captures a pre-lock clock...
        deadline = timezone.now() + timedelta(seconds=EXPIRY_WINDOW)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=timezone.now() - timedelta(days=1), expires_at=deadline,
        )

        # ...and expired by the time the lock is released.
        result = self.hold_the_restaurant_row(
            lambda: time.sleep(EXPIRY_WINDOW + LOCK_HOLD_MARGIN),
        )

        self.assertIsNotNone(result, 'the redemption thread produced no outcome')
        kind, value = result
        self.assertEqual(
            kind, 'error',
            f'an invitation that expired during the lock wait was redeemed: {value!r}',
        )
        self.assertIsInstance(value, owner_claim_redemption.RedemptionRefused)
        self.assertEqual(value.code, owner_claim_redemption.INVITATION_EXPIRED)
        self.assertIsNone(self.fresh().consumed_at)
        self.assertEqual(
            self.fresh_owner().customer_access_state,
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_the_same_invitation_redeems_when_the_wait_is_short(self):
        """
        THE CONTROL. Same fixture, same lock wait mechanics, but released well inside the
        window — so the refusal above is the expiry and not the blocking.
        """
        import time

        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=timezone.now() - timedelta(days=1),
            expires_at=timezone.now() + timedelta(seconds=EXPIRY_WINDOW * 6),
        )

        result = self.hold_the_restaurant_row(lambda: time.sleep(0.2))

        self.assertEqual(result[0], 'ok', repr(result[1]))
        self.assertIsNotNone(self.fresh().consumed_at)

    def test_consumed_at_is_the_moment_the_claim_actually_happened(self):
        """
        The evidence timestamp must not predate the redemption. Measured across a REAL
        lock wait: `consumed_at` has to land after the blocker released the row, not
        before the redemption started queuing for it.
        """
        started = timezone.now()
        released = {}

        def pause():
            # Long enough that a pre-lock clock would be visibly stale.
            import time
            time.sleep(1.0)
            released['at'] = timezone.now()

        result = self.hold_the_restaurant_row(pause)

        kind, value = result
        self.assertEqual(kind, 'ok', f'expected success, got {value!r}')
        consumed_at = self.fresh().consumed_at
        self.assertIsNotNone(consumed_at)
        self.assertGreaterEqual(
            consumed_at, released['at'],
            'consumed_at predates the moment the lock was released, so the decision '
            'clock was read before the serialization point was held',
        )
        self.assertGreater(consumed_at, started)

    def test_owner_control_evidence_at_matches_the_consume(self):
        """The projection reads `consumed_at`, so the two move together by construction."""
        import time

        result = self.hold_the_restaurant_row(lambda: time.sleep(0.5))
        self.assertEqual(result[0], 'ok', repr(result[1]))

        summary = onboarding_summary(self.restaurant)
        self.assertEqual(
            summary['owner_control']['evidence_at'],
            self.fresh().consumed_at.isoformat(),
        )
