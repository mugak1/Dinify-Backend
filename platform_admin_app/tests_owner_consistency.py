"""
``platform_admin_app.onboarding.assert_owner_consistency`` — the owner-FK /
owner-membership invariant.

Dinify answers "who owns this restaurant?" twice: ``Restaurant.owner`` (the owner
OF RECORD) and an active owner-role ``RestaurantEmployee`` (the owner AUTHORITY,
which is what the customer plane actually resolves permissions from). Nothing today
keeps the two in step. These tests pin what "in step" means before Step 2 starts
writing owners, because every disagreement below is silent in production: the Admin
portal and a diner-facing request would each render a confident, different answer.

The suite is organised around what a WRITER would get wrong:

  ACCEPTING A DRIFTED TENANT — the three inconsistent shapes each raise a distinct
  code, because they need different remedies: create a membership, resolve the
  duplicates, or decide which of two answers is authoritative.

  COUNTING AUTHORITY THAT ISN'T THERE — an inactive row, a soft-deleted row, a
  manager, or a global ``User.roles`` string. Each is something the permission
  resolver already refuses to honour; a validator that honoured them would pass a
  restaurant whose "owner" cannot actually act.

  REPAIRING ON THE WAY PAST — the validator must read and nothing else. A helper
  that quietly fixed the data would make the drift undetectable next time, and
  would do it with no actor, no reason and no audit row behind it.
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_KITCHEN,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from platform_admin_app.onboarding import (
    MISSING_OWNER_MEMBERSHIP,
    MULTIPLE_OWNER_MEMBERSHIPS,
    OWNER_CONSISTENCY_CODES,
    OWNER_MEMBERSHIP_MISMATCH,
    OwnerConsistencyError,
    assert_owner_consistency,
)
from restaurants_app.models import (
    Restaurant,
    RestaurantEmployee,
    RestaurantRolePermission,
)
from users_app.models import User

# Distinct phone range from the other admin suites (…082…) so the unique
# phone_number constraint cannot collide when suites run in one process.
_PHONE = iter(f'2567082000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'


def _make_user(email, roles=None):
    phone_number = next(_PHONE)
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number, username=phone_number,
        country='Uganda', password=PASSWORD, roles=roles or [],
        account_type=ACCOUNT_TYPE_RESTAURANT_USER,
    )


class _ConsistencyFixture(TestCase):
    """One restaurant, its owner of record, and a second user to drift with."""

    def setUp(self):
        super().setUp()
        self.owner = _make_user('consistency-owner@t.com')
        self.other = _make_user('consistency-other@t.com')
        self.restaurant = Restaurant.objects.create(
            name='Consistency House', location='Consistency Road',
            status=RestaurantStatus_Live, owner=self.owner,
        )

    def employ(self, user, roles, *, active=True, deleted=False, restaurant=None):
        return RestaurantEmployee.objects.create(
            user=user,
            restaurant=restaurant or self.restaurant,
            roles=roles,
            active=active,
            deleted=deleted,
        )

    def assertRaisesCode(self, code):
        """Assert the invariant fails with exactly ``code``, and returns it."""
        with self.assertRaises(OwnerConsistencyError) as caught:
            assert_owner_consistency(self.restaurant)
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertIn(error.code, OWNER_CONSISTENCY_CODES)
        return error


# --- A: the consistent case ----------------------------------------------------------

class ConsistentOwnershipTests(_ConsistencyFixture):
    def test_owner_of_record_matching_sole_owner_membership_passes(self):
        membership = self.employ(self.owner, [RESTAURANT_OWNER])

        self.assertEqual(assert_owner_consistency(self.restaurant).pk, membership.pk)

    def test_it_returns_the_membership_so_a_caller_need_not_re_derive_it(self):
        membership = self.employ(self.owner, [RESTAURANT_OWNER, RESTAURANT_MANAGER])

        resolved = assert_owner_consistency(self.restaurant)

        # The row the writer will act on is the row that was just validated —
        # re-querying for it would leave room to pick a different one.
        self.assertEqual(resolved.pk, membership.pk)
        self.assertEqual(resolved.user_id, self.owner.pk)

    def test_other_staff_at_the_same_restaurant_are_irrelevant(self):
        membership = self.employ(self.owner, [RESTAURANT_OWNER])
        self.employ(self.other, [RESTAURANT_MANAGER, RESTAURANT_KITCHEN])

        self.assertEqual(assert_owner_consistency(self.restaurant).pk, membership.pk)


# --- B: no owner authority at all ----------------------------------------------------

class MissingOwnerMembershipTests(_ConsistencyFixture):
    def test_owner_fk_without_any_owner_membership_fails(self):
        # A name on the tenant and no power in it: the customer plane would resolve
        # this user no modules at all.
        error = self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

        self.assertEqual(error.details['owner_id'], str(self.owner.pk))
        self.assertEqual(error.details['restaurant_id'], str(self.restaurant.pk))

    def test_no_employees_whatsoever_fails(self):
        RestaurantEmployee.objects.all().delete()

        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

    def test_an_owner_membership_at_a_different_restaurant_does_not_count(self):
        elsewhere = Restaurant.objects.create(
            name='Elsewhere House', location='Elsewhere Road',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.employ(self.owner, [RESTAURANT_OWNER], restaurant=elsewhere)

        # Authority is per-restaurant. Owning one tenant is not owning another.
        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)


# --- C: the two answers disagree -----------------------------------------------------

class OwnerMembershipMismatchTests(_ConsistencyFixture):
    def test_sole_owner_membership_belonging_to_someone_else_fails(self):
        self.employ(self.other, [RESTAURANT_OWNER])

        error = self.assertRaisesCode(OWNER_MEMBERSHIP_MISMATCH)

        # Both identities are reported, because a human has to decide which one is
        # right — the validator deliberately does not pick.
        self.assertEqual(error.details['owner_id'], str(self.owner.pk))
        self.assertEqual(error.details['membership_user_id'], str(self.other.pk))

    def test_the_owner_holding_a_non_owner_role_is_still_a_mismatch(self):
        self.employ(self.owner, [RESTAURANT_MANAGER])
        self.employ(self.other, [RESTAURANT_OWNER])

        self.assertRaisesCode(OWNER_MEMBERSHIP_MISMATCH)


# --- D: two live owners --------------------------------------------------------------

class MultipleOwnerMembershipsTests(_ConsistencyFixture):
    def test_two_active_owner_memberships_fail_even_when_one_matches(self):
        self.employ(self.owner, [RESTAURANT_OWNER])
        self.employ(self.other, [RESTAURANT_OWNER])

        # Multiplicity is checked BEFORE the identity match on purpose: "one of them
        # happens to match the FK" is ambiguity, not agreement.
        error = self.assertRaisesCode(MULTIPLE_OWNER_MEMBERSHIPS)

        self.assertEqual(
            error.details['membership_user_ids'],
            sorted([str(self.owner.pk), str(self.other.pk)]),
        )

    def test_two_active_owner_memberships_fail_when_neither_matches(self):
        third = _make_user('consistency-third@t.com')
        self.employ(self.other, [RESTAURANT_OWNER])
        self.employ(third, [RESTAURANT_OWNER])

        self.assertRaisesCode(MULTIPLE_OWNER_MEMBERSHIPS)

    def test_a_superseded_duplicate_that_is_inactive_does_not_count(self):
        membership = self.employ(self.owner, [RESTAURANT_OWNER])
        self.employ(self.other, [RESTAURANT_OWNER], active=False)

        self.assertEqual(assert_owner_consistency(self.restaurant).pk, membership.pk)


# --- E + F + G + H: what does NOT count as owner authority ---------------------------

class OwnerAuthorityExclusionTests(_ConsistencyFixture):
    def test_an_inactive_owner_membership_does_not_count(self):
        self.employ(self.owner, [RESTAURANT_OWNER], active=False)

        # These are exactly the rows the permission resolver already refuses to
        # read, so honouring them here would pass a restaurant whose owner cannot act.
        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

    def test_a_soft_deleted_owner_membership_does_not_count(self):
        self.employ(self.owner, [RESTAURANT_OWNER], deleted=True)

        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

    def test_a_manager_membership_does_not_count_as_owner(self):
        # Managers hold the same default module grid as owners, which makes them
        # easy to mistake for one. They are not the owner of the business.
        self.employ(self.owner, [RESTAURANT_MANAGER])

        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

    def test_global_user_roles_cannot_satisfy_the_invariant(self):
        # Global role metadata is not restaurant authority — that is precisely the
        # ambient-authority mechanism Phase 0.5 removed, and it must not creep back
        # in as an accepted proof of ownership.
        User.objects.filter(pk=self.owner.pk).update(
            roles=[RESTAURANT_OWNER, 'dinify_admin'],
        )
        self.restaurant.refresh_from_db()

        self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)

    def test_a_malformed_roles_value_grants_nothing(self):
        # `roles` is an untyped JSONField, so it can hold any JSON value — a bare
        # string containing 'owner' would satisfy a naive `in` test. Each of these
        # must DENY rather than raise: a validator that 500s on bad data cannot
        # report the drift it exists to find. (`None` is absent because the column
        # is NOT NULL, so the database already refuses it.)
        for malformed in (RESTAURANT_OWNER, {'role': RESTAURANT_OWNER}, 7, []):
            with self.subTest(roles=malformed):
                RestaurantEmployee.objects.all().delete()
                self.employ(self.owner, malformed)

                self.assertRaisesCode(MISSING_OWNER_MEMBERSHIP)


# --- account eligibility is a SEPARATE question --------------------------------------

class EligibilityIsNotConsistencyTests(_ConsistencyFixture):
    def test_a_deactivated_owner_account_is_still_structurally_consistent(self):
        membership = self.employ(self.owner, [RESTAURANT_OWNER])
        User.objects.filter(pk=self.owner.pk).update(is_active=False)

        # A deactivated owner may well block adoption or go-live, but that is an
        # ELIGIBILITY judgement for a later service. Conflating the two would make
        # this answer "is this restaurant ready?" when the only thing it can answer
        # is whether the owner of record agrees with the owner authority.
        self.assertEqual(assert_owner_consistency(self.restaurant).pk, membership.pk)


# --- I + J: the validator reads, and only reads --------------------------------------

class ValidatorMutatesNothingTests(_ConsistencyFixture):
    def _assert_read_only(self, call):
        """Run ``call`` and assert every statement it issued was a read."""
        with CaptureQueriesContext(connection) as captured:
            call()
        statements = [entry['sql'] for entry in captured.captured_queries]
        self.assertTrue(statements, msg='Expected the validator to query at all.')
        for sql in statements:
            upper = sql.upper()
            self.assertTrue(
                upper.lstrip().startswith('SELECT'),
                msg=f'The validator issued a non-SELECT statement: {sql}',
            )
            # No hidden locking, either. Burying `select_for_update` or an advisory
            # lock inside something that reads like a harmless assertion is how
            # lock-ordering cycles get introduced by accident — the mutating callers
            # own transaction and lock order explicitly, not this helper.
            self.assertNotIn('FOR UPDATE', upper)
            self.assertNotIn('FOR NO KEY UPDATE', upper)
            self.assertNotIn('FOR SHARE', upper)
            self.assertNotIn('ADVISORY', upper)

    def test_the_passing_path_writes_nothing(self):
        self.employ(self.owner, [RESTAURANT_OWNER])

        self._assert_read_only(lambda: assert_owner_consistency(self.restaurant))

    def test_the_failing_path_writes_nothing(self):
        self.employ(self.other, [RESTAURANT_OWNER])

        def call():
            with self.assertRaises(OwnerConsistencyError):
                assert_owner_consistency(self.restaurant)

        self._assert_read_only(call)

    def test_it_does_not_repair_a_mismatch(self):
        membership = self.employ(self.other, [RESTAURANT_OWNER])

        with self.assertRaises(OwnerConsistencyError):
            assert_owner_consistency(self.restaurant)

        # Repair is a decision with an actor, a reason and an audit row behind it.
        # A validator that quietly fixed this would make the drift undetectable.
        self.restaurant.refresh_from_db()
        membership.refresh_from_db()
        self.assertEqual(self.restaurant.owner_id, self.owner.pk)
        self.assertEqual(membership.user_id, self.other.pk)
        self.assertTrue(membership.active)
        self.assertFalse(membership.deleted)

    def test_it_does_not_deactivate_duplicate_memberships(self):
        first = self.employ(self.owner, [RESTAURANT_OWNER])
        second = self.employ(self.other, [RESTAURANT_OWNER])

        with self.assertRaises(OwnerConsistencyError):
            assert_owner_consistency(self.restaurant)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.active)
        self.assertTrue(second.active)
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True, deleted=False,
            ).count(),
            2,
        )

    def test_it_creates_no_role_permission_rows(self):
        # Seeding a role grid is `ensure_role_permissions`' job, invoked by the
        # owner-only management surface. A validator must not seed anything.
        self.employ(self.owner, [RESTAURANT_OWNER])
        self.assertEqual(RestaurantRolePermission.objects.count(), 0)

        assert_owner_consistency(self.restaurant)

        self.assertEqual(RestaurantRolePermission.objects.count(), 0)

    def test_it_creates_no_role_permission_rows_on_the_failing_path(self):
        with self.assertRaises(OwnerConsistencyError):
            assert_owner_consistency(self.restaurant)

        self.assertEqual(RestaurantRolePermission.objects.count(), 0)


# --- the exception itself ------------------------------------------------------------

class ErrorSurfaceTests(_ConsistencyFixture):
    def test_details_carry_identifiers_and_no_contact_information(self):
        self.employ(self.other, [RESTAURANT_OWNER])

        error = self.assertRaisesCode(OWNER_MEMBERSHIP_MISMATCH)

        # This ends up in logs and, eventually, in operator-facing surfaces. UUIDs
        # are enough to investigate with; an owner's email or phone is not ours to
        # scatter through them.
        rendered = f'{error} {error.details}'
        for leaked in (
            self.owner.email, self.other.email,
            self.owner.phone_number, self.other.phone_number,
            self.owner.first_name, self.restaurant.name,
        ):
            self.assertNotIn(leaked, rendered)

    def test_it_is_not_a_drf_exception(self):
        from rest_framework.exceptions import APIException

        # A plain Exception on purpose: a future writer must handle this
        # explicitly — refuse the adoption, surface the conflict — rather than have
        # an exception handler render it as a tidy 4xx on a path that had no
        # business continuing.
        self.assertFalse(issubclass(OwnerConsistencyError, APIException))
