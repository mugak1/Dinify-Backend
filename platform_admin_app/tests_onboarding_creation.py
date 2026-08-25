"""
The admin restaurant-creation DOMAIN SERVICE (Phase 1, Step 2D).

``create_admin_restaurant`` is the authoritative primitive; the HTTP endpoint is an
adapter over it, and a future second adapter would inherit exactly what is pinned
here. So this suite tests the SERVICE — the rows it writes, the rows it refuses to
write, the invariant it proves before committing, and the long list of things it
deliberately does NOT touch. The HTTP contract (status codes, audit rows, elevation,
CSRF, the no-store response) is ``tests_restaurant_creation_endpoint``'s subject.
"""
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
)
from platform_admin_app import onboarding_creation, sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import assert_owner_consistency
from platform_admin_app.onboarding_creation import (
    CreationResult,
    ExistingOwner,
    NewOwner,
    RestaurantCreationError,
    create_admin_restaurant,
)
from platform_admin_app.onboarding_reads import onboarding_summary
from restaurants_app.models import (
    DiningArea,
    MenuItem,
    MenuSection,
    Restaurant,
    RestaurantEmployee,
    RestaurantRolePermission,
    Table,
)
from users_app.models import User

PASSWORD = 'correct-horse-battery'
REASON = 'Signed onboarding agreement received from the owner this morning.'

# Distinct phone range from every other suite.
_ADMIN_PHONE = iter(f'25670960{n:05d}' for n in range(1, 9999))


def make_admin(username='create-admin', email='create-admin@t.com'):
    return User.objects.create_user(
        username=username, email=email, first_name='Ada', last_name='Admin',
        phone_number=None, country='UG', password=PASSWORD, roles=[],
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


def make_restaurant_user(phone=None, email=None, is_active=True, **extra):
    phone = phone or next(_ADMIN_PHONE)
    user = User.objects.create_user(
        username=phone, phone_number=phone, email=email,
        first_name='Ray', last_name='Restaurant', country='UG',
        password=PASSWORD, roles=[], account_type=ACCOUNT_TYPE_RESTAURANT_USER,
        **extra,
    )
    if not is_active:
        User.objects.filter(pk=user.pk).update(is_active=False)
        user.refresh_from_db()
    return user


def new_owner(**overrides):
    facts = dict(
        first_name='jane', last_name='doe',
        phone_number='0772123456', email='Jane@Example.COM',
    )
    facts.update(overrides)
    return NewOwner(**facts)


# A sentinel, so a test can pass `owner=None` and have it REACH the service — the
# helper substituting a default for None would silently make that case untestable.
_DEFAULT = object()


def create(actor, *, name='Kampala Bistro', location='Kololo, Kampala',
           is_test=False, owner=_DEFAULT, reason=REASON):
    return create_admin_restaurant(
        name=name, location=location, is_test=is_test,
        owner=new_owner() if owner is _DEFAULT else owner,
        actor=actor, reason=reason,
    )


class _CreationTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_admin()

    def assertRefused(self, code, **kwargs):
        """The service refuses with ``code`` and writes nothing at all."""
        before = (
            User.objects.count(), Restaurant.objects.count(),
            RestaurantEmployee.objects.count(),
            RestaurantOnboarding.objects.count(), OwnerInvitation.objects.count(),
        )
        with self.assertRaises(RestaurantCreationError) as ctx:
            create(self.admin, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        after = (
            User.objects.count(), Restaurant.objects.count(),
            RestaurantEmployee.objects.count(),
            RestaurantOnboarding.objects.count(), OwnerInvitation.objects.count(),
        )
        self.assertEqual(before, after, 'a refusal must have zero side effects')
        return ctx.exception


# --- §31 the happy path, new owner -------------------------------------------

class CreateWithNewOwnerTests(_CreationTestCase):
    """One request, six rows, and every platform-owned fact supplied by the server."""

    def setUp(self):
        super().setUp()
        self.result = create(self.admin)

    def test_returns_a_frozen_result_stating_what_happened(self):
        self.assertIsInstance(self.result, CreationResult)
        self.assertTrue(self.result.owner_created)
        with self.assertRaises(Exception):
            self.result.owner_created = False

    def test_exactly_one_owner_account_was_created(self):
        # The actor plus the owner. Nothing else.
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(self.result.owner, User.objects.exclude(pk=self.admin.pk).get())

    def test_username_and_phone_are_the_canonical_msisdn(self):
        owner = self.result.owner
        self.assertEqual(owner.phone_number, '256772123456')
        self.assertEqual(owner.username, '256772123456')

    def test_owner_is_a_restaurant_user(self):
        self.assertEqual(self.result.owner.account_type, ACCOUNT_TYPE_RESTAURANT_USER)

    def test_owner_password_is_unusable(self):
        """
        NO PASSWORD IS GENERATED — the whole point of the invitation architecture.

        An unusable password means there is no credential to email, SMS, leak or
        rotate, and nothing can authenticate as this account until a future
        redemption establishes one.
        """
        self.assertFalse(self.result.owner.has_usable_password())

    def test_owner_holds_no_platform_authority(self):
        self.assertEqual(self.result.owner.roles, [])
        self.assertFalse(self.result.owner.is_staff)
        self.assertFalse(self.result.owner.is_superuser)

    def test_owner_name_and_email_are_normalised(self):
        owner = self.result.owner
        self.assertEqual((owner.first_name, owner.last_name), ('Jane', 'Doe'))
        self.assertEqual(owner.email, 'jane@example.com')

    def test_restaurant_is_owned_by_the_new_account(self):
        self.assertEqual(self.result.restaurant.owner_id, self.result.owner.id)

    def test_restaurant_starts_onboarding(self):
        self.assertEqual(self.result.restaurant.status, RestaurantStatus_Onboarding)

    def test_restaurant_name_and_location_are_stored_verbatim_but_trimmed(self):
        result = create(
            self.admin, name='  Java   House  ', location=' Ntinda,  Kampala ',
            owner=new_owner(phone_number='0700000001', email=None),
        )
        # Whitespace collapsed; case NOT touched — `.title()` would render "KFC" as
        # "Kfc", and the operator typed the business's real name.
        self.assertEqual(result.restaurant.name, 'Java House')
        self.assertEqual(result.restaurant.location, 'Ntinda, Kampala')

    def test_country_is_the_server_phase_one_value(self):
        self.assertEqual(self.result.restaurant.country, 'UG')
        self.assertEqual(self.result.owner.country, 'UG')

    def test_created_by_is_the_platform_actor_and_is_not_authority(self):
        self.assertEqual(self.result.restaurant.created_by_id, self.admin.pk)
        # Attribution only: the actor holds no membership at the new restaurant.
        self.assertFalse(
            RestaurantEmployee.objects.filter(
                user=self.admin, restaurant=self.result.restaurant,
            ).exists()
        )

    # --- owner authority ---

    def test_exactly_one_active_owner_membership_exists(self):
        memberships = RestaurantEmployee.objects.filter(
            restaurant=self.result.restaurant,
        )
        self.assertEqual(memberships.count(), 1)
        membership = memberships.get()
        self.assertEqual(membership.user_id, self.result.owner.id)
        self.assertTrue(membership.active)
        self.assertFalse(membership.deleted)
        self.assertEqual(membership.created_by_id, self.admin.pk)

    def test_membership_uses_the_canonical_owner_role(self):
        membership = RestaurantEmployee.objects.get(restaurant=self.result.restaurant)
        self.assertEqual(membership.roles, [RESTAURANT_OWNER])

    def test_owner_consistency_holds(self):
        # The invariant the service proves before committing, re-proved from outside.
        self.assertIsNotNone(assert_owner_consistency(self.result.restaurant))

    def test_no_role_permission_rows_are_seeded(self):
        """
        The resolver falls back to the coded defaults, so override rows would be
        redundant state whose only future is to drift from them.
        """
        self.assertFalse(
            RestaurantRolePermission.objects.filter(
                restaurant=self.result.restaurant,
            ).exists()
        )

    # --- provenance ---

    def test_onboarding_is_admin_created_and_attributed(self):
        onboarding = RestaurantOnboarding.objects.get(
            restaurant=self.result.restaurant,
        )
        self.assertEqual(onboarding.source, ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(onboarding.created_by_id, self.admin.pk)

    def test_no_adoption_or_attestation_fields_are_written(self):
        onboarding = RestaurantOnboarding.objects.get(
            restaurant=self.result.restaurant,
        )
        self.assertIsNone(onboarding.adopted_at)
        self.assertIsNone(onboarding.adopted_by_id)
        self.assertIsNone(onboarding.owner_control_attested_at)
        self.assertIsNone(onboarding.owner_control_attested_user_id)
        self.assertIsNone(onboarding.owner_control_attested_by_id)

    # --- the credential ---

    def test_exactly_one_unresolved_invitation_is_issued(self):
        invitations = OwnerInvitation.objects.filter(
            onboarding=self.result.onboarding,
        )
        self.assertEqual(invitations.count(), 1)
        invitation = invitations.get()
        self.assertFalse(invitation.is_resolved)
        self.assertTrue(invitation.is_claimable)
        self.assertIsNone(invitation.consumed_at)
        self.assertIsNone(invitation.cancelled_at)
        self.assertIsNone(invitation.cancelled_by_id)
        self.assertIsNone(invitation.superseded_at)

    def test_invitation_targets_the_owner_and_names_the_issuer(self):
        invitation = self.result.invitation
        self.assertEqual(invitation.invited_user_id, self.result.restaurant.owner_id)
        self.assertEqual(invitation.issued_by_id, self.admin.pk)

    def test_only_the_token_hash_is_persisted(self):
        invitation = OwnerInvitation.objects.get(pk=self.result.invitation.pk)
        self.assertEqual(
            invitation.token_hash, sessions.hash_token(self.result.claim_token),
        )
        self.assertNotIn(self.result.claim_token, invitation.token_hash)

    def test_the_raw_token_is_high_entropy_and_url_safe(self):
        token = self.result.claim_token
        self.assertGreaterEqual(len(token), 43)
        self.assertRegex(token, r'^[A-Za-z0-9_-]+$')

    def test_two_creations_mint_different_tokens(self):
        other = create(
            self.admin, name='Second Bistro', location='Ntinda',
            owner=new_owner(phone_number='0700000002', email=None),
        )
        self.assertNotEqual(self.result.claim_token, other.claim_token)

    def test_expiry_is_exactly_the_configured_ttl_after_issue(self):
        invitation = self.result.invitation
        self.assertEqual(
            invitation.expires_at - invitation.issued_at,
            onboarding_creation.owner_invitation_ttl(),
        )

    def test_default_ttl_is_seven_days(self):
        self.assertEqual(
            onboarding_creation.OWNER_INVITATION_TTL_DEFAULT, timedelta(days=7),
        )

    @override_settings(ADMIN_OWNER_INVITATION_TTL=timedelta(hours=6))
    def test_ttl_is_configurable(self):
        result = create(
            self.admin, name='Short Window', location='Kabalagala',
            owner=new_owner(phone_number='0700000003', email=None),
        )
        self.assertEqual(
            result.invitation.expires_at - result.invitation.issued_at,
            timedelta(hours=6),
        )

    def test_issue_and_expiry_come_from_one_captured_instant(self):
        """No sub-millisecond drift: the window is exactly the TTL, not TTL±epsilon."""
        invitation = self.result.invitation
        self.assertEqual(invitation.issued_at.microsecond,
                         (invitation.expires_at).microsecond)

    # --- the canonical read ---

    def test_the_onboarding_projection_derives_the_expected_state(self):
        """
        The read model is UNTOUCHED by this PR: these four words are derived by the
        existing evidence rules from the rows the service wrote.
        """
        summary = onboarding_summary(self.result.restaurant)
        self.assertTrue(summary['tracked'])
        self.assertEqual(summary['source'], ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(summary['owner_relationship']['status'], 'consistent')
        self.assertEqual(summary['owner_control']['status'], 'not_established')
        self.assertIsNone(summary['owner_control']['evidence'])
        self.assertEqual(summary['invitation']['status'], 'pending')

    def test_recorded_at_is_the_onboarding_rows_own_created_at(self):
        summary = onboarding_summary(self.result.restaurant)
        self.assertEqual(
            summary['recorded_at'], self.result.onboarding.created_at.isoformat(),
        )


# --- §31 explicit test classification ----------------------------------------

class TestClassificationTests(_CreationTestCase):
    def test_is_test_false_is_persisted(self):
        self.assertFalse(create(self.admin, is_test=False).restaurant.is_test)

    def test_is_test_true_is_persisted(self):
        self.assertTrue(create(self.admin, is_test=True).restaurant.is_test)

    def test_is_test_must_be_a_real_boolean(self):
        for value in (None, 1, 0, 'true', 'false', '', []):
            with self.subTest(value=value):
                self.assertRefused(
                    onboarding_creation.INVALID_TEST_CLASSIFICATION, is_test=value,
                )

    def test_classification_is_never_inferred_from_the_name(self):
        result = create(
            self.admin, name='Test Kitchen Demo', location='Internal', is_test=False,
        )
        self.assertFalse(result.restaurant.is_test)

    def test_creation_writes_no_test_classification_audit_row(self):
        """
        `mark_restaurant_test` exists to CHANGE an existing tenant's classification.
        Creation states it once, and the creation audit records it — a second event
        for a fact the creation request already stated would double-count it.
        """
        create(self.admin, is_test=True)
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action='admin.restaurant.test_classification_changed',
            ).exists()
        )


# --- §31 phone canonicalisation ----------------------------------------------

class PhoneCanonicalisationTests(_CreationTestCase):
    def test_every_accepted_spelling_stores_the_same_canonical_value(self):
        for index, spelling in enumerate((
            '0772123456', '772123456', '256772123456', '+256772123456',
            '+256 772 123 456', '0772-123-456',
        )):
            with self.subTest(spelling=spelling):
                # Each is the SAME number, so only the first can succeed; the rest
                # must collide, which is itself the proof they canonicalise equally.
                if index == 0:
                    result = create(
                        self.admin, owner=new_owner(phone_number=spelling, email=None),
                    )
                    self.assertEqual(result.owner.phone_number, '256772123456')
                else:
                    self.assertRefused(
                        onboarding_creation.OWNER_ACCOUNT_ALREADY_EXISTS,
                        name=f'Bistro {index}', location=f'Road {index}',
                        owner=new_owner(phone_number=spelling, email=None),
                    )

    def test_a_non_ugandan_or_malformed_number_is_refused(self):
        for spelling in ('', None, 'not-a-number', '12345', '+15551234567'):
            with self.subTest(spelling=spelling):
                self.assertRefused(
                    onboarding_creation.INVALID_OWNER_PHONE,
                    owner=new_owner(phone_number=spelling, email=None),
                )

    def test_the_refusal_never_echoes_the_raw_number(self):
        exc = self.assertRefused(
            onboarding_creation.INVALID_OWNER_PHONE,
            owner=new_owner(phone_number='07721234567890', email=None),
        )
        self.assertNotIn('0772123456', str(exc))
        self.assertEqual(exc.details, {})


# --- §31 new-owner collisions ------------------------------------------------

class NewOwnerCollisionTests(_CreationTestCase):
    def test_an_existing_phone_is_a_conflict_never_a_silent_reuse(self):
        existing = make_restaurant_user(phone='256772123456')
        exc = self.assertRefused(onboarding_creation.OWNER_ACCOUNT_ALREADY_EXISTS)
        # The operator is told WHICH account, by UUID, so they can look at it and
        # deliberately choose the existing-account path.
        self.assertEqual(exc.details, {'owner_user_id': str(existing.pk)})

    def test_the_conflict_carries_no_pii(self):
        make_restaurant_user(phone='256772123456', email='someone@else.com')
        exc = self.assertRefused(onboarding_creation.OWNER_ACCOUNT_ALREADY_EXISTS)
        blob = f'{exc.message} {exc.details}'
        self.assertNotIn('someone@else.com', blob)
        self.assertNotIn('256772123456', blob)

    def test_a_platform_staff_phone_collision_is_also_refused(self):
        User.objects.filter(pk=self.admin.pk).update(phone_number='256772123456')
        self.assertRefused(onboarding_creation.OWNER_ACCOUNT_ALREADY_EXISTS)

    def test_a_duplicate_email_is_refused_and_names_no_account(self):
        """
        Email is NOT identity — but `login` and `reset_password._resolve_user` both
        call `User.objects.get(email=...)`, so a duplicate would 500 BOTH users'
        email login and password reset.
        """
        make_restaurant_user(email='jane@example.com')
        exc = self.assertRefused(onboarding_creation.OWNER_EMAIL_ALREADY_IN_USE)
        self.assertEqual(exc.details, {})

    def test_the_email_check_is_case_insensitive(self):
        make_restaurant_user(email='JANE@example.com')
        self.assertRefused(onboarding_creation.OWNER_EMAIL_ALREADY_IN_USE)

    def test_a_blank_email_never_collides_with_another_blank_one(self):
        create(self.admin, owner=new_owner(phone_number='0700000011', email=None))
        result = create(
            self.admin, name='Blank Two', location='Bugolobi',
            owner=new_owner(phone_number='0700000012', email='   '),
        )
        self.assertIsNone(result.owner.email)

    def test_email_is_never_used_to_select_an_owner(self):
        existing = make_restaurant_user(email='jane@example.com')
        self.assertRefused(onboarding_creation.OWNER_EMAIL_ALREADY_IN_USE)
        # The pre-existing account gained nothing.
        self.assertFalse(
            RestaurantEmployee.objects.filter(user=existing).exists()
        )


# --- §31 existing owner ------------------------------------------------------

class ExistingOwnerTests(_CreationTestCase):
    def setUp(self):
        super().setUp()
        self.existing = make_restaurant_user(email='owner@existing.com')
        self.snapshot = {
            field: getattr(self.existing, field)
            for field in (
                'username', 'email', 'phone_number', 'password', 'roles',
                'first_name', 'last_name', 'country', 'account_type',
                'is_active', 'prompt_password_change',
            )
        }

    def attach(self, **kwargs):
        kwargs.setdefault('owner', ExistingOwner(user_id=self.existing.pk))
        return create(self.admin, **kwargs)

    def assertOwnerUntouched(self):
        self.existing.refresh_from_db()
        for field, value in self.snapshot.items():
            self.assertEqual(getattr(self.existing, field), value, field)

    def test_an_active_restaurant_user_is_reused(self):
        result = self.attach()
        self.assertFalse(result.owner_created)
        self.assertEqual(result.owner.pk, self.existing.pk)
        self.assertEqual(result.restaurant.owner_id, self.existing.pk)

    def test_no_new_user_is_created(self):
        before = User.objects.count()
        self.attach()
        self.assertEqual(User.objects.count(), before)

    def test_the_existing_account_is_not_modified_in_any_way(self):
        self.attach()
        self.assertOwnerUntouched()

    def test_a_uuid_string_is_accepted(self):
        result = self.attach(owner=ExistingOwner(user_id=str(self.existing.pk)))
        self.assertEqual(result.owner.pk, self.existing.pk)

    def test_one_owner_may_hold_several_restaurants(self):
        first = self.attach(name='First Bistro', location='Kololo')
        second = self.attach(name='Second Bistro', location='Ntinda')
        self.assertNotEqual(first.restaurant.pk, second.restaurant.pk)
        for result in (first, second):
            with self.subTest(restaurant=result.restaurant.name):
                self.assertIsNotNone(assert_owner_consistency(result.restaurant))
                self.assertEqual(
                    RestaurantEmployee.objects.filter(
                        restaurant=result.restaurant, active=True,
                    ).count(),
                    1,
                )
                self.assertEqual(
                    OwnerInvitation.objects.filter(
                        onboarding=result.onboarding,
                    ).count(),
                    1,
                )
        self.assertNotEqual(first.claim_token, second.claim_token)

    def test_each_restaurant_gets_its_own_onboarding_and_invitation(self):
        first = self.attach(name='First Bistro', location='Kololo')
        second = self.attach(name='Second Bistro', location='Ntinda')
        self.assertNotEqual(first.onboarding.pk, second.onboarding.pk)
        self.assertNotEqual(first.invitation.pk, second.invitation.pk)
        self.assertEqual(second.invitation.invited_user_id, self.existing.pk)

    def test_an_unknown_account_is_refused_and_named_as_such(self):
        exc = self.assertRefused(
            onboarding_creation.OWNER_ACCOUNT_NOT_FOUND,
            owner=ExistingOwner(user_id='7f1c0000-0000-0000-0000-00000000dead'),
        )
        self.assertIn('owner_user_id', exc.details)

    def test_a_malformed_uuid_is_a_request_error_not_a_conflict(self):
        for value in ('', None, 'not-a-uuid', 42):
            with self.subTest(value=value):
                self.assertRefused(
                    onboarding_creation.INVALID_OWNER_USER_ID,
                    owner=ExistingOwner(user_id=value),
                )

    def test_a_deactivated_account_is_refused_and_never_reactivated(self):
        User.objects.filter(pk=self.existing.pk).update(is_active=False)
        self.snapshot['is_active'] = False
        self.assertRefused(
            onboarding_creation.OWNER_ACCOUNT_INACTIVE,
            owner=ExistingOwner(user_id=self.existing.pk),
        )
        self.assertOwnerUntouched()

    def test_a_platform_staff_account_can_never_become_an_owner(self):
        self.assertRefused(
            onboarding_creation.OWNER_ACCOUNT_NOT_RESTAURANT_USER,
            owner=ExistingOwner(user_id=self.admin.pk),
        )
        self.assertFalse(RestaurantEmployee.objects.filter(user=self.admin).exists())

    def test_the_actor_cannot_name_themselves_as_the_owner(self):
        self.assertRefused(
            onboarding_creation.OWNER_ACCOUNT_NOT_RESTAURANT_USER,
            owner=ExistingOwner(user_id=self.admin.pk),
        )


# --- §31 duplicate restaurants -----------------------------------------------

class DuplicateRestaurantTests(_CreationTestCase):
    def test_the_same_owner_cannot_hold_two_identical_restaurants(self):
        existing = make_restaurant_user()
        create(self.admin, owner=ExistingOwner(user_id=existing.pk))
        self.assertRefused(
            onboarding_creation.RESTAURANT_ALREADY_EXISTS,
            owner=ExistingOwner(user_id=existing.pk),
        )

    def test_a_different_owner_cannot_duplicate_a_live_name_and_location(self):
        create(self.admin, owner=new_owner(phone_number='0700000021', email=None))
        exc = self.assertRefused(
            onboarding_creation.RESTAURANT_ALREADY_EXISTS,
            owner=new_owner(phone_number='0700000022', email=None),
        )
        self.assertIn('restaurant_id', exc.details)

    def test_the_duplicate_rule_is_case_and_whitespace_insensitive(self):
        create(self.admin, owner=new_owner(phone_number='0700000023', email=None))
        self.assertRefused(
            onboarding_creation.RESTAURANT_ALREADY_EXISTS,
            name='  kampala   BISTRO ', location='kololo, KAMPALA',
            owner=new_owner(phone_number='0700000024', email=None),
        )

    def test_a_different_location_is_not_a_duplicate(self):
        first = create(self.admin, owner=new_owner(phone_number='0700000025', email=None))
        second = create(
            self.admin, location='Ntinda, Kampala',
            owner=new_owner(phone_number='0700000026', email=None),
        )
        self.assertNotEqual(first.restaurant.pk, second.restaurant.pk)

    def test_a_soft_deleted_restaurant_still_blocks_its_own_owner(self):
        """
        The unique index carries no ``deleted`` predicate, so the slot is still
        occupied — said as a sentence rather than surfaced as an integrity error.
        """
        existing = make_restaurant_user()
        first = create(self.admin, owner=ExistingOwner(user_id=existing.pk))
        Restaurant.objects.filter(pk=first.restaurant.pk).update(deleted=True)
        self.assertRefused(
            onboarding_creation.RESTAURANT_ALREADY_EXISTS,
            owner=ExistingOwner(user_id=existing.pk),
        )

    def test_a_soft_deleted_restaurant_does_not_block_a_different_owner(self):
        first = create(self.admin, owner=new_owner(phone_number='0700000027', email=None))
        Restaurant.objects.filter(pk=first.restaurant.pk).update(deleted=True)
        second = create(
            self.admin, owner=new_owner(phone_number='0700000028', email=None),
        )
        self.assertNotEqual(first.restaurant.pk, second.restaurant.pk)

    def test_creation_never_returns_the_existing_restaurant_as_though_it_made_it(self):
        first = create(self.admin, owner=new_owner(phone_number='0700000029', email=None))
        with self.assertRaises(RestaurantCreationError):
            create(self.admin, owner=new_owner(phone_number='0700000030', email=None))
        self.assertEqual(Restaurant.objects.count(), 1)
        self.assertEqual(Restaurant.objects.get().pk, first.restaurant.pk)

    def test_a_lost_uniqueness_race_becomes_a_conflict_not_an_integrity_error(self):
        """
        The pre-check can lose a race; the database constraint is the backstop, and
        the savepoint around the INSERT is what turns it into a named refusal.
        """
        existing = make_restaurant_user()
        create(self.admin, owner=ExistingOwner(user_id=existing.pk))
        with patch.object(
            onboarding_creation, '_duplicate_restaurant_id',
            side_effect=[None, 'irrelevant'],
        ):
            with self.assertRaises(RestaurantCreationError) as ctx:
                create(self.admin, owner=ExistingOwner(user_id=existing.pk))
        self.assertEqual(ctx.exception.code, onboarding_creation.RESTAURANT_ALREADY_EXISTS)

    def test_an_unrelated_integrity_error_is_never_relabelled_a_duplicate(self):
        with patch.object(
            Restaurant.objects, 'create', side_effect=IntegrityError('something else'),
        ):
            with self.assertRaises(IntegrityError):
                create(self.admin)


# --- §31 request-level validation --------------------------------------------

class RequestValidationTests(_CreationTestCase):
    def test_a_blank_name_is_refused(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                self.assertRefused(
                    onboarding_creation.INVALID_RESTAURANT_NAME, name=value,
                )

    def test_a_blank_location_is_refused(self):
        for value in ('', '\t\n ', None):
            with self.subTest(value=value):
                self.assertRefused(
                    onboarding_creation.INVALID_RESTAURANT_LOCATION, location=value,
                )

    def test_an_over_long_name_is_a_named_refusal_not_a_database_error(self):
        self.assertRefused(
            onboarding_creation.INVALID_RESTAURANT_NAME, name='x' * 256,
        )

    def test_a_short_or_missing_reason_is_refused(self):
        for value in (None, '', '   ', 'too short'):
            with self.subTest(value=value):
                self.assertRefused(onboarding_creation.INVALID_REASON, reason=value)

    def test_the_reason_bar_is_the_house_one(self):
        from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
        from platform_admin_app.delegation import (
            MIN_REASON_LENGTH as DELEGATION_MIN,
        )
        self.assertEqual(MIN_REASON_LENGTH, DELEGATION_MIN)

    def test_a_blank_owner_name_is_refused(self):
        for field in ('first_name', 'last_name'):
            with self.subTest(field=field):
                self.assertRefused(
                    onboarding_creation.INVALID_OWNER_NAME,
                    owner=new_owner(**{field: '  '}),
                )

    def test_an_unrecognised_owner_spec_is_refused(self):
        for spec in (None, {'mode': 'new'}, 'existing'):
            with self.subTest(spec=spec):
                self.assertRefused(
                    onboarding_creation.INVALID_OWNER_SPEC, owner=spec,
                )

    def test_a_malformed_email_is_refused(self):
        for value in ('not-an-email', '@example.com', 'jane@'):
            with self.subTest(value=value):
                self.assertRefused(
                    onboarding_creation.INVALID_OWNER_EMAIL,
                    owner=new_owner(email=value),
                )


# --- §31 the actor -----------------------------------------------------------

class ActorTests(_CreationTestCase):
    def test_a_restaurant_user_can_never_be_the_actor(self):
        with self.assertRaises(RestaurantCreationError) as ctx:
            create(make_restaurant_user())
        self.assertEqual(ctx.exception.code, onboarding_creation.INVALID_ACTOR)

    def test_a_deactivated_platform_actor_is_refused(self):
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(RestaurantCreationError) as ctx:
            create(self.admin)
        self.assertEqual(ctx.exception.code, onboarding_creation.INVALID_ACTOR)

    def test_the_actor_is_re_read_rather_than_trusted(self):
        """An in-memory instance cannot assert an eligibility the row denies."""
        User.objects.filter(pk=self.admin.pk).update(
            account_type=ACCOUNT_TYPE_RESTAURANT_USER,
        )
        # The stale instance still claims platform staff.
        self.assertEqual(self.admin.account_type, ACCOUNT_TYPE_PLATFORM_STAFF)
        with self.assertRaises(RestaurantCreationError) as ctx:
            create(self.admin)
        self.assertEqual(ctx.exception.code, onboarding_creation.INVALID_ACTOR)

    def test_a_non_user_actor_is_refused(self):
        for actor in (None, 'admin', 42):
            with self.subTest(actor=actor):
                with self.assertRaises(RestaurantCreationError) as ctx:
                    create(actor)
                self.assertEqual(ctx.exception.code, onboarding_creation.INVALID_ACTOR)


# --- §31 atomicity -----------------------------------------------------------

class AtomicityTests(_CreationTestCase):
    """A failure at any stage leaves nothing behind — through the database, not cleanup."""

    def assertNothingCreated(self):
        self.assertEqual(User.objects.count(), 1)  # the actor only
        self.assertFalse(Restaurant.objects.exists())
        self.assertFalse(RestaurantEmployee.objects.exists())
        self.assertFalse(RestaurantOnboarding.objects.exists())
        self.assertFalse(OwnerInvitation.objects.exists())

    def test_an_invitation_failure_leaves_no_orphan_user_or_tenant(self):
        with patch.object(
            OwnerInvitation.objects, 'create', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                create(self.admin)
        self.assertNothingCreated()

    def test_an_onboarding_failure_leaves_no_orphan_user_or_tenant(self):
        with patch.object(
            RestaurantOnboarding.objects, 'create', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                create(self.admin)
        self.assertNothingCreated()

    def test_a_membership_failure_leaves_no_orphan_user_or_tenant(self):
        with patch.object(
            RestaurantEmployee.objects, 'create', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                create(self.admin)
        self.assertNothingCreated()

    def test_an_owner_consistency_failure_takes_the_whole_creation_with_it(self):
        from platform_admin_app.onboarding import OwnerConsistencyError

        with patch.object(
            onboarding_creation, 'assert_owner_consistency',
            side_effect=OwnerConsistencyError('missing_owner_membership'),
        ):
            with self.assertRaises(OwnerConsistencyError):
                create(self.admin)
        self.assertNothingCreated()

    def test_an_existing_owner_survives_a_later_failure_untouched(self):
        existing = make_restaurant_user()
        with patch.object(
            OwnerInvitation.objects, 'create', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                create(self.admin, owner=ExistingOwner(user_id=existing.pk))
        self.assertTrue(User.objects.filter(pk=existing.pk).exists())
        existing.refresh_from_db()
        self.assertTrue(existing.is_active)
        self.assertFalse(Restaurant.objects.exists())


# --- §31 what creation must NOT do -------------------------------------------

class NoSideEffectsTests(_CreationTestCase):
    """
    A newborn tenant is commercially and operationally EMPTY, and that is correct:
    those absences are the readiness blockers a later step will name.
    """

    def setUp(self):
        super().setUp()
        self.result = create(self.admin)

    def test_no_commercial_rows_are_created(self):
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_no_operational_rows_are_created(self):
        self.assertFalse(DiningArea.objects.exists())
        self.assertFalse(Table.objects.exists())
        self.assertFalse(MenuSection.objects.exists())
        self.assertFalse(MenuItem.objects.exists())

    def test_the_service_writes_no_audit_row(self):
        """
        The audit belongs to the ADAPTER, because the auditable unit is the request
        — which also has to record denials and unreadable bodies the service never
        sees. Pinned so nobody "helpfully" adds a second one here.
        """
        self.assertFalse(AdminAuditLog.objects.exists())

    def test_readiness_is_unchanged_and_still_fails_closed(self):
        from restaurants_app.controllers import lifecycle

        readiness = lifecycle.check_go_live_readiness(self.result.restaurant)
        self.assertFalse(readiness.ready)
        self.assertIn(
            lifecycle.BLOCKER_READINESS_NOT_CONFIGURED, readiness.blockers,
        )

    def test_creation_never_writes_legacy_adopted_provenance(self):
        self.assertFalse(
            RestaurantOnboarding.objects.filter(
                source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            ).exists()
        )

    def test_no_notification_is_sent(self):
        """Issuance is NOT delivery: nothing here reaches the notification stack."""
        with patch(
            'misc_app.controllers.notifications.notification.Notification'
        ) as notification:
            create(
                self.admin, name='Silent House', location='Muyenga',
                owner=new_owner(phone_number='0700000031', email=None),
            )
        notification.assert_not_called()

    def test_no_legacy_action_log_is_written(self):
        with patch('misc_app.controllers.save_action_log.save_action') as save_action:
            create(
                self.admin, name='Quiet House', location='Bukoto',
                owner=new_owner(phone_number='0700000032', email=None),
            )
        save_action.assert_not_called()

    def test_the_retired_self_register_path_is_never_called(self):
        with patch('users_app.controllers.self_register.self_register') as register:
            create(
                self.admin, name='Fresh House', location='Kansanga',
                owner=new_owner(phone_number='0700000033', email=None),
            )
        register.assert_not_called()

    def test_no_otp_is_manufactured(self):
        from users_app.models import UserOtp

        self.assertFalse(UserOtp.objects.exists())

    def test_creation_does_not_change_an_existing_restaurants_lifecycle(self):
        other = Restaurant.objects.create(
            name='Untouched', location='Elsewhere', status=RestaurantStatus_Live,
            owner=make_restaurant_user(),
        )
        create(
            self.admin, name='New One', location='Somewhere',
            owner=new_owner(phone_number='0700000034', email=None),
        )
        other.refresh_from_db()
        self.assertEqual(other.status, RestaurantStatus_Live)


# --- §29 J/K structural guards ------------------------------------------------

class ModuleStructureTests(TestCase):
    """
    STRUCTURAL, not behavioural, and deliberately so.

    A mock only proves the path was not taken on the one input the test supplied; an
    AST scan proves the module cannot take it at all. The retired
    ``admin_register_restaurant`` is exactly the architecture this PR exists not to
    rebuild, so the names that made it up are refused by NAME.
    """

    # Every ingredient of the retired flow, plus the two legacy side-channels.
    FORBIDDEN_NAMES = (
        'self_register', 'create_employee', 'admin_register_restaurant',
        'Notification', 'save_action', 'save_action_log',
        'OtpManager', 'make_otp', 'ensure_role_permissions', 'Secretary',
        # Password MATERIAL. `secrets` is imported for the claim token, so the
        # generators are named individually rather than banning the module.
        'random', 'get_random_string', 'make_password', 'set_password',
        'choices', 'choice',
    )

    def _names(self, module):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(module))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.update(node.module.split('.'))
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    def test_the_service_cannot_reach_the_retired_creation_architecture(self):
        names = self._names(onboarding_creation)
        for forbidden in self.FORBIDDEN_NAMES:
            self.assertNotIn(
                forbidden, names,
                f'{forbidden!r} appears in onboarding_creation — the retired '
                f'temporary-password / credential-delivery architecture must not '
                f'be rebuilt.',
            )

    def test_the_service_sets_an_unusable_password_explicitly(self):
        self.assertIn('set_unusable_password', self._names(onboarding_creation))

    def test_the_service_writes_no_audit_row(self):
        """
        The audit is the ADAPTER's, so the service must not import the recorder.
        Stated structurally so a "helpful" second audit row cannot appear here.
        """
        names = self._names(onboarding_creation)
        for forbidden in ('audit', 'record', 'AdminAuditLog'):
            self.assertNotIn(forbidden, names)

    def test_the_service_takes_no_admission_advisory_lock(self):
        """
        Deliberate: an order path cannot read a restaurant that does not exist yet,
        and taking the lock would invert the documented ``advisory -> Restaurant``
        order for no benefit.
        """
        names = self._names(onboarding_creation)
        for forbidden in ('lock_admission_exclusive', 'lock_admission_shared'):
            self.assertNotIn(forbidden, names)

    def test_the_service_never_deletes_anything(self):
        """Rollback is the database's job — never a compensating delete."""
        self.assertNotIn('delete', self._names(onboarding_creation))

    def test_the_endpoint_modules_never_write_a_model(self):
        from platform_admin_app.endpoints import restaurant_creation, restaurants

        for module in (restaurants, restaurant_creation):
            with self.subTest(module=module.__name__):
                names = self._names(module)
                for forbidden in ('save', 'update_or_create', 'get_or_create',
                                  'bulk_create', 'delete'):
                    self.assertNotIn(
                        forbidden, names,
                        f'{forbidden}() appears in {module.__name__}; every row must '
                        f'be written by onboarding_creation.',
                    )

    def test_the_endpoint_delegates_to_the_domain_service(self):
        import ast
        import inspect

        from platform_admin_app.endpoints import restaurants

        called = {
            node.func.attr
            for node in ast.walk(ast.parse(inspect.getsource(restaurants)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn('create_admin_restaurant', called)
