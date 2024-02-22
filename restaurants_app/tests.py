from django.db import transaction
from django.test import TestCase
from dinify_backend.configs import MESSAGES, ROLES
from users_app.tests import TEST_PHONE, seed_user
from users_app.models import User
from restaurants_app.controllers.create_restaurant import create_restaurant
from restaurants_app.models import Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table


TEST_RESTAURANT_NAME = 'Seed Test Restaurant'
TEST_MENU_SECTION_NAME = 'Seed Test Menu Section'
TEST_MENU_ITEM1_NAME = 'Seed Test Menu Item1'
TEST_MENU_ITEM2_NAME = 'Seed Test Menu Item2'
TEST_MENU_ITEM3_NAME = 'Seed Test Menu Item3'
TEST_MENU_ITEM4_NAME = 'Seed Test Menu Item4'
TEST_MENU_ITEM5_NAME = 'Seed Test Menu Item5'
TEST_UNAVAILABLE_MENU_ITEM_NAME = 'Seed Unavailable Test Menu Item'
TEST_DISCOUNTED_MENU_ITEM_NAME = 'Seed Test Discounted Menu Item'
TEST_TABLE_NUMBER1 = 1
TEST_TABLE_NUMBER2 = 2


def seed_restaurant(seed_owner=True):
    """
    seed the restaurant
    """
    owner = User.objects.get(username=TEST_PHONE)
    with transaction.atomic():
        restaurant = Restaurant.objects.create(
            name=TEST_RESTAURANT_NAME,
            location='Seed Test location',
            owner=owner
        )
        if seed_owner:
            RestaurantEmployee.objects.create(
                user=owner,
                restaurant=restaurant,
                roles=[ROLES.get('RESTAURANT_OWNER')]
            )


def seed_menu_section():
    """
    seed the menu section
    """
    restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    MenuSection.objects.create(
        name=TEST_MENU_SECTION_NAME,
        restaurant=restaurant
    )


def seed_menu_items():
    """
    seed menu items to use
    """
    menu_section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)
    # bulk create menu items
    menu_items = [
        MenuItem(name=TEST_MENU_ITEM1_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False),  # noqa
        MenuItem(name=TEST_MENU_ITEM2_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False),  # noqa
        MenuItem(name=TEST_MENU_ITEM3_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False),  # noqa
        MenuItem(name=TEST_MENU_ITEM4_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False),  # noqa
        MenuItem(name=TEST_MENU_ITEM5_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False),  # noqa
        MenuItem(name=TEST_UNAVAILABLE_MENU_ITEM_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=False, available=False),  # noqa
        MenuItem(name=TEST_DISCOUNTED_MENU_ITEM_NAME, section=menu_section, primary_price=1000.0, discounted_price=900.0, running_discount=True),  # noqa
    ]
    MenuItem.objects.bulk_create(menu_items)


def seed_tables():
    """
    seed the table
    """
    restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    Table.objects.create(
        number=TEST_TABLE_NUMBER1,
        restaurant=restaurant,
        prepayment_required=False
    )
    Table.objects.create(
        number=TEST_TABLE_NUMBER2,
        restaurant=restaurant,
        prepayment_required=True
    )


# Create your tests here.
class RestaurantAppTestFunctions(TestCase):
    """
    test the functions for restaurant app
    """

    def setUp(self) -> None:
        """
        set up for the tests
        """
        seed_user()
        seed_restaurant()

    def test_create_restaurant(self):
        """
        test the restaurant self register function
        """
        user_id = str(User.objects.get(username=TEST_PHONE).pk)
        auth_info = {
            'user_id': user_id
        }

        def test_missing_info():
            data = {
                'name': 'Test Restaurant',
                'owner': user_id
            }
            result = create_restaurant(data, auth_info)
            self.assertEqual(result['status'], 400)

        def test_ok():
            data = {
                'name': 'Test Restaurant',
                'location': 'Test location',
                'owner': user_id
            }
            result = create_restaurant(data, auth_info)
            self.assertEqual(result['status'], 200)
            self.assertEqual(result['message'], MESSAGES.get('OK_CREATE_RESTAURANT'))

        test_missing_info()
        test_ok()
