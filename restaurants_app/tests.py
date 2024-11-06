from cgi import test
from django.db import transaction
from django.test import TestCase
from dinify_backend.configs import ROLES
from dinify_backend.configss.messages import MESSAGES
from users_app.tests import TEST_PHONE, seed_user
from users_app.models import User
from restaurants_app.controllers.create_restaurant import (
    create_restaurant, admin_register_restaurant
)
from restaurants_app.controllers.create_employee import create_employee
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
TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME = 'Seed Test Extra Discounted Menu Item'
TEST_OPTION_MENU_ITEM_NAME = 'Seed Test Options Menu Item'
TEST_TABLE_NUMBER1 = 1
TEST_TABLE_NUMBER2 = 2
TEST_TABLE_NUMBER3 = 3
TEST_TABLE_NUMBER4 = 4


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
        MenuItem(
            name=TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME,
            section=menu_section,
            primary_price=1000.0,
            discounted_price=900.0,
            running_discount=True,
            consider_discount_object=True,
            discount_details={
                'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '2021-01-01',
                'end_date': '2030-12-31',
                'start_time': '00:00',
                'end_time': '23:59',
                'discount_percentage': 20.0,
                'discount_amount': 0.0
            }
        ),  # noqa
        MenuItem(
            name=TEST_OPTION_MENU_ITEM_NAME,
            section=menu_section,
            primary_price=1000.0,
            discounted_price=900.0,
            running_discount=True,
            options={
                'min_selections': 1,
                'max_selections': 3,
                'options': [
                    {
                        'name': 'Option 1',
                        'selectable': True,
                        'choices': ['oo1', 'oo2', 'oo3'],
                        'cost': 1100
                    }
                ]
            }
        ),
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
    Table.objects.create(
        number=TEST_TABLE_NUMBER3,
        restaurant=restaurant,
        prepayment_required=True
    )
    Table.objects.create(
        number=TEST_TABLE_NUMBER4,
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
            'user_id': user_id,
            'first_name': 'First',
            'email': 'dummy@email.com'
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

        def test_create_employee():
            restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
            result = create_employee(
                first_name='Test',
                last_name='Employee',
                email='dummy@email.com',
                phone_number='256777777777',
                restaurant=restaurant,
                roles=[ROLES.get('RESTAURANT_KITCHEN')],
                creator=restaurant.owner
            )
            self.assertEqual(result['status'], 200)

        test_missing_info()
        test_ok()
        test_create_employee()

    def test_admin_register_restaurant(self):
        user_id = str(User.objects.get(username=TEST_PHONE).pk)
        auth_info = {
            'user_id': user_id,
            'first_name': 'First',
            'email': 'dummy@email.com'
        }

        data = {
            'name': 'Test Restaurant',
            'location': 'Test location',

            'first_name': 'Test',
            'last_name': 'Owner',
            'email': 'sample@org.org',
            'phone_number': '256777777777',
            'country': 'UG',
        }

        result = admin_register_restaurant(data, auth_info)
        print(f'admin result: {result}')
        self.assertEqual(result['status'], 200)


