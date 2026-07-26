"""
Tests for the role-based permission RESOLUTION layer (data + resolver + payload).

This layer is purely additive — it applies NO enforcement. Covered surface:
  * resolve_module_permissions falls back to coded defaults when no override
    row exists, an override row supersedes the default, multi-role unions to the
    most permissive, and the owner short-circuit grants billing/team.
  * can_user_access_module treats ``support`` as ungated.
  * the resolved ``permissions`` map is attached identically by BOTH payload
    build points (get_any_restaurant_roles + SerGetUserProfile).
  * ensure_role_permissions seeds the four default rows idempotently.
"""
from django.test import TestCase

from platform_admin_app.testing import give_legacy_platform_role
from users_app.models import User
from users_app.serializers import SerGetUserProfile
from users_app.controllers.permissions_check import (
    resolve_module_permissions,
    can_user_access_module,
    get_any_restaurant_roles,
)
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, RestaurantRolePermission,
)
from restaurants_app.controllers.role_permissions import ensure_role_permissions
from restaurants_app.configs.role_defaults import DEFAULT_ROLE_MODULES
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_KITCHEN, RESTAURANT_STAFF,
    GRID_MODULES, MODULE_BILLING, MODULE_TEAM, MODULE_SUPPORT,
    MODULE_KITCHEN, MODULE_TABLES, MODULE_MENU, MODULE_DASHBOARD,
)


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


# The stable 9-key shape every resolved permissions map carries.
ALL_KEYS = set(GRID_MODULES) | {MODULE_BILLING, MODULE_TEAM}


class RolePermissionResolverTests(TestCase):
    def setUp(self):
        self.owner = make_user('256700000001')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.manager = make_user('256700000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.chef = make_user('256700000003')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])
        self.staff = make_user('256700000004')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        # multi-role user (chef + staff)
        self.multi = make_user('256700000005')
        RestaurantEmployee.objects.create(
            user=self.multi, restaurant=self.restaurant,
            roles=[RESTAURANT_KITCHEN, RESTAURANT_STAFF])
        # An account carrying the RETIRED platform role, employed nowhere. It used
        # to resolve to the full-access map at every restaurant.
        self.legacy_role_holder = give_legacy_platform_role(
            make_user('256700000006'))

    # --- defaults (no override rows seeded) -----------------------------
    def test_chef_default_kitchen_only(self):
        perms = resolve_module_permissions(self.chef, self.restaurant.id)
        self.assertEqual(set(perms.keys()), ALL_KEYS)  # stable 9-key shape
        self.assertTrue(perms[MODULE_KITCHEN])
        for key in ALL_KEYS - {MODULE_KITCHEN}:
            self.assertFalse(perms[key], key)

    def test_staff_default_tables_only(self):
        perms = resolve_module_permissions(self.staff, self.restaurant.id)
        self.assertTrue(perms[MODULE_TABLES])
        for key in ALL_KEYS - {MODULE_TABLES}:
            self.assertFalse(perms[key], key)

    def test_manager_default_all_grid_no_billing_team(self):
        perms = resolve_module_permissions(self.manager, self.restaurant.id)
        for module in GRID_MODULES:
            self.assertTrue(perms[module], module)
        # manager is NOT owner/admin -> billing/team stay False
        self.assertFalse(perms[MODULE_BILLING])
        self.assertFalse(perms[MODULE_TEAM])

    # --- override supersedes default ------------------------------------
    def test_override_supersedes_default(self):
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant, role=RESTAURANT_KITCHEN,
            modules={MODULE_KITCHEN: False, MODULE_MENU: True},
        )
        perms = resolve_module_permissions(self.chef, self.restaurant.id)
        self.assertTrue(perms[MODULE_MENU])      # granted by override
        self.assertFalse(perms[MODULE_KITCHEN])  # revoked by override

    # --- multi-role union -----------------------------------------------
    def test_multi_role_union_most_permissive(self):
        perms = resolve_module_permissions(self.multi, self.restaurant.id)
        self.assertTrue(perms[MODULE_KITCHEN])   # from chef
        self.assertTrue(perms[MODULE_TABLES])    # from staff
        self.assertFalse(perms[MODULE_MENU])     # neither grants menu

    # --- owner short-circuit ---------------------------------------------
    def test_owner_short_circuit_grants_billing_team(self):
        perms = resolve_module_permissions(self.owner, self.restaurant.id)
        for key in ALL_KEYS:
            self.assertTrue(perms[key], key)

    def test_owner_short_circuit_ignores_restrictive_override(self):
        # a locked-down override row cannot reduce an owner
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant, role=RESTAURANT_OWNER,
            modules={module: False for module in GRID_MODULES},
        )
        perms = resolve_module_permissions(self.owner, self.restaurant.id)
        for key in ALL_KEYS:
            self.assertTrue(perms[key], key)

    def test_legacy_platform_role_resolves_to_no_access(self):
        # Was `test_admin_full_access`: the role string short-circuited straight to
        # `_full_access_map()` — every grid module PLUS billing and team — at any
        # restaurant. It now resolves like the stranger it is: the stable key shape,
        # all False.
        perms = resolve_module_permissions(self.legacy_role_holder, self.restaurant.id)
        self.assertEqual(set(perms.keys()), ALL_KEYS)
        for key in ALL_KEYS:
            self.assertFalse(perms[key], key)

    def test_legacy_platform_role_matches_an_unemployed_stranger(self):
        stranger = make_user('256700000007')
        self.assertEqual(
            resolve_module_permissions(self.legacy_role_holder, self.restaurant.id),
            resolve_module_permissions(stranger, self.restaurant.id),
        )

    # --- can_user_access_module -----------------------------------------
    def test_support_always_allowed(self):
        nobody = make_user('256700000099')
        self.assertTrue(
            can_user_access_module(nobody, self.restaurant.id, MODULE_SUPPORT))

    def test_can_user_access_module_reflects_resolution(self):
        self.assertTrue(
            can_user_access_module(self.chef, self.restaurant.id, MODULE_KITCHEN))
        self.assertFalse(
            can_user_access_module(self.chef, self.restaurant.id, MODULE_MENU))
        self.assertFalse(
            can_user_access_module(self.chef, self.restaurant.id, MODULE_BILLING))

    def test_non_member_denied(self):
        nobody = make_user('256700000098')
        perms = resolve_module_permissions(nobody, self.restaurant.id)
        for key in ALL_KEYS:
            self.assertFalse(perms[key], key)


class RolePermissionPayloadTests(TestCase):
    def setUp(self):
        self.owner = make_user('256700000100')
        self.restaurant = Restaurant.objects.create(
            name='Payload Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.chef = make_user('256700000101')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])

    def test_get_any_restaurant_roles_includes_permissions(self):
        payload = get_any_restaurant_roles(self.chef)
        self.assertEqual(len(payload), 1)
        entry = payload[0]
        self.assertIn('permissions', entry)
        self.assertEqual(set(entry['permissions'].keys()), ALL_KEYS)
        self.assertTrue(entry['permissions'][MODULE_KITCHEN])
        self.assertFalse(entry['permissions'][MODULE_DASHBOARD])

    def test_get_any_restaurant_roles_excludes_inactive_employment(self):
        # A deactivated (active=False, deleted=False) employment must NOT load
        # the portal — matching get_employed_restaurant_ids / module resolution.
        inactive = make_user('256700000102')
        RestaurantEmployee.objects.create(
            user=inactive, restaurant=self.restaurant,
            roles=[RESTAURANT_KITCHEN], active=False)
        self.assertEqual(get_any_restaurant_roles(inactive), [])
        # The active chef from setUp is still included.
        self.assertEqual(len(get_any_restaurant_roles(self.chef)), 1)

    def test_serializer_no_context_matches_canonical_builder(self):
        # the no-context profile-fetch path must equal get_any_restaurant_roles
        serialized = SerGetUserProfile(self.chef).data
        expected = get_any_restaurant_roles(self.chef)
        self.assertEqual(serialized['restaurant_roles'], expected)
        self.assertIn('permissions', serialized['restaurant_roles'][0])

    def test_serializer_context_short_circuit_returned_verbatim(self):
        sentinel = [{
            'restaurant_id': 'x', 'restaurant': 'y',
            'roles': [], 'permissions': {},
        }]
        serialized = SerGetUserProfile(
            self.chef, context={'restaurant_roles': sentinel}).data
        self.assertEqual(serialized['restaurant_roles'], sentinel)


class EnsureRolePermissionsTests(TestCase):
    def setUp(self):
        self.owner = make_user('256700000200')
        self.restaurant = Restaurant.objects.create(
            name='Seed Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )

    def test_seeds_four_rows(self):
        ensure_role_permissions(self.restaurant)
        rows = RestaurantRolePermission.objects.filter(restaurant=self.restaurant)
        self.assertEqual(rows.count(), 4)
        self.assertEqual(
            {row.role for row in rows}, set(DEFAULT_ROLE_MODULES.keys()))

    def test_idempotent(self):
        ensure_role_permissions(self.restaurant)
        ensure_role_permissions(self.restaurant)  # second call -> no dupes
        self.assertEqual(
            RestaurantRolePermission.objects.filter(
                restaurant=self.restaurant).count(),
            4,
        )

    def test_accepts_restaurant_id(self):
        ensure_role_permissions(self.restaurant.id)
        self.assertEqual(
            RestaurantRolePermission.objects.filter(
                restaurant=self.restaurant).count(),
            4,
        )

    def test_seeded_grids_match_defaults(self):
        ensure_role_permissions(self.restaurant)
        for role, modules in DEFAULT_ROLE_MODULES.items():
            row = RestaurantRolePermission.objects.get(
                restaurant=self.restaurant, role=role)
            self.assertEqual(row.modules, modules)
