"""
Tests for reports_app — tenant isolation on RestaurantReportsEndpoint.

Every restaurant report is single-target (scoped by the client ``?restaurant=``).
``RestaurantReportsEndpoint.get`` authorizes that one restaurant via
``can_user_access_module(.., MODULE_REPORTS)`` before dispatching, returning 404
(not 403) on a cross-tenant / non-member / missing-id read so a restaurant's
existence is never confirmed to an outsider. A dinify admin is unrestricted;
unauthenticated callers are stopped by the global IsAuthenticated default (401).
The guard is report-name agnostic, so a single report name (``sales-listing``)
exercises it. Owner/manager hold the ``reports`` module by default (these tests);
roles without it are covered in users_app/tests_permission_enforcement.py.
"""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RESTAURANT_OWNER, RESTAURANT_MANAGER, DINIFY_ADMIN,
)

# A 30-day range — deliberately inside sales-listing's 31-day cap — and an empty
# result set still returns 200, so the authorized path needs no order fixtures.
DATE_QS = 'from=2024-01-01&to=2024-01-31'


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


class ReportsTenantScopeTests(TestCase):
    def setUp(self):
        # Restaurant A: an owner and a manager (both hold the reports module).
        self.owner_a = make_user('256700000210')
        self.restaurant_a = Restaurant.objects.create(
            name='Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_OWNER],
        )
        self.manager_a = make_user('256700000211')
        RestaurantEmployee.objects.create(
            user=self.manager_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_MANAGER],
        )

        # Restaurant B: a separate owner — the cross-tenant target.
        self.owner_b = make_user('256700000220')
        self.restaurant_b = Restaurant.objects.create(
            name='Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )

        # Authenticated but employed nowhere (e.g. a diner with a valid JWT).
        self.outsider = make_user('256700000230')
        # Dinify admin — unrestricted reads.
        self.admin = make_user('256700000240', roles=[DINIFY_ADMIN])

    # --- request helpers ------------------------------------------------
    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get_report(self, user=None, restaurant=None, name='sales-listing'):
        url = f'/api/v1/reports/restaurant/{name}/'
        qs = f'restaurant={restaurant}&{DATE_QS}' if restaurant is not None else DATE_QS
        headers = self.auth(user) if user is not None else {}
        return self.client.get(f'{url}?{qs}', **headers)

    # --- allowed --------------------------------------------------------
    def test_owner_reads_own_restaurant(self):
        resp = self.get_report(self.owner_a, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_manager_reads_own_restaurant(self):
        resp = self.get_report(self.manager_a, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_admin_reads_any_restaurant(self):
        resp = self.get_report(self.admin, self.restaurant_b.id)
        self.assertEqual(resp.status_code, 200, resp.content)

    # --- denied (404, fail closed, existence not confirmed) -------------
    def test_owner_cannot_read_other_restaurant(self):
        resp = self.get_report(self.owner_a, self.restaurant_b.id)
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_non_member_denied(self):
        resp = self.get_report(self.outsider, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_missing_restaurant_param_denied_for_non_admin(self):
        resp = self.get_report(self.owner_a, restaurant=None)
        self.assertEqual(resp.status_code, 404, resp.content)

    # --- auth boundary --------------------------------------------------
    def test_unauthenticated_denied(self):
        resp = self.get_report(user=None, restaurant=self.restaurant_a.id)
        self.assertEqual(resp.status_code, 401, resp.content)

    # --- guard precedence -----------------------------------------------
    def test_guard_precedes_invalid_report_name(self):
        # Unauthorized caller + invalid report name → 404 (guard first), not 400,
        # so report-name validity isn't leaked to an outsider.
        resp = self.get_report(self.owner_a, self.restaurant_b.id, name='not-a-report')
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_authorized_invalid_report_name_is_400(self):
        # Authorized caller + invalid report name → the normal 400 branch.
        resp = self.get_report(self.owner_a, self.restaurant_a.id, name='not-a-report')
        self.assertEqual(resp.status_code, 400, resp.content)


class DinifyReportsAdminGateTests(TestCase):
    """
    Platform-admin reports (``/api/v1/reports/dinify/<name>/``) must be
    dinify-admin-only. ``DinifyReportsEndpoint`` inherited only the global
    IsAuthenticated default, so any authenticated principal — a self-registered
    diner, or a real restaurant owner — could read cross-tenant revenue, owner
    PII (restaurant-listing) and the entire transaction ledger. The gate denies
    non-admins with 404 (existence non-disclosure), mirroring
    RestaurantReportsEndpoint, and fires before the invalid-name 400 branch so
    report-name validity is never leaked. The three controllers are unchanged.
    """

    SLUGS = ('dashboard', 'restaurant-listing', 'transactions-listing')

    def setUp(self):
        # A real restaurant with a real owner — a genuine tenant principal that
        # nonetheless holds no dinify-admin role. The seeded restaurant also
        # backs the restaurant-listing row assertion below.
        self.owner = make_user('256700000251')
        self.restaurant = Restaurant.objects.create(
            name='Gate Restaurant', location='loc-gate',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        # Role-less authenticated diner (valid JWT, no roles, employed nowhere).
        self.diner = make_user('256700000252')
        # Dinify admin — the only principal allowed to read platform reports.
        self.admin = make_user('256700000250', roles=[DINIFY_ADMIN])

    # --- request helpers ------------------------------------------------
    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get_dinify(self, user, name):
        headers = self.auth(user) if user is not None else {}
        return self.client.get(f'/api/v1/reports/dinify/{name}/', **headers)

    # --- denied: role-less diner (404 on all three slugs) ---------------
    def test_roleless_diner_denied_on_all_slugs(self):
        for name in self.SLUGS:
            resp = self.get_dinify(self.diner, name)
            self.assertEqual(resp.status_code, 404, f'{name}: {resp.content}')

    # --- denied: non-admin owner (404 on all three slugs) ---------------
    def test_non_admin_owner_denied_on_all_slugs(self):
        for name in self.SLUGS:
            resp = self.get_dinify(self.owner, name)
            self.assertEqual(resp.status_code, 404, f'{name}: {resp.content}')

    # --- allowed: dinify admin (200 on all three slugs) -----------------
    def test_admin_allowed_on_all_slugs(self):
        for name in self.SLUGS:
            resp = self.get_dinify(self.admin, name)
            self.assertEqual(resp.status_code, 200, f'{name}: {resp.content}')

    def test_admin_restaurant_listing_returns_seeded_row(self):
        resp = self.get_dinify(self.admin, 'restaurant-listing')
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [row['id'] for row in resp.json()['data']]
        self.assertIn(str(self.restaurant.id), ids)

    # --- non-disclosure: gate answers before the invalid-name 400 -------
    def test_non_admin_unknown_and_valid_name_both_404(self):
        # For a non-admin, an unknown report name and a valid one are BOTH 404 —
        # the gate (not the report-name branch) answers, so report validity is
        # never leaked (an authorized invalid name would be 400).
        unknown = self.get_dinify(self.diner, 'not-a-report')
        valid = self.get_dinify(self.diner, 'dashboard')
        self.assertEqual(unknown.status_code, 404, unknown.content)
        self.assertEqual(valid.status_code, 404, valid.content)
