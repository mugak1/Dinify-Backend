from django.test import TestCase
from orders_app.controllers.order import Order
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_MENU_SECTION_NAME,
    TEST_MENU_ITEM1_NAME, TEST_MENU_ITEM2_NAME,
    TEST_MENU_ITEM3_NAME, TEST_MENU_ITEM4_NAME,
    TEST_MENU_ITEM5_NAME, TEST_UNAVAILABLE_MENU_ITEM_NAME,
    TEST_DISCOUNTED_MENU_ITEM_NAME, TEST_TABLE_NUMBER1, TEST_TABLE_NUMBER2
)
from restaurants_app.models import Restaurant, Table, MenuItem


# Create your tests here.
class TestOrderFunctions(TestCase):
    """
    test cases for the order functions
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()

    def test_initiate_order(self):
        """
        test initiating an order
        """
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table1 = Table.objects.get(number=TEST_TABLE_NUMBER1)
        table2 = Table.objects.get(number=TEST_TABLE_NUMBER2)
        user = User.objects.get(username=TEST_PHONE)

        def test_post_paid_initiate():
            data = {
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
            result = Order(data).initiate_order()
            print("\n===\n", result, "\n===")

        test_post_paid_initiate()
