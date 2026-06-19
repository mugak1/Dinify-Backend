"""
Tests for reports_app — tenant isolation on RestaurantReportsEndpoint.

Every restaurant report is single-target (scoped by the client ``?restaurant=``).
``RestaurantReportsEndpoint.get`` authorizes that one restaurant via
``can_read_restaurant`` before dispatching, returning 404 (not 403) on a
cross-tenant / non-member / missing-id read so a restaurant's existence is never
confirmed to an outsider. A dinify admin is unrestricted; unauthenticated callers
are stopped by the global IsAuthenticated default (401). The guard is
report-name agnostic, so a single report name (``sales-summary``) exercises it.
"""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RESTAURANT_OWNER, RESTAURANT_MANAGER, DINIFY_ADMIN,
)

# A valid date range — sales-summary has no day cap, and an empty result set
# still returns 200, so the authorized path needs no order fixtures.
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
        # Restaurant A: an owner and a manager (both READ_ROLES).
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

    def get_report(self, user=None, restaurant=None, name='sales-summary'):
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
