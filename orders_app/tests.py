from turtle import update
from django.test import TestCase
# from orders_app.controllers.order import Order as OrderController
from orders_app.controllers.initiate_order import initiate_order
from orders_app.models import Order, OrderItem
from orders_app.controllers.manage_order import update_item_status
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_MENU_SECTION_NAME,
    TEST_MENU_ITEM1_NAME, TEST_MENU_ITEM2_NAME,
    TEST_MENU_ITEM3_NAME, TEST_MENU_ITEM4_NAME,
    TEST_MENU_ITEM5_NAME, TEST_UNAVAILABLE_MENU_ITEM_NAME,
    TEST_DISCOUNTED_MENU_ITEM_NAME,
    TEST_TABLE_NUMBER1,
    TEST_TABLE_NUMBER2,
    TEST_TABLE_NUMBER3,
    TEST_TABLE_NUMBER4,
    TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME,
    TEST_OPTION_MENU_ITEM_NAME
)
from restaurants_app.models import Restaurant, Table, MenuItem
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated,
    OrderStatus_Pending,
    OrderItemStatus_Preparing, OrderItemStatus_Served,
    OrderStatus_Cancelled,
    OrderStatus_Served
)
from orders_app.controllers.v2_initiate_order import (
    determine_effective_unit_price,
    add_order_item,
    update_order_amounts,
    v2_initiate_order
)


def seed_order():
    """
    seed the order for the test
    """
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

    # order for table 3
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


# Create your tests here.
class TestOrderFunctions(TestCase):
    print("\n===TESTING ORDERS===\n")
    """
    test cases for the order functions
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

    def test_initiate_order(self):
        """
        test initiating an order
        """
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table1 = Table.objects.get(number=TEST_TABLE_NUMBER1)
        table2 = Table.objects.get(number=TEST_TABLE_NUMBER2)

        def test_post_paid_initiate():
            data = {
                'customer': None,
                'created_by': None,
                'restaurant': str(restaurant.pk),
                'table': str(table1.pk),
                'items': [
                    {
                        'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
                        'quantity': 2
                    },
                    {
                        'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME).pk),
                        'quantity': 1
                    },
                    {
                        'item': str(MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME).pk),
                        'quantity': 3
                    },
                    {
                        'item': str(MenuItem.objects.get(name=TEST_UNAVAILABLE_MENU_ITEM_NAME).pk),
                        'quantity': 1
                    },

                ]
            }
            result = initiate_order(data)
            self.assertEqual(result['status'], 200)
            self.assertEqual(len(result['data']['unavailable_items']), 1)
            self.assertEqual(len(result['data']['available_items']), 3)
            self.assertEqual(result['data']['order_details']['prepayment_required'], False)

        def test_pre_paid_initiate():
            data = {
                'restaurant': str(restaurant.pk),
                'table': str(table2.pk),
                'items': [
                    {
                        'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
                        'quantity': 2
                    },
                    {
                        'item': str(MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME).pk),
                        'quantity': 1
                    },
                    {
                        'item': str(MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME).pk),
                        'quantity': 3
                    }
                ]
            }
            result = initiate_order(data)
            self.assertEqual(result['status'], 200)
            self.assertEqual(len(result['data']['unavailable_items']), 0)
            self.assertEqual(len(result['data']['available_items']), 3)
            self.assertEqual(result['data']['order_details']['prepayment_required'], True)

        def test_order_item_status_update():
            # get a sample order
            order_item1 = OrderItem.objects.filter(
                item=str(MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME).pk),
                order__table=str(table2.pk)
            )[0]
            order_item2 = OrderItem.objects.filter(
                item=str(MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME).pk),
                order__table=str(table2.pk)
            )[0]
            order_item3 = OrderItem.objects.filter(
                item=str(MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME).pk),
                order__table=str(table2.pk)
            )[0]

            user = User.objects.get(username=TEST_PHONE)

            # updating to preparing
            result = update_item_status(
                item_id=str(order_item1.pk),
                new_status=OrderItemStatus_Preparing,
                user=user
            )
            self.assertEqual(result['status'], 200)

            # updating to served
            update_item_status(
                item_id=str(order_item1.pk),
                new_status=OrderItemStatus_Served,
                user=user
            )
            update_item_status(
                item_id=str(order_item2.pk),
                new_status=OrderItemStatus_Served,
                user=user
            )
            update_item_status(
                item_id=str(order_item3.pk),
                new_status=OrderItemStatus_Served,
                user=user
            )

            order = order_item1.order
            order.refresh_from_db()
            self.assertEqual(order.order_status, OrderStatus_Served)
        
        test_post_paid_initiate()
        test_pre_paid_initiate()
        test_order_item_status_update()

    def test_extra_discounted_item(self):
        menu_item = MenuItem.objects.get(name=TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME)
        effective_unit_price = determine_effective_unit_price(menu_item=menu_item)
        self.assertEqual(effective_unit_price['status'], 200)
        self.assertEqual(effective_unit_price['price'], 800)

    def test_options_item(self):
        menu_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        effective_unit_price = determine_effective_unit_price(
            menu_item=menu_item,
            option=0
        )
        self.assertEqual(effective_unit_price['status'], 200)
        self.assertEqual(effective_unit_price['price'], 1100)

        effective_unit_price = determine_effective_unit_price(
            menu_item=menu_item,
            option=1
        )
        self.assertEqual(effective_unit_price['status'], 400)

    def test_add_order_item(self):
        order_record = Order.objects.get(
            table=Table.objects.get(number=TEST_TABLE_NUMBER3)
        )
        order_id = str(order_record.pk)

        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)

        print('\n...testing order item | no discount, no options, no extras...\n')
        item = {
            'item': str(menu_item1.pk),
            'quantity': 2
        }
        result = add_order_item(item=item, order_id=order_id)
        self.assertEqual(result['status'], 200)

        print('\n...testing order item | comprehensive discount consideration...\n')
        item = {
            'item': str(MenuItem.objects.get(name=TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME).pk),
            'quantity': 1
        }
        result = add_order_item(item=item, order_id=order_id)
        self.assertEqual(result['status'], 200)

        print('\n...testing order item | options\n')
        item = {
            'item': str(MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME).pk),
            'quantity': 1,
            'option': 0,
            'choice': 1
        }
        result = add_order_item(item=item, order_id=order_id)
        self.assertEqual(result['status'], 200)

        print('\n testing order item | options,choices,extras')
        item = {
            'item': str(MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME).pk),
            'quantity': 1,
            'option': 0,
            'choice': 1,
            'extras': [str(menu_item1.pk), str(menu_item2.pk)]
        }
        result = add_order_item(item=item, order_id=order_id)
        self.assertEqual(result['status'], 200)

        # check that the order has items with parent items
        order_items = OrderItem.objects.filter(
            order=order_id
        )
        self.assertGreater(order_items.count(), 0)

        print('...testing order amount update...')
        update_order_amounts(order=order_record)

        order_record.refresh_from_db()
        print(order_record.total_cost, order_record.discounted_cost, order_record.savings)

    def test_v2_initiate_order(self):
        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER4)
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    

        items = [
            {
                'item': str(menu_item1.pk),
                'quantity': 2
            },
            {
                'item': str(MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME).pk),
                'quantity': 1,
                'option': 0,
                'choice': 1,
                'extras': [str(menu_item1.pk), str(menu_item2.pk)]
            }
        ]
        response = v2_initiate_order(
            restaurant_id=str(restaurant.pk),
            table_id=str(table.pk),
            items=items
        )
        print(response)
        self.assertEqual(response['status'], 200)
