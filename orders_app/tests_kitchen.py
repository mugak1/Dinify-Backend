"""
Phase 2 kitchen-view backend tests.

Covers the order-creation service (daily numbering + idempotency), the snapshot
capture, the role-checked kitchen API (active set, fulfilment transitions,
priority), the finance/kitchen field isolation, and that the retired KDS routes
are gone.
"""
import uuid
import warnings
from decimal import Decimal
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.initiate_order import any_present_ongoing_order
from orders_app.controllers.manage_order import update_order_status
from orders_app.controllers.services.create_order import (
    _create_order,
    allocate_daily_order_number,
)
from users_app.models import User
from users_app.tests import seed_user, TEST_PHONE
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_MENU_SECTION_NAME,
    TEST_MENU_ITEM1_NAME, TEST_OPTION_MENU_ITEM_NAME,
    TEST_UNAVAILABLE_MENU_ITEM_NAME,
    TEST_OPTION_GROUP_ID, TEST_OPTION_CHOICE_SMALL_ID,
    TEST_TABLE_NUMBER1, TEST_TABLE_NUMBER2,
    TEST_TABLE_NUMBER3, TEST_TABLE_NUMBER4,
)
from restaurants_app.models import (
    Restaurant, Table, MenuItem, MenuSection, RestaurantEmployee, RestaurantTag,
    MenuItemTag, RestaurantRolePermission,
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_KITCHEN, RESTAURANT_WAITER,
    OrderStatus_Initiated, OrderStatus_Cancelled,
    OrderStatus_Served, OrderStatus_Pending,
    PaymentStatus_Pending,
    RestaurantStatus_Live,
    CancellationReason_CustomerChangedMind,
    MODULE_KITCHEN,
)
from reports_app.controllers.common.sale_filters import sale_orders, revenue_sum

ACTIVE_URL = '/api/v1/kitchen/orders/active/'
COMPLETED_URL = '/api/v1/kitchen/orders/completed/'
MENU_ITEMS_URL = '/api/v1/kitchen/menu-items/'


def _fulfilment_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/fulfilment-status/'


def _priority_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/priority/'


def _cancel_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/cancel/'


def _stock_url(pk):
    return f'/api/v1/kitchen/menu-items/{pk}/stock/'


class KitchenTestBase(TestCase):
    def setUp(self):
        seed_user()                      # dinify_admin owner used by seed_restaurant
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()

        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        # get_user_restaurant_roles only returns roles for active restaurants
        self.restaurant.status = RestaurantStatus_Live
        self.restaurant.save(update_fields=['status'])

        self.table1 = Table.objects.get(restaurant=self.restaurant, number=TEST_TABLE_NUMBER1)
        self.table2 = Table.objects.get(restaurant=self.restaurant, number=TEST_TABLE_NUMBER2)
        self.table3 = Table.objects.get(restaurant=self.restaurant, number=TEST_TABLE_NUMBER3)
        self.table4 = Table.objects.get(restaurant=self.restaurant, number=TEST_TABLE_NUMBER4)

        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.kitchen_user = self._make_member('256900000001', [RESTAURANT_KITCHEN])
        self.manager_user = self._make_member('256900000002', [RESTAURANT_MANAGER])
        self.owner_user = self._make_member('256900000003', [RESTAURANT_OWNER])
        self.waiter_user = self._make_member('256900000004', [RESTAURANT_WAITER])
        self.outsider_user = self._make_member('256900000005', None)

        self.client = APIClient()

    def _make_member(self, phone, restaurant_roles):
        user = User.objects.create_user(
            first_name='Kitchen', last_name='Member',
            email=f'{phone}@test.com',
            phone_number=phone, username=phone,
            country='Uganda', password='password',
            roles=[],  # NOT a dinify admin — exercises the restaurant-role path
        )
        if restaurant_roles is not None:
            RestaurantEmployee.objects.create(
                user=user, restaurant=self.restaurant, roles=restaurant_roles,
            )
        return user

    def _make_order(self, table=None, **overrides):
        defaults = dict(
            restaurant=self.restaurant,
            table=table or self.table1,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=OrderStatus_Pending,
            payment_status=PaymentStatus_Pending,
            fulfilment_status='new',
            order_date=timezone.localdate(),
        )
        defaults.update(overrides)
        return Order.objects.create(**defaults)


class KitchenNumberingTests(KitchenTestBase):
    def test_allocate_daily_order_number_is_sequential(self):
        today = timezone.localdate()
        numbers = [allocate_daily_order_number(self.restaurant, today) for _ in range(5)]
        self.assertEqual(numbers, [1, 2, 3, 4, 5])
        counter = RestaurantDailyOrderCounter.objects.get(
            restaurant=self.restaurant, order_date=today,
        )
        self.assertEqual(counter.next_number, 6)

    def test_two_orders_same_day_get_distinct_numbers(self):
        items = [{'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk), 'quantity': 1}]
        r1 = _create_order(restaurant=self.restaurant, table=self.table1, items=items)
        r2 = _create_order(restaurant=self.restaurant, table=self.table2, items=items)
        self.assertEqual(r1['status'], 200)
        self.assertEqual(r2['status'], 200)
        self.assertEqual(
            {r1['order'].order_number, r2['order'].order_number}, {1, 2},
        )

    def test_idempotent_retry_returns_existing_and_skips_gating(self):
        items = [{'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk), 'quantity': 1}]
        client_order_id = uuid.uuid4()

        first = _create_order(
            restaurant=self.restaurant, table=self.table1, items=items,
            client_order_id=client_order_id,
        )
        self.assertEqual(first['status'], 200)
        count_after_first = Order.objects.filter(restaurant=self.restaurant).count()

        # Same restaurant + client_order_id on the SAME table: a non-idempotent
        # create would be table-gated (400); idempotency short-circuits to 200
        # with the existing order and creates nothing new.
        second = _create_order(
            restaurant=self.restaurant, table=self.table1, items=items,
            client_order_id=client_order_id,
        )
        self.assertEqual(second['status'], 200)
        self.assertTrue(second['idempotent'])
        self.assertEqual(str(second['order'].id), str(first['order'].id))
        self.assertEqual(
            Order.objects.filter(restaurant=self.restaurant).count(),
            count_after_first,
        )

    def test_concurrent_create_conflict_returns_existing_order(self):
        """
        The CREATE-time race (distinct from the step-1 replay): two requests
        carrying the same client_order_id both clear the step-1 lookup before
        either commits, so the loser's INSERT trips
        uniq_order_restaurant_client_order_id. Simulate the winner appearing
        between step 1 and step 4 by injecting it at daily-number allocation
        (step 3 — after the step-1 lookup, before the create). The real create
        then raises IntegrityError and the catch must re-fetch and return the
        winner (200, idempotent) — creating no second row and never 500-ing.
        """
        items = [{'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
                  'quantity': 1}]
        client_order_id = uuid.uuid4()
        winner_box = {}

        def _inject_concurrent_winner(restaurant, order_date):
            # a racing request wins the INSERT for this client_order_id; its
            # order_number stays NULL so ONLY the client_order_id constraint trips
            winner = self._make_order(
                table=self.table1,
                client_order_id=client_order_id,
                order_number=None,
                order_date=order_date,
            )
            winner_box['id'] = winner.id
            return 1  # the losing request's (soon-to-conflict) order number

        with mock.patch(
            'orders_app.controllers.services.create_order.allocate_daily_order_number',
            side_effect=_inject_concurrent_winner,
        ):
            result = _create_order(
                restaurant=self.restaurant, table=self.table1, items=items,
                client_order_id=client_order_id,
            )

        self.assertEqual(result['status'], 200)
        self.assertTrue(result['idempotent'])
        self.assertEqual(str(result['order'].id), str(winner_box['id']))
        # exactly one row for this (restaurant, client_order_id): the winner
        self.assertEqual(
            Order.objects.filter(
                restaurant=self.restaurant, client_order_id=client_order_id,
            ).count(),
            1,
        )

    def test_create_integrity_error_unrelated_propagates(self):
        """
        An IntegrityError that is NOT the client_order_id conflict (no existing
        row for this client_order_id) must propagate rather than be swallowed by
        the idempotency catch.
        """
        items = [{'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
                  'quantity': 1}]
        client_order_id = uuid.uuid4()  # fresh — nothing to re-fetch

        with mock.patch.object(
            Order.objects, 'create',
            side_effect=IntegrityError('unrelated constraint'),
        ):
            with self.assertRaises(IntegrityError):
                _create_order(
                    restaurant=self.restaurant, table=self.table1, items=items,
                    client_order_id=client_order_id,
                )

        self.assertFalse(
            Order.objects.filter(
                restaurant=self.restaurant, client_order_id=client_order_id,
            ).exists()
        )


class KitchenSnapshotTests(KitchenTestBase):
    def test_snapshots_populated_on_creation(self):
        # link an allergen tag to a menu item so the snapshot has content
        allergen_tag = RestaurantTag.objects.filter(
            restaurant=self.restaurant, category='allergen',
        ).first()
        self.assertIsNotNone(allergen_tag)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        MenuItemTag.objects.create(menu_item=options_item, tag=allergen_tag)

        items = [{
            'item': str(options_item.pk),
            'quantity': 1,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
        }]
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table4.pk),
            items=items,
        )
        self.assertEqual(response['status'], 200)

        order_item = OrderItem.objects.get(
            order__id=response['data']['order_details']['id'],
            item=options_item,
            parent_item__isnull=True,
        )
        self.assertEqual(order_item.item_name_snapshot, options_item.name)
        self.assertEqual(order_item.modifiers_snapshot, ['Size: Small'])
        self.assertEqual(
            order_item.allergen_tags_snapshot,
            [{'name': allergen_tag.name, 'icon': allergen_tag.icon, 'colour': allergen_tag.colour}],
        )


class KitchenActiveEndpointTests(KitchenTestBase):
    def _active_ids(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(ACTIVE_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        return {row['id'] for row in response.json()['data']}

    def test_active_set_filtering(self):
        new_order = self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Pending)
        preparing_order = self._make_order(
            self.table2, fulfilment_status='preparing', order_status=OrderStatus_Pending)
        ready_order = self._make_order(
            self.table3, fulfilment_status='ready', order_status=OrderStatus_Pending)
        # served leaves the board immediately — it lives in the Completed feed
        served_just_now = self._make_order(
            self.table4, fulfilment_status='served', served_at=timezone.now(),
        )
        cancelled = self._make_order(
            self.table4, fulfilment_status='new', order_status=OrderStatus_Cancelled,
        )
        deleted = self._make_order(self.table1, fulfilment_status='new', deleted=True)

        ids = self._active_ids(self.kitchen_user)
        self.assertIn(str(new_order.id), ids)
        self.assertIn(str(preparing_order.id), ids)
        self.assertIn(str(ready_order.id), ids)
        self.assertNotIn(str(served_just_now.id), ids)
        self.assertNotIn(str(cancelled.id), ids)
        self.assertNotIn(str(deleted.id), ids)

    def test_active_serializer_shape(self):
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        # Wire a dedicated published is_extra item into the options item's
        # allowlist so the extra it carries passes the extra-applicability gate.
        extra = MenuItem.objects.create(
            name='Kitchen Shape Extra', section=options_item.section,
            primary_price=Decimal('500'), approved=True, enabled=True, is_extra=True,
        )
        options_item.has_extras = True
        options_item.extras_applicable = [str(extra.pk)]
        options_item.save(update_fields=['has_extras', 'extras_applicable'])

        items = [{
            'item': str(options_item.pk),
            'quantity': 2,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
            'extras': [str(extra.pk)],
        }]
        created = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table4.pk),
            items=items,
        )
        self.assertEqual(created['status'], 200)
        order_id = created['data']['order_details']['id']

        # A draft is invisible to the kitchen — submit it so the ticket reaches
        # the board (order_status initiated -> pending; fulfilment stays 'new').
        submitted = update_order_status(
            Order.objects.get(pk=order_id), OrderStatus_Pending, None,
        )
        self.assertEqual(submitted['status'], 200)

        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(ACTIVE_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        order_row = next(r for r in response.json()['data'] if r['id'] == str(order_id))

        for key in ('order_number', 'table_label', 'order_source',
                    'fulfilment_status', 'priority', 'created_at', 'served_at', 'items'):
            self.assertIn(key, order_row)
        self.assertEqual(order_row['fulfilment_status'], 'new')

        main = order_row['items'][0]
        for key in ('item_name_snapshot', 'quantity', 'modifiers',
                    'allergen_tags', 'extras'):
            self.assertIn(key, main)
        self.assertEqual(main['modifiers'], ['Size: Small'])
        self.assertEqual(len(main['extras']), 1)

    def test_active_permissions(self):
        rid = str(self.restaurant.id)
        for user in (self.kitchen_user, self.manager_user, self.owner_user, self.admin_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(ACTIVE_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 200, msg=f'expected 200 for {user.username}')

        for user in (self.waiter_user, self.outsider_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(ACTIVE_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 403, msg=f'expected 403 for {user.username}')

        # unauthenticated → 401 (proves AllowAny is gone)
        self.client.force_authenticate(user=None)
        response = self.client.get(ACTIVE_URL, {'restaurant': rid})
        self.assertEqual(response.status_code, 401)

    def test_kds_routes_are_retired(self):
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get('/api/v1/kds/tickets/')
        self.assertEqual(response.status_code, 404)


class KitchenCompletedEndpointTests(KitchenTestBase):
    """
    The Completed feed: GET /api/v1/kitchen/orders/completed/ — served tickets
    from the last COMPLETED_WINDOW, newest-completed first. Recall (served →
    ready) is initiated from here, so the feed's window is what bounds
    recallability.
    """

    def _completed_rows(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(COMPLETED_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        return response.json()['data']

    def test_completed_set_filtering_and_ordering(self):
        now = timezone.now()
        served_older = self._make_order(
            self.table1, fulfilment_status='served', served_at=now - timedelta(hours=2),
        )
        served_newest = self._make_order(
            self.table2, fulfilment_status='served', served_at=now,
        )
        # outside the 24h rolling window
        self._make_order(
            self.table3, fulfilment_status='served', served_at=now - timedelta(hours=25),
        )
        self._make_order(
            self.table4, fulfilment_status='served', served_at=now,
            order_status=OrderStatus_Cancelled,
        )
        self._make_order(
            self.table1, fulfilment_status='served', served_at=now, deleted=True,
        )
        # still active — belongs to the board, not the feed
        self._make_order(self.table2, fulfilment_status='ready')

        ids = [row['id'] for row in self._completed_rows(self.kitchen_user)]
        # exactly the in-window served orders, newest-completed first
        self.assertEqual(ids, [str(served_newest.id), str(served_older.id)])

    def test_completed_scoped_to_restaurant(self):
        other_restaurant = Restaurant.objects.create(
            name='Other Completed Restaurant', location='Elsewhere', owner=self.admin_user,
        )
        other_table = Table.objects.create(restaurant=other_restaurant, number=1)
        other_served = self._make_order(
            other_table, restaurant=other_restaurant,
            fulfilment_status='served', served_at=timezone.now(),
        )
        own_served = self._make_order(
            self.table1, fulfilment_status='served', served_at=timezone.now(),
        )

        ids = {row['id'] for row in self._completed_rows(self.kitchen_user)}
        self.assertIn(str(own_served.id), ids)
        self.assertNotIn(str(other_served.id), ids)

    def test_completed_serializer_matches_active_cards(self):
        self._make_order(self.table1, fulfilment_status='served', served_at=timezone.now())
        rows = self._completed_rows(self.kitchen_user)
        self.assertEqual(len(rows), 1)
        for key in ('order_number', 'table_label', 'order_source',
                    'fulfilment_status', 'priority', 'created_at', 'served_at', 'items'):
            self.assertIn(key, rows[0])
        self.assertEqual(rows[0]['fulfilment_status'], 'served')
        self.assertIsNotNone(rows[0]['served_at'])

    def test_completed_permissions(self):
        rid = str(self.restaurant.id)
        for user in (self.kitchen_user, self.manager_user, self.owner_user, self.admin_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(COMPLETED_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 200, msg=f'expected 200 for {user.username}')

        for user in (self.waiter_user, self.outsider_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(COMPLETED_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 403, msg=f'expected 403 for {user.username}')

        # unauthenticated → 401
        self.client.force_authenticate(user=None)
        response = self.client.get(COMPLETED_URL, {'restaurant': rid})
        self.assertEqual(response.status_code, 401)

    def test_missing_restaurant_param_returns_400(self):
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(COMPLETED_URL)
        self.assertEqual(response.status_code, 400)


class KitchenMenuItemsListTests(KitchenTestBase):
    """The kitchen sold-out panel's read: GET /api/v1/kitchen/menu-items/."""

    def test_list_permissions(self):
        rid = str(self.restaurant.id)
        # owner / manager / kitchen / admin can each list
        for user in (self.kitchen_user, self.manager_user, self.owner_user, self.admin_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(MENU_ITEMS_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 200, msg=f'expected 200 for {user.username}')

        # a user with no role at the restaurant is denied (cross-tenant denial)
        for user in (self.waiter_user, self.outsider_user):
            self.client.force_authenticate(user=user)
            response = self.client.get(MENU_ITEMS_URL, {'restaurant': rid})
            self.assertEqual(response.status_code, 403, msg=f'expected 403 for {user.username}')

        # unauthenticated → 401
        self.client.force_authenticate(user=None)
        response = self.client.get(MENU_ITEMS_URL, {'restaurant': rid})
        self.assertEqual(response.status_code, 401)

    def test_lists_available_items_with_stock_state(self):
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(MENU_ITEMS_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']

        # every row carries the panel's contract and is an on-menu item
        self.assertTrue(data)
        for row in data:
            for key in ('id', 'name', 'in_stock', 'available', 'section_name'):
                self.assertIn(key, row)
            self.assertTrue(row['available'])
            self.assertEqual(row['section_name'], TEST_MENU_SECTION_NAME)

        # a known available item is present with its in_stock state (default True)
        item1 = next(r for r in data if r['name'] == TEST_MENU_ITEM1_NAME)
        self.assertTrue(item1['in_stock'])

    def test_unavailable_item_excluded(self):
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(MENU_ITEMS_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        names = {row['name'] for row in response.json()['data']}
        self.assertNotIn(TEST_UNAVAILABLE_MENU_ITEM_NAME, names)

    def test_missing_restaurant_param_returns_400(self):
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(MENU_ITEMS_URL)
        self.assertEqual(response.status_code, 400)


class KitchenTransitionTests(KitchenTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _patch_status(self, order, target):
        return self.client.put(
            _fulfilment_url(order.id), {'fulfilment_status': target}, format='json',
        )

    def test_forward_transitions(self):
        order = self._make_order(fulfilment_status='new')
        self.assertEqual(self._patch_status(order, 'preparing').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')

        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')

        self.assertEqual(self._patch_status(order, 'served').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')
        self.assertIsNotNone(order.served_at)
        self.assertEqual(order.fulfilment_status_updated_by_id, self.kitchen_user.id)

    def test_illegal_transitions_rejected(self):
        order = self._make_order(fulfilment_status='new')
        self.assertEqual(self._patch_status(order, 'ready').status_code, 400)
        self.assertEqual(self._patch_status(order, 'served').status_code, 400)
        self.assertEqual(self._patch_status(order, 'banana').status_code, 400)

    def test_recall_served_to_ready_clears_served_at(self):
        order = self._make_order(fulfilment_status='served', served_at=timezone.now())
        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')
        self.assertIsNone(order.served_at)

    def test_recall_served_to_ready_regardless_of_age(self):
        # no recall age gate — the Completed feed's own window bounds what is
        # visible/recallable
        order = self._make_order(
            fulfilment_status='served',
            served_at=timezone.now() - timedelta(days=2),
        )
        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')
        self.assertIsNone(order.served_at)

    def test_recall_ready_to_preparing_allowed(self):
        order = self._make_order(fulfilment_status='ready')
        self.assertEqual(self._patch_status(order, 'preparing').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')

    def test_patch_cannot_mutate_finance_fields(self):
        order = self._make_order(
            fulfilment_status='new',
            order_status=OrderStatus_Initiated,
            payment_status=PaymentStatus_Pending,
        )
        response = self.client.put(
            _fulfilment_url(order.id),
            {'fulfilment_status': 'preparing', 'order_status': 'cancelled', 'payment_status': 'paid'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        self.assertEqual(order.payment_status, PaymentStatus_Pending)

    def test_priority_toggle_and_explicit_set(self):
        order = self._make_order(priority=False)
        self.assertEqual(self.client.put(_priority_url(order.id), {}, format='json').status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.priority)

        self.client.put(_priority_url(order.id), {'priority': False}, format='json')
        order.refresh_from_db()
        self.assertFalse(order.priority)

    def test_patch_denied_for_waiter(self):
        order = self._make_order(fulfilment_status='new')
        self.client.force_authenticate(user=self.waiter_user)
        self.assertEqual(self._patch_status(order, 'preparing').status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'new')


class TableGatingTests(KitchenTestBase):
    """
    Table-occupancy gating keys off the kitchen-owned fulfilment axis: a table
    is occupied iff it has an order that is not deleted, not cancelled, and
    whose fulfilment_status is not 'served'. A kitchen-served order frees the
    table even though order_status / payment_status are left untouched (diner
    payment is not wired up). Both copies of any_present_ongoing_order — the
    ConOrder staticmethod (order-create gating) and the standalone function
    (restaurant-portal table view) — must behave identically.
    """

    def _items(self):
        return [{
            'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
            'quantity': 1,
        }]

    def _assert_both(self, table, expected):
        """Both copies of the gate must agree on `present` (and order_id)."""
        con = ConOrder.any_present_ongoing_order(table)
        standalone = any_present_ongoing_order(table)
        self.assertEqual(con, standalone)
        self.assertEqual(con['present'], expected['present'])
        if expected['present']:
            self.assertEqual(str(con['order_id']), expected['order_id'])
        return con

    def test_non_served_order_blocks_new_order(self):
        # A SUBMITTED (pending) non-served order occupies the table.
        order = self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Pending,
        )
        self._assert_both(self.table1, {'present': True, 'order_id': str(order.id)})

        # A genuinely new submission on the same table is gated.
        result = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], 'The table has an ongoing order')
        self.assertEqual(str(result['data']['order_id']), str(order.id))

    def test_served_order_frees_the_same_table(self):
        order = self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Pending,
        )
        self._assert_both(self.table1, {'present': True, 'order_id': str(order.id)})

        # Kitchen serves the order — only the fulfilment axis moves.
        order.fulfilment_status = 'served'
        order.served_at = timezone.now()
        order.save(update_fields=['fulfilment_status', 'served_at'])

        # The table is now free, even though order_status / payment_status
        # are unchanged.
        self._assert_both(self.table1, {'present': False})

        # And a brand-new order is allowed on the freed table.
        result = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
        )
        self.assertEqual(result['status'], 200)
        self.assertNotEqual(str(result['order'].id), str(order.id))

    def test_cancelled_order_does_not_block(self):
        self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Cancelled,
        )
        self._assert_both(self.table1, {'present': False})

    def test_deleted_order_does_not_block(self):
        self._make_order(self.table1, fulfilment_status='new', deleted=True)
        self._assert_both(self.table1, {'present': False})

    def test_returns_most_recent_ongoing_order(self):
        older = self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Pending,
        )
        newer = self._make_order(
            self.table1, fulfilment_status='preparing', order_status=OrderStatus_Pending,
        )
        # Force distinct creation timestamps so "most recent" is deterministic
        # (time_created is auto_now_add, so override via .update()).
        now = timezone.now()
        Order.objects.filter(id=older.id).update(time_created=now - timedelta(minutes=5))
        Order.objects.filter(id=newer.id).update(time_created=now)
        self._assert_both(self.table1, {'present': True, 'order_id': str(newer.id)})


class TableLockTests(KitchenTestBase):
    """
    BUG-P2-4 (double-seating): _create_order locks the Table row
    (select_for_update) between the idempotency short-circuit and the occupancy
    gate, so two concurrent genuinely-new submissions for the same table
    serialize — the second blocks until the first commits, then its gate sees the
    first order and is rejected. True concurrency can't be simulated in the test
    harness, so these assert the post-lock gate, that the lock is applied, and
    that idempotent replays skip it.
    """

    def _items(self):
        return [{
            'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
            'quantity': 1,
        }]

    def test_second_new_submission_same_table_is_gated(self):
        # A SUBMITTED order occupies the table; a genuinely-new submission
        # (DIFFERENT client_order_id) on the same table is then gated at create
        # time. (A draft no longer occupies — two drafts coexist until one is
        # submitted; see SubmitClaimsTableTests.)
        first = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=uuid.uuid4(),
        )
        self.assertEqual(first['status'], 200)
        self.assertFalse(first['idempotent'])
        # submit the first order so it claims the table
        self.assertEqual(
            update_order_status(first['order'], OrderStatus_Pending, None)['status'],
            200,
        )

        second = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=uuid.uuid4(),
        )
        self.assertEqual(second['status'], 400)
        self.assertEqual(second['message'], 'The table has an ongoing order')
        self.assertEqual(str(second['data']['order_id']), str(first['order'].id))

        # Exactly one order exists on the table — no double-seating.
        self.assertEqual(
            Order.objects.filter(
                restaurant=self.restaurant, table=self.table1,
            ).count(),
            1,
        )

    def test_create_locks_the_table_row(self):
        # The lock is verifiably applied even though the harness can't force a
        # real race. `wraps` spies without replacing, so the real
        # select_for_update still runs (a no-op on SQLite, a real lock on the CI
        # Postgres) — the assertion is backend-agnostic and isolated to
        # Table.objects (the counter / final-order locks use other managers).
        with mock.patch.object(
            Table.objects, 'select_for_update',
            wraps=Table.objects.select_for_update,
        ) as spy:
            result = _create_order(
                restaurant=self.restaurant, table=self.table1, items=self._items(),
            )
        self.assertEqual(result['status'], 200)
        self.assertTrue(spy.called)

    def test_normal_single_order_succeeds(self):
        result = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
        )
        self.assertEqual(result['status'], 200)
        self.assertFalse(result['idempotent'])
        self.assertTrue(Order.objects.filter(id=result['order'].id).exists())

    def test_idempotent_replay_returns_existing_without_locking(self):
        # A replay (same client_order_id) short-circuits on idempotency BEFORE the
        # lock: it returns the existing order and never locks the table row.
        client_order_id = uuid.uuid4()
        first = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=client_order_id,
        )
        self.assertEqual(first['status'], 200)

        with mock.patch.object(
            Table.objects, 'select_for_update',
            wraps=Table.objects.select_for_update,
        ) as spy:
            replay = _create_order(
                restaurant=self.restaurant, table=self.table1, items=self._items(),
                client_order_id=client_order_id,
            )
        self.assertEqual(replay['status'], 200)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(str(replay['order'].id), str(first['order'].id))
        # The lock sits AFTER the idempotency short-circuit, so a replay never
        # reaches it.
        self.assertFalse(spy.called)


class SubmitClaimsTableTests(KitchenTestBase):
    """
    BUG-P2-2: an 'initiated' order is an unconfirmed DRAFT — it does not occupy
    the table and does not reach the kitchen. The order becomes real, and CLAIMS
    the table, only at SUBMIT (order_status 'initiated' -> 'pending'), which is
    therefore where the race-safe occupancy gate lives. Two diners submitting for
    the same table serialize on the table lock: the first claims it, the second
    gets a clean 400.
    """

    def _items(self):
        return [{
            'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
            'quantity': 1,
        }]

    def _active_ids(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(ACTIVE_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        return {row['id'] for row in response.json()['data']}

    def _draft(self, table, client_order_id=None):
        result = _create_order(
            restaurant=self.restaurant, table=table, items=self._items(),
            client_order_id=client_order_id,
        )
        self.assertEqual(result['status'], 200)
        return result

    def _submit(self, order):
        return update_order_status(order, OrderStatus_Pending, None)

    def _assert_free(self, table):
        # both copies of the occupancy gate must agree the table is free
        con = ConOrder.any_present_ongoing_order(table)
        standalone = any_present_ongoing_order(table)
        self.assertEqual(con, standalone)
        self.assertFalse(con['present'])

    # --- a draft is invisible ---------------------------------------------

    def test_draft_does_not_occupy_table(self):
        # A never-submitted draft does not occupy: the table reads as free, and
        # a SECOND initiate on the same table succeeds instead of a 400.
        first = self._draft(self.table1, client_order_id=uuid.uuid4())
        self.assertFalse(first['idempotent'])
        self._assert_free(self.table1)

        second = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=uuid.uuid4(),
        )
        self.assertEqual(second['status'], 200)
        self.assertFalse(second['idempotent'])
        self.assertNotEqual(str(second['order'].id), str(first['order'].id))

    def test_draft_not_on_kitchen_board_until_submitted(self):
        order = self._draft(self.table1)['order']
        # invisible to the kitchen while a draft
        self.assertNotIn(str(order.id), self._active_ids(self.kitchen_user))
        # visible once submitted
        self.assertEqual(self._submit(order)['status'], 200)
        self.assertIn(str(order.id), self._active_ids(self.kitchen_user))

    def test_p2_2_repro_abandoned_draft_does_not_lock_out(self):
        # THE reported bug: a diner initiates, abandons (no submit), then
        # re-initiates with a NEW client_order_id. The stale draft must NOT lock
        # them out of their own table with a 400 'ongoing order'.
        self._draft(self.table1, client_order_id=uuid.uuid4())  # abandoned draft
        resume = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=uuid.uuid4(),
        )
        self.assertEqual(resume['status'], 200)
        self.assertFalse(resume['idempotent'])

    # --- the table is claimed at submit -----------------------------------

    def test_submit_claims_the_table(self):
        order = self._draft(self.table1)['order']
        self.assertEqual(self._submit(order)['status'], 200)

        present = ConOrder.any_present_ongoing_order(self.table1)
        self.assertTrue(present['present'])
        self.assertEqual(str(present['order_id']), str(order.id))

        # a fresh initiate on the now-claimed table is gated
        blocked = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=uuid.uuid4(),
        )
        self.assertEqual(blocked['status'], 400)
        self.assertEqual(blocked['message'], 'The table has an ongoing order')

    def test_two_drafts_submit_serialize_second_gated(self):
        # Two drafts on one table (different client_order_ids). Submit both: the
        # first claims the table, the second is cleanly gated by the new gate.
        a = self._draft(self.table1, client_order_id=uuid.uuid4())['order']
        b = self._draft(self.table1, client_order_id=uuid.uuid4())['order']

        first = self._submit(a)
        self.assertEqual(first['status'], 200)

        second = self._submit(b)
        self.assertEqual(second['status'], 400)
        self.assertEqual(second['message'], 'The table has an ongoing order')

    def test_submit_locks_the_table_row(self):
        # The submit path takes the same Table row lock the create path does
        # (mirrors TableLockTests.test_create_locks_the_table_row). `wraps` spies
        # without replacing, so the real select_for_update still runs.
        order = self._draft(self.table1)['order']
        with mock.patch.object(
            Table.objects, 'select_for_update',
            wraps=Table.objects.select_for_update,
        ) as spy:
            result = self._submit(order)
        self.assertEqual(result['status'], 200)
        self.assertTrue(spy.called)

    def test_double_submit_same_order_is_rejected(self):
        # The status is re-checked on the FRESH row under the lock, so a second
        # submit of an already-submitted order gets today's 400.
        order = self._draft(self.table1)['order']
        self.assertEqual(self._submit(order)['status'], 200)
        second = self._submit(order)
        self.assertEqual(second['status'], 400)
        self.assertEqual(second['message'], 'This order cannot be submitted.')

    def test_idempotent_resume_then_submit(self):
        # initiate K -> abandon -> initiate SAME K returns the existing draft (no
        # second order) -> submit succeeds.
        k = uuid.uuid4()
        first = self._draft(self.table1, client_order_id=k)
        replay = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
            client_order_id=k,
        )
        self.assertEqual(replay['status'], 200)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(str(replay['order'].id), str(first['order'].id))
        self.assertEqual(
            Order.objects.filter(
                restaurant=self.restaurant, table=self.table1,
            ).count(),
            1,
        )
        self.assertEqual(self._submit(replay['order'])['status'], 200)

    def test_end_to_end_submit_prepare_serve_frees_table(self):
        # Regression: a submitted order reaches the KDS, advances through the
        # fulfilment axis, and serving frees the table (occupancy keys off
        # fulfilment) and drops it from the active board.
        order = self._draft(self.table1)['order']
        self.assertEqual(self._submit(order)['status'], 200)
        self.assertIn(str(order.id), self._active_ids(self.kitchen_user))

        self.client.force_authenticate(user=self.kitchen_user)
        for target in ('preparing', 'ready', 'served'):
            resp = self.client.put(
                _fulfilment_url(order.id),
                {'fulfilment_status': target}, format='json',
            )
            self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')

        self._assert_free(self.table1)
        self.assertNotIn(str(order.id), self._active_ids(self.kitchen_user))


class KitchenMenuItemStockTests(KitchenTestBase):
    """
    86 endpoint — toggle MenuItem.in_stock. Mirrors the priority-endpoint tests:
    set-or-toggle, the owner / manager / kitchen role matrix, 404 on a bad id,
    and cross-restaurant denial (kitchen staff at A cannot 86 B's item). It writes
    the same in_stock column the menu module writes, so the two stay in sync.
    """

    def setUp(self):
        super().setUp()
        self.item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

    def test_stock_toggle_and_explicit_set(self):
        self.client.force_authenticate(user=self.kitchen_user)

        # in_stock defaults to True; an empty payload toggles the current value.
        self.assertTrue(self.item.in_stock)
        self.assertEqual(
            self.client.put(_stock_url(self.item.id), {}, format='json').status_code, 200,
        )
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)

        # An explicit value wins over the current state, either direction.
        self.client.put(_stock_url(self.item.id), {'in_stock': True}, format='json')
        self.item.refresh_from_db()
        self.assertTrue(self.item.in_stock)

        self.client.put(_stock_url(self.item.id), {'in_stock': False}, format='json')
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)

    def test_stock_permissions(self):
        # Owner / manager / kitchen / admin may 86 the item; waiter / outsider
        # (no kitchen role at this restaurant) are denied and change nothing.
        for user in (self.kitchen_user, self.manager_user, self.owner_user, self.admin_user):
            self.client.force_authenticate(user=user)
            response = self.client.put(_stock_url(self.item.id), {'in_stock': True}, format='json')
            self.assertEqual(response.status_code, 200, msg=f'expected 200 for {user.username}')

        for user in (self.waiter_user, self.outsider_user):
            self.client.force_authenticate(user=user)
            response = self.client.put(_stock_url(self.item.id), {'in_stock': False}, format='json')
            self.assertEqual(response.status_code, 403, msg=f'expected 403 for {user.username}')

        # The denied PUTs must not have flipped the value the allowed ones left.
        self.item.refresh_from_db()
        self.assertTrue(self.item.in_stock)

    def test_stock_unknown_or_malformed_id_404(self):
        self.client.force_authenticate(user=self.kitchen_user)
        # Unknown-but-valid UUID and a malformed id both resolve to 404, never 500.
        self.assertEqual(
            self.client.put(_stock_url(uuid.uuid4()), {}, format='json').status_code, 404,
        )
        self.assertEqual(
            self.client.put(_stock_url('not-a-uuid'), {}, format='json').status_code, 404,
        )

    def test_stock_denied_cross_restaurant(self):
        # Build an item under a DIFFERENT restaurant. self.kitchen_user holds a
        # kitchen role only at self.restaurant, so it must not reach this item.
        other_restaurant = Restaurant.objects.create(
            name='Other Seed Restaurant', location='Elsewhere', owner=self.admin_user,
        )
        other_restaurant.status = RestaurantStatus_Live
        other_restaurant.save(update_fields=['status'])
        other_section = MenuSection.objects.create(
            name='Other Section', restaurant=other_restaurant,
        )
        other_item = MenuItem.objects.create(
            name='Other Item', section=other_section,
            primary_price=1000, discounted_price=900, running_discount=False,
        )

        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.put(_stock_url(other_item.id), {'in_stock': False}, format='json')
        self.assertEqual(response.status_code, 403)
        other_item.refresh_from_db()
        self.assertTrue(other_item.in_stock)  # unchanged


class KitchenCancelTests(KitchenTestBase):
    """
    State-aware order cancellation (PUT /api/v1/kitchen/orders/<pk>/cancel/).

      - 'new'                 : any kitchen user may void (base kitchen gate)
      - 'preparing' / 'ready' : manager/owner only (kitchen-only → 403)
      - 'served'              : not cancellable (recall it first → 400)

    Cancelling sets order_status='cancelled', which frees the table and drops
    the ticket from the active set; payment_status and the fulfilment axis are
    left untouched.
    """

    VALID_REASON = CancellationReason_CustomerChangedMind

    def _cancel(self, order, user, **payload):
        self.client.force_authenticate(user=user)
        return self.client.put(_cancel_url(order.id), payload, format='json')

    def _active_ids(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(ACTIVE_URL, {'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 200)
        return {row['id'] for row in response.json()['data']}

    def test_new_order_cancellable_by_each_privileged_role(self):
        # kitchen, manager and owner can each void a not-yet-started order.
        for user in (self.kitchen_user, self.manager_user, self.owner_user):
            order = self._make_order(fulfilment_status='new')
            response = self._cancel(order, user, cancellation_reason=self.VALID_REASON)
            self.assertEqual(response.status_code, 200, msg=f'expected 200 for {user.username}')
            order.refresh_from_db()
            self.assertEqual(order.order_status, OrderStatus_Cancelled)
            self.assertEqual(order.cancelled_by_id, user.id)
            self.assertIsNotNone(order.cancelled_at)
            self.assertEqual(order.cancellation_reason, self.VALID_REASON)
            # the fulfilment axis is untouched by a cancellation
            self.assertEqual(order.fulfilment_status, 'new')

    def test_cancel_frees_table_and_drops_from_active_set(self):
        order = self._make_order(
            self.table1, fulfilment_status='new', order_status=OrderStatus_Pending,
        )
        # occupied and on the board before cancellation
        self.assertTrue(any_present_ongoing_order(self.table1)['present'])
        self.assertIn(str(order.id), self._active_ids(self.kitchen_user))

        self.assertEqual(
            self._cancel(order, self.kitchen_user, cancellation_reason=self.VALID_REASON).status_code,
            200,
        )

        # both copies of the occupancy gate now agree the table is free
        con = ConOrder.any_present_ongoing_order(self.table1)
        standalone = any_present_ongoing_order(self.table1)
        self.assertEqual(con, standalone)
        self.assertFalse(con['present'])
        # and the ticket has dropped from the active set
        self.assertNotIn(str(order.id), self._active_ids(self.kitchen_user))

    def test_preparing_or_ready_requires_manager(self):
        for status in ('preparing', 'ready'):
            # a kitchen-only user is denied once preparation has started
            order = self._make_order(fulfilment_status=status, order_status=OrderStatus_Initiated)
            response = self._cancel(order, self.kitchen_user, cancellation_reason=self.VALID_REASON)
            self.assertEqual(response.status_code, 403, msg=f'kitchen denied for {status}')
            order.refresh_from_db()
            self.assertEqual(order.order_status, OrderStatus_Initiated)  # unchanged

            # manager and owner may cancel
            for user in (self.manager_user, self.owner_user):
                order = self._make_order(fulfilment_status=status)
                response = self._cancel(order, user, cancellation_reason=self.VALID_REASON)
                self.assertEqual(
                    response.status_code, 200,
                    msg=f'expected 200 for {user.username} on {status}',
                )
                order.refresh_from_db()
                self.assertEqual(order.order_status, OrderStatus_Cancelled)
                self.assertEqual(order.cancelled_by_id, user.id)

    def test_served_order_not_cancellable(self):
        # 'served' is blocked even for a manager with a valid reason; recall first.
        order = self._make_order(fulfilment_status='served', served_at=timezone.now(), order_status=OrderStatus_Initiated)
        response = self._cancel(order, self.manager_user, cancellation_reason=self.VALID_REASON)
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_already_cancelled_order_returns_400(self):
        order = self._make_order(fulfilment_status='new', order_status=OrderStatus_Cancelled)
        response = self._cancel(order, self.manager_user, cancellation_reason=self.VALID_REASON)
        self.assertEqual(response.status_code, 400)

    def test_missing_or_invalid_reason_returns_400(self):
        order = self._make_order(fulfilment_status='new', order_status=OrderStatus_Initiated)
        # missing reason
        self.assertEqual(self._cancel(order, self.kitchen_user).status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

        # invalid reason
        self.assertEqual(
            self._cancel(order, self.kitchen_user, cancellation_reason='banana').status_code, 400,
        )
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_denied_for_user_without_kitchen_role(self):
        order = self._make_order(fulfilment_status='new', order_status=OrderStatus_Initiated)
        for user in (self.waiter_user, self.outsider_user):
            response = self._cancel(order, user, cancellation_reason=self.VALID_REASON)
            self.assertEqual(response.status_code, 403, msg=f'expected 403 for {user.username}')
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)  # unchanged

    def test_unknown_or_malformed_pk_404(self):
        self.client.force_authenticate(user=self.kitchen_user)
        # Unknown-but-valid UUID and a malformed id both resolve to 404, never 500.
        self.assertEqual(
            self.client.put(
                _cancel_url(uuid.uuid4()),
                {'cancellation_reason': self.VALID_REASON}, format='json',
            ).status_code, 404,
        )
        self.assertEqual(
            self.client.put(
                _cancel_url('not-a-uuid'),
                {'cancellation_reason': self.VALID_REASON}, format='json',
            ).status_code, 404,
        )


class KitchenModuleGridOverrideTests(KitchenTestBase):
    """
    H2: kitchen endpoints gate on the central permission resolver
    (can_user_access_module / MODULE_KITCHEN), so the owner-configured Roles &
    Access grid takes server-side effect for the kitchen module — like every
    other portal module.

    A RestaurantRolePermission override row that GRANTS kitchen to a normally
    denied role (waiter) lets it in; one that REVOKES kitchen from a normally
    granted role (kitchen) locks it out — proven across both a read gate (the
    active feed) and a write gate (the 86 / stock toggle), so the conversion is
    uniform. With no override rows the seeded defaults are unchanged (the
    per-endpoint permission tests above remain the full default-matrix coverage).
    """

    def setUp(self):
        super().setUp()
        self.item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

    def _active_status(self, user):
        """Status of a representative READ gate (GET active feed) for ``user``."""
        self.client.force_authenticate(user=user)
        return self.client.get(
            ACTIVE_URL, {'restaurant': str(self.restaurant.id)},
        ).status_code

    def _stock_status(self, user):
        """Status of a representative WRITE gate (86 / stock toggle) for ``user``."""
        self.client.force_authenticate(user=user)
        return self.client.put(
            _stock_url(self.item.id), {'in_stock': True}, format='json',
        ).status_code

    def test_owner_grant_kitchen_to_waiter_enables_access(self):
        # Baseline: a waiter holds no kitchen module by default -> denied on both
        # a read and a write kitchen gate.
        self.assertEqual(self._active_status(self.waiter_user), 403)
        self.assertEqual(self._stock_status(self.waiter_user), 403)

        # The owner customises the grid to GRANT kitchen to the waiter role.
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant, role=RESTAURANT_WAITER,
            modules={MODULE_KITCHEN: True},
        )

        # The override now takes server-side effect across every kitchen gate.
        self.assertEqual(self._active_status(self.waiter_user), 200)
        self.assertEqual(self._stock_status(self.waiter_user), 200)

    def test_owner_revoke_kitchen_from_kitchen_role_denies_access(self):
        # Baseline: the kitchen role holds kitchen by default -> allowed.
        self.assertEqual(self._active_status(self.kitchen_user), 200)
        self.assertEqual(self._stock_status(self.kitchen_user), 200)

        # The owner customises the grid to REVOKE kitchen from the kitchen role.
        # A non-empty override dict supersedes the coded default even when its
        # value is False.
        RestaurantRolePermission.objects.create(
            restaurant=self.restaurant, role=RESTAURANT_KITCHEN,
            modules={MODULE_KITCHEN: False},
        )

        # The revoke now takes server-side effect across every kitchen gate.
        self.assertEqual(self._active_status(self.kitchen_user), 403)
        self.assertEqual(self._stock_status(self.kitchen_user), 403)

    def test_seeded_default_grid_unchanged(self):
        # With NO override rows the resolver falls back to the coded defaults,
        # which must match the pre-fix hardcoded set exactly: owner / manager /
        # kitchen / admin in, waiter / outsider out — on both a read and a write
        # kitchen gate.
        for user in (self.kitchen_user, self.manager_user, self.owner_user, self.admin_user):
            self.assertEqual(self._active_status(user), 200, msg=f'active 200 for {user.username}')
            self.assertEqual(self._stock_status(user), 200, msg=f'stock 200 for {user.username}')

        for user in (self.waiter_user, self.outsider_user):
            self.assertEqual(self._active_status(user), 403, msg=f'active 403 for {user.username}')
            self.assertEqual(self._stock_status(user), 403, msg=f'stock 403 for {user.username}')


class KitchenServeAdvancesOrderStatusTests(KitchenTestBase):
    """
    BUG-P1-2: the kitchen serve/recall completion transition couples
    order_status to the fulfilment axis, so a served order counts as a sale
    (order_status='served' is in SALE_STATUSES) and a recall reverts it
    (-> 'pending'). The finance-owned no-clobber behaviour is preserved on every
    non-completion transition, and a cancelled order is never resurrected into a
    sale. payment_status is never touched.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)
        self.today = timezone.localdate()

    def _patch_status(self, order, target):
        return self.client.put(
            _fulfilment_url(order.id), {'fulfilment_status': target}, format='json',
        )

    def _sale_qs(self):
        return sale_orders(self.restaurant.id, self.today, self.today)

    def _is_sale(self, order):
        return self._sale_qs().filter(id=order.id).exists()

    def test_serve_advances_order_status_and_qualifies_as_sale(self):
        order = self._make_order(
            fulfilment_status='ready',
            order_status=OrderStatus_Pending,
            actual_cost=Decimal('5000.00'),
        )
        # in-flight: not yet a sale
        self.assertFalse(self._is_sale(order))

        self.assertEqual(self._patch_status(order, 'served').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')
        self.assertEqual(order.order_status, OrderStatus_Served)
        self.assertIsNotNone(order.served_at)

        # now a sale, and its actual_cost is recognised as revenue
        self.assertTrue(self._is_sale(order))
        revenue = self._sale_qs().aggregate(revenue=revenue_sum())['revenue']
        self.assertEqual(revenue, Decimal('5000.00'))

    def test_recall_reverts_order_status_and_drops_from_sales(self):
        order = self._make_order(
            fulfilment_status='served',
            order_status=OrderStatus_Served,
            served_at=timezone.now(),
            actual_cost=Decimal('5000.00'),
        )
        self.assertTrue(self._is_sale(order))

        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertIsNone(order.served_at)
        self.assertFalse(self._is_sale(order))

    def test_non_completion_transitions_do_not_write_order_status(self):
        # (a) new -> preparing: order_status is NOT in the save's update_fields
        order = self._make_order(fulfilment_status='new', order_status=OrderStatus_Pending)
        with mock.patch.object(Order, 'save', autospec=True) as save_mock:
            self.assertEqual(self._patch_status(order, 'preparing').status_code, 200)
        self.assertEqual(save_mock.call_count, 1)
        self.assertNotIn('order_status', save_mock.call_args.kwargs['update_fields'])

        # (b) unmocked: order_status is untouched across the non-completion steps
        order = self._make_order(fulfilment_status='new', order_status=OrderStatus_Pending)
        self.assertEqual(self._patch_status(order, 'preparing').status_code, 200)
        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')
        self.assertEqual(order.order_status, OrderStatus_Pending)

    def test_cancelled_order_driven_to_served_stays_cancelled(self):
        # The fulfilment endpoint doesn't itself block a cancelled order on the
        # fulfilment axis (pre-existing gap), but the serve guard keeps
        # order_status='cancelled' -> it never becomes a sale.
        order = self._make_order(
            fulfilment_status='ready',
            order_status=OrderStatus_Cancelled,
            actual_cost=Decimal('5000.00'),
        )
        self.assertEqual(self._patch_status(order, 'served').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')
        self.assertEqual(order.order_status, OrderStatus_Cancelled)
        self.assertFalse(self._is_sale(order))

    def test_serve_is_server_authoritative_ignoring_body_order_status(self):
        # A spoofed order_status in the body must not win — the server sets it
        # from its own constant, so serving cannot inflate an order to 'paid'.
        order = self._make_order(
            fulfilment_status='ready', order_status=OrderStatus_Pending,
        )
        response = self.client.put(
            _fulfilment_url(order.id),
            {'fulfilment_status': 'served', 'order_status': 'paid'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Served)


class KitchenClockTests(KitchenTestBase):
    """
    Class B: the manual ``time_last_updated = datetime.now()`` writes were removed
    from manage_order — ``BaseModel.time_last_updated`` is ``auto_now=True``, so the
    save (not a hand-rolled naive assignment) stamps it with an aware
    ``timezone.now()``. Freeze the clock and assert a live submit transition stamps
    the frozen aware instant and emits no naive-datetime RuntimeWarning.
    """

    FROZEN = datetime(2026, 7, 31, 22, 30, tzinfo=dt_timezone.utc)

    @mock.patch('django.utils.timezone.now')
    def test_submit_stamps_aware_time_last_updated_without_warning(self, mock_now):
        mock_now.return_value = self.FROZEN
        order = self._make_order(order_status=OrderStatus_Initiated)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            result = update_order_status(order, OrderStatus_Pending, self.owner_user)
        self.assertEqual(result['status'], 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        # auto_now stamped it with the aware, frozen instant.
        self.assertTrue(timezone.is_aware(order.time_last_updated))
        self.assertEqual(order.time_last_updated, self.FROZEN)
        naive = [w for w in caught if 'received a naive datetime' in str(w.message)]
        self.assertEqual(naive, [])
