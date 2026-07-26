"""
Tests for reports_app — tenant isolation on RestaurantReportsEndpoint.

Every restaurant report is single-target (scoped by the client ``?restaurant=``).
``RestaurantReportsEndpoint.get`` authorizes that one restaurant via
``can_user_access_module(.., MODULE_REPORTS)`` before dispatching, returning 404
(not 403) on a cross-tenant / non-member / missing-id read so a restaurant's
existence is never confirmed to an outsider. There is no unrestricted principal —
the dinify-admin bypass is gone, and so is the platform reports surface it served.
Unauthenticated callers are stopped by the global IsAuthenticated default (401).
The guard is report-name agnostic, so a single report name (``sales-listing``)
exercises it. Owner/manager hold the ``reports`` module by default (these tests);
roles without it are covered in users_app/tests_permission_enforcement.py.
"""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from platform_admin_app.testing import give_legacy_platform_role
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, RESTAURANT_OWNER, RESTAURANT_MANAGER,
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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )

        # Authenticated but employed nowhere (e.g. a diner with a valid JWT).
        self.outsider = make_user('256700000230')
        # An account carrying the RETIRED platform role string. It used to read
        # every restaurant's reports; it is now an outsider like any other.
        self.legacy_role_holder = give_legacy_platform_role(
            make_user('256700000240'))

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

    # --- denied (404, fail closed, existence not confirmed) -------------
    def test_legacy_platform_role_reads_nothing(self):
        # Was `test_admin_reads_any_restaurant`. A dinify_admin role string used to
        # make get_module_restaurant_ids return None ("do not scope"), which read
        # straight through to every tenant's revenue. It now grants nothing at all
        # — not restaurant B, and not restaurant A either.
        for restaurant in (self.restaurant_a.id, self.restaurant_b.id):
            resp = self.get_report(self.legacy_role_holder, restaurant)
            self.assertEqual(resp.status_code, 404, resp.content)

    def test_legacy_platform_role_is_indistinguishable_from_an_outsider(self):
        holder = self.get_report(self.legacy_role_holder, self.restaurant_a.id)
        outsider = self.get_report(self.outsider, self.restaurant_a.id)
        self.assertEqual(holder.status_code, outsider.status_code)
        self.assertEqual(holder.content, outsider.content)

    def test_owner_cannot_read_other_restaurant(self):
        resp = self.get_report(self.owner_a, self.restaurant_b.id)
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_non_member_denied(self):
        resp = self.get_report(self.outsider, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_missing_restaurant_param_denied(self):
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


class DinifyReportsRetirementTests(TestCase):
    """
    The platform-admin reports surface (``/api/v1/reports/dinify/<name>/``) is
    GONE — endpoint, route and the three cross-tenant controllers behind it.

    It served every restaurant's revenue, owner PII (restaurant-listing) and the
    entire transaction ledger, gated on nothing but a ``dinify_admin`` string in
    the caller's ``User.roles``, and its only consumer was the deleted dinify-mgt
    frontend. Full retirement SUBSUMES the old gate tests: with no route there is
    no principal — role-holder, owner, diner or anonymous — for whom any of it
    resolves. Phase 1 rebuilds platform reporting natively on /api/admin/v1.
    """

    SLUGS = ('dashboard', 'restaurant-listing', 'transactions-listing')

    def setUp(self):
        self.owner = make_user('256700000251')
        self.restaurant = Restaurant.objects.create(
            name='Gate Restaurant', location='loc-gate',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.diner = make_user('256700000252')
        # Carries the retired role string — the principal that used to be allowed.
        self.legacy_role_holder = give_legacy_platform_role(
            make_user('256700000250'))

    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get_dinify(self, user, name):
        headers = self.auth(user) if user is not None else {}
        return self.client.get(f'/api/v1/reports/dinify/{name}/', **headers)

    def test_the_route_is_retired_for_every_principal(self):
        for user in (None, self.diner, self.owner, self.legacy_role_holder):
            for name in self.SLUGS:
                label = getattr(user, 'username', 'anonymous')
                resp = self.get_dinify(user, name)
                self.assertEqual(
                    resp.status_code, 404, f'{label}/{name}: {resp.content}')

    def test_an_unknown_slug_is_equally_gone(self):
        self.assertEqual(
            self.get_dinify(self.legacy_role_holder, 'not-a-report').status_code, 404)

    def test_the_restaurant_reports_route_still_serves_its_owner(self):
        # Retiring the platform surface must not have taken the tenant one with it.
        resp = self.client.get(
            f'/api/v1/reports/restaurant/sales-listing/'
            f'?restaurant={self.restaurant.id}&{DATE_QS}',
            **self.auth(self.owner),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
