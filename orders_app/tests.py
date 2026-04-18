from decimal import Decimal

from django.test import TestCase

from orders_app.models import Order, OrderItem
from orders_app.controllers.con_orders import ConOrder, handle_add_order_items
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME,
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
from restaurants_app.models import Restaurant, Table, MenuItem


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
