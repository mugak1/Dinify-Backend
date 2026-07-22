"""
Role-permission ENFORCEMENT tests (PR C).

The resolution layer (resolve_module_permissions / can_user_access_module) is
covered by users_app/tests_role_permissions.py. THIS module verifies the portal
endpoints now ROUTE their gates through it:

  * the scoping primitives get_module_restaurant_ids / get_employed_restaurant_ids
  * the RestaurantSetupEndpoint catch-all (write gate + list scoping + detail read)
  * the dedicated endpoints (menu vs tables module split) + table-transfer
  * reports / reviews / support widening
  * a dinify admin retains the manage-level elevation actions (kitchen
    goodwill-cancel, review resolution)

Behaviour is neutral for the seeded owner/manager defaults (both hold every grid
module); the denials below are for non-owner/manager roles (kitchen, staff) and
cross-tenant access.
"""
import json

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from users_app.controllers.permissions_check import (
    get_module_restaurant_ids,
    get_employed_restaurant_ids,
)
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, RestaurantRolePermission,
    MenuSection, MenuItem, Table,
)
from orders_app.models import Order
from reviews_app.models import Review
from support_app.models import SupportIssue
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RestaurantStatus_Pending,
    RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_KITCHEN, RESTAURANT_STAFF,
    DINIFY_ADMIN,
    MODULE_MENU, MODULE_TABLES, MODULE_REPORTS, MODULE_REVIEWS, MODULE_TEAM,
    MODULE_SETTINGS, MODULE_SUPPORT,
)

SETUP_URL = '/api/v1/restaurant-setup/'
REPORTS_URL = '/api/v1/reports/restaurant/'
REVIEWS_URL = '/api/v1/reviews/'
SUPPORT_URL = '/api/v1/support/issues/'
DATE_QS = 'from=2024-01-01&to=2024-01-31'


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


def auth(user):
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


def records(resp):
    """The paginated list shape is data.records; fall back gracefully."""
    try:
        data = resp.json().get('data')
    except (ValueError, AttributeError):
        return []
    if isinstance(data, dict):
        return data.get('records', [])
    return data or []


def record_ids(resp):
    return {str(r.get('id')) for r in records(resp) if isinstance(r, dict)}


class ScopingPrimitiveTests(TestCase):
    """Unit tests for the two new list-scoping primitives."""

    def setUp(self):
        self.owner = make_user('256730000001')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc', status=RestaurantStatus_Active,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.manager = make_user('256730000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.chef = make_user('256730000003')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])
        self.staff = make_user('256730000004')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.admin = make_user('256730000005', roles=[DINIFY_ADMIN])
        self.outsider = make_user('256730000006')
        self.rid = str(self.restaurant.id)

    def test_admin_is_unrestricted_none(self):
        self.assertIsNone(get_module_restaurant_ids(self.admin, MODULE_MENU))
        self.assertIsNone(get_employed_restaurant_ids(self.admin))

    def test_owner_has_every_module(self):
        for module in (MODULE_MENU, MODULE_TABLES, MODULE_REPORTS, MODULE_REVIEWS,
                       MODULE_SETTINGS, MODULE_TEAM):
            self.assertEqual(
                get_module_restaurant_ids(self.owner, module), {self.rid}, module)

    def test_manager_has_grid_but_not_team(self):
        self.assertEqual(get_module_restaurant_ids(self.manager, MODULE_MENU), {self.rid})
        self.assertEqual(get_module_restaurant_ids(self.manager, MODULE_REPORTS), {self.rid})
        # team is owner/admin-only
        self.assertEqual(get_module_restaurant_ids(self.manager, MODULE_TEAM), set())

    def test_staff_tables_only(self):
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_TABLES), {self.rid})
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_MENU), set())
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_REPORTS), set())

    def test_chef_no_tables_no_menu(self):
        self.assertEqual(get_module_restaurant_ids(self.chef, MODULE_TABLES), set())
        self.assertEqual(get_module_restaurant_ids(self.chef, MODULE_MENU), set())

    def test_employed_ids_are_role_agnostic(self):
        for user in (self.owner, self.manager, self.chef, self.staff):
            self.assertEqual(get_employed_restaurant_ids(user), {self.rid})
        self.assertEqual(get_employed_restaurant_ids(self.outsider), set())

    def test_support_module_maps_to_employed_set(self):
        self.assertEqual(
            get_module_restaurant_ids(self.staff, MODULE_SUPPORT),
            get_employed_restaurant_ids(self.staff),
        )

    def test_override_row_grants_module(self):
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant, role=RESTAURANT_STAFF,
            modules={MODULE_MENU: True},
        )
        self.assertEqual(
            get_module_restaurant_ids(self.staff, MODULE_MENU), {self.rid})

    def test_inactive_employment_excluded(self):
        emp = RestaurantEmployee.objects.get(
            user=self.staff, restaurant=self.restaurant)
        emp.active = False
        emp.save(update_fields=['active'])
        self.assertEqual(get_employed_restaurant_ids(self.staff), set())
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_TABLES), set())

    def test_non_active_restaurant_excluded_from_module_but_not_support(self):
        pending_owner = make_user('256730000007')
        pending = Restaurant.objects.create(
            name='P', location='loc', status=RestaurantStatus_Pending,
            owner=pending_owner,
        )
        staff2 = make_user('256730000008')
        RestaurantEmployee.objects.create(
            user=staff2, restaurant=pending, roles=[RESTAURANT_STAFF])
        # a non-active restaurant is excluded from module access ...
        self.assertEqual(get_module_restaurant_ids(staff2, MODULE_TABLES), set())
        # ... but support (ungated, employment-based) still reaches it
        self.assertEqual(get_employed_restaurant_ids(staff2), {str(pending.id)})

    def test_inactive_user_denied(self):
        self.staff.is_active = False
        self.staff.save(update_fields=['is_active'])
        self.assertEqual(get_employed_restaurant_ids(self.staff), set())
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_TABLES), set())


class CatchAllModuleEnforcementTests(TestCase):
    """RestaurantSetupEndpoint catch-all now gates per-module via _RECORD_MODULE."""

    def setUp(self):
        self.owner = make_user('256731000001')
        self.restaurant = Restaurant.objects.create(
            name='A', location='loc-a', status=RestaurantStatus_Active,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.manager = make_user('256731000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.staff = make_user('256731000003')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.chef = make_user('256731000004')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])
        self.admin = make_user('256731000005', roles=[DINIFY_ADMIN])

        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, listing_position=0)
        self.item = MenuItem.objects.create(
            name='Item', section=self.section, primary_price=1000, listing_position=0)
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant)

        # Second restaurant for the cross-tenant spoof.
        self.owner_b = make_user('256731000006')
        self.restaurant_b = Restaurant.objects.create(
            name='B', location='loc-b', status=RestaurantStatus_Active,
            owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b, roles=[RESTAURANT_OWNER])
        self.section_b = MenuSection.objects.create(
            name='B Mains', restaurant=self.restaurant_b, listing_position=0)
        self.item_b = MenuItem.objects.create(
            name='B Item', section=self.section_b, primary_price=1000, listing_position=0)

    def _get(self, user, config_detail, qs=''):
        url = f'{SETUP_URL}{config_detail}/'
        if qs:
            url = f'{url}?{qs}'
        return self.client.get(url, **auth(user))

    def _write(self, user, method, config_detail, body):
        return getattr(self.client, method)(
            f'{SETUP_URL}{config_detail}/',
            data=json.dumps(body), content_type='application/json', **auth(user))

    # -- list scoping: the module split ---------------------------------

    def test_staff_sees_tables_not_menu(self):
        self.assertIn(str(self.table.id), record_ids(self._get(self.staff, 'tables')))
        self.assertNotIn(str(self.item.id), record_ids(self._get(self.staff, 'menuitems')))

    def test_chef_sees_neither_tables_nor_menu(self):
        self.assertNotIn(str(self.table.id), record_ids(self._get(self.chef, 'tables')))
        self.assertNotIn(str(self.item.id), record_ids(self._get(self.chef, 'menuitems')))

    def test_owner_sees_menu_and_employees(self):
        self.assertIn(str(self.item.id), record_ids(self._get(self.owner, 'menuitems')))
        # owner holds `team` -> employees list is populated
        self.assertTrue(len(records(self._get(self.owner, 'employees'))) >= 1)

    def test_orders_vocab_is_retired(self):
        # The `orders` record type was retired from the setup catch-all (its
        # serializer, filters, list-path and _RECORD_MODULE mapping are gone), so
        # a GET falls through to the generic unmapped-resource rejection (403) for
        # owner AND staff — the endpoint no longer lists orders. Reports-module
        # enforcement over order data lives on the reports endpoint and is covered
        # by the REPORTS_URL probes in ReportsReviewsSupportEnforcementTests.
        for user in (self.owner, self.staff):
            resp = self._get(user, 'orders')
            self.assertEqual(resp.status_code, 403, resp.content)

    def test_manager_sees_menu_but_not_employees(self):
        # manager holds every grid module (menu shows) ...
        self.assertIn(str(self.item.id), record_ids(self._get(self.manager, 'menuitems')))
        # ... but NOT team (employees -> team is owner-only), so it scopes empty
        self.assertEqual(records(self._get(self.manager, 'employees')), [])

    def test_admin_unrestricted_across_tenants(self):
        # admin reads restaurant B's menu items it has no employment at
        self.assertIn(
            str(self.item_b.id),
            record_ids(self._get(self.admin, 'menuitems',
                                 qs=f'restaurant={self.restaurant_b.id}')),
        )

    # -- write gate: the module split -----------------------------------

    def test_staff_cannot_create_menusection(self):
        resp = self._write(self.staff, 'post', 'menusections',
                            {'name': 'Nope', 'restaurant': str(self.restaurant.id)})
        self.assertEqual(resp.status_code, 403)

    def test_manager_cannot_create_employee_team_owner_only(self):
        # employees -> team (Decision 1): a manager is denied.
        resp = self._write(self.manager, 'post', 'employees',
                           {'restaurant': str(self.restaurant.id),
                            'user': str(self.staff.id), 'roles': ['waiter']})
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_create_menusection(self):
        resp = self._write(self.owner, 'post', 'menusections',
                           {'name': 'Owner Section', 'restaurant': str(self.restaurant.id)})
        self.assertEqual(resp.status_code, 200, resp.content)

    # -- anti-spoof: the resource-resolved restaurant governs -----------

    def test_spoof_update_denied_resolved_restaurant_governs(self):
        # owner_a HOLDS menu at A, but the resolver walks item_b -> B, where
        # owner_a has no employment. The spoofed restaurant=A is ignored.
        resp = self._write(self.owner, 'put', 'menuitems',
                           {'id': str(self.item_b.id), 'name': 'hacked',
                            'restaurant': str(self.restaurant.id)})
        self.assertEqual(resp.status_code, 403)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.name, 'B Item')

    # -- fail-closed: missing / unknown resource on the detail read -----

    def test_detail_missing_pk_returns_404(self):
        resp = self._get(self.owner, 'details',
                         qs='record=menuitems&id=00000000-0000-0000-0000-000000000000')
        self.assertEqual(resp.status_code, 404)

    def test_detail_unknown_record_returns_404(self):
        resp = self._get(self.owner, 'details',
                         qs=f'record=bogus&id={self.item.id}')
        self.assertEqual(resp.status_code, 404)


class DedicatedEndpointModuleEnforcementTests(TestCase):
    """Dedicated endpoints: tables-domain vs menu-domain module split."""

    def setUp(self):
        self.owner = make_user('256732000001')
        self.restaurant = Restaurant.objects.create(
            name='A', location='loc-a', status=RestaurantStatus_Active,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.manager = make_user('256732000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.staff = make_user('256732000003')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.chef = make_user('256732000004')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])

        self.table_a1 = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant, status='seated')
        self.table_a2 = Table.objects.create(
            number=2, str_number='2', restaurant=self.restaurant, status='available')

        # Second restaurant + table for the cross-restaurant transfer.
        self.owner_b = make_user('256732000005')
        self.restaurant_b = Restaurant.objects.create(
            name='B', location='loc-b', status=RestaurantStatus_Active,
            owner=self.owner_b,
        )
        self.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_b, status='available')

    def _get(self, user, path, qs=''):
        url = f'{SETUP_URL}{path}/'
        if qs:
            url = f'{url}?{qs}'
        return self.client.get(url, **auth(user))

    def _transfer(self, user, source, dest):
        return self.client.post(
            f'{SETUP_URL}table-actions/transfer/',
            data=json.dumps({'source_table_id': str(source.id),
                             'destination_table_id': str(dest.id)}),
            content_type='application/json', **auth(user))

    # tables-domain (reservations) — staff allowed, chef denied
    def test_staff_can_read_reservations_tables_module(self):
        resp = self._get(self.staff, 'reservations', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_chef_denied_reservations(self):
        resp = self._get(self.chef, 'reservations', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 403)

    # menu-domain (tags / upsell) — staff denied, manager allowed
    def test_staff_denied_restaurant_tags_menu_module(self):
        resp = self._get(self.staff, 'restaurant-tags', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 403)

    def test_staff_denied_preset_tags(self):
        resp = self._get(self.staff, 'preset-tags', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 403)

    def test_staff_denied_upsell_config(self):
        resp = self._get(self.staff, 'upsell-config', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 403)

    def test_manager_allowed_restaurant_tags(self):
        resp = self._get(self.manager, 'restaurant-tags', qs=f'restaurant={self.restaurant.id}')
        self.assertEqual(resp.status_code, 200, resp.content)

    # table-transfer
    def test_cross_restaurant_transfer_denied(self):
        # source in A, destination in B -> rejected before any seating change.
        resp = self._transfer(self.owner, self.table_a1, self.table_b)
        self.assertEqual(resp.status_code, 400)
        self.table_b.refresh_from_db()
        self.assertEqual(self.table_b.status, 'available')

    def test_staff_can_transfer_within_own_restaurant(self):
        resp = self._transfer(self.staff, self.table_a1, self.table_a2)
        self.assertEqual(resp.status_code, 200, resp.content)


class ReportsReviewsSupportEnforcementTests(TestCase):
    """reports (reports module), reviews (reviews module), support (widened)."""

    def setUp(self):
        self.owner = make_user('256733000001')
        self.restaurant = Restaurant.objects.create(
            name='A', location='loc-a', status=RestaurantStatus_Active,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.staff = make_user('256733000002')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.chef = make_user('256733000003')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])
        self.admin = make_user('256733000004', roles=[DINIFY_ADMIN])

        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant)
        self.order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000)
        self.review = Review.objects.create(order=self.order, overall_rating=2)
        self.issue = SupportIssue.objects.create(
            restaurant=self.restaurant, category='bug', impact='question',
            title='An issue', description='A description long enough.')

        # cross-tenant target
        self.owner_b = make_user('256733000005')
        self.restaurant_b = Restaurant.objects.create(
            name='B', location='loc-b', status=RestaurantStatus_Active,
            owner=self.owner_b,
        )
        self.issue_b = SupportIssue.objects.create(
            restaurant=self.restaurant_b, category='bug', impact='question',
            title='B issue', description='Another description here.')

    # reports -> reports module (404 fail-closed for non-members/roles)
    def test_owner_reads_reports(self):
        resp = self.client.get(
            f'{REPORTS_URL}sales-listing/?restaurant={self.restaurant.id}&{DATE_QS}',
            **auth(self.owner))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_staff_denied_reports(self):
        resp = self.client.get(
            f'{REPORTS_URL}sales-listing/?restaurant={self.restaurant.id}&{DATE_QS}',
            **auth(self.staff))
        self.assertEqual(resp.status_code, 404)

    def test_chef_denied_reports(self):
        resp = self.client.get(
            f'{REPORTS_URL}sales-listing/?restaurant={self.restaurant.id}&{DATE_QS}',
            **auth(self.chef))
        self.assertEqual(resp.status_code, 404)

    def test_admin_reads_reports_any_tenant(self):
        resp = self.client.get(
            f'{REPORTS_URL}sales-listing/?restaurant={self.restaurant_b.id}&{DATE_QS}',
            **auth(self.admin))
        self.assertEqual(resp.status_code, 200, resp.content)

    # reviews -> reviews module
    def test_owner_sees_reviews_staff_does_not(self):
        owner_resp = self.client.get(
            f'{REVIEWS_URL}?restaurant={self.restaurant.id}', **auth(self.owner))
        self.assertIn(str(self.review.id), record_ids(owner_resp))
        staff_resp = self.client.get(
            f'{REVIEWS_URL}?restaurant={self.restaurant.id}', **auth(self.staff))
        self.assertEqual(records(staff_resp), [])

    def test_review_analytics_staff_denied_owner_allowed(self):
        staff_resp = self.client.get(
            f'{REVIEWS_URL}summary/?restaurant={self.restaurant.id}', **auth(self.staff))
        self.assertEqual(staff_resp.status_code, 403)
        owner_resp = self.client.get(
            f'{REVIEWS_URL}summary/?restaurant={self.restaurant.id}', **auth(self.owner))
        self.assertEqual(owner_resp.status_code, 200, owner_resp.content)

    # support -> widened to ANY active employee
    def test_staff_can_list_and_read_support(self):
        list_resp = self.client.get(SUPPORT_URL, **auth(self.staff))
        self.assertIn(str(self.issue.id), record_ids(list_resp))
        detail_resp = self.client.get(f'{SUPPORT_URL}{self.issue.id}/', **auth(self.staff))
        self.assertEqual(detail_resp.status_code, 200, detail_resp.content)

    def test_chef_can_create_support_issue(self):
        resp = self.client.post(
            SUPPORT_URL,
            data=json.dumps({'restaurant': str(self.restaurant.id),
                             'category': 'bug', 'impact': 'question',
                             'title': 'Chef issue', 'description': 'Reported by chef.'}),
            content_type='application/json', **auth(self.chef))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_support_detail_cross_tenant_still_404(self):
        resp = self.client.get(f'{SUPPORT_URL}{self.issue_b.id}/', **auth(self.staff))
        self.assertEqual(resp.status_code, 404)


class AdminElevatedActionsTests(TestCase):
    """A security PR must not lock dinify-admin out of the manage-level gates."""

    def setUp(self):
        self.owner = make_user('256734000001')
        self.restaurant = Restaurant.objects.create(
            name='A', location='loc-a', status=RestaurantStatus_Active,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.manager = make_user('256734000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.chef = make_user('256734000003')
        RestaurantEmployee.objects.create(
            user=self.chef, restaurant=self.restaurant, roles=[RESTAURANT_KITCHEN])
        self.staff = make_user('256734000004')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.admin = make_user('256734000005', roles=[DINIFY_ADMIN])

        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant)

    def _preparing_order(self):
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000)
        order.fulfilment_status = 'preparing'
        order.save(update_fields=['fulfilment_status'])
        return order

    def _cancel(self, user, order):
        return self.client.put(
            f'/api/v1/kitchen/orders/{order.id}/cancel/',
            data=json.dumps({'cancellation_reason': 'other'}),
            content_type='application/json', **auth(user))

    def _resolve(self, user, review):
        return self.client.patch(
            f'{REVIEWS_URL}{review.id}/resolution/',
            data=json.dumps({'resolution_status': 'resolved'}),
            content_type='application/json', **auth(user))

    def _review(self):
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000)
        return Review.objects.create(order=order, overall_rating=2)

    # kitchen goodwill-cancel (manage-level elevation over 'preparing')
    def test_admin_can_goodwill_cancel(self):
        resp = self._cancel(self.admin, self._preparing_order())
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_chef_cannot_goodwill_cancel_preparing(self):
        # chef has kitchen access but goodwill-cancel needs manage-level.
        resp = self._cancel(self.chef, self._preparing_order())
        self.assertEqual(resp.status_code, 403)

    def test_cancel_missing_order_is_404(self):
        resp = self.client.put(
            '/api/v1/kitchen/orders/00000000-0000-0000-0000-000000000000/cancel/',
            data=json.dumps({'cancellation_reason': 'other'}),
            content_type='application/json', **auth(self.admin))
        self.assertEqual(resp.status_code, 404)

    # review resolution (can_manage_restaurant)
    def test_admin_can_resolve_review(self):
        resp = self._resolve(self.admin, self._review())
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_manager_can_resolve_review(self):
        resp = self._resolve(self.manager, self._review())
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_staff_cannot_resolve_review(self):
        resp = self._resolve(self.staff, self._review())
        self.assertEqual(resp.status_code, 403)
