from cgi import test
from django.db import transaction
from django.test import TestCase
from dinify_backend.configs import ROLES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import RestaurantStatus_Active
from misc_app.controllers.secretary import Secretary
from users_app.tests import TEST_PHONE, seed_user
from users_app.models import User
from restaurants_app.controllers.create_restaurant import (
    create_restaurant, admin_register_restaurant
)
from restaurants_app.controllers.create_employee import create_employee
from restaurants_app.controllers.menu_sections import ConMenuSection
from restaurants_app.endpoints.restaurant_setup import normalize_ordered_section_ids
from restaurants_app.models import Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table
from users_app.controllers.otp_manager import OtpManager


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

TEST_OPTION_GROUP_ID = 'grp-size'
TEST_OPTION_CHOICE_SMALL_ID = 'choice-small'
TEST_OPTION_CHOICE_LARGE_ID = 'choice-large'
TEST_OPTION_CHOICE_SMALL_COST = 1100
TEST_OPTION_CHOICE_LARGE_COST = 1500


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
                'hasModifiers': True,
                'groups': [
                    {
                        'id': TEST_OPTION_GROUP_ID,
                        'name': 'Size',
                        'required': True,
                        'selectionType': 'single',
                        'minSelections': 1,
                        'maxSelections': 1,
                        'choices': [
                            {
                                'id': TEST_OPTION_CHOICE_SMALL_ID,
                                'name': 'Small',
                                'additionalCost': TEST_OPTION_CHOICE_SMALL_COST,
                                'available': True
                            },
                            {
                                'id': TEST_OPTION_CHOICE_LARGE_ID,
                                'name': 'Large',
                                'additionalCost': TEST_OPTION_CHOICE_LARGE_COST,
                                'available': True
                            }
                        ]
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
                creator=restaurant.owner,
                skip_otp=True
            )
            print(f'employee result: {result}')
            self.assertEqual(result['status'], 200)

            print('editing the employee')
            employee = RestaurantEmployee.objects.get(
                user__phone_number='256777777777',
                restaurant=restaurant
            )
            # secretary_args = {
            #     'serializer': serializer,
            #     'data': put_data,
            #     'edit_considerations': EDIT_INFORMATION.get('restaurant_employee'),
            #     'user_id': auth['id'],
            #     'username': auth['username'],
            #     'success_message': success_message,
            #     'error_message': error_message
            # }
            
            # Secretary



        test_missing_info()
        test_ok()
        test_create_employee()

    def test_admin_register_restaurant(self):
        user = User.objects.get(username=TEST_PHONE)
        user_id = str(user.pk)
        # get the otp for the user
        # OtpManager().make_otp(user=user)
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
            # 'otp': '1234'
        }

        result = admin_register_restaurant(data, auth_info)
        print(f'admin result: {result}')
        self.assertEqual(result['status'], 200)


def _seed_two_sections(restaurant):
    """Helper: seed two sections at positions 0 and 1, return them."""
    s1 = MenuSection.objects.create(
        name='Starters', restaurant=restaurant, listing_position=0,
    )
    s2 = MenuSection.objects.create(
        name='Mains', restaurant=restaurant, listing_position=1,
    )
    return s1, s2


class MenuSectionReorderTests(TestCase):
    """Tests for ConMenuSection.reorder_listing and the dispatch slug."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

    def test_reorder_flips_two_sections(self):
        s1, s2 = _seed_two_sections(self.restaurant)
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s2.id), str(s1.id)],
            user=self.admin_user,
        )
        self.assertEqual(result['status'], 200)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s2.listing_position, 0)
        self.assertEqual(s1.listing_position, 1)

    def test_reorder_rejects_cross_restaurant(self):
        s1, _ = _seed_two_sections(self.restaurant)
        other_restaurant = Restaurant.objects.create(
            name='Other Restaurant',
            location='elsewhere',
            owner=self.admin_user,
        )
        other_section = MenuSection.objects.create(
            name='Drinks', restaurant=other_restaurant, listing_position=0,
        )
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s1.id), str(other_section.id)],
            user=self.admin_user,
        )
        self.assertEqual(result['status'], 400)
        self.assertIn('same restaurant', result['message'])

    def test_reorder_rejects_unknown_id(self):
        s1, _ = _seed_two_sections(self.restaurant)
        bogus_id = '00000000-0000-0000-0000-000000000000'
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s1.id), bogus_id],
            user=self.admin_user,
        )
        self.assertEqual(result['status'], 400)

    def test_reorder_rejects_empty_list(self):
        result = ConMenuSection().reorder_listing(
            ordered_ids=[],
            user=self.admin_user,
        )
        self.assertEqual(result['status'], 400)

    def test_reorder_rejects_duplicate_ids(self):
        s1, s2 = _seed_two_sections(self.restaurant)
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s1.id), str(s2.id), str(s1.id)],
            user=self.admin_user,
        )
        self.assertEqual(result['status'], 400)
        self.assertIn('duplicate', result['message'])

    def test_reorder_rejects_unauthorized_user(self):
        # User who is neither dinify admin nor owner/manager of the restaurant.
        outsider = User.objects.create_user(
            first_name='No', last_name='Access',
            email='outsider@test.com', phone_number='256700000001',
            username='256700000001', country='Uganda', password='password',
            roles=['diner'],
        )
        s1, s2 = _seed_two_sections(self.restaurant)
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s2.id), str(s1.id)],
            user=outsider,
        )
        self.assertEqual(result['status'], 403)

    def test_reorder_allows_owner_role(self):
        # Activate the seeded restaurant so role-lookup matches the
        # `restaurant__status__in=['active']` filter in get_user_restaurant_roles.
        self.restaurant.status = RestaurantStatus_Active
        self.restaurant.save(update_fields=['status'])
        owner = User.objects.create_user(
            first_name='Restaurant', last_name='Owner',
            email='owner@test.com', phone_number='256700000002',
            username='256700000002', country='Uganda', password='password',
            roles=['diner'],
        )
        RestaurantEmployee.objects.create(
            user=owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        s1, s2 = _seed_two_sections(self.restaurant)
        result = ConMenuSection().reorder_listing(
            ordered_ids=[str(s2.id), str(s1.id)],
            user=owner,
        )
        self.assertEqual(result['status'], 200)


class NormalizeOrderedSectionIdsTests(TestCase):
    """Unit tests for the legacy/new shape resolver used by the dispatch."""

    def test_new_shape_passes_through(self):
        ids = ['a', 'b', 'c']
        self.assertEqual(
            normalize_ordered_section_ids({'ordered_ids': ids}),
            ids,
        )

    def test_legacy_dict_shape_extracted(self):
        legacy = {
            'ordering': [
                {'id': 'a', 'listing_position': 0},
                {'id': 'b', 'listing_position': 1},
            ],
        }
        self.assertEqual(
            normalize_ordered_section_ids(legacy),
            ['a', 'b'],
        )

    def test_legacy_string_shape_passes_through(self):
        self.assertEqual(
            normalize_ordered_section_ids({'ordering': ['a', 'b']}),
            ['a', 'b'],
        )

    def test_no_payload_returns_none(self):
        self.assertIsNone(normalize_ordered_section_ids({}))


class NewSectionListingPositionTests(TestCase):
    """Tests that a newly-POSTed section gets a clean listing_position."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

    def _post_section(self, name):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.admin_user).access_token)
        return self.client.post(
            f'/api/v1/restaurant-setup/menusections/',
            data={'name': name, 'restaurant': str(self.restaurant.id)},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_new_section_assigned_next_position(self):
        MenuSection.objects.create(
            name='First', restaurant=self.restaurant, listing_position=0,
        )
        MenuSection.objects.create(
            name='Second', restaurant=self.restaurant, listing_position=1,
        )
        response = self._post_section('Drinks')
        self.assertEqual(response.status_code, 200)
        created = MenuSection.objects.get(name='Drinks', restaurant=self.restaurant)
        self.assertEqual(created.listing_position, 2)

    def test_first_section_assigned_position_zero(self):
        response = self._post_section('Brunch')
        self.assertEqual(response.status_code, 200)
        created = MenuSection.objects.get(name='Brunch', restaurant=self.restaurant)
        self.assertEqual(created.listing_position, 0)
