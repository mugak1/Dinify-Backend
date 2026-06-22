"""
Tests for the owner-only role-permissions management endpoint
(GET/PUT /api/v1/restaurant-setup/role-permissions/) and for the create-employee
temp-password surfacing (PR D).
"""
import uuid

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configs import ROLES
from dinify_backend.configss.string_definitions import (
    GRID_MODULES, RestaurantStatus_Active,
)
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, RestaurantRolePermission,
)
from users_app.models import User


OWNER = ROLES.get('RESTAURANT_OWNER')        # 'owner'
MANAGER = ROLES.get('RESTAURANT_MANAGER')    # 'manager'
KITCHEN = ROLES.get('RESTAURANT_KITCHEN')    # 'kitchen'
STAFF = ROLES.get('RESTAURANT_STAFF')        # 'restaurant_staff'

URL = '/api/v1/restaurant-setup/role-permissions/'


def _make_user(phone, email, roles=None):
    return User.objects.create_user(
        first_name='T', last_name=phone[-3:],
        email=email, phone_number=phone, username=phone,
        country='Uganda', password='password', roles=roles or [],
    )


class RolePermissionsEndpointTests(TestCase):
    """CRUD + validation + tenant-isolation for the role-permissions endpoint."""

    def setUp(self):
        # Restaurant A with an owner + a manager / kitchen / staff employee.
        self.owner_a = _make_user('256701000001', 'rp_owner_a@test.com')
        self.restaurant_a = Restaurant.objects.create(
            name='RP Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a, roles=[OWNER],
        )
        self.manager_a = _make_user('256701000002', 'rp_manager_a@test.com')
        RestaurantEmployee.objects.create(
            user=self.manager_a, restaurant=self.restaurant_a, roles=[MANAGER],
        )
        self.kitchen_a = _make_user('256701000003', 'rp_kitchen_a@test.com')
        RestaurantEmployee.objects.create(
            user=self.kitchen_a, restaurant=self.restaurant_a, roles=[KITCHEN],
        )
        self.staff_a = _make_user('256701000004', 'rp_staff_a@test.com')
        RestaurantEmployee.objects.create(
            user=self.staff_a, restaurant=self.restaurant_a, roles=[STAFF],
        )

        # Restaurant B with its own owner (cross-tenant attacker target).
        self.owner_b = _make_user('256701000005', 'rp_owner_b@test.com')
        self.restaurant_b = Restaurant.objects.create(
            name='RP Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b, roles=[OWNER],
        )

    # -- helpers -------------------------------------------------------------

    def _token(self, user):
        return str(RefreshToken.for_user(user).access_token)

    def _get(self, user, restaurant_id):
        return self.client.get(
            f'{URL}?restaurant={restaurant_id}',
            HTTP_AUTHORIZATION=f'Bearer {self._token(user)}',
        )

    def _put(self, user, body):
        return self.client.put(
            URL, data=body, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token(user)}',
        )

    def _persisted(self, role):
        return RestaurantRolePermission.objects.get(
            restaurant=self.restaurant_a, role=role,
        ).modules

    # -- GET -----------------------------------------------------------------

    def test_get_returns_four_roles_in_canonical_order(self):
        response = self._get(self.owner_a, self.restaurant_a.id)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(
            [row['role'] for row in data],
            [OWNER, MANAGER, KITCHEN, STAFF],
        )
        by_role = {row['role']: row for row in data}

        # Owner: all-True, advisory editable=False.
        self.assertFalse(by_role[OWNER]['editable'])
        self.assertTrue(all(by_role[OWNER]['modules'][m] for m in GRID_MODULES))

        # The other three: editable=True, coded defaults.
        for role in (MANAGER, KITCHEN, STAFF):
            self.assertTrue(by_role[role]['editable'])
        self.assertTrue(all(by_role[MANAGER]['modules'][m] for m in GRID_MODULES))
        self.assertTrue(by_role[KITCHEN]['modules']['kitchen'])
        self.assertFalse(by_role[KITCHEN]['modules']['menu'])
        self.assertTrue(by_role[STAFF]['modules']['tables'])
        self.assertFalse(by_role[STAFF]['modules']['kitchen'])

    def test_get_reflects_persisted_override_merged_over_default(self):
        # Seed a partial kitchen override that adds `tables` on top of the
        # kitchen default (kitchen=True, everything else False).
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant_a, role=KITCHEN,
            modules={'tables': True},
        )
        data = self._get(self.owner_a, self.restaurant_a.id).json()['data']
        kitchen = {row['role']: row for row in data}[KITCHEN]['modules']
        self.assertTrue(kitchen['tables'])    # from the override
        self.assertTrue(kitchen['kitchen'])   # preserved from the default
        self.assertFalse(kitchen['menu'])     # still default-off

    # -- PUT validation ------------------------------------------------------

    def test_put_rejects_owner_role(self):
        response = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': OWNER,
            'modules': {'reports': False},
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('not editable', response.json()['message'])
        self.assertFalse(RestaurantRolePermission.objects.filter(
            restaurant=self.restaurant_a, role=OWNER,
        ).exists())

    def test_put_rejects_unknown_role(self):
        response = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': 'chef',
            'modules': {'reports': False},
        })
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_unknown_module_key(self):
        response = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'reports': True, 'made_up': True},
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('made_up', response.json()['message'])

    def test_put_rejects_billing_and_team_keys(self):
        for key in ('billing', 'team', 'support'):
            response = self._put(self.owner_a, {
                'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
                'modules': {key: True},
            })
            self.assertEqual(response.status_code, 400, key)
            self.assertIn(key, response.json()['message'])

    def test_put_rejects_non_boolean_value(self):
        response = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'reports': 'true'},
        })
        self.assertEqual(response.status_code, 400)

    # -- PUT persistence / merge ---------------------------------------------

    def test_put_creates_then_updates_same_row(self):
        first = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'reports': False},
        })
        self.assertEqual(first.status_code, 200)
        second = self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'reports': True},
        })
        self.assertEqual(second.status_code, 200)
        # Idempotent on the (restaurant, role) pair — never a duplicate.
        self.assertEqual(
            RestaurantRolePermission.objects.filter(
                restaurant=self.restaurant_a, role=MANAGER,
            ).count(),
            1,
        )
        self.assertTrue(self._persisted(MANAGER)['reports'])

    def test_partial_put_preserves_prior_customization(self):
        # Full PUT establishes a non-default grid (reports + settings OFF).
        full = {m: True for m in GRID_MODULES}
        full['reports'] = False
        full['settings'] = False
        self.assertEqual(self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': full,
        }).status_code, 200)

        # Partial PUT touches ONLY dashboard.
        self.assertEqual(self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'dashboard': False},
        }).status_code, 200)

        persisted = self._persisted(MANAGER)
        self.assertFalse(persisted['dashboard'])          # from the partial PUT
        self.assertFalse(persisted['reports'])            # preserved, not reset to default
        self.assertFalse(persisted['settings'])           # preserved, not reset to default
        for m in ('kitchen', 'tables', 'menu', 'reviews'):
            self.assertTrue(persisted[m])

    def test_boolean_round_trip_preserves_type(self):
        self.assertEqual(self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
            'modules': {'reports': False, 'settings': True},
        }).status_code, 200)
        data = self._get(self.owner_a, self.restaurant_a.id).json()['data']
        manager = {row['role']: row for row in data}[MANAGER]['modules']
        # Every value must come back as a genuine JSON boolean, not "true"/1.
        for value in manager.values():
            self.assertIn(value, (True, False))
            self.assertIsInstance(value, bool)
        self.assertIs(manager['reports'], False)
        self.assertIs(manager['settings'], True)

    # -- access control ------------------------------------------------------

    def test_non_owner_denied_get_and_put(self):
        for user in (self.manager_a, self.kitchen_a, self.staff_a):
            self.assertEqual(
                self._get(user, self.restaurant_a.id).status_code, 403,
            )
            self.assertEqual(self._put(user, {
                'restaurant': str(self.restaurant_a.id), 'role': KITCHEN,
                'modules': {'menu': True},
            }).status_code, 403)

    def test_cross_tenant_owner_denied(self):
        # owner_a has no role at restaurant B.
        self.assertEqual(self._get(self.owner_a, self.restaurant_b.id).status_code, 403)
        self.assertEqual(self._put(self.owner_a, {
            'restaurant': str(self.restaurant_b.id), 'role': MANAGER,
            'modules': {'menu': False},
        }).status_code, 403)

    def test_missing_restaurant_param_400_and_nonexistent_404(self):
        # Absent query param -> 400.
        self.assertEqual(
            self.client.get(
                URL, HTTP_AUTHORIZATION=f'Bearer {self._token(self.owner_a)}',
            ).status_code,
            400,
        )
        # Well-formed but nonexistent id -> 404 (before the gate).
        self.assertEqual(self._get(self.owner_a, uuid.uuid4()).status_code, 404)
        self.assertEqual(self._put(self.owner_a, {
            'restaurant': str(uuid.uuid4()), 'role': MANAGER,
            'modules': {'menu': True},
        }).status_code, 404)

    def test_unauthenticated_401(self):
        self.assertEqual(
            self.client.get(f'{URL}?restaurant={self.restaurant_a.id}').status_code,
            401,
        )
        self.assertEqual(
            self.client.put(
                URL, data={'restaurant': str(self.restaurant_a.id), 'role': MANAGER,
                           'modules': {'menu': True}},
                content_type='application/json',
            ).status_code,
            401,
        )


class CreateEmployeeTempPasswordTests(TestCase):
    """The create-employee response surfaces a working one-time temp password."""

    def setUp(self):
        self.owner = _make_user('256701000020', 'ce_owner@test.com')
        self.restaurant = Restaurant.objects.create(
            name='CE Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[OWNER],
        )

    def test_create_employee_response_contains_working_temp_password(self):
        token = str(RefreshToken.for_user(self.owner).access_token)
        response = self.client.post(
            '/api/v1/restaurant-setup/create-employee/',
            data={
                'first_name': 'New', 'last_name': 'Hire',
                'email': 'ce_newhire@test.com', 'phone_number': '256701000021',
                'restaurant': str(self.restaurant.id), 'roles': [KITCHEN],
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200)
        temp_password = response.json()['data'].get('temp_password')
        self.assertTrue(temp_password)

        # Proves it is the REAL credential, not just a present field.
        new_user = User.objects.get(email='ce_newhire@test.com')
        self.assertTrue(new_user.check_password(temp_password))
