"""
Phase 2 kitchen-view backend tests.

Covers the order-creation service (daily numbering + idempotency), the snapshot
capture, the role-checked kitchen API (active set, fulfilment transitions,
priority), the finance/kitchen field isolation, and that the retired KDS routes
are gone.
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.initiate_order import any_present_ongoing_order
from orders_app.controllers.services.create_order import (
    _create_order,
    allocate_daily_order_number,
)
from users_app.models import User
from users_app.tests import seed_user, TEST_PHONE
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME,
    TEST_MENU_ITEM1_NAME, TEST_OPTION_MENU_ITEM_NAME,
    TEST_OPTION_GROUP_ID, TEST_OPTION_CHOICE_SMALL_ID,
    TEST_TABLE_NUMBER1, TEST_TABLE_NUMBER2,
    TEST_TABLE_NUMBER3, TEST_TABLE_NUMBER4,
)
from restaurants_app.models import (
    Restaurant, Table, MenuItem, RestaurantEmployee, RestaurantTag, MenuItemTag,
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_KITCHEN, RESTAURANT_WAITER,
    OrderStatus_Initiated, OrderStatus_Cancelled,
    PaymentStatus_Pending,
    RestaurantStatus_Active,
)

ACTIVE_URL = '/api/v1/kitchen/orders/active/'


def _fulfilment_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/fulfilment-status/'


def _priority_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/priority/'


class KitchenTestBase(TestCase):
    def setUp(self):
        seed_user()                      # dinify_admin owner used by seed_restaurant
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()

        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        # get_user_restaurant_roles only returns roles for active restaurants
        self.restaurant.status = RestaurantStatus_Active
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
            order_status=OrderStatus_Initiated,
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
        now = timezone.now()
        new_order = self._make_order(self.table1, fulfilment_status='new')
        served_recent = self._make_order(
            self.table2, fulfilment_status='served', served_at=now,
        )
        served_old = self._make_order(
            self.table3, fulfilment_status='served', served_at=now - timedelta(minutes=11),
        )
        cancelled = self._make_order(
            self.table4, fulfilment_status='new', order_status=OrderStatus_Cancelled,
        )
        deleted = self._make_order(self.table1, fulfilment_status='new', deleted=True)

        ids = self._active_ids(self.kitchen_user)
        self.assertIn(str(new_order.id), ids)
        self.assertIn(str(served_recent.id), ids)
        self.assertNotIn(str(served_old.id), ids)
        self.assertNotIn(str(cancelled.id), ids)
        self.assertNotIn(str(deleted.id), ids)

    def test_active_serializer_shape(self):
        items = [{
            'item': str(MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME).pk),
            'quantity': 2,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
            'extras': [str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk)],
        }]
        created = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table4.pk),
            items=items,
        )
        self.assertEqual(created['status'], 200)
        order_id = created['data']['order_details']['id']

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
                    'allergen_tags', 'item_note', 'extras'):
            self.assertIn(key, main)
        self.assertIsNone(main['item_note'])
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

    def test_recall_served_to_ready_within_window(self):
        order = self._make_order(fulfilment_status='served', served_at=timezone.now())
        self.assertEqual(self._patch_status(order, 'ready').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')
        self.assertIsNone(order.served_at)

    def test_recall_served_to_ready_outside_window_rejected(self):
        order = self._make_order(
            fulfilment_status='served',
            served_at=timezone.now() - timedelta(minutes=11),
        )
        self.assertEqual(self._patch_status(order, 'ready').status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')

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
        order = self._make_order(self.table1, fulfilment_status='new')
        self._assert_both(self.table1, {'present': True, 'order_id': str(order.id)})

        # A genuinely new submission on the same table is gated.
        result = _create_order(
            restaurant=self.restaurant, table=self.table1, items=self._items(),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], 'The table has an ongoing order')
        self.assertEqual(str(result['data']['order_id']), str(order.id))

    def test_served_order_frees_the_same_table(self):
        order = self._make_order(self.table1, fulfilment_status='new')
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
        older = self._make_order(self.table1, fulfilment_status='new')
        newer = self._make_order(self.table1, fulfilment_status='preparing')
        # Force distinct creation timestamps so "most recent" is deterministic
        # (time_created is auto_now_add, so override via .update()).
        now = timezone.now()
        Order.objects.filter(id=older.id).update(time_created=now - timedelta(minutes=5))
        Order.objects.filter(id=newer.id).update(time_created=now)
        self._assert_both(self.table1, {'present': True, 'order_id': str(newer.id)})
