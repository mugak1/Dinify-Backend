"""
``adopt_existing_restaurant`` and ``manage.py adopt_restaurant_onboarding`` — the
audited legacy-adoption writer.

Adoption is a PROVENANCE record: it states how a pre-existing canonical Restaurant
entered the Admin onboarding domain, and it states nothing else. Every failure mode
worth testing is a way that record could come to say something untrue, so the suite
is organised around those rather than around the service's arguments:

  IT COULD CLAIM MORE THAN HAPPENED. An adoption that also stamped the attestation
  triple would assert that an administrator personally verified the owner's control;
  one that minted an ``OwnerInvitation`` would assert the owner was invited and
  accepted. Neither happened, and neither is distinguishable from the real thing
  afterwards. ``NoFabricatedEvidenceTests`` pins both to nothing.

  IT COULD BE ABOUT THE WRONG TENANT, OR NOBODY. Targeting is UUID-only and exact,
  and the actor is validated against the row rather than the instance handed in — so
  the log cannot name an account that could not have made the decision.

  IT COULD REWRITE HISTORY. A second run must not re-stamp ``adopted_at`` /
  ``adopted_by``, must not write a second audit row, and must not become a way to
  convert ``admin_created`` provenance into ``legacy_adopted``.

  IT COULD CHANGE THE RESTAURANT. Adoption represents a tenant; it does not touch
  one. ``TenantUntouchedTests`` snapshots the restaurant, its owner membership and a
  representative table / menu item / order and proves all of it identical afterwards.

  IT COULD REPAIR OWNERSHIP ON THE WAY PAST. A NEW adoption requires the owner of
  record and the owner authority to already agree; when they do not it refuses with
  the canonical ``OwnerConsistencyError`` code and leaves the drift in place for a
  human. And it deliberately does NOT re-ask that question of an ALREADY-adopted
  restaurant — see ``HistoricalAdoptionSurvivesDriftTests``, which is the pin on the
  distinction between historical provenance and current-state evaluation.

THE OWNER-CONSISTENCY DEFINITION IS NOT RE-ASSERTED HERE. What counts as consistent
— and specifically that ``User.is_active`` is NOT part of it — is owned by
``tests_owner_consistency``. This suite asserts only what the ADOPTION writer does
with each verdict, plus the one case that would be easy to regress from the other
direction (``test_adopts_a_restaurant_whose_owner_account_is_deactivated``).
"""
import threading
import uuid
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from orders_app.models import Order
from platform_admin_app.audit_actions import ADMIN_RESTAURANT_ONBOARDING_ADOPTED
from platform_admin_app.management.commands.adopt_restaurant_onboarding import (
    Command as AdoptCommand,
)
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    RESULT_SUCCESS,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    MISSING_OWNER_MEMBERSHIP,
    MULTIPLE_OWNER_MEMBERSHIPS,
    OWNER_MEMBERSHIP_MISMATCH,
    OwnerConsistencyError,
)
from platform_admin_app.onboarding_adoption import (
    INVALID_ACTOR,
    INVALID_REASON,
    INVALID_RESTAURANT_ID,
    ONBOARDING_SOURCE_CONFLICT,
    OUTCOME_ADOPTED,
    OUTCOME_ALREADY_ADOPTED,
    RESTAURANT_NOT_FOUND,
    AdoptionError,
    adopt_existing_restaurant,
)
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import (
    DiningArea,
    MenuItem,
    MenuSection,
    Restaurant,
    RestaurantEmployee,
    Table,
)
from users_app.models import User

# Distinct phone range from the other admin suites (…091…) so the unique
# phone_number constraint cannot collide when suites run in one process.
_PHONE = iter(f'2567091000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
REASON = 'Pre-existing UAT tenant, reconciling provenance into Admin.'

COMMAND = 'adopt_restaurant_onboarding'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True, is_active=True, roles=None):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=roles or [],
        account_type=account_type, is_active=is_active,
    )


def _make_staff(username='adopt-admin', email='adopt-admin@t.com', is_active=True):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False, is_active=is_active,
    )


def _make_restaurant(name='Legacy Fixture', *, status=RestaurantStatus_Live,
                     deleted=False, owner=None, with_owner_membership=True):
    """
    A restaurant that is structurally consistent by default.

    The owner FK and the active owner-role membership are created together, because
    that is the state a legacy tenant has to be in to be adoptable, and building it
    by hand in each test is how one of the two quietly goes missing.
    """
    owner = owner or _make_user(f'owner-{uuid.uuid4().hex[:8]}@t.com')
    restaurant = Restaurant.objects.create(
        name=name, location=f'{name} Road', status=status,
        deleted=deleted, owner=owner,
    )
    if with_owner_membership:
        RestaurantEmployee.objects.create(
            user=owner, restaurant=restaurant, roles=[RESTAURANT_OWNER],
            active=True, deleted=False,
        )
    return restaurant


class _AdoptionFixture(AuditAssertionsMixin, TestCase):
    """One platform-staff actor and one structurally consistent legacy restaurant."""

    def setUp(self):
        super().setUp()
        self.actor = _make_staff()
        self.restaurant = _make_restaurant()

    def adopt(self, *, restaurant_id=None, actor=None, reason=REASON):
        return adopt_existing_restaurant(
            restaurant_id=(
                self.restaurant.id if restaurant_id is None else restaurant_id
            ),
            actor=self.actor if actor is None else actor,
            reason=reason,
        )

    def assertRefused(self, code, **kwargs):
        """Assert adoption raises ``AdoptionError(code)`` and returns it."""
        with self.assertRaises(AdoptionError) as caught:
            self.adopt(**kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assertNothingWritten(self):
        """No provenance, no invitation, no audit — the fail-closed baseline."""
        self.assertFalse(RestaurantOnboarding.objects.exists())
        self.assertFalse(OwnerInvitation.objects.exists())
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_RESTAURANT_ONBOARDING_ADOPTED,
            ).exists()
        )


class CanonicalAdoptionTests(_AdoptionFixture):
    """The success path: exactly one row, saying exactly what happened."""

    def test_creates_one_legacy_adopted_onboarding_row(self):
        before = timezone.now()
        result = self.adopt()
        after = timezone.now()

        self.assertEqual(result.outcome, OUTCOME_ADOPTED)
        self.assertTrue(result.created)

        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
        row = RestaurantOnboarding.objects.get()
        self.assertEqual(row.pk, result.onboarding.pk)
        self.assertEqual(row.restaurant_id, self.restaurant.id)
        self.assertEqual(row.source, ONBOARDING_SOURCE_LEGACY_ADOPTED)

        # Dinify did not create this tenant, so there is no creating actor.
        self.assertIsNone(row.created_by_id)
        # The adoption is a real event with a real moment and a real operator.
        self.assertEqual(row.adopted_by_id, self.actor.id)
        self.assertIsNotNone(row.adopted_at)
        self.assertGreaterEqual(row.adopted_at, before)
        self.assertLessEqual(row.adopted_at, after)

    def test_result_names_the_row_and_the_restaurant_it_adopted(self):
        result = self.adopt()
        self.assertEqual(result.restaurant.id, self.restaurant.id)
        self.assertEqual(result.onboarding.restaurant_id, self.restaurant.id)

    def test_adopts_in_every_lifecycle_state(self):
        """
        Lifecycle state is not a blocker, in either direction.

        A legacy restaurant may be onboarding, live, suspended or offboarded and
        still need truthful Admin provenance — provenance answers how it ARRIVED,
        not whether it is trading. And adoption must not move it: a provenance
        writer that also changed commercial state would be a second writer of
        ``Restaurant.status``, which the lifecycle service owns alone.
        """
        for state in (
            RestaurantStatus_Onboarding, RestaurantStatus_Live,
            RestaurantStatus_Suspended, RestaurantStatus_Offboarded,
        ):
            with self.subTest(state=state):
                restaurant = _make_restaurant(f'Legacy {state}', status=state)
                result = adopt_existing_restaurant(
                    restaurant_id=restaurant.id, actor=self.actor, reason=REASON,
                )
                self.assertEqual(result.outcome, OUTCOME_ADOPTED)
                restaurant.refresh_from_db()
                self.assertEqual(restaurant.status, state)

    def test_accepts_a_uuid_object_as_well_as_its_string_form(self):
        """Both callers this service will have — a command and an endpoint."""
        result = adopt_existing_restaurant(
            restaurant_id=str(self.restaurant.id), actor=self.actor, reason=REASON,
        )
        self.assertEqual(result.outcome, OUTCOME_ADOPTED)

        other = _make_restaurant('Second Legacy')
        result = adopt_existing_restaurant(
            restaurant_id=other.id, actor=self.actor, reason=REASON,
        )
        self.assertEqual(result.outcome, OUTCOME_ADOPTED)

    def test_adopts_a_restaurant_whose_owner_account_is_deactivated(self):
        """
        Structural consistency and account eligibility are separate questions.

        Step 2A drew that line deliberately: ``assert_owner_consistency`` asks
        whether the owner of record IS the owner authority, not whether that person
        can currently sign in. A deactivated owner account may well block go-live
        later, but the historical fact of how the restaurant entered Dinify does not
        depend on it — and refusing here would quietly redefine the validator by
        making adoption the place account eligibility is enforced.
        """
        owner = _make_user('dormant-owner@t.com', is_active=False)
        restaurant = _make_restaurant('Dormant Owner Ltd', owner=owner)

        result = adopt_existing_restaurant(
            restaurant_id=restaurant.id, actor=self.actor, reason=REASON,
        )

        self.assertEqual(result.outcome, OUTCOME_ADOPTED)
        self.assertEqual(result.onboarding.source, ONBOARDING_SOURCE_LEGACY_ADOPTED)


class NoFabricatedEvidenceTests(_AdoptionFixture):
    """
    Adoption must not leave behind evidence of things that did not happen.

    Both fabrications below are indistinguishable from the real thing to every
    future reader, which is why they are asserted rather than assumed: an attestation
    triple says an administrator personally vouched for the owner's control, and an
    ``OwnerInvitation`` says the owner was invited to claim the account. A legacy
    tenant experienced neither.
    """

    def test_leaves_the_owner_control_attestation_triple_null(self):
        result = self.adopt()
        row = result.onboarding
        self.assertIsNone(row.owner_control_attested_at)
        self.assertIsNone(row.owner_control_attested_user_id)
        self.assertIsNone(row.owner_control_attested_by_id)

    def test_creates_no_owner_invitation(self):
        self.adopt()
        self.assertEqual(OwnerInvitation.objects.count(), 0)

    def test_creates_no_users_employees_or_restaurants(self):
        users = User.objects.count()
        employees = RestaurantEmployee.objects.count()
        restaurants = Restaurant.objects.count()

        self.adopt()

        self.assertEqual(User.objects.count(), users)
        self.assertEqual(RestaurantEmployee.objects.count(), employees)
        self.assertEqual(Restaurant.objects.count(), restaurants)


class TenantUntouchedTests(_AdoptionFixture):
    """
    Adoption REPRESENTS a tenant; it does not modify one.

    The restaurant row is snapshotted field by field rather than spot-checked,
    including ``time_last_updated``: the service never calls ``restaurant.save()``,
    so even the ``auto_now`` stamp should be identical, and a stamp that moved would
    mean a write crept in somewhere.
    """

    SNAPSHOT_FIELDS = (
        'id', 'name', 'location', 'owner_id', 'status', 'is_test', 'deleted',
        'archived', 'vacuumed',
        # Legacy subscription columns — the ones the admin read contract reports
        # under `legacy_*` names. Adoption is not a billing event.
        'subscription_validity', 'subscription_expiry_date',
        'preferred_subscription_method', 'flat_fee',
        # Payment / prepayment settings: `require_order_prepayments` is a
        # diner-checkout toggle and adoption has no opinion about checkout.
        'require_order_prepayments', 'accepting_orders',
        'time_created', 'time_last_updated',
    )

    def setUp(self):
        super().setUp()
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            restaurant=self.restaurant, number=1, dining_area=area,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=Decimal('5000.00'),
        )
        zero = Decimal('0.00')
        self.order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=zero, discounted_cost=zero, savings=zero, actual_cost=zero,
        )
        self.membership = RestaurantEmployee.objects.get(
            restaurant=self.restaurant, user=self.restaurant.owner,
        )

    def _snapshot(self):
        row = Restaurant.objects.get(pk=self.restaurant.pk)
        return {field: getattr(row, field) for field in self.SNAPSHOT_FIELDS}

    def test_restaurant_row_is_identical_afterwards(self):
        before = self._snapshot()
        self.adopt()
        self.assertEqual(self._snapshot(), before)

    def test_owner_membership_is_identical_afterwards(self):
        fields = ('id', 'user_id', 'restaurant_id', 'roles', 'active', 'deleted')
        before = {f: getattr(self.membership, f) for f in fields}

        self.adopt()

        self.membership.refresh_from_db()
        after = {f: getattr(self.membership, f) for f in fields}
        self.assertEqual(after, before)
        self.assertEqual(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant).count(), 1,
        )

    def test_representative_operational_state_is_untouched(self):
        """
        One table, one menu item, one order — the three surfaces an operator would
        notice. Not an exhaustive fixture forest: the guarantee is that adoption
        writes one provenance row, and one ratchet across the operational domains is
        enough to catch a writer that started reaching further.
        """
        table_before = (self.table.number, self.table.status, self.table.is_active)
        item_before = (self.item.name, self.item.primary_price, self.item.available)
        order_before = (
            self.order.order_status, self.order.actual_cost, self.order.is_test,
        )

        self.adopt()

        self.table.refresh_from_db()
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(
            (self.table.number, self.table.status, self.table.is_active),
            table_before,
        )
        self.assertEqual(
            (self.item.name, self.item.primary_price, self.item.available),
            item_before,
        )
        self.assertEqual(
            (self.order.order_status, self.order.actual_cost, self.order.is_test),
            order_before,
        )


class AdoptionAuditTests(_AdoptionFixture):
    """
    The audit row is the evidence that a real administrator decided this.

    It carries the decision and nothing else. The two negative assertions below are
    not decoration: ``before_state`` / ``after_state`` accept arbitrary dicts, so a
    future edit that "helpfully" included the owner for context would ship an email
    address and a phone number into an append-only table that is never redacted after
    the fact.
    """

    def test_writes_exactly_one_entry_with_the_expected_shape(self):
        result = self.adopt()

        entry = self.assertAudited(
            ADMIN_RESTAURANT_ONBOARDING_ADOPTED,
            result=RESULT_SUCCESS,
            actor=self.actor,
            restaurant_id=self.restaurant.id,
        )
        self.assertEqual(entry.actor_label, self.actor.username)
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(entry.before_state, {'admin_onboarding_source': None})
        self.assertEqual(
            entry.after_state,
            {'admin_onboarding_source': ONBOARDING_SOURCE_LEGACY_ADOPTED},
        )
        self.assertEqual(result.onboarding.source, entry.after_state[
            'admin_onboarding_source'
        ])

    def test_records_the_trimmed_reason(self):
        self.adopt(reason=f'   {REASON}   ')
        entry = self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED)
        self.assertEqual(entry.reason, REASON)

    def test_audit_state_carries_no_owner_or_tenant_detail(self):
        owner = self.restaurant.owner
        self.adopt()
        entry = self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED)

        blob = f'{entry.before_state}{entry.after_state}'
        for secret in (
            owner.email, owner.phone_number, owner.first_name, owner.username,
            self.restaurant.name, self.restaurant.location,
        ):
            self.assertNotIn(str(secret), blob)

    def test_a_failed_audit_write_rolls_the_adoption_back(self):
        """
        No audit, no adoption. Provenance nobody can be shown to have decided is
        worse than no provenance, so the row and its evidence share one transaction.
        """
        with patch(
            'platform_admin_app.onboarding_adoption.audit.record',
            side_effect=RuntimeError('audit table unavailable'),
        ):
            with self.assertRaises(RuntimeError):
                self.adopt()

        self.assertNothingWritten()


class IdempotencyTests(_AdoptionFixture):
    """
    A re-run of the runbook is not a second adoption.

    The first adoption is the historical event. Re-stamping ``adopted_at`` /
    ``adopted_by`` would silently reassign a past decision to whoever pasted the
    command most recently — and there would be no trace that it had ever said
    anything else.
    """

    def setUp(self):
        super().setUp()
        self.first = self.adopt()
        self.original = RestaurantOnboarding.objects.get()

    def test_second_identical_adoption_is_a_no_op(self):
        result = self.adopt()

        self.assertEqual(result.outcome, OUTCOME_ALREADY_ADOPTED)
        self.assertFalse(result.created)
        self.assertEqual(result.onboarding.pk, self.original.pk)
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)

    def test_second_adoption_writes_no_second_audit_row(self):
        self.adopt()
        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=1)

    def test_a_different_actor_and_reason_cannot_rewrite_the_original(self):
        other = _make_staff(username='second-admin', email='second-admin@t.com')

        result = self.adopt(
            actor=other, reason='A different operator running the same runbook.',
        )

        self.assertEqual(result.outcome, OUTCOME_ALREADY_ADOPTED)
        row = RestaurantOnboarding.objects.get()
        self.assertEqual(row.pk, self.original.pk)
        self.assertEqual(row.adopted_by_id, self.actor.id)
        self.assertEqual(row.adopted_at, self.original.adopted_at)
        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=1)

    def test_no_op_creates_no_invitation_and_no_attestation(self):
        self.adopt()
        row = RestaurantOnboarding.objects.get()
        self.assertEqual(OwnerInvitation.objects.count(), 0)
        self.assertIsNone(row.owner_control_attested_at)
        self.assertIsNone(row.owner_control_attested_user_id)
        self.assertIsNone(row.owner_control_attested_by_id)


class HistoricalAdoptionSurvivesDriftTests(_AdoptionFixture):
    """
    Ownership drifting AFTER an adoption does not undo the adoption.

    This is the pin on the distinction the whole domain rests on: *historical
    adoption provenance* versus *current owner consistency*. The restaurant was
    adopted; that happened. If the owner FK and the owner authority disagree a year
    later, the honest answer is still "adopted, and currently inconsistent" — two
    facts, surfaced separately, by Step 2C's read projection and Step 3's readiness
    engine. Making current consistency a precondition for merely REPORTING an
    existing adoption would let a present-day problem erase a past decision.
    """

    def setUp(self):
        super().setUp()
        self.adopt()
        self.original = RestaurantOnboarding.objects.get()
        # Drift introduced after the fact: the owner authority is withdrawn while
        # the owner FK keeps pointing at the same person.
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.restaurant.owner,
        ).update(active=False)

    def test_rerun_reports_already_adopted_despite_present_inconsistency(self):
        result = self.adopt()
        self.assertEqual(result.outcome, OUTCOME_ALREADY_ADOPTED)

    def test_rerun_does_not_rewrite_or_replace_the_historical_row(self):
        self.adopt()
        row = RestaurantOnboarding.objects.get()
        self.assertEqual(row.pk, self.original.pk)
        self.assertEqual(row.adopted_at, self.original.adopted_at)
        self.assertEqual(row.adopted_by_id, self.original.adopted_by_id)
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=1)


class SourceConflictTests(_AdoptionFixture):
    """
    ``admin_created`` provenance is never converted to ``legacy_adopted``.

    The two make contradictory claims about where a tenant came from, and one of them
    names the staff member who created it. Overwriting that attribution to make a
    command succeed would delete a fact and invent a different one; two records
    disagreeing is something a human has to look at.
    """

    def setUp(self):
        super().setUp()
        self.creator = _make_staff(username='creator-admin', email='creator@t.com')
        self.existing = RestaurantOnboarding.objects.create(
            restaurant=self.restaurant,
            source=ONBOARDING_SOURCE_ADMIN_CREATED,
            created_by=self.creator,
        )

    def test_refuses_with_a_source_conflict(self):
        error = self.assertRefused(ONBOARDING_SOURCE_CONFLICT)
        self.assertEqual(error.details['existing_source'],
                         ONBOARDING_SOURCE_ADMIN_CREATED)

    def test_leaves_the_existing_row_exactly_as_it_was(self):
        self.assertRefused(ONBOARDING_SOURCE_CONFLICT)

        self.existing.refresh_from_db()
        self.assertEqual(self.existing.source, ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(self.existing.created_by_id, self.creator.id)
        self.assertIsNone(self.existing.adopted_at)
        self.assertIsNone(self.existing.adopted_by_id)
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)

    def test_writes_no_audit_no_invitation_and_does_not_touch_the_restaurant(self):
        stamp = self.restaurant.time_last_updated

        self.assertRefused(ONBOARDING_SOURCE_CONFLICT)

        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=0)
        self.assertEqual(OwnerInvitation.objects.count(), 0)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.time_last_updated, stamp)


class OwnerConsistencyPreconditionTests(_AdoptionFixture):
    """
    A NEW adoption requires the owner of record and the owner authority to agree.

    The writer propagates the canonical ``OwnerConsistencyError`` rather than
    flattening it into an adoption error: the three codes need three different
    remedies, and every one of them is a decision about who runs a business. So it
    refuses, writes nothing, and repairs nothing.
    """

    def setUp(self):
        super().setUp()
        # Start from a restaurant with NO owner membership; each test builds the
        # inconsistent shape it needs on top of that.
        self.restaurant = _make_restaurant(
            'Drifted Ltd', with_owner_membership=False,
        )

    def assertRefusesWith(self, code):
        with self.assertRaises(OwnerConsistencyError) as caught:
            self.adopt()
        self.assertEqual(caught.exception.code, code)
        self.assertNothingWritten()

    def test_missing_owner_membership(self):
        self.assertRefusesWith(MISSING_OWNER_MEMBERSHIP)

    def test_owner_membership_mismatch(self):
        stranger = _make_user('stranger@t.com')
        RestaurantEmployee.objects.create(
            user=stranger, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
        )
        self.assertRefusesWith(OWNER_MEMBERSHIP_MISMATCH)

    def test_multiple_active_owner_memberships(self):
        RestaurantEmployee.objects.create(
            user=self.restaurant.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        RestaurantEmployee.objects.create(
            user=_make_user('co-owner@t.com'), restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.assertRefusesWith(MULTIPLE_OWNER_MEMBERSHIPS)

    def test_a_manager_is_not_an_owner(self):
        RestaurantEmployee.objects.create(
            user=self.restaurant.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_MANAGER],
        )
        self.assertRefusesWith(MISSING_OWNER_MEMBERSHIP)

    def test_the_inconsistency_is_not_repaired(self):
        stranger = _make_user('unrepaired@t.com')
        membership = RestaurantEmployee.objects.create(
            user=stranger, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
        )
        owner_id = self.restaurant.owner_id

        with self.assertRaises(OwnerConsistencyError):
            self.adopt()

        membership.refresh_from_db()
        self.restaurant.refresh_from_db()
        self.assertTrue(membership.active)
        self.assertEqual(membership.roles, [RESTAURANT_OWNER])
        self.assertEqual(membership.user_id, stranger.id)
        self.assertEqual(self.restaurant.owner_id, owner_id)
        self.assertEqual(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant).count(), 1,
        )


class TargetValidationTests(_AdoptionFixture):
    """One invocation, one immutable UUID — never a name, never a candidate set."""

    def test_malformed_uuid(self):
        self.assertRefused(INVALID_RESTAURANT_ID, restaurant_id='not-a-uuid')
        self.assertNothingWritten()

    def test_blank_target(self):
        self.assertRefused(INVALID_RESTAURANT_ID, restaurant_id='   ')
        self.assertNothingWritten()

    def test_restaurant_name_supplied_where_a_uuid_belongs(self):
        """A name never resolves — not even the exact name of a real restaurant."""
        self.assertRefused(INVALID_RESTAURANT_ID, restaurant_id=self.restaurant.name)
        self.assertNothingWritten()

    def test_unknown_uuid(self):
        self.assertRefused(RESTAURANT_NOT_FOUND, restaurant_id=uuid.uuid4())
        self.assertNothingWritten()

    def test_soft_deleted_restaurant(self):
        """
        Refused, and with the same message as an unknown UUID: a soft-deleted tenant
        is not a valid target, and adoption has no business confirming it existed.
        """
        deleted = _make_restaurant('Gone Ltd', deleted=True)
        error = self.assertRefused(RESTAURANT_NOT_FOUND, restaurant_id=deleted.id)
        unknown = self.assertRefused(
            RESTAURANT_NOT_FOUND, restaurant_id=uuid.uuid4(),
        )
        self.assertEqual(error.message, unknown.message)
        self.assertNothingWritten()


class ActorValidationTests(_AdoptionFixture):
    """
    The log must never name an account that could not have made the decision.

    Enforced by the SERVICE, not only by its adapter — the future Admin endpoint is
    the second caller, and a rule that lives in the shell command would not be there
    for it.
    """

    def test_unknown_actor(self):
        ghost = _make_staff(username='ghost', email='ghost@t.com')
        ghost_id = ghost.id
        User.objects.filter(pk=ghost_id).delete()
        ghost.pk = ghost_id  # an instance the caller still holds

        self.assertRefused(INVALID_ACTOR, actor=ghost)
        self.assertNothingWritten()

    def test_restaurant_user_actor(self):
        self.assertRefused(INVALID_ACTOR, actor=_make_user('tenant@t.com'))
        self.assertNothingWritten()

    def test_inactive_platform_staff_actor(self):
        dormant = _make_staff(
            username='dormant-admin', email='dormant-admin@t.com', is_active=False,
        )
        self.assertRefused(INVALID_ACTOR, actor=dormant)
        self.assertNothingWritten()

    def test_missing_actor(self):
        # Called directly rather than through the fixture helper, which supplies a
        # default actor — the point of this case is that None reaches the service.
        with self.assertRaises(AdoptionError) as caught:
            adopt_existing_restaurant(
                restaurant_id=self.restaurant.id, actor=None, reason=REASON,
            )
        self.assertEqual(caught.exception.code, INVALID_ACTOR)
        self.assertNothingWritten()

    def test_actor_eligibility_is_read_from_the_row_not_the_instance(self):
        """
        An instance's ``is_active`` is whatever anything in memory last assigned to
        it. The row is the fact, so the service re-reads it.
        """
        User.objects.filter(pk=self.actor.pk).update(is_active=False)
        self.actor.is_active = True  # stale instance says otherwise

        self.assertRefused(INVALID_ACTOR)
        self.assertNothingWritten()


class ReasonValidationTests(_AdoptionFixture):
    """The same bar as a lifecycle transition and a delegation grant."""

    def test_blank_reason(self):
        self.assertRefused(INVALID_REASON, reason='')
        self.assertNothingWritten()

    def test_whitespace_only_reason(self):
        self.assertRefused(INVALID_REASON, reason='      ')
        self.assertNothingWritten()

    def test_reason_below_the_minimum(self):
        self.assertRefused(INVALID_REASON, reason='x' * (MIN_REASON_LENGTH - 1))
        self.assertNothingWritten()

    def test_reason_of_exactly_the_minimum_is_accepted(self):
        reason = 'x' * MIN_REASON_LENGTH
        result = self.adopt(reason=reason)
        self.assertEqual(result.outcome, OUTCOME_ADOPTED)
        entry = self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED)
        self.assertEqual(entry.reason, reason)

    def test_a_reason_that_is_only_long_enough_before_trimming_is_refused(self):
        self.assertRefused(INVALID_REASON, reason='  short   ')
        self.assertNothingWritten()


class ManagementCommandTests(AuditAssertionsMixin, TestCase):
    """
    The operator adapter: shell arguments in, sentences out.

    It adds no policy — the service owns every rule — so these tests cover the
    translation: that the outcome is reported truthfully, that a refusal is a
    ``CommandError`` rather than a traceback, and that nothing an operator should not
    see is printed.
    """

    def setUp(self):
        super().setUp()
        self.actor = _make_staff(username='cmd-admin', email='cmd-admin@t.com')
        self.restaurant = _make_restaurant('Command Ltd')

    def run_command(self, *, restaurant=None, actor=None, reason=REASON):
        out = StringIO()
        call_command(
            COMMAND,
            restaurant=str(
                self.restaurant.id if restaurant is None else restaurant
            ),
            actor=self.actor.username if actor is None else actor,
            reason=reason,
            stdout=out,
        )
        return out.getvalue()

    def test_adopts_and_reports_what_it_did(self):
        output = self.run_command()

        row = RestaurantOnboarding.objects.get()
        self.assertEqual(row.source, ONBOARDING_SOURCE_LEGACY_ADOPTED)
        self.assertIn(str(self.restaurant.id), output)
        self.assertIn(ONBOARDING_SOURCE_LEGACY_ADOPTED, output)
        self.assertIn('adopted', output)
        # The three absences the operator is entitled to see stated.
        self.assertIn('not recorded', output)   # attestation
        self.assertIn('none', output)           # invitation
        self.assertIn('verified', output)       # owner consistency
        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=1)

    def test_output_carries_no_owner_contact_detail(self):
        owner = self.restaurant.owner
        output = self.run_command()
        for secret in (owner.email, owner.phone_number, PASSWORD):
            self.assertNotIn(str(secret), output)

    def test_rerun_says_no_changes_made(self):
        self.run_command()
        output = self.run_command()

        self.assertIn('already represented', output)
        self.assertIn('No changes made.', output)
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
        self.assertAudited(ADMIN_RESTAURANT_ONBOARDING_ADOPTED, count=1)

    def test_source_conflict_fails_loudly(self):
        RestaurantOnboarding.objects.create(
            restaurant=self.restaurant,
            source=ONBOARDING_SOURCE_ADMIN_CREATED,
            created_by=_make_staff(username='mk', email='mk@t.com'),
        )
        with self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn(ONBOARDING_SOURCE_CONFLICT, str(caught.exception))

    def test_owner_inconsistency_fails_with_the_code_and_a_next_step(self):
        drifted = _make_restaurant('Adrift Ltd', with_owner_membership=False)
        with self.assertRaises(CommandError) as caught:
            self.run_command(restaurant=drifted.id)
        message = str(caught.exception)
        self.assertIn(MISSING_OWNER_MEMBERSHIP, message)
        self.assertIn('no ownership was repaired', message)
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_unknown_actor_is_refused_before_anything_is_written(self):
        with self.assertRaises(CommandError):
            self.run_command(actor='nobody-at-all')
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_restaurant_user_cannot_be_the_actor(self):
        tenant = _make_user('cmd-tenant@t.com')
        with self.assertRaises(CommandError):
            self.run_command(actor=tenant.username)
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_malformed_uuid_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command(restaurant='Command Ltd')
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_short_reason_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command(reason='too short')
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_the_command_offers_no_flag_that_could_fabricate_evidence(self):
        """
        Provenance is derived from the operation, never chosen by the caller, and
        adoption cannot be talked into attesting, inviting or notifying. Asserted
        against the parser rather than trusted to review: adding one of these is a
        one-line change that would look harmless in a diff.
        """
        parser = AdoptCommand().create_parser('manage.py', COMMAND)
        flags = {
            option for action in parser._actions for option in action.option_strings
        }
        for forbidden in (
            '--source', '--restaurant-name', '--owner', '--owner-email',
            '--owner-phone', '--attest-owner', '--invite-owner', '--send-email',
            '--send-sms',
        ):
            self.assertNotIn(forbidden, flags)
        for required in ('--restaurant', '--actor', '--reason'):
            self.assertIn(required, flags)


class ConcurrentAdoptionTests(TransactionTestCase):
    """
    Two operators adopting the same restaurant at once produce ONE adoption.

    The ``Restaurant`` row is the serialization point, and it has to be: the
    onboarding row does not exist yet, so there is nothing else the two attempts have
    in common to queue on. Without the lock both would pass the "does an onboarding
    row exist?" check, both would INSERT, and the loser would surface a raw
    ``IntegrityError`` from the OneToOne unique violation — a traceback where an
    idempotent no-op belongs.

    Written as a real two-connection race rather than a structural assertion about
    ``select_for_update``, because the behaviour is the guarantee; the barrier below
    only ensures both threads are genuinely in flight together.
    """

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking behaviour requires PostgreSQL')
        self.actor_a = _make_staff(username='race-a', email='race-a@t.com')
        self.actor_b = _make_staff(username='race-b', email='race-b@t.com')
        self.restaurant = _make_restaurant('Race Ltd')

    def test_only_one_adoption_survives_a_concurrent_attempt(self):
        start = threading.Barrier(2, timeout=10)
        outcomes = {}
        failures = {}

        def attempt(label, actor):
            try:
                start.wait()
                result = adopt_existing_restaurant(
                    restaurant_id=self.restaurant.id, actor=actor, reason=REASON,
                )
                outcomes[label] = result.outcome
            except Exception as error:  # noqa: BLE001 - recorded, asserted below
                failures[label] = f'{type(error).__name__}: {error}'
            finally:
                connection.close()

        threads = [
            threading.Thread(target=attempt, args=('a', self.actor_a)),
            threading.Thread(target=attempt, args=('b', self.actor_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # No IntegrityError — or anything else — escapes either caller.
        self.assertEqual(failures, {})
        self.assertEqual(
            sorted(outcomes.values()),
            sorted([OUTCOME_ADOPTED, OUTCOME_ALREADY_ADOPTED]),
        )
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action=ADMIN_RESTAURANT_ONBOARDING_ADOPTED,
            ).count(),
            1,
        )

    def test_adoption_waits_for_a_transaction_holding_the_restaurant_row(self):
        """
        The structural half, stated behaviourally: the lock is taken BEFORE the
        onboarding row is inspected or created, so a holder of the restaurant row
        blocks the whole decision — not just its final INSERT.

        Proving that something BLOCKS needs a bounded wait, so this assertion is
        timed; it is written so the timing can only cause a missed regression, never
        a spurious failure.
        """
        holder_ready = threading.Event()
        adopt_done = threading.Event()
        observed = {}

        def hold_restaurant_row():
            try:
                with transaction.atomic():
                    Restaurant.objects.select_for_update().get(
                        pk=self.restaurant.pk,
                    )
                    holder_ready.set()
                    # If nothing excluded it, the adoption's single INSERT pair
                    # commits far inside this window. A timeout here is the lock
                    # working; only an overloaded machine could mask a regression,
                    # and that direction costs a missed failure, not a false one.
                    observed['finished_while_held'] = adopt_done.wait(3)
            finally:
                connection.close()

        def adopt():
            try:
                holder_ready.wait(10)
                adopt_existing_restaurant(
                    restaurant_id=self.restaurant.id, actor=self.actor_a,
                    reason=REASON,
                )
                adopt_done.set()
            finally:
                connection.close()

        threads = [
            threading.Thread(target=hold_restaurant_row),
            threading.Thread(target=adopt),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse(
            observed.get('finished_while_held', True),
            'Adoption completed while another transaction held the Restaurant row '
            '— the serialization point is not being taken.',
        )
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
