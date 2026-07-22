"""
Tests for the platform-admin identity layer (PR-1):

* `PlatformStaffAuth` model defaults,
* the invariant services (`promote_to_platform_staff` / `guard_membership_creation`)
  exercised in BOTH directions and THROUGH the wired membership write sites,
* the Fernet crypto helper (round-trip + fail-closed),
* exposure guards (`account_type` never writable on a User serializer; the
  `PlatformStaffAuth` secret columns never serialized; `account_type` absent from
  EDIT_INFORMATION),
* dual-role tolerance (a pre-existing platform_staff-with-active-membership row
  does not break editing that membership).

The migration flip is covered separately in `tests_migration_flip.py`.
"""
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
)
from dinify_backend.tenancy.discovery import all_project_serializers
from platform_admin_app import crypto
from platform_admin_app.models import PlatformStaffAuth
from platform_admin_app.services import (
    PlatformStaffInvariantError,
    guard_membership_creation,
    promote_to_platform_staff,
)
from restaurants_app.controllers.employees.create_employee import (
    create_employee_from_existing_user,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from restaurants_app.serializers import SerializerPutRestaurantEmployee
from users_app.models import User

_PHONE = iter(f'2567010000{n:02d}' for n in range(1, 99))


def _make_user(email, roles=None, account_type=ACCOUNT_TYPE_RESTAURANT_USER):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='T', last_name=phone[-3:], email=email,
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=roles or [], account_type=account_type,
    )


class PlatformStaffAuthModelTests(TestCase):
    def test_defaults_and_related_name(self):
        user = _make_user('psa_model@test.com')
        auth = PlatformStaffAuth.objects.create(user=user)
        self.assertIsNone(auth.totp_secret_encrypted)
        self.assertIsNone(auth.totp_enrolled_at)
        self.assertEqual(auth.recovery_code_hashes, [])
        self.assertIsNone(auth.recovery_generated_at)
        self.assertEqual(auth.failed_attempts, 0)
        self.assertIsNone(auth.locked_until)
        # OneToOne related_name resolves back from the user.
        self.assertEqual(user.platform_auth, auth)


class PromoteToPlatformStaffTests(TestCase):
    def setUp(self):
        self.owner = _make_user('promote_owner@test.com')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc', status='active', owner=self.owner,
        )

    def test_promote_succeeds_without_membership(self):
        user = _make_user('promote_ok@test.com')
        auth = promote_to_platform_staff(user)
        user.refresh_from_db()
        self.assertEqual(user.account_type, ACCOUNT_TYPE_PLATFORM_STAFF)
        self.assertIsInstance(auth, PlatformStaffAuth)
        self.assertTrue(PlatformStaffAuth.objects.filter(user=user).exists())

    def test_promote_is_idempotent(self):
        user = _make_user('promote_twice@test.com')
        promote_to_platform_staff(user)
        promote_to_platform_staff(user)  # get_or_create — no duplicate
        self.assertEqual(PlatformStaffAuth.objects.filter(user=user).count(), 1)

    def test_promote_refused_with_active_membership(self):
        user = _make_user('promote_blocked@test.com')
        RestaurantEmployee.objects.create(
            user=user, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True,
        )
        with self.assertRaises(PlatformStaffInvariantError):
            promote_to_platform_staff(user)
        user.refresh_from_db()
        self.assertEqual(user.account_type, ACCOUNT_TYPE_RESTAURANT_USER)
        self.assertFalse(PlatformStaffAuth.objects.filter(user=user).exists())

    def test_promote_ignores_inactive_and_deleted_memberships(self):
        # Neither an inactive nor a soft-deleted membership counts as "active".
        user = _make_user('promote_inactive@test.com')
        RestaurantEmployee.objects.create(
            user=user, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=False,
        )
        r2 = Restaurant.objects.create(
            name='R2', location='loc2', status='active', owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=user, restaurant=r2, roles=[RESTAURANT_OWNER],
            active=True, deleted=True,
        )
        promote_to_platform_staff(user)  # must not raise
        user.refresh_from_db()
        self.assertEqual(user.account_type, ACCOUNT_TYPE_PLATFORM_STAFF)


class MembershipGuardTests(TestCase):
    """The invariant exercised THROUGH each wired site, not just the service."""

    def setUp(self):
        self.owner = _make_user('guard_owner@test.com')
        self.restaurant = Restaurant.objects.create(
            name='RG', location='loc', status='active', owner=self.owner,
        )
        self.staff = _make_user(
            'guard_staff@test.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.normal = _make_user('guard_normal@test.com')

    # --- service in isolation ------------------------------------------------
    def test_service_noop_for_restaurant_user(self):
        guard_membership_creation(self.normal)  # no raise

    def test_service_raises_for_platform_staff(self):
        with self.assertRaises(PlatformStaffInvariantError):
            guard_membership_creation(self.staff)

    # --- A2/A6: create via the serializer choke point ------------------------
    def test_serializer_create_refused_for_platform_staff(self):
        ser = SerializerPutRestaurantEmployee(
            data={'user': str(self.staff.id), 'roles': [RESTAURANT_OWNER], 'active': True}
        )
        self.assertFalse(ser.is_valid())
        self.assertIn('platform-staff', str(ser.errors).lower())

    def test_serializer_create_allowed_for_restaurant_user(self):
        ser = SerializerPutRestaurantEmployee(
            data={'user': str(self.normal.id), 'roles': [RESTAURANT_OWNER], 'active': True}
        )
        self.assertTrue(ser.is_valid(), ser.errors)

    # --- A7: reactivation via the serializer update path ---------------------
    def test_serializer_update_reactivation_refused_for_platform_staff(self):
        membership = RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=False,
        )
        ser = SerializerPutRestaurantEmployee(
            instance=membership, data={'active': True}, partial=True,
        )
        self.assertFalse(ser.is_valid())
        self.assertIn('platform-staff', str(ser.errors).lower())

    # --- A3: reactivation via the direct-save controller ---------------------
    def test_reactivation_controller_refused_for_platform_staff(self):
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=False, deleted=True,
        )
        with self.assertRaises(PlatformStaffInvariantError):
            create_employee_from_existing_user(
                user_id=str(self.staff.id), restaurant_id=str(self.restaurant.id),
                roles=[RESTAURANT_OWNER],
            )

    def test_reactivation_controller_allowed_for_restaurant_user(self):
        membership = RestaurantEmployee.objects.create(
            user=self.normal, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=False, deleted=True,
        )
        result = create_employee_from_existing_user(
            user_id=str(self.normal.id), restaurant_id=str(self.restaurant.id),
            roles=[RESTAURANT_OWNER],
        )
        self.assertEqual(result['status'], 200)
        membership.refresh_from_db()
        self.assertTrue(membership.active)
        self.assertFalse(membership.deleted)


class DualRoleToleranceTests(TestCase):
    """A pre-existing platform_staff-with-active-membership row is tolerated:
    editing that already-active membership (not a reactivation) is NOT refused."""

    def test_editing_active_membership_of_dual_role_user_is_allowed(self):
        owner = _make_user('dual_owner@test.com')
        restaurant = Restaurant.objects.create(
            name='RD', location='loc', status='active', owner=owner,
        )
        staff = _make_user(
            'dual_staff@test.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        # Dual-role fixture: an ACTIVE membership created out-of-band (bypassing
        # the serializer guard), mirroring a pre-flip standing row.
        membership = RestaurantEmployee.objects.create(
            user=staff, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
        )
        # Editing roles while it stays active is not a create/reactivation.
        ser = SerializerPutRestaurantEmployee(
            instance=membership, data={'roles': ['manager']}, partial=True,
        )
        self.assertTrue(ser.is_valid(), ser.errors)


class CryptoHelperTests(TestCase):
    def test_round_trip_with_valid_key(self):
        key = Fernet.generate_key().decode()
        with patch('platform_admin_app.crypto.config', return_value=key):
            token = crypto.encrypt_secret('super-secret-totp')
            self.assertNotEqual(token, 'super-secret-totp')
            self.assertEqual(crypto.decrypt_secret(token), 'super-secret-totp')

    def test_fail_closed_when_key_missing(self):
        with patch('platform_admin_app.crypto.config', return_value=None):
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt_secret('x')

    def test_fail_closed_when_key_invalid(self):
        with patch('platform_admin_app.crypto.config', return_value='not-a-fernet-key'):
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt_secret('x')


class ExposureGuardTests(TestCase):
    def test_no_user_serializer_exposes_account_type_writably(self):
        user_serializers = [
            cls for cls in all_project_serializers()
            if getattr(getattr(cls, 'Meta', None), 'model', None) is User
        ]
        self.assertTrue(
            user_serializers,
            'No project ModelSerializer with Meta.model=User was discovered — '
            'discovery may be broken; the assertion would be vacuous.',
        )
        for cls in user_serializers:
            field = cls().fields.get('account_type')
            self.assertTrue(
                field is None or field.read_only,
                f"{cls.__module__}.{cls.__qualname__} exposes a writable "
                "'account_type' field (must be absent or read_only).",
            )

    def test_no_serializer_exposes_platform_staff_secrets(self):
        secret_fields = {'totp_secret_encrypted', 'recovery_code_hashes'}
        for cls in all_project_serializers():
            exposed = secret_fields.intersection(cls().fields.keys())
            self.assertEqual(
                exposed, set(),
                f"{cls.__module__}.{cls.__qualname__} exposes PlatformStaffAuth "
                f"secret field(s): {exposed}.",
            )

    def test_account_type_absent_from_edit_information(self):
        from dinify_backend.configss.edit_information import (
            EDIT_INFORMATION, EI_DINING_AREA, EI_RESTAURANT_TAG, EI_SECTION_GROUP,
        )
        entries = []
        for section in EDIT_INFORMATION.values():
            entries.extend(section)
        for extra in (EI_SECTION_GROUP, EI_RESTAURANT_TAG, EI_DINING_AREA):
            entries.extend(extra)
        keys = {entry.get('key') for entry in entries}
        self.assertNotIn('account_type', keys)
