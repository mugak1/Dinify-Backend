from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.db.models.deletion import ProtectedError
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem
from orders_app.controllers.con_orders import ConOrder, handle_add_order_items
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_MENU_SECTION_NAME,
    TEST_MENU_ITEM1_NAME, TEST_MENU_ITEM2_NAME,
    TEST_DISCOUNTED_MENU_ITEM_NAME,
    TEST_TABLE_NUMBER1,
    TEST_TABLE_NUMBER3,
    TEST_TABLE_NUMBER4,
    TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME,
    TEST_OPTION_MENU_ITEM_NAME,
    TEST_OPTION_GROUP_ID,
    TEST_OPTION_CHOICE_SMALL_ID,
    TEST_OPTION_CHOICE_LARGE_ID,
    TEST_OPTION_CHOICE_SMALL_COST,
)
from restaurants_app.models import Restaurant, Table, MenuItem, MenuSection
from dinify_backend.configss.messages import OK_ORDER_UPDATED
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, OrderStatus_Pending,
)


def seed_order():
    restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    table = Table.objects.get(number=TEST_TABLE_NUMBER1)
    table3 = Table.objects.get(number=TEST_TABLE_NUMBER3)
    user = User.objects.get(username=TEST_PHONE)

    Order.objects.create(
        restaurant=restaurant,
        table=table,
        customer=user,
        total_cost=10000,
        discounted_cost=9000,
        savings=1000,
        actual_cost=9000,
        prepayment_required=True,
        payment_status='paid',
        order_status='completed'
    )

    Order.objects.create(
        restaurant=restaurant,
        table=table3,
        customer=user,
        total_cost=0,
        discounted_cost=0,
        savings=0,
        actual_cost=0,
        prepayment_required=True,
        payment_status='paid',
        order_status='completed'
    )


class TestOrderFunctions(TestCase):
    print("\n===TESTING ORDERS===\n")

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

    def test_determine_effective_unit_price_no_modifiers(self):
        menu_item = MenuItem.objects.get(name=TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME)
        result = ConOrder.determine_effective_unit_price(menu_item=menu_item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('800.00'))
        self.assertEqual(result['cost_of_options'], Decimal('0.00'))

    def test_determine_effective_unit_price_with_modifiers(self):
        menu_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        result = ConOrder.determine_effective_unit_price(
            menu_item=menu_item,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]}
        )
        self.assertEqual(result['status'], 200)
        # discounted_price 900 + small choice 1100 = 2000
        self.assertEqual(result['price'], Decimal('2000.00'))
        self.assertEqual(result['cost_of_options'], Decimal(str(TEST_OPTION_CHOICE_SMALL_COST)) + Decimal('0'))

    def test_check_options_requirements(self):
        menu_item_with_options = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        menu_item_without_options = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

        ok_items = [{
            'item': str(menu_item_without_options.pk),
            'quantity': 2,
        }]
        self.assertEqual(ConOrder.check_options_requirements(ok_items)['status'], 200)

        missing_selection = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {}
        }]
        self.assertEqual(ConOrder.check_options_requirements(missing_selection)['status'], 400)

        too_many = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {
                TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID, TEST_OPTION_CHOICE_LARGE_ID],
            }
        }]
        self.assertEqual(ConOrder.check_options_requirements(too_many)['status'], 400)

        valid = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {
                TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID],
            }
        }]
        self.assertEqual(ConOrder.check_options_requirements(valid)['status'], 200)

    def test_check_extras_requirements(self):
        # No fixture is pre-seeded with extras, so configure one inline
        # (mirrors how the options test reuses a pre-configured option item).
        mi = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        mi.has_extras = True
        mi.extras_min_selections = 1
        mi.extras_max_selections = 2
        mi.save()
        pk = str(mi.pk)

        # check_extras_requirements only counts len(item['extras']),
        # so placeholder values of the right length are sufficient.
        under_min = [{'item': pk, 'quantity': 1, 'extras': []}]
        self.assertEqual(ConOrder.check_extras_requirements(under_min)['status'], 400)

        at_min = [{'item': pk, 'quantity': 1, 'extras': ['x']}]
        self.assertEqual(ConOrder.check_extras_requirements(at_min)['status'], 200)

        over_max = [{'item': pk, 'quantity': 1, 'extras': ['x', 'y', 'z']}]
        self.assertEqual(ConOrder.check_extras_requirements(over_max)['status'], 400)

        # Unlimited maximum when extras_max_selections is null.
        mi.extras_max_selections = None
        mi.save()
        unlimited = [{'item': pk, 'quantity': 1, 'extras': ['a', 'b', 'c', 'd']}]
        self.assertEqual(ConOrder.check_extras_requirements(unlimited)['status'], 200)

    def test_con_order_initiate_and_item_lifecycle(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER4)

        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        discounted = MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        # first attempt has the options item with no selected_modifiers → rejected
        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {
                'item': str(options_item.pk),
                'quantity': 1,
                'extras': [str(menu_item1.pk), str(menu_item2.pk)],
            },
        ]
        response = ConOrder.initiate_order(
            restaurant_id=str(restaurant.pk),
            table_id=str(table.pk),
            items=items,
        )
        self.assertEqual(response['status'], 400)

        # second attempt supplies grouped modifier selections
        order_item_payload = {
            'item': str(options_item.pk),
            'quantity': 1,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
            'extras': [str(menu_item1.pk), str(menu_item2.pk)],
        }
        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {'item': str(discounted.pk), 'quantity': 3},
            order_item_payload,
        ]
        response = ConOrder.initiate_order(
            restaurant_id=str(restaurant.pk),
            table_id=str(table.pk),
            items=items,
        )
        self.assertEqual(response['status'], 200)

        order_id = str(response['data']['order_details']['id'])
        # dedup check: re-submitting the same grouped modifier selection finds the existing row
        self.assertTrue(
            ConOrder.determine_existing_order_item(item=order_item_payload, order_id=order_id)
        )
        # but a different choice is a distinct row
        different_choice = dict(order_item_payload)
        different_choice['selected_modifiers'] = {
            TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_LARGE_ID]
        }
        self.assertFalse(
            ConOrder.determine_existing_order_item(item=different_choice, order_id=order_id)
        )

        # confirm stored fields on the created item
        saved_option_item = OrderItem.objects.get(
            order__id=order_id,
            item=options_item,
            parent_item__isnull=True,
        )
        self.assertEqual(
            saved_option_item.selected_modifiers,
            {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
        )
        self.assertEqual(len(saved_option_item.options), 1)
        self.assertEqual(saved_option_item.options[0]['name'], 'Size')
        self.assertEqual(saved_option_item.options[0]['choices'], 'Small')

    def test_handle_add_order_items(self):
        order_record = Order.objects.get(
            table=Table.objects.get(number=TEST_TABLE_NUMBER3)
        )
        old_total_cost = order_record.total_cost

        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {
                'item': str(options_item.pk),
                'quantity': 1,
                'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
                'extras': [str(menu_item1.pk), str(menu_item2.pk)],
            },
        ]
        response = handle_add_order_items(order_id=str(order_record.pk), items=items)
        self.assertEqual(response['status'], 200)

        order_record.refresh_from_db()
        self.assertGreater(order_record.total_cost, old_total_cost)


class TestOrderTableProtect(TestCase):
    """
    Leg 1 of the deletion model: Order.table is on_delete=PROTECT, so a Table
    (or, by chain, a Restaurant) that has orders can never be hard-deleted —
    financial/order history is protected instead of being silently
    cascade-destroyed.
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()  # creates orders on tables 1 and 3

    def test_deleting_table_with_orders_raises_protected_error(self):
        table_with_order = Table.objects.get(number=TEST_TABLE_NUMBER1)
        with self.assertRaises(ProtectedError):
            table_with_order.delete()
        # the table and its orders survive the blocked delete
        self.assertTrue(Table.objects.filter(number=TEST_TABLE_NUMBER1).exists())
        self.assertTrue(Order.objects.filter(table=table_with_order).exists())

    def test_deleting_table_without_orders_succeeds(self):
        table_without_order = Table.objects.get(number=TEST_TABLE_NUMBER4)
        self.assertFalse(Order.objects.filter(table=table_without_order).exists())
        table_without_order.delete()
        self.assertFalse(Table.objects.filter(number=TEST_TABLE_NUMBER4).exists())

    def test_deleting_restaurant_with_orders_raises_protected_error(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        # PROTECT propagates up the Restaurant -> Table -> Order chain: the
        # cascade collects the tables, then the Order.table PROTECT blocks.
        with self.assertRaises(ProtectedError):
            restaurant.delete()
        self.assertTrue(Restaurant.objects.filter(name=TEST_RESTAURANT_NAME).exists())


class TestAnonymousOrderPaths(TestCase):
    """
    Regression coverage for the AnonymousUser order-path bug: DRF hands
    unauthenticated requests an AnonymousUser (not None), which used to slip past
    `if user is None` guards and get assigned to User FK fields, raising a
    ValueError that surfaced as a generic failure (submit) or a 500 (delete-item).
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        # APIClient with no force_authenticate → request.user is AnonymousUser,
        # exactly reproducing an unauthenticated diner.
        self.client = APIClient()
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER1)
        self.menu_item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

    def _initiate_anonymous_order(self) -> str:
        """Place an order as an anonymous diner and return its id."""
        response = self.client.post(
            '/api/v2/orders/initiate/',
            {
                'restaurant': str(self.restaurant.pk),
                'table': str(self.table.pk),
                'items': [{'item': str(self.menu_item.pk), 'quantity': 1}],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        return str(response.json()['data']['order_details']['id'])

    def test_anonymous_diner_initiate_then_submit_succeeds(self):
        order_id = self._initiate_anonymous_order()

        response = self.client.put(
            '/api/v1/orders/submit/',
            {'order': order_id},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['message'], OK_ORDER_UPDATED)

        order = Order.objects.get(id=order_id)
        self.assertEqual(order.order_status, OrderStatus_Pending)
        # attribution is left null rather than crashing on AnonymousUser
        self.assertIsNone(order.last_updated_by)

    def test_anonymous_prepare_and_cancel_require_login(self):
        order_id = self._initiate_anonymous_order()

        for action in ['prepare', 'cancel']:
            response = self.client.put(
                f'/api/v1/orders/{action}/',
                {'order': order_id},
                format='json',
            )
            # auth-gated: rejected cleanly, not a 500 or generic update error
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['message'], 'Please log in')

        # the rejected actions never touched the order
        order = Order.objects.get(id=order_id)
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_anonymous_delete_item_does_not_500(self):
        order_id = self._initiate_anonymous_order()
        item = OrderItem.objects.filter(order__id=order_id).first()
        self.assertIsNotNone(item)

        response = self.client.delete(
            '/api/v2/orders/add-items/',
            {'item': str(item.pk), 'reason': 'changed mind'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertTrue(item.deleted)
        # deleted_by FK is left null instead of raising on AnonymousUser
        self.assertIsNone(item.deleted_by)


class TestDiscountActivationPricing(TestCase):
    """The effective unit price honours the single, timezone-aware discount
    window predicate: an inactive window charges primary_price even when
    running_discount=True and a discounted_price is stored (that column is no
    longer consulted — discount_details is the source of truth)."""

    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _make_item(self, name, discount_details):
        # discounted_price is deliberately set to a stale 8000 to prove the
        # window predicate — not the stored column — decides the charge.
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=Decimal('8000'),
            running_discount=True,
            consider_discount_object=True,
            discount_details=discount_details,
        )

    def test_expired_window_charges_primary_price(self):
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        item = self._make_item('Expired Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': yesterday,
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('10000.00'))

    def test_wrong_recurring_day_charges_primary_price(self):
        today_iso = timezone.localdate().isoweekday()
        other_days = [d for d in range(1, 8) if d != today_iso]
        item = self._make_item('Wrong Day Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': other_days,
            'start_date': '', 'end_date': '',
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('10000.00'))

    def test_active_window_charges_discounted_price(self):
        item = self._make_item('Active Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': '',
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

    def test_empty_recurring_days_means_every_day(self):
        # Empty recurring_days == "every day". Locks the chosen semantic so a
        # future switch to "never" is a deliberate change.
        today = timezone.localdate()
        item = self._make_item('Empty Recurring Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [],
            'start_date': (today - timedelta(days=5)).isoformat(),
            'end_date': (today + timedelta(days=5)).isoformat(),
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))
