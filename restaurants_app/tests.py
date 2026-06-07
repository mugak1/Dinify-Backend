import json
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
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea,
)
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
            # discounted_price must match the canonical shape: 20% off 1000 = 800.
            # Post-0042 the migration recomputes discounted_price from
            # discount_details, so seed values that drifted (e.g. 900) are not
            # representative of production data.
            discounted_price=800.0,
            running_discount=True,
            consider_discount_object=True,
            # CANONICAL discount_details schema (must match restaurants_app/models.py:234-242):
            #   discount_type:        'percentage' | 'fixed'
            #   discount_percentage:  0..100 (subtract this % of primary_price)
            #   discount_amount:      UGX amount to subtract from primary_price
            #   recurring_days:       list[int 1..7] ISO weekday filter
            #   start_date/end_date:  'YYYY-MM-DD' strings, '' for unbounded
            #   start_time/end_time:  'HH:MM' strings, '' for unbounded
            # Never add raw_discount_value / raw_discount_type — those are the
            # pre-0042 buggy schema and were migrated out of all production rows.
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


class NewMenuItemListingPositionTests(TestCase):
    """Tests that a newly-POSTed menu item gets a clean listing_position
    relative to the section it's added to."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _post_item(self, name, section_id=None):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.admin_user).access_token)
        return self.client.post(
            f'/api/v1/restaurant-setup/menuitems/',
            data={
                'name': name,
                'section': str(section_id or self.section.id),
                'primary_price': '1000.00',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_first_item_in_section_gets_position_zero(self):
        response = self._post_item('First Item')
        self.assertEqual(response.status_code, 200)
        created = MenuItem.objects.get(name='First Item', section=self.section)
        self.assertEqual(created.listing_position, 0)

    def test_subsequent_items_assigned_next_position(self):
        MenuItem.objects.create(
            name='Existing 1', section=self.section,
            primary_price=1000, listing_position=0,
        )
        MenuItem.objects.create(
            name='Existing 2', section=self.section,
            primary_price=1000, listing_position=1,
        )
        response = self._post_item('Newcomer')
        self.assertEqual(response.status_code, 200)
        created = MenuItem.objects.get(name='Newcomer', section=self.section)
        self.assertEqual(created.listing_position, 2)

    def test_position_is_per_section_not_per_restaurant(self):
        """A new item in section B should get position 0 even if section A
        has items at higher positions."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        section_b = MenuSection.objects.create(
            name='Second Section', restaurant=restaurant,
        )
        MenuItem.objects.create(
            name='A Item 1', section=self.section,
            primary_price=1000, listing_position=0,
        )
        MenuItem.objects.create(
            name='A Item 2', section=self.section,
            primary_price=1000, listing_position=1,
        )
        response = self._post_item('B First Item', section_id=section_b.id)
        self.assertEqual(response.status_code, 200)
        created = MenuItem.objects.get(name='B First Item', section=section_b)
        self.assertEqual(created.listing_position, 0)


class MenuItemReorderTests(TestCase):
    """Tests for ConMenuItem.reorder_listing — the item-reorder controller."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

        # Three items in the section, positions 0/1/2.
        self.item_a = MenuItem.objects.create(
            name='Alpha', section=self.section,
            primary_price=1000, listing_position=0,
        )
        self.item_b = MenuItem.objects.create(
            name='Bravo', section=self.section,
            primary_price=1000, listing_position=1,
        )
        self.item_c = MenuItem.objects.create(
            name='Charlie', section=self.section,
            primary_price=1000, listing_position=2,
        )

    def _put_reorder(self, section_id, ordered_ids, user=None):
        from rest_framework_simplejwt.tokens import RefreshToken
        actor = user or self.admin_user
        token = str(RefreshToken.for_user(actor).access_token)
        return self.client.put(
            f'/api/v1/restaurant-setup/reorder-section-items/',
            data={'section_id': str(section_id), 'ordered_ids': ordered_ids},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_reorder_flips_two_items(self):
        # Swap A and C: target order [C, B, A]
        ordered = [str(self.item_c.id), str(self.item_b.id), str(self.item_a.id)]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 200)
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.item_c.refresh_from_db()
        self.assertEqual(self.item_c.listing_position, 0)
        self.assertEqual(self.item_b.listing_position, 1)
        self.assertEqual(self.item_a.listing_position, 2)

    def test_reorder_rejects_cross_section(self):
        # Create another section with one item; include its id in our reorder.
        other_section = MenuSection.objects.create(
            name='Other', restaurant=self.restaurant,
        )
        other_item = MenuItem.objects.create(
            name='Other Item', section=other_section,
            primary_price=1000, listing_position=0,
        )
        ordered = [
            str(self.item_a.id),
            str(self.item_b.id),
            str(self.item_c.id),
            str(other_item.id),  # belongs to a different section
        ]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 400)
        # Item C should still have its original position.
        self.item_c.refresh_from_db()
        self.assertEqual(self.item_c.listing_position, 2)

    def test_reorder_rejects_unknown_item_id(self):
        bogus = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        ordered = [str(self.item_a.id), str(self.item_b.id), bogus]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 400)

    def test_reorder_rejects_unknown_section_id(self):
        bogus = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        ordered = [str(self.item_a.id), str(self.item_b.id), str(self.item_c.id)]
        response = self._put_reorder(bogus, ordered)
        self.assertEqual(response.status_code, 404)

    def test_reorder_rejects_empty_list(self):
        response = self._put_reorder(self.section.id, [])
        self.assertEqual(response.status_code, 400)

    def test_reorder_rejects_duplicate_ids(self):
        ordered = [str(self.item_a.id), str(self.item_b.id), str(self.item_a.id)]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 400)

    def test_reorder_rejects_partial_set(self):
        # Section has three items; sending only two is a partial reorder.
        ordered = [str(self.item_a.id), str(self.item_b.id)]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 400)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.listing_position, 0)  # unchanged

    def test_reorder_rejects_unauthorized_user(self):
        # Create a user with no restaurant role.
        outsider = User.objects.create(
            username='outsider_phone', first_name='Out', last_name='Sider',
        )
        ordered = [str(self.item_c.id), str(self.item_b.id), str(self.item_a.id)]
        response = self._put_reorder(self.section.id, ordered, user=outsider)
        # check_permission gate at the dispatch level rejects users without
        # owner/manager roles on any restaurant — returns 403.
        self.assertEqual(response.status_code, 403)
        self.item_c.refresh_from_db()
        self.assertEqual(self.item_c.listing_position, 2)  # unchanged

    def test_reorder_allows_owner_role(self):
        # The admin user seeded by seed_restaurant has the OWNER role.
        # Just a sanity check that owner-roled users succeed end-to-end.
        ordered = [str(self.item_b.id), str(self.item_a.id), str(self.item_c.id)]
        response = self._put_reorder(self.section.id, ordered)
        self.assertEqual(response.status_code, 200)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.listing_position, 0)


class MenuItemSortModeTests(TestCase):
    """Tests for the per-restaurant menu item sort mode: the
    menu-item-sort-mode config_detail (GET/PUT via ConMenuItemSortMode) and
    its surfacing on the diner show-menu. The backend only stores the mode;
    it never re-sorts items."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.admin_user = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

    def _get_mode(self, restaurant_id, user=None):
        from rest_framework_simplejwt.tokens import RefreshToken
        actor = user or self.admin_user
        token = str(RefreshToken.for_user(actor).access_token)
        return self.client.get(
            f'/api/v1/restaurant-setup/menu-item-sort-mode/',
            data={'restaurant': str(restaurant_id)},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def _put_mode(self, restaurant_id, mode, user=None):
        from rest_framework_simplejwt.tokens import RefreshToken
        actor = user or self.admin_user
        token = str(RefreshToken.for_user(actor).access_token)
        return self.client.put(
            f'/api/v1/restaurant-setup/menu-item-sort-mode/',
            data={'restaurant': str(restaurant_id), 'mode': mode},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_default_mode_is_manual(self):
        # A freshly seeded restaurant inherits the model default.
        self.assertEqual(self.restaurant.menu_item_sort_mode, 'manual')

    def test_put_persists_valid_mode(self):
        response = self._put_mode(self.restaurant.id, 'a-z')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get('item_sort_mode'), 'a-z')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.menu_item_sort_mode, 'a-z')

    def test_put_accepts_all_four_modes(self):
        for mode in ('manual', 'a-z', 'price-low', 'price-high'):
            response = self._put_mode(self.restaurant.id, mode)
            self.assertEqual(response.status_code, 200)
            self.restaurant.refresh_from_db()
            self.assertEqual(self.restaurant.menu_item_sort_mode, mode)

    def test_put_rejects_invalid_mode(self):
        response = self._put_mode(self.restaurant.id, 'banana')
        self.assertEqual(response.status_code, 400)
        self.restaurant.refresh_from_db()
        # The bad write must not have touched the stored value.
        self.assertEqual(self.restaurant.menu_item_sort_mode, 'manual')

    def test_get_returns_current_mode(self):
        self.restaurant.menu_item_sort_mode = 'price-low'
        self.restaurant.save(update_fields=['menu_item_sort_mode'])
        response = self._get_mode(self.restaurant.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get('item_sort_mode'), 'price-low')

    def test_get_defaults_to_manual(self):
        response = self._get_mode(self.restaurant.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get('item_sort_mode'), 'manual')

    def test_outsider_cannot_write_mode(self):
        # A user with no owner/manager role on the restaurant is denied.
        outsider = User.objects.create(
            username='outsider_phone', first_name='Out', last_name='Sider',
        )
        response = self._put_mode(self.restaurant.id, 'a-z', user=outsider)
        self.assertEqual(response.status_code, 403)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.menu_item_sort_mode, 'manual')

    def test_outsider_cannot_read_mode(self):
        outsider = User.objects.create(
            username='outsider_phone', first_name='Out', last_name='Sider',
        )
        response = self._get_mode(self.restaurant.id, user=outsider)
        self.assertEqual(response.status_code, 403)

    def test_show_menu_includes_item_sort_mode(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_menu,
        )
        self.restaurant.menu_item_sort_mode = 'price-high'
        self.restaurant.save(update_fields=['menu_item_sort_mode'])
        response = handle_show_menu(str(self.restaurant.id), 'false')
        self.assertEqual(response['status'], 200)
        self.assertIn('item_sort_mode', response)
        self.assertEqual(response['item_sort_mode'], 'price-high')

    def test_show_menu_defaults_to_manual(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_menu,
        )
        response = handle_show_menu(str(self.restaurant.id), 'false')
        self.assertEqual(response.get('item_sort_mode'), 'manual')


class MenuItemDiscountMathTests(TestCase):
    """Regression tests for the canonical discount_details schema and the
    order pipeline's effective-unit-price calculation."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _make_item(self, name, primary, discount_details, discounted_price):
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            primary_price=primary,
            discounted_price=discounted_price,
            running_discount=True,
            consider_discount_object=True,
            discount_details=discount_details,
        )

    @staticmethod
    def _always_active_temporal():
        # recurring_days covers every weekday and the date/time fields are
        # left blank so the temporal gates in con_orders.py don't fire.
        return {
            'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '',
            'end_date': '',
            'start_time': '',
            'end_time': '',
        }

    def test_percentage_discount_serializer_and_pipeline(self):
        from decimal import Decimal
        from orders_app.controllers.con_orders import ConOrder
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        details = {
            'discount_type': 'percentage',
            'discount_percentage': 20.0,
            'discount_amount': 0.0,
            **self._always_active_temporal(),
        }
        item = self._make_item('Pct Item', Decimal('10000'), details, Decimal('8000.00'))

        data = SerializerPublicGetMenuItem(item).data
        self.assertEqual(data['discount_percentage'], 20.0)

        result = ConOrder.determine_effective_unit_price(item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

    def test_fixed_discount_serializer_and_pipeline(self):
        from decimal import Decimal
        from orders_app.controllers.con_orders import ConOrder
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        details = {
            'discount_type': 'fixed',
            'discount_percentage': 0.0,
            'discount_amount': 2000.0,
            **self._always_active_temporal(),
        }
        item = self._make_item('Fixed Item', Decimal('10000'), details, Decimal('8000.00'))

        data = SerializerPublicGetMenuItem(item).data
        self.assertEqual(data['discount_percentage'], 20.0)

        result = ConOrder.determine_effective_unit_price(item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

    def test_no_discount_returns_primary_price(self):
        from decimal import Decimal
        from orders_app.controllers.con_orders import ConOrder
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        item = MenuItem.objects.create(
            name='Plain Item',
            section=self.section,
            primary_price=Decimal('5000'),
            discounted_price=None,
            running_discount=False,
            consider_discount_object=False,
            discount_details={},
        )

        data = SerializerPublicGetMenuItem(item).data
        self.assertEqual(data['discount_percentage'], 0)

        result = ConOrder.determine_effective_unit_price(item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('5000.00'))

    def test_canonical_shape_has_no_raw_keys(self):
        # Regression guard: the canonical schema must never include
        # raw_discount_value or raw_discount_type. The pre-0042 buggy keys
        # are migrated out and must not be reintroduced.
        from decimal import Decimal

        details = {
            'discount_type': 'percentage',
            'discount_percentage': 15.0,
            'discount_amount': 0.0,
            **self._always_active_temporal(),
        }
        item = self._make_item('Guard Item', Decimal('10000'), details, Decimal('8500.00'))
        item.refresh_from_db()
        self.assertNotIn('raw_discount_value', item.discount_details)
        self.assertNotIn('raw_discount_type', item.discount_details)


class TenantIsolationTests(TestCase):
    """
    Cross-restaurant authorization tests for the RestaurantSetupEndpoint
    permission gate. Owner of restaurant A must NOT be able to mutate
    resources belonging to restaurant B; only dinify admins or active
    owner/manager employees of the *target* restaurant may write.

    Headline security property: update/delete resolvers walk FK chains
    server-side from the record's id and ignore any client-supplied
    `restaurant` field. Spoofing `{id: <victim's record>, restaurant:
    <attacker's own>}` must not authorize.
    """

    def setUp(self):
        from rest_framework_simplejwt.tokens import RefreshToken  # noqa: F401
        # Two unrelated restaurants, two unrelated owners.
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='owner_a@test.com', phone_number='256700000010',
            username='256700000010', country='Uganda', password='password',
            roles=[],  # not a dinify admin
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        self.employment_a = RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='owner_b@test.com', phone_number='256700000020',
            username='256700000020', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Resources living in restaurant B that owner_a may try to attack.
        self.section_b = MenuSection.objects.create(
            name='B Mains', restaurant=self.restaurant_b, listing_position=0,
        )
        self.item_b = MenuItem.objects.create(
            name='B Item', section=self.section_b, primary_price=1000,
            listing_position=0,
        )
        self.group_b = SectionGroup.objects.create(
            name='B Group', section=self.section_b,
        )
        self.dining_area_b = DiningArea.objects.create(
            name='B Patio', restaurant=self.restaurant_b,
        )
        self.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_b,
        )
        self.section_a = MenuSection.objects.create(
            name='A Mains', restaurant=self.restaurant_a, listing_position=0,
        )

        # Outsider with zero employments.
        self.outsider = User.objects.create_user(
            first_name='Out', last_name='Sider',
            email='outsider@test.com', phone_number='256700000030',
            username='256700000030', country='Uganda', password='password',
            roles=[],
        )

        # Independent dinify admin (no employments at either restaurant).
        self.dinify_admin = User.objects.create_user(
            first_name='Dinify', last_name='Admin',
            email='admin@test.com', phone_number='256700000040',
            username='256700000040', country='Uganda', password='password',
            roles=['dinify_admin'],
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _request(self, user, method, config_detail, body):
        path = f'/api/v1/restaurant-setup/{config_detail}/'
        token = self._token_for(user)
        kwargs = {
            'data': body,
            'content_type': 'application/json',
            'HTTP_AUTHORIZATION': f'Bearer {token}',
        }
        return getattr(self.client, method)(path, **kwargs)

    # -- admin bypass --------------------------------------------------------

    def test_dinify_admin_can_create_in_any_restaurant(self):
        # Admin has no employment at restaurant B but should still be allowed.
        response = self._request(
            self.dinify_admin, 'post', 'menusections',
            {'name': 'Admin Section', 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(MenuSection.objects.filter(
            name='Admin Section', restaurant=self.restaurant_b,
        ).exists())

    def test_inactive_dinify_admin_is_denied(self):
        self.dinify_admin.is_active = False
        self.dinify_admin.save(update_fields=['is_active'])
        response = self._request(
            self.dinify_admin, 'post', 'menusections',
            {'name': 'Should Fail', 'restaurant': str(self.restaurant_b.id)},
        )
        # JWT auth itself rejects inactive users; the contract is "not 200".
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(MenuSection.objects.filter(name='Should Fail').exists())

    # -- happy path ----------------------------------------------------------

    def test_owner_can_update_own_menusection(self):
        response = self._request(
            self.owner_a, 'put', 'menusections',
            {'id': str(self.section_a.id), 'name': 'A Mains Renamed',
             'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 200)
        self.section_a.refresh_from_db()
        self.assertEqual(self.section_a.name, 'A Mains Renamed')

    # -- cross-restaurant rejection: menusections ----------------------------

    def test_owner_of_a_cannot_create_menusection_in_b(self):
        response = self._request(
            self.owner_a, 'post', 'menusections',
            {'name': 'Hostile', 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MenuSection.objects.filter(name='Hostile').exists())

    def test_owner_of_a_cannot_update_menusection_in_b_via_spoof(self):
        # Spoof payload: id points at B's section, restaurant points at A.
        # Resolver must walk FK from id to detect the real target is B.
        original_name = self.section_b.name
        response = self._request(
            self.owner_a, 'put', 'menusections',
            {'id': str(self.section_b.id), 'name': 'pwned',
             'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.section_b.refresh_from_db()
        self.assertEqual(self.section_b.name, original_name)

    def test_owner_of_a_cannot_delete_menusection_in_b(self):
        response = self._request(
            self.owner_a, 'delete', 'menusections',
            {'id': str(self.section_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.section_b.refresh_from_db()
        self.assertFalse(self.section_b.deleted)

    # -- cross-restaurant rejection: menuitems (two-hop FK) ------------------

    def test_owner_of_a_cannot_create_menuitem_in_b(self):
        response = self._request(
            self.owner_a, 'post', 'menuitems',
            {'name': 'Hostile Item', 'section': str(self.section_b.id),
             'primary_price': '1000.00'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MenuItem.objects.filter(name='Hostile Item').exists())

    def test_owner_of_a_cannot_update_menuitem_in_b_via_spoof(self):
        original_name = self.item_b.name
        response = self._request(
            self.owner_a, 'put', 'menuitems',
            {'id': str(self.item_b.id), 'name': 'pwned',
             'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.name, original_name)

    def test_owner_of_a_cannot_delete_menuitem_in_b(self):
        response = self._request(
            self.owner_a, 'delete', 'menuitems',
            {'id': str(self.item_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.item_b.refresh_from_db()
        self.assertFalse(self.item_b.deleted)

    # -- cross-restaurant rejection: sectiongroups (two-hop FK) --------------

    def test_owner_of_a_cannot_create_sectiongroup_in_b(self):
        response = self._request(
            self.owner_a, 'post', 'sectiongroups',
            {'name': 'Hostile Group', 'section': str(self.section_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SectionGroup.objects.filter(name='Hostile Group').exists())

    def test_owner_of_a_cannot_delete_sectiongroup_in_b(self):
        response = self._request(
            self.owner_a, 'delete', 'sectiongroups',
            {'id': str(self.group_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.group_b.refresh_from_db()
        self.assertFalse(self.group_b.deleted)

    # -- cross-restaurant rejection: tables ----------------------------------

    def test_owner_of_a_cannot_create_table_in_b(self):
        response = self._request(
            self.owner_a, 'post', 'tables',
            {'number': 99, 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Table.objects.filter(
            number=99, restaurant=self.restaurant_b,
        ).exists())

    def test_owner_of_a_cannot_delete_table_in_b(self):
        response = self._request(
            self.owner_a, 'delete', 'tables',
            {'id': str(self.table_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.table_b.refresh_from_db()
        self.assertFalse(self.table_b.deleted)

    # -- cross-restaurant rejection: diningareas -----------------------------

    def test_owner_of_a_cannot_create_diningarea_in_b(self):
        response = self._request(
            self.owner_a, 'post', 'diningareas',
            {'name': 'Hostile Patio', 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DiningArea.objects.filter(name='Hostile Patio').exists())

    def test_owner_of_a_cannot_delete_diningarea_in_b(self):
        response = self._request(
            self.owner_a, 'delete', 'diningareas',
            {'id': str(self.dining_area_b.id)},
        )
        self.assertEqual(response.status_code, 403)
        self.dining_area_b.refresh_from_db()
        self.assertFalse(self.dining_area_b.deleted)

    # -- cross-restaurant rejection: employees -------------------------------

    def test_owner_of_a_cannot_create_employee_in_b_via_create_employee(self):
        response = self._request(
            self.owner_a, 'post', 'create-employee',
            {'first_name': 'Mole', 'last_name': 'Spy',
             'email': 'mole@test.com', 'phone_number': '256700000099',
             'restaurant': str(self.restaurant_b.id),
             'roles': [ROLES.get('RESTAURANT_KITCHEN')]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(phone_number='256700000099').exists())

    def test_owner_of_a_cannot_create_employee_in_b_via_employees_shortcut(self):
        # The /employees/ shortcut path bypassed the gate before the fix —
        # it now has its own check_permission call.
        response = self._request(
            self.owner_a, 'post', 'employees',
            {'user': str(self.owner_b.id),
             'restaurant': str(self.restaurant_b.id),
             'roles': [ROLES.get('RESTAURANT_MANAGER')]},
        )
        self.assertEqual(response.status_code, 403)
        # owner_b should still have only their original owner row.
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                user=self.owner_b, restaurant=self.restaurant_b,
            ).count(),
            1,
        )

    def test_owner_of_a_cannot_delete_employee_in_b(self):
        b_employee = RestaurantEmployee.objects.get(
            user=self.owner_b, restaurant=self.restaurant_b,
        )
        response = self._request(
            self.owner_a, 'delete', 'employees',
            {'id': str(b_employee.id)},
        )
        self.assertEqual(response.status_code, 403)
        b_employee.refresh_from_db()
        self.assertTrue(b_employee.active)

    # -- role gating ---------------------------------------------------------

    def test_outsider_with_no_employment_denied_for_all_resources(self):
        cases = [
            ('post', 'menusections',
             {'name': 'X', 'restaurant': str(self.restaurant_a.id)}),
            ('post', 'menuitems',
             {'name': 'X', 'section': str(self.section_a.id), 'primary_price': '1000'}),
            ('post', 'sectiongroups',
             {'name': 'X', 'section': str(self.section_a.id)}),
            ('post', 'tables',
             {'number': 50, 'restaurant': str(self.restaurant_a.id)}),
            ('post', 'diningareas',
             {'name': 'X', 'restaurant': str(self.restaurant_a.id)}),
            ('delete', 'menusections', {'id': str(self.section_a.id)}),
            ('delete', 'menuitems', {'id': str(self.item_b.id)}),
            ('delete', 'tables', {'id': str(self.table_b.id)}),
        ]
        for method, resource, body in cases:
            with self.subTest(method=method, resource=resource):
                response = self._request(self.outsider, method, resource, body)
                self.assertEqual(
                    response.status_code, 403,
                    f'Expected 403 for outsider {method} {resource}, got {response.status_code}',
                )

    def test_waiter_role_at_own_restaurant_denied(self):
        waiter = User.objects.create_user(
            first_name='W', last_name='aiter',
            email='waiter@test.com', phone_number='256700000050',
            username='256700000050', country='Uganda', password='password',
            roles=[],
        )
        RestaurantEmployee.objects.create(
            user=waiter, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_WAITER')],
        )
        response = self._request(
            waiter, 'post', 'menusections',
            {'name': 'Waiter section', 'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 403)

    def test_inactive_employee_record_denied(self):
        # Owner role at the right restaurant, but the employee row is inactive.
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        response = self._request(
            self.owner_a, 'post', 'menusections',
            {'name': 'Should Fail', 'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 403)

    def test_soft_deleted_employee_record_denied(self):
        self.employment_a.deleted = True
        self.employment_a.save(update_fields=['deleted'])
        response = self._request(
            self.owner_a, 'post', 'menusections',
            {'name': 'Should Fail', 'restaurant': str(self.restaurant_a.id)},
        )
        self.assertEqual(response.status_code, 403)

    # -- resolver failure modes (deny by default) ----------------------------

    def test_delete_with_unknown_record_id_returns_403(self):
        response = self._request(
            self.owner_a, 'delete', 'menuitems',
            {'id': '00000000-0000-0000-0000-000000000000'},
        )
        self.assertEqual(response.status_code, 403)

    def test_put_with_missing_id_returns_403(self):
        response = self._request(
            self.owner_a, 'put', 'menusections',
            {'name': 'no id here'},
        )
        self.assertEqual(response.status_code, 403)

    def test_post_menusection_missing_restaurant_returns_403(self):
        response = self._request(
            self.owner_a, 'post', 'menusections',
            {'name': 'no restaurant here'},
        )
        self.assertEqual(response.status_code, 403)

    def test_post_menuitem_missing_section_returns_403(self):
        response = self._request(
            self.owner_a, 'post', 'menuitems',
            {'name': 'no section here', 'primary_price': '1000'},
        )
        self.assertEqual(response.status_code, 403)


class TenantReadIsolationTests(TestCase):
    """
    Cross-restaurant READ isolation for the RestaurantSetupEndpoint GET.

    The analogue of TenantIsolationTests (which covers writes): owner of
    restaurant A must NOT be able to READ resources belonging to restaurant B
    by changing the ?restaurant= query param — covering tables, dining areas,
    menu items, employees (staff PII), menu sections, orders, and order
    reviews — while its own reads keep working and a dinify admin retains its
    legitimate cross-restaurant access.

    Headline property: the returned queryset is authoritatively bound to the
    caller's owner/manager restaurants server-side; the client ?restaurant=
    can only narrow within that set, never widen it.
    """

    def setUp(self):
        from orders_app.models import Order

        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='read_owner_a@test.com', phone_number='256700000110',
            username='256700000110', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Read Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        self.employment_a = RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='read_owner_b@test.com', phone_number='256700000120',
            username='256700000120', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Read Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        self.employment_b = RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Restaurant A's own resources (positive controls).
        self.section_a = MenuSection.objects.create(
            name='A Mains', restaurant=self.restaurant_a, listing_position=0,
        )
        self.item_a = MenuItem.objects.create(
            name='A Item', section=self.section_a, primary_price=1000,
            listing_position=0,
        )
        self.dining_area_a = DiningArea.objects.create(
            name='A Patio', restaurant=self.restaurant_a,
        )
        self.table_a = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_a,
        )

        # Restaurant B's resources (owner_a must never see these).
        self.section_b = MenuSection.objects.create(
            name='B Mains', restaurant=self.restaurant_b, listing_position=0,
        )
        self.item_b = MenuItem.objects.create(
            name='B Item', section=self.section_b, primary_price=1000,
            listing_position=0,
        )
        self.dining_area_b = DiningArea.objects.create(
            name='B Patio', restaurant=self.restaurant_b,
        )
        self.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_b,
        )
        # An order with a review at B — exercises orders + orderreviews.
        self.order_b = Order.objects.create(
            restaurant=self.restaurant_b, table=self.table_b,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            rating=5, review='B review',
        )

        # Independent dinify admin with no employment at either restaurant.
        self.dinify_admin = User.objects.create_user(
            first_name='Dinify', last_name='Admin',
            email='read_admin@test.com', phone_number='256700000140',
            username='256700000140', country='Uganda', password='password',
            roles=['dinify_admin'],
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _get(self, user, path):
        return self.client.get(
            path, HTTP_AUTHORIZATION=f'Bearer {self._token_for(user)}',
        )

    def _records(self, response):
        return response.json().get('data', {}).get('records', [])

    def _record_ids(self, response):
        return {str(r.get('id')) for r in self._records(response)}

    BASE = '/api/v1/restaurant-setup'

    # -- read isolation: owner_a must not read B via ?restaurant=<B> ----------

    def test_owner_of_a_cannot_read_b_tables(self):
        r = self._get(self.owner_a, f'{self.BASE}/tables/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.table_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_diningareas(self):
        r = self._get(self.owner_a, f'{self.BASE}/diningareas/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.dining_area_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_menuitems(self):
        r = self._get(self.owner_a, f'{self.BASE}/menuitems/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.item_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_menusections(self):
        r = self._get(self.owner_a, f'{self.BASE}/menusections/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.section_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_employees_pii(self):
        # Staff PII — the most sensitive cross-tenant read.
        r = self._get(self.owner_a, f'{self.BASE}/employees/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.employment_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_orders(self):
        r = self._get(self.owner_a, f'{self.BASE}/orders/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.order_b.id), self._record_ids(r))

    def test_owner_of_a_cannot_read_b_orderreviews_or_summary(self):
        r = self._get(self.owner_a, f'{self.BASE}/orderreviews/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(str(self.order_b.id), self._record_ids(r))
        # The ratings summary must not leak B's aggregates either.
        summary = r.json().get('data', {}).get('summary', {})
        self.assertEqual(summary.get('total_ratings'), 0)

    def test_no_restaurant_param_does_not_leak_other_tenants(self):
        # Omitting ?restaurant= previously returned every tenant's records.
        r = self._get(self.owner_a, f'{self.BASE}/menuitems/')
        self.assertEqual(r.status_code, 200)
        ids = self._record_ids(r)
        self.assertNotIn(str(self.item_b.id), ids)
        self.assertIn(str(self.item_a.id), ids)

    # -- positive controls: owner_a can still read its OWN resources ---------

    def test_owner_of_a_can_read_own_tables(self):
        r = self._get(self.owner_a, f'{self.BASE}/tables/?restaurant={self.restaurant_a.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.table_a.id), self._record_ids(r))

    def test_owner_of_a_can_read_own_diningareas(self):
        r = self._get(self.owner_a, f'{self.BASE}/diningareas/?restaurant={self.restaurant_a.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.dining_area_a.id), self._record_ids(r))

    def test_owner_of_a_can_read_own_menuitems(self):
        r = self._get(self.owner_a, f'{self.BASE}/menuitems/?restaurant={self.restaurant_a.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.item_a.id), self._record_ids(r))

    def test_owner_of_a_can_read_own_employees(self):
        r = self._get(self.owner_a, f'{self.BASE}/employees/?restaurant={self.restaurant_a.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.employment_a.id), self._record_ids(r))

    # -- single-record branches: 404 on cross-tenant, 200 on own -------------

    def test_owner_of_a_cannot_read_b_employee_detail(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/details/?record=employees&id={self.employment_b.id}',
        )
        self.assertEqual(r.status_code, 404)

    def test_owner_of_a_can_read_own_employee_detail(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/details/?record=employees&id={self.employment_a.id}',
        )
        self.assertEqual(r.status_code, 200)

    def test_owner_of_a_cannot_read_b_subscription_details(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/subscription-details/?restaurant={self.restaurant_b.id}',
        )
        self.assertEqual(r.status_code, 404)

    def test_owner_of_a_can_read_own_subscription_details(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/subscription-details/?restaurant={self.restaurant_a.id}',
        )
        self.assertEqual(r.status_code, 200)

    def test_owner_of_a_cannot_read_b_tables_grouping(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/tables/?grouping=area&restaurant={self.restaurant_b.id}',
        )
        self.assertEqual(r.status_code, 404)

    def test_owner_of_a_can_read_own_tables_grouping(self):
        r = self._get(
            self.owner_a,
            f'{self.BASE}/tables/?grouping=area&restaurant={self.restaurant_a.id}',
        )
        self.assertEqual(r.status_code, 200)

    # -- role-awareness: dinify admin retains cross-restaurant access ---------

    def test_dinify_admin_can_read_any_restaurant_tables(self):
        r = self._get(self.dinify_admin, f'{self.BASE}/tables/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(str(self.table_b.id), self._record_ids(r))

    def test_dinify_admin_can_read_any_restaurant_employee_detail(self):
        r = self._get(
            self.dinify_admin,
            f'{self.BASE}/details/?record=employees&id={self.employment_b.id}',
        )
        self.assertEqual(r.status_code, 200)


class MenuSectionScheduleTests(TestCase):
    """Tests for is_section_currently_active and the diner show-menu filter."""

    @staticmethod
    def _make_section(availability='scheduled', schedules=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            availability=availability,
            schedules=[] if schedules is None else schedules,
        )

    @staticmethod
    def _at(year, month, day, hour, minute):
        import pytz
        from datetime import datetime as real_dt
        return pytz.timezone('Africa/Nairobi').localize(
            real_dt(year, month, day, hour, minute)
        )

    # 2026-01-05 is Monday, 2026-01-06 Tuesday, ..., 2026-01-11 Sunday.

    def test_availability_always_returns_true(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(availability='always', schedules=[])
        self.assertTrue(is_section_currently_active(section))

    def test_scheduled_with_empty_schedules_returns_true(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(availability='scheduled', schedules=[])
        self.assertTrue(is_section_currently_active(section))

    def test_scheduled_in_window_active(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon', 'tue'], 'startTime': '07:00', 'endTime': '11:00'},
        ])
        # Monday 10:00 -> inside window.
        self.assertTrue(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 10, 0))
        )

    def test_scheduled_wrong_day_inactive(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon', 'tue'], 'startTime': '07:00', 'endTime': '11:00'},
        ])
        # Wednesday 10:00 -> day not in slot.
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 7, 10, 0))
        )

    def test_scheduled_in_day_outside_window_inactive(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon'], 'startTime': '07:00', 'endTime': '11:00'},
        ])
        # Monday 13:00 -> outside window.
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 13, 0))
        )

    def test_overnight_window_late_evening_active(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon'], 'startTime': '22:00', 'endTime': '02:00'},
        ])
        # Monday 23:30 -> past start of overnight window.
        self.assertTrue(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 23, 30))
        )

    def test_overnight_window_early_morning_active(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        # Slot anchored to Monday but with overnight 22:00-02:00 window.
        # The frontend considers Tuesday 01:00 still inside the Monday slot's
        # overnight tail. Backend mirrors the frontend by treating "current
        # day" as the slot day and accepting times before end_min.
        section = self._make_section(schedules=[
            {'days': ['tue'], 'startTime': '22:00', 'endTime': '02:00'},
        ])
        # Tuesday 01:00 -> before end of overnight window for the Tuesday slot.
        self.assertTrue(
            is_section_currently_active(section, now=self._at(2026, 1, 6, 1, 0))
        )

    def test_overnight_window_outside_inactive(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon'], 'startTime': '22:00', 'endTime': '02:00'},
        ])
        # Monday 05:00 -> outside the overnight window (after end, before start).
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 5, 0))
        )

    def test_malformed_days_string_instead_of_list_skipped(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': 'mon', 'startTime': '07:00', 'endTime': '11:00'},
        ])
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 10, 0))
        )

    def test_malformed_days_numeric_skipped(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        # Legacy int-day shape: 'mon' is not in [1, 2, 3] so slot is rejected.
        section = self._make_section(schedules=[
            {'days': [1, 2, 3], 'startTime': '07:00', 'endTime': '11:00'},
        ])
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 10, 0))
        )

    def test_malformed_time_string_skipped(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['mon'], 'startTime': '25:99', 'endTime': '11:00'},
        ])
        self.assertFalse(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 10, 0))
        )

    def test_multiple_slots_any_match_active(self):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active,
        )
        section = self._make_section(schedules=[
            {'days': ['tue'], 'startTime': '07:00', 'endTime': '11:00'},
            {'days': ['mon'], 'startTime': '12:00', 'endTime': '14:00'},
        ])
        # Monday 13:00 -> matches the second slot only.
        self.assertTrue(
            is_section_currently_active(section, now=self._at(2026, 1, 5, 13, 0))
        )

    def test_handle_show_menu_filters_inactive_scheduled_sections(self):
        from unittest.mock import patch
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_menu,
        )

        seed_user()
        seed_restaurant()
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

        always_section = MenuSection.objects.create(
            name='Always Section', restaurant=restaurant,
            availability='always', schedules=[],
            approved=True, enabled=True, available=True,
        )
        # Scheduled for Tuesday only; "now" will be Monday 10:00 -> inactive.
        MenuSection.objects.create(
            name='Scheduled Inactive', restaurant=restaurant,
            availability='scheduled',
            schedules=[
                {'days': ['tue'], 'startTime': '07:00', 'endTime': '11:00'},
            ],
            approved=True, enabled=True, available=True,
        )

        monday_10am = self._at(2026, 1, 5, 10, 0)
        with patch(
            'restaurants_app.controllers.utils.schedule_utils.datetime'
        ) as mock_datetime:
            mock_datetime.now.return_value = monday_10am
            response = handle_show_menu(str(restaurant.id), 'false')

        self.assertEqual(response['status'], 200)
        returned_ids = [section['id'] for section in response['data']]
        self.assertEqual(returned_ids, [str(always_section.id)])


class NullableFieldClearingDirectNullTests(TestCase):
    """
    Endpoint-level coverage of the post-fix contract: PUT payloads with
    explicit `null` values clear nullable scalar fields directly. The
    Bug 6 `clear_<field>: true` sentinel and its handler in
    RestaurantSetupEndpoint.put have been removed; Secretary now honours
    absent-vs-None semantics (misc_app/controllers/secretary.py).
    """

    def setUp(self):
        from decimal import Decimal
        self.owner = User.objects.create_user(
            first_name='Null', last_name='Clearer',
            email='nullclearer@test.com', phone_number='256700000050',
            username='256700000050', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Null Clearing Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, listing_position=0,
        )
        # Item starts with both clearable fields set so we can verify
        # they actually flip to None.
        self.item = MenuItem.objects.create(
            name='Burger', section=self.section,
            primary_price=Decimal('10.00'),
            calories=200,
            discounted_price=Decimal('5.00'),
            listing_position=0,
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _put(self, body):
        path = '/api/v1/restaurant-setup/menuitems/'
        token = self._token_for(self.owner)
        return self.client.put(
            path,
            data=body,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_clear_calories_via_null(self):
        response = self._put({
            'id': str(self.item.id),
            'calories': None,
        })
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.calories)

    def test_clear_discounted_price_via_null(self):
        response = self._put({
            'id': str(self.item.id),
            'discounted_price': None,
        })
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.discounted_price)

    def test_clearing_does_not_affect_other_fields(self):
        from decimal import Decimal
        original_name = self.item.name
        original_price = self.item.primary_price
        response = self._put({
            'id': str(self.item.id),
            'calories': None,
        })
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.calories)
        self.assertEqual(self.item.name, original_name)
        self.assertEqual(self.item.primary_price, Decimal(original_price))


class PresetTagsEndpointTests(TestCase):
    """
    Locks in the contract for PUT /api/v1/restaurant-setup/preset-tags/.
    The endpoint expects `tags` to be a native JSON array; a stringified
    array (the historical frontend bug) must be rejected with 400 so the
    bug cannot regress unnoticed.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Preset', last_name='Owner',
            email='preset_owner@test.com', phone_number='256700000060',
            username='256700000060', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Preset Tags Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _put(self, body):
        token = self._token_for(self.owner)
        return self.client.put(
            '/api/v1/restaurant-setup/preset-tags/',
            data=body,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def _tag(self, tid, name):
        return {
            'id': tid, 'name': name, 'icon': 'tag',
            'color': 'gray', 'filterable': True,
        }

    def test_put_with_native_array_succeeds(self):
        tags = [self._tag('a', 'vegan'), self._tag('b', 'spicy')]
        response = self._put({
            'restaurant': str(self.restaurant.id),
            'tags': tags,
        })
        self.assertEqual(response.status_code, 200)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.preset_tags, tags)

    def test_put_with_stringified_json_returns_400(self):
        import json
        tags = [self._tag('a', 'vegan')]
        response = self._put({
            'restaurant': str(self.restaurant.id),
            'tags': json.dumps(tags),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            'status': 400, 'message': 'tags (list) is required',
        })
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.preset_tags, [])

    def test_put_with_missing_tags_returns_400(self):
        response = self._put({'restaurant': str(self.restaurant.id)})
        self.assertEqual(response.status_code, 400)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.preset_tags, [])

    def test_put_with_empty_list_succeeds(self):
        self.restaurant.preset_tags = [self._tag('x', 'old')]
        self.restaurant.save(update_fields=['preset_tags'])
        response = self._put({
            'restaurant': str(self.restaurant.id),
            'tags': [],
        })
        self.assertEqual(response.status_code, 200)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.preset_tags, [])


class DedicatedEndpointAuthorizationTests(TestCase):
    """
    Locks in the tightened authorization filters (`active=True, deleted=False`)
    on the two permission helpers used outside the catch-all PUT path:

    - users_app.controllers.permissions_check.get_user_restaurant_roles
    - restaurants_app.controllers.first_time_batch_approval.check_permissions

    These helpers gate every dedicated endpoint (preset_tags, upsell_config,
    reservations, waitlist, table_actions) and the first-time-menu-review
    manager action. The catch-all PUT path is already covered by
    TenantIsolationTests; this class is its analogue for the dedicated
    endpoints, plus a cross-tenant regression guard at preset_tags.
    """

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='owner_a_dedicated@test.com', phone_number='256700000110',
            username='256700000110', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Dedicated A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        self.employment_a = RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='owner_b_dedicated@test.com', phone_number='256700000120',
            username='256700000120', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Dedicated B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # first-time-menu-review bails with 400 if the restaurant has no
        # menu sections (see first_time_batch_approval.py:75-82), so seed one.
        MenuSection.objects.create(
            name='A Section', restaurant=self.restaurant_a, listing_position=0,
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _tag(self, tid, name):
        return {
            'id': tid, 'name': name, 'icon': 'tag',
            'color': 'gray', 'filterable': True,
        }

    def _preset_tags_put(self, user, body):
        token = self._token_for(user)
        return self.client.put(
            '/api/v1/restaurant-setup/preset-tags/',
            data=body,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def _first_time_menu_review_post(self, user, body):
        token = self._token_for(user)
        return self.client.post(
            '/api/v1/restaurant-setup/manager-actions/first-time-menu-review/',
            data=body,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    # -- unit: get_user_restaurant_roles --------------------------------

    def test_get_user_restaurant_roles_excludes_deactivated_employee(self):
        from users_app.controllers.permissions_check import (
            get_user_restaurant_roles,
        )
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        roles = get_user_restaurant_roles(
            user_id=str(self.owner_a.id),
            restaurant_id=str(self.restaurant_a.id),
        )
        self.assertEqual(roles, [])

    def test_get_user_restaurant_roles_excludes_soft_deleted_employee(self):
        from users_app.controllers.permissions_check import (
            get_user_restaurant_roles,
        )
        self.employment_a.deleted = True
        self.employment_a.save(update_fields=['deleted'])
        roles = get_user_restaurant_roles(
            user_id=str(self.owner_a.id),
            restaurant_id=str(self.restaurant_a.id),
        )
        self.assertEqual(roles, [])

    # -- unit: first_time_batch_approval.check_permissions --------------

    def test_first_time_batch_check_excludes_deactivated_employee(self):
        from restaurants_app.controllers.first_time_batch_approval import (
            check_permissions,
        )
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        self.assertFalse(check_permissions(
            restaurant_id=str(self.restaurant_a.id),
            user_id=str(self.owner_a.id),
        ))

    def test_first_time_batch_check_excludes_soft_deleted_employee(self):
        from restaurants_app.controllers.first_time_batch_approval import (
            check_permissions,
        )
        self.employment_a.deleted = True
        self.employment_a.save(update_fields=['deleted'])
        self.assertFalse(check_permissions(
            restaurant_id=str(self.restaurant_a.id),
            user_id=str(self.owner_a.id),
        ))

    # -- integration: preset_tags ---------------------------------------

    def test_deactivated_employee_blocked_from_preset_tags_put(self):
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        response = self._preset_tags_put(
            self.owner_a,
            {
                'restaurant': str(self.restaurant_a.id),
                'tags': [self._tag('a', 'vegan')],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.restaurant_a.refresh_from_db()
        self.assertEqual(self.restaurant_a.preset_tags, [])

    def test_cross_tenant_blocked_from_preset_tags_put(self):
        # owner_a has an active employment at A but none at B.
        response = self._preset_tags_put(
            self.owner_a,
            {
                'restaurant': str(self.restaurant_b.id),
                'tags': [self._tag('a', 'vegan')],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.restaurant_b.refresh_from_db()
        self.assertEqual(self.restaurant_b.preset_tags, [])

    # -- integration: first-time-menu-review ----------------------------

    def test_deactivated_employee_blocked_from_first_time_menu_review(self):
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        response = self._first_time_menu_review_post(
            self.owner_a,
            {
                'restaurant': str(self.restaurant_a.id),
                'decision': 'submit',
            },
        )
        # The controller returns status 401 in the body when permission is
        # denied (see first_time_batch_approval.py:67-71).
        self.assertEqual(response.json().get('status'), 401)
        self.restaurant_a.refresh_from_db()
        self.assertNotEqual(
            self.restaurant_a.first_time_menu_approval_decision, 'submit',
        )


class BackendTechDebtBundleTests(TestCase):
    """Regression guards for the con_orders.py structural fixes:
    if/elif conversion of mutually-exclusive discount branches and the
    end_date inclusivity correction."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    @staticmethod
    def _always_active_temporal():
        return {
            'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '',
            'end_date': '',
            'start_time': '',
            'end_time': '',
        }

    def test_discount_with_clean_data_unchanged(self):
        # Regression: with normalized (post-0042) discount_details, the
        # if/elif structural fix must not change behavior on clean data.
        # Both percentage and fixed-amount paths are covered here.
        from decimal import Decimal
        from orders_app.controllers.con_orders import ConOrder

        pct_item = MenuItem.objects.create(
            name='Bundle Pct Item',
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=Decimal('8000.00'),
            running_discount=True,
            consider_discount_object=False,
            discount_details={
                'discount_type': 'percentage',
                'discount_percentage': 20.0,
                'discount_amount': 0.0,
                **self._always_active_temporal(),
            },
        )
        result = ConOrder.determine_effective_unit_price(pct_item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

        # Discount-object path with a percentage value.
        obj_pct_item = MenuItem.objects.create(
            name='Bundle Obj Pct Item',
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=None,
            running_discount=False,
            consider_discount_object=True,
            discount_details={
                'discount_type': 'percentage',
                'discount_percentage': 25.0,
                'discount_amount': 0.0,
                **self._always_active_temporal(),
            },
        )
        result = ConOrder.determine_effective_unit_price(obj_pct_item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('7500.00'))

        # Discount-object path with a fixed amount.
        obj_fixed_item = MenuItem.objects.create(
            name='Bundle Obj Fixed Item',
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=None,
            running_discount=False,
            consider_discount_object=True,
            discount_details={
                'discount_type': 'fixed',
                'discount_percentage': 0.0,
                'discount_amount': 2000.0,
                **self._always_active_temporal(),
            },
        )
        result = ConOrder.determine_effective_unit_price(obj_fixed_item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

    def test_discount_end_date_semantic(self):
        # end_date is treated as INCLUSIVE — the last day the discount is
        # valid. Setting end_date == today must therefore keep the discount
        # active. The pre-fix code used '>=' on the expiration check, which
        # incorrectly fired on the end_date itself (off-by-one).
        from datetime import datetime
        from decimal import Decimal
        from orders_app.controllers.con_orders import ConOrder

        today_iso = datetime.now().date().isoformat()
        item = MenuItem.objects.create(
            name='Bundle End Date Item',
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=None,
            running_discount=False,
            consider_discount_object=True,
            discount_details={
                'discount_type': 'percentage',
                'discount_percentage': 20.0,
                'discount_amount': 0.0,
                'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '',
                'end_date': today_iso,
                'start_time': '',
                'end_time': '',
            },
        )
        result = ConOrder.determine_effective_unit_price(item)
        self.assertEqual(result['status'], 200)
        # Discount IS applied on the end_date itself under inclusive semantics.
        self.assertEqual(result['price'], Decimal('8000.00'))


# ---------------------------------------------------------------------------
# Image optimisation tests
# ---------------------------------------------------------------------------

import io  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import patch  # noqa: E402

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import override_settings  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from restaurants_app.utils.image_optimizer import optimize_image  # noqa: E402


def _make_image_bytes(size=(1200, 1200), mode='RGB'):
    """
    Build an in-memory image in the requested PIL mode, encoded in a format
    that preserves that mode on round-trip. Returns (bytes, extension).
    """
    if mode == 'CMYK':
        img = PILImage.new(mode, size, color=(10, 20, 30, 40))
        fmt = 'JPEG'
        ext = 'jpg'
    elif mode == 'RGBA':
        img = PILImage.new(mode, size, color=(255, 0, 0, 200))
        fmt = 'PNG'
        ext = 'png'
    elif mode == 'LA':
        img = PILImage.new(mode, size, color=(128, 200))
        fmt = 'PNG'
        ext = 'png'
    elif mode == 'P':
        base = PILImage.new('RGB', size, color=(0, 128, 64))
        img = base.convert('P', palette=PILImage.ADAPTIVE)
        fmt = 'PNG'
        ext = 'png'
    else:
        img = PILImage.new('RGB', size, color=(123, 200, 80))
        fmt = 'JPEG'
        ext = 'jpg'
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf.getvalue(), ext


def _is_webp(blob):
    return len(blob) >= 12 and blob[0:4] == b'RIFF' and blob[8:12] == b'WEBP'


class _MediaTempDirMixin:
    """
    Per-test MEDIA_ROOT in a tempdir so writes don't pollute the repo
    uploads/ directory and don't leak between tests.
    """

    def setUp(self):
        self._media_tmp = tempfile.mkdtemp(prefix='dinify_test_media_')
        self._media_override = override_settings(MEDIA_ROOT=self._media_tmp)
        self._media_override.enable()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._media_override.disable()
        shutil.rmtree(self._media_tmp, ignore_errors=True)


class ImageOptimizerTests(_MediaTempDirMixin, TestCase):
    """Direct tests for restaurants_app.utils.image_optimizer.optimize_image."""

    def setUp(self):
        super().setUp()
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _attach_image(self, item, filename, content):
        """Attach a file to item.image WITHOUT triggering MenuItem.save()."""
        upload = SimpleUploadedFile(filename, content)
        item.image.save(filename, upload, save=False)

    def _new_item_with_image(self, filename='photo.jpg', size=(1200, 1200), mode='RGB'):
        item = MenuItem.objects.create(
            name=f'Img test {filename}',
            section=self.section,
            primary_price=1000.0,
        )
        content, _ = _make_image_bytes(size=size, mode=mode)
        self._attach_image(item, filename, content)
        return item

    def test_optimize_image_produces_webp_for_oversized_input(self):
        item = self._new_item_with_image(filename='big.jpg', size=(1200, 1200))
        result = optimize_image(item.image)
        self.assertTrue(result)
        self.assertTrue(item.image.name.endswith('.webp'))
        with item.image.open('rb') as fh:
            self.assertTrue(_is_webp(fh.read(16)))

    def test_optimize_image_force_false_skips_small_image(self):
        item = self._new_item_with_image(filename='small.jpg', size=(400, 400))
        result = optimize_image(item.image)
        self.assertFalse(result)
        self.assertTrue(item.image.name.endswith('.jpg'))

    def test_optimize_image_force_true_reprocesses_small_image(self):
        item = self._new_item_with_image(filename='small2.jpg', size=(400, 400))
        result = optimize_image(item.image, force=True)
        self.assertTrue(result)
        self.assertTrue(item.image.name.endswith('.webp'))
        with item.image.open('rb') as fh:
            self.assertTrue(_is_webp(fh.read(16)))

    def test_optimize_image_handles_non_rgb_modes(self):
        for mode in ('RGBA', 'P', 'LA', 'CMYK'):
            with self.subTest(mode=mode):
                content, ext = _make_image_bytes(size=(1200, 1200), mode=mode)
                item = MenuItem.objects.create(
                    name=f'Mode {mode}',
                    section=self.section,
                    primary_price=1000.0,
                )
                self._attach_image(item, f'mode_{mode.lower()}.{ext}', content)
                result = optimize_image(item.image)
                self.assertTrue(result, f'optimize_image failed for mode={mode}')
                self.assertTrue(item.image.name.endswith('.webp'))


class ReoptimiseMenuImagesCommandTests(_MediaTempDirMixin, TestCase):
    """Tests for the reoptimise_menu_images management command."""

    # Patch target: MenuItem.save() imports optimize_image lazily from this
    # module path, so patching it here disables auto-optimisation during
    # fixture setup, letting us seed real .jpg-named rows.
    _PATCH_TARGET = 'restaurants_app.utils.image_optimizer.optimize_image'

    def setUp(self):
        super().setUp()
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _seed_item_with_jpg(self, name, size=(1200, 1200)):
        content, _ = _make_image_bytes(size=size, mode='RGB')
        upload = SimpleUploadedFile(f'{name}.jpg', content, content_type='image/jpeg')
        with patch(self._PATCH_TARGET, return_value=False):
            item = MenuItem.objects.create(
                name=name,
                section=self.section,
                primary_price=1000.0,
                image=upload,
            )
        return item

    def _seed_item_no_image(self, name):
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            primary_price=1000.0,
        )

    def test_processes_items_and_skips_empty(self):
        a = self._seed_item_with_jpg('Item A')
        b = self._seed_item_with_jpg('Item B')
        c = self._seed_item_no_image('Item C')
        self.assertTrue(a.image.name.endswith('.jpg'))
        self.assertTrue(b.image.name.endswith('.jpg'))
        self.assertFalse(bool(c.image))

        call_command('reoptimise_menu_images')

        a.refresh_from_db()
        b.refresh_from_db()
        c.refresh_from_db()
        self.assertTrue(a.image.name.endswith('.webp'))
        self.assertTrue(b.image.name.endswith('.webp'))
        self.assertFalse(bool(c.image))

    def test_dry_run_makes_no_writes(self):
        item = self._seed_item_with_jpg('Dry Run Item')
        original_name = item.image.name
        self.assertTrue(original_name.endswith('.jpg'))

        call_command('reoptimise_menu_images', '--dry-run')

        item.refresh_from_db()
        self.assertEqual(item.image.name, original_name)
        self.assertTrue(item.image.name.endswith('.jpg'))

    def test_respects_limit(self):
        items = [self._seed_item_with_jpg(f'Limit Item {i}') for i in range(3)]

        call_command('reoptimise_menu_images', '--limit', '1')

        webp_count = 0
        jpg_count = 0
        for it in items:
            it.refresh_from_db()
            if it.image.name.endswith('.webp'):
                webp_count += 1
            elif it.image.name.endswith('.jpg'):
                jpg_count += 1
        self.assertEqual(webp_count, 1)
        self.assertEqual(jpg_count, 2)


class OptimizeImagePathNormalizationTests(_MediaTempDirMixin, TestCase):
    """
    Regression coverage for the upload_to path-duplication bug. The previous
    implementation passed image_field.name (which already contains the
    upload_to prefix) to image_field.save(), and Django then re-applied
    upload_to on top of it — yielding menu_items/menu_items/foo.webp, and
    nesting one level deeper on every subsequent re-run.
    """

    def setUp(self):
        super().setUp()
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def test_optimize_image_applies_upload_to_exactly_once(self):
        content, _ = _make_image_bytes(size=(1200, 1200), mode='RGB')
        upload = SimpleUploadedFile('orig.jpg', content, content_type='image/jpeg')
        # Bypass MenuItem.save()'s auto-optimisation so we end up with a
        # stored .jpg whose name already carries the menu_items/ prefix —
        # the exact precondition the management command operates on.
        with patch(
            'restaurants_app.utils.image_optimizer.optimize_image',
            return_value=False,
        ):
            item = MenuItem.objects.create(
                name='Path normalisation item',
                section=self.section,
                primary_price=1000.0,
                image=upload,
            )

        self.assertTrue(item.image.name.startswith('menu_items/'))
        self.assertNotIn('menu_items/menu_items/', item.image.name)

        result = optimize_image(item.image, force=True)
        self.assertTrue(result)

        self.assertTrue(
            item.image.name.startswith('menu_items/'),
            f'Expected single menu_items/ prefix, got {item.image.name}',
        )
        self.assertNotIn('menu_items/menu_items/', item.image.name)
        rest = item.image.name[len('menu_items/'):]
        self.assertNotIn('/', rest)
        self.assertTrue(rest.endswith('.webp'))


class ReoptimiseCommandCountingTests(_MediaTempDirMixin, TestCase):
    """
    The management command's summary must distinguish exceptions (failed)
    from legitimate skips (False return). Previously both collapsed into
    'skipped', producing succeeded=0, failed=0, skipped=N even when every
    item raised.
    """

    # The command imports optimize_image at module load time, so patches
    # must target the command module's binding — not the source module.
    _COMMAND_PATCH = (
        'restaurants_app.management.commands.reoptimise_menu_images.optimize_image'
    )
    # MenuItem.save() lazily imports optimize_image, so the source-module
    # binding is the right patch target for disabling auto-optimisation
    # during fixture setup.
    _MODEL_PATCH = 'restaurants_app.utils.image_optimizer.optimize_image'

    def setUp(self):
        super().setUp()
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _seed_item_with_jpg(self, name):
        content, _ = _make_image_bytes(size=(1200, 1200), mode='RGB')
        upload = SimpleUploadedFile(f'{name}.jpg', content, content_type='image/jpeg')
        with patch(self._MODEL_PATCH, return_value=False):
            return MenuItem.objects.create(
                name=name,
                section=self.section,
                primary_price=1000.0,
                image=upload,
            )

    def test_exception_counted_as_failed(self):
        self._seed_item_with_jpg('Failing Item 1')
        self._seed_item_with_jpg('Failing Item 2')

        out = io.StringIO()
        err = io.StringIO()
        with patch(self._COMMAND_PATCH, side_effect=RuntimeError('boom')):
            call_command('reoptimise_menu_images', stdout=out, stderr=err)

        summary = out.getvalue()
        self.assertIn('processed=2', summary)
        self.assertIn('succeeded=0', summary)
        self.assertIn('failed=2', summary)
        self.assertIn('skipped=0', summary)
        self.assertIn('Failed for item', err.getvalue())

    def test_false_return_counted_as_skipped(self):
        self._seed_item_with_jpg('Skipping Item 1')
        self._seed_item_with_jpg('Skipping Item 2')

        out = io.StringIO()
        with patch(self._COMMAND_PATCH, return_value=False):
            call_command('reoptimise_menu_images', stdout=out)

        summary = out.getvalue()
        self.assertIn('processed=2', summary)
        self.assertIn('succeeded=0', summary)
        self.assertIn('failed=0', summary)
        self.assertIn('skipped=2', summary)


# =============================================================================
# Restaurant tag catalog (PR 1 of 5) — seed, backfill, CRUD, tenant isolation.
# =============================================================================

class RestaurantTagSeedSignalTests(TestCase):
    """Creating a Restaurant must seed the 14 system-default presets."""

    def setUp(self):
        seed_user()

    def test_creating_restaurant_seeds_14_presets(self):
        from restaurants_app.models import RestaurantTag, SYSTEM_PRESET_TAGS
        owner = User.objects.get(username=TEST_PHONE)
        restaurant = Restaurant.objects.create(
            name='Signal Seed Restaurant',
            location='Anywhere',
            owner=owner,
        )
        tags = RestaurantTag.objects.filter(restaurant=restaurant)
        self.assertEqual(tags.count(), len(SYSTEM_PRESET_TAGS))
        self.assertEqual(tags.filter(is_system_preset=True).count(), 14)

        # Spot-check a few presets carry the right metadata.
        vegan = tags.get(name='Vegan')
        self.assertEqual(vegan.category, 'dietary')
        self.assertEqual(vegan.colour, 'emerald')
        self.assertEqual(vegan.icon, 'sprout')
        self.assertTrue(vegan.filterable)

        spicy = tags.get(name='Spicy')
        self.assertEqual(spicy.category, 'descriptor')
        self.assertFalse(spicy.filterable)

        # display_order is 1..14 in catalog declaration order.
        orders = list(tags.order_by('display_order').values_list('display_order', flat=True))
        self.assertEqual(orders, list(range(1, 15)))

    def test_seed_signal_is_idempotent_on_resave(self):
        from restaurants_app.models import RestaurantTag
        owner = User.objects.get(username=TEST_PHONE)
        restaurant = Restaurant.objects.create(
            name='Idempotent Seed Restaurant',
            location='Anywhere',
            owner=owner,
        )
        # Saving again must not duplicate the catalog. The signal only
        # seeds when created=True, which is False on subsequent saves.
        restaurant.location = 'Elsewhere'
        restaurant.save()
        self.assertEqual(
            RestaurantTag.objects.filter(restaurant=restaurant).count(), 14,
        )

    def test_seed_helper_is_idempotent(self):
        from restaurants_app.models import RestaurantTag, seed_system_preset_tags
        owner = User.objects.get(username=TEST_PHONE)
        restaurant = Restaurant.objects.create(
            name='Helper Idempotent', location='Anywhere', owner=owner,
        )
        # Re-running the seed helper produces no duplicates.
        seed_system_preset_tags(restaurant)
        seed_system_preset_tags(restaurant)
        self.assertEqual(
            RestaurantTag.objects.filter(restaurant=restaurant).count(), 14,
        )


class SerializerPublicGetMenuItemTagShapeTests(TestCase):
    """SerializerPublicGetMenuItem must return full tag objects."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()

    def test_tags_field_returns_full_objects(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)
        item = MenuItem.objects.create(
            name='Tagged Item', section=section, primary_price=1000,
        )

        vegan = RestaurantTag.objects.get(restaurant=restaurant, name='Vegan')
        spicy = RestaurantTag.objects.get(restaurant=restaurant, name='Spicy')
        MenuItemTag.objects.create(menu_item=item, tag=vegan)
        MenuItemTag.objects.create(menu_item=item, tag=spicy)

        data = SerializerPublicGetMenuItem(item).data
        self.assertIsInstance(data['tags'], list)
        self.assertEqual(len(data['tags']), 2)
        names = {t['name'] for t in data['tags']}
        self.assertEqual(names, {'Vegan', 'Spicy'})
        for tag_obj in data['tags']:
            self.assertEqual(
                set(tag_obj.keys()),
                {'id', 'name', 'category', 'icon', 'colour'},
            )


class SerializerPublicGetMenuItemExtrasDiscountTests(TestCase):
    """get_extras must serialize each extra's discount_details so the diner UI
    can price extras at their discounted figure (same path as the parent item)."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()

    def test_extras_include_discount_details(self):
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

        discount = {
            'discount_percentage': 20,
            'discount_amount': 0,
            'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '',
            'end_date': '',
            'start_time': '',
            'end_time': '',
        }
        discounted_extra = MenuItem.objects.create(
            name='Discounted Extra', section=section, primary_price=1000,
            running_discount=True, discount_details=discount,
        )
        plain_extra = MenuItem.objects.create(
            name='Plain Extra', section=section, primary_price=500,
        )
        parent = MenuItem.objects.create(
            name='Parent With Extras', section=section, primary_price=5000,
            extras_applicable=[str(discounted_extra.id), str(plain_extra.id)],
        )

        extras = SerializerPublicGetMenuItem(parent).data['extras']
        self.assertEqual(len(extras), 2)
        for extra in extras:
            self.assertIn('discount_details', extra)

        by_name = {e['name']: e for e in extras}
        self.assertEqual(by_name['Discounted Extra']['discount_details'], discount)
        # A non-discounted extra serialises as the JSONField default ({}).
        self.assertEqual(by_name['Plain Extra']['discount_details'], {})


class RestaurantTagsEndpointTests(TestCase):
    """CRUD + tenant-isolation tests for /api/v1/restaurant-setup/restaurant-tags/."""

    def setUp(self):
        from rest_framework_simplejwt.tokens import RefreshToken  # noqa: F401
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='owner_a_tag@test.com', phone_number='256700000110',
            username='256700000110', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Tag Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='owner_b_tag@test.com', phone_number='256700000120',
            username='256700000120', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Tag Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

    def _token(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _auth(self, user):
        return {'HTTP_AUTHORIZATION': f'Bearer {self._token(user)}'}

    def test_list_returns_seeded_catalog(self):
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/?restaurant={self.restaurant_a.id}',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['data']), 14)

    def test_list_rejects_cross_restaurant_caller(self):
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/?restaurant={self.restaurant_b.id}',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_create_custom_tag(self):
        from restaurants_app.models import RestaurantTag
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/',
            data={
                'restaurant': str(self.restaurant_a.id),
                'name': 'House Smoked',
                'category': 'descriptor',
                'colour': 'purple',
                'icon': 'flame',
                'filterable': False,
                'display_order': 99,
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        created = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='House Smoked',
        )
        # Caller-supplied is_system_preset must be ignored.
        self.assertFalse(created.is_system_preset)

    def test_create_rejects_cross_restaurant(self):
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/',
            data={
                'restaurant': str(self.restaurant_b.id),
                'name': 'Hostile Tag',
                'category': 'descriptor',
                'colour': 'red',
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_patch_via_secretary(self):
        from restaurants_app.models import RestaurantTag
        tag = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Vegan',
        )
        response = self.client.patch(
            f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/',
            data={
                'name': 'Plant-Based',
                'colour': 'green',
                'display_order': 5,
                'filterable': False,
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        tag.refresh_from_db()
        self.assertEqual(tag.name, 'Plant-Based')
        self.assertEqual(tag.colour, 'green')
        self.assertEqual(tag.display_order, 5)
        self.assertFalse(tag.filterable)

    def test_patch_rejects_cross_restaurant(self):
        from restaurants_app.models import RestaurantTag
        # Tag belongs to B; owner_a must not be able to mutate it.
        b_tag = RestaurantTag.objects.get(
            restaurant=self.restaurant_b, name='Vegan',
        )
        original_name = b_tag.name
        response = self.client.patch(
            f'/api/v1/restaurant-setup/restaurant-tags/{b_tag.id}/',
            data={'name': 'pwned'},
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)
        b_tag.refresh_from_db()
        self.assertEqual(b_tag.name, original_name)

    def test_delete_cascades_links(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant_a,
        )
        item = MenuItem.objects.create(
            name='Burger', section=section, primary_price=1000,
        )
        tag = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Spicy',
        )
        MenuItemTag.objects.create(menu_item=item, tag=tag)

        response = self.client.delete(
            f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            RestaurantTag.objects.filter(id=tag.id).exists()
        )
        # ON DELETE CASCADE on MenuItemTag.tag
        self.assertFalse(
            MenuItemTag.objects.filter(menu_item=item).exists()
        )

    def test_delete_rejects_cross_restaurant(self):
        from restaurants_app.models import RestaurantTag
        b_tag = RestaurantTag.objects.get(
            restaurant=self.restaurant_b, name='Halal',
        )
        response = self.client.delete(
            f'/api/v1/restaurant-setup/restaurant-tags/{b_tag.id}/',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(RestaurantTag.objects.filter(id=b_tag.id).exists())

    def test_unauthenticated_rejected(self):
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/?restaurant={self.restaurant_a.id}'
        )
        self.assertEqual(response.status_code, 401)


class MenuItemTagIdsTests(TestCase):
    """Tests for the tag_ids payload on menu item create / update.

    Covers:
    - POST /menuitems/ accepts tag_ids and persists MenuItemTag rows.
    - PUT  /menuitems/ replaces the relation atomically.
    - Cross-restaurant tag IDs are rejected (tenant isolation).
    - Legacy `tags` text field is no longer Secretary-editable.
    - SerializerPublicGetMenuItem returns full tag objects on both
      operator-facing GET and diner-facing renders.
    """

    def setUp(self):
        from rest_framework_simplejwt.tokens import RefreshToken  # noqa: F401
        self.owner_a = User.objects.create_user(
            first_name='Tag', last_name='OwnerA',
            email='tag_owner_a@test.com', phone_number='256700000210',
            username='256700000210', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Tag Items Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.section_a = MenuSection.objects.create(
            name='A Mains', restaurant=self.restaurant_a, listing_position=0,
        )

        self.owner_b = User.objects.create_user(
            first_name='Tag', last_name='OwnerB',
            email='tag_owner_b@test.com', phone_number='256700000220',
            username='256700000220', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Tag Items Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

    def _token(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _auth(self, user):
        return {'HTTP_AUTHORIZATION': f'Bearer {self._token(user)}'}

    def test_create_menuitem_with_tag_ids_persists_relation(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Vegan',
        )
        spicy = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Spicy',
        )
        response = self.client.post(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'name': 'Veg Bowl',
                'section': str(self.section_a.id),
                'primary_price': '1200.00',
                'tag_ids': [str(vegan.id), str(spicy.id)],
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        item = MenuItem.objects.get(name='Veg Bowl', section=self.section_a)
        tagged = set(
            MenuItemTag.objects.filter(menu_item=item).values_list(
                'tag_id', flat=True
            )
        )
        self.assertEqual(tagged, {vegan.id, spicy.id})

    def test_create_menuitem_with_multipart_tag_ids_string_persists_relation(self):
        """tag_ids arrives as a JSON-encoded string via multipart/form-data
        (the shape the frontend sends when an image is attached)."""
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Vegan',
        )
        spicy = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Spicy',
        )
        response = self.client.post(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'name': 'Veg Bowl Multipart',
                'section': str(self.section_a.id),
                'primary_price': '1200.00',
                'tag_ids': json.dumps([str(vegan.id), str(spicy.id)]),
            },
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        item = MenuItem.objects.get(
            name='Veg Bowl Multipart', section=self.section_a,
        )
        tagged = set(
            MenuItemTag.objects.filter(menu_item=item).values_list(
                'tag_id', flat=True
            )
        )
        self.assertEqual(tagged, {vegan.id, spicy.id})

    def test_create_menuitem_with_multipart_empty_tag_ids_string(self):
        """tag_ids arrives as the string "[]" via multipart/form-data."""
        from restaurants_app.models import MenuItemTag
        response = self.client.post(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'name': 'Empty Bowl Multipart',
                'section': str(self.section_a.id),
                'primary_price': '1200.00',
                'tag_ids': '[]',
            },
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        item = MenuItem.objects.get(
            name='Empty Bowl Multipart', section=self.section_a,
        )
        self.assertEqual(
            MenuItemTag.objects.filter(menu_item=item).count(), 0,
        )

    def test_update_menuitem_with_tag_ids_replaces_relation(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        item = MenuItem.objects.create(
            name='Existing Item', section=self.section_a,
            primary_price=1000,
        )
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Vegan',
        )
        spicy = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Spicy',
        )
        gluten = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Contains Gluten',
        )
        # Pre-seed with vegan + spicy.
        MenuItemTag.objects.create(menu_item=item, tag=vegan)
        MenuItemTag.objects.create(menu_item=item, tag=spicy)

        # Replace with just gluten.
        response = self.client.put(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'id': str(item.id),
                'tag_ids': [str(gluten.id)],
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        tagged = set(
            MenuItemTag.objects.filter(menu_item=item).values_list(
                'tag_id', flat=True
            )
        )
        self.assertEqual(tagged, {gluten.id})

    def test_update_with_empty_tag_ids_clears_relation(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        item = MenuItem.objects.create(
            name='Clearable Item', section=self.section_a,
            primary_price=1000,
        )
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Vegan',
        )
        MenuItemTag.objects.create(menu_item=item, tag=vegan)

        response = self.client.put(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'id': str(item.id),
                'tag_ids': [],
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            MenuItemTag.objects.filter(menu_item=item).exists()
        )

    def test_create_rejects_cross_restaurant_tag_ids(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        # A's section + B's tag → must be rejected.
        b_vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_b, name='Vegan',
        )
        response = self.client.post(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'name': 'Hostile Item',
                'section': str(self.section_a.id),
                'primary_price': '1000.00',
                'tag_ids': [str(b_vegan.id)],
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 400, response.content)
        # Item must not have been created and no MenuItemTag link should exist.
        self.assertFalse(
            MenuItem.objects.filter(
                name='Hostile Item', section=self.section_a
            ).exists()
        )
        self.assertFalse(
            MenuItemTag.objects.filter(tag=b_vegan).exists()
        )

    def test_update_rejects_cross_restaurant_tag_ids(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        item = MenuItem.objects.create(
            name='Item For Spoof', section=self.section_a,
            primary_price=1000,
        )
        b_vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant_b, name='Vegan',
        )
        response = self.client.put(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'id': str(item.id),
                'tag_ids': [str(b_vegan.id)],
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(
            MenuItemTag.objects.filter(menu_item=item).exists()
        )

    def test_legacy_tags_field_no_longer_secretary_editable(self):
        """Submitting `tags` (legacy free-text list) via PUT is a no-op.

        Secretary only forwards keys listed in EDIT_INFORMATION. After
        replacing `tags` with `tag_ids`, sending `tags: [...]` to the
        menu item PUT endpoint must NOT write anywhere.
        """
        from restaurants_app.models import MenuItemTag
        item = MenuItem.objects.create(
            name='Legacy Tags Probe', section=self.section_a,
            primary_price=1000,
        )
        response = self.client.put(
            '/api/v1/restaurant-setup/menuitems/',
            data={
                'id': str(item.id),
                'tags': ['vegan', 'spicy'],
                'name': 'Renamed Probe',  # included so Secretary sees a change
            },
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        # Free-form `tags` payload must not have created any M2M links.
        self.assertFalse(
            MenuItemTag.objects.filter(menu_item=item).exists()
        )

    def test_diner_facing_menuitem_returns_full_tag_objects(self):
        """Confirm the serializer returns the documented tag shape.

        Locks in the contract for the diner frontend: each tag entry
        carries id/name/category/icon/colour.
        """
        from restaurants_app.models import RestaurantTag, MenuItemTag
        from restaurants_app.serializers import SerializerPublicGetMenuItem
        item = MenuItem.objects.create(
            name='Tag Shape Item', section=self.section_a,
            primary_price=1000,
        )
        dairy = RestaurantTag.objects.get(
            restaurant=self.restaurant_a, name='Contains Dairy',
        )
        MenuItemTag.objects.create(menu_item=item, tag=dairy)
        data = SerializerPublicGetMenuItem(item).data
        self.assertEqual(len(data['tags']), 1)
        tag_obj = data['tags'][0]
        self.assertEqual(
            set(tag_obj.keys()),
            {'id', 'name', 'category', 'icon', 'colour'},
        )
        self.assertEqual(tag_obj['name'], 'Contains Dairy')
        self.assertEqual(tag_obj['icon'], 'milk')
        self.assertEqual(tag_obj['colour'], 'blue')


class MenuItemSyncTagLinksTests(TestCase):
    """Unit tests for MenuItem.sync_tag_links()."""

    def setUp(self):
        seed_user()
        seed_restaurant()
        seed_menu_section()
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)
        self.item = MenuItem.objects.create(
            name='Sync Probe', section=self.section, primary_price=1000,
        )

    def test_sync_creates_links(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant, name='Vegan',
        )
        self.item.sync_tag_links([vegan.id])
        self.assertEqual(
            list(
                MenuItemTag.objects.filter(menu_item=self.item).values_list(
                    'tag_id', flat=True
                )
            ),
            [vegan.id],
        )

    def test_sync_replaces_existing_links(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant, name='Vegan',
        )
        spicy = RestaurantTag.objects.get(
            restaurant=self.restaurant, name='Spicy',
        )
        MenuItemTag.objects.create(menu_item=self.item, tag=vegan)
        self.item.sync_tag_links([spicy.id])
        tagged = set(
            MenuItemTag.objects.filter(menu_item=self.item).values_list(
                'tag_id', flat=True
            )
        )
        self.assertEqual(tagged, {spicy.id})

    def test_sync_dedupes_repeated_ids(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant, name='Vegan',
        )
        self.item.sync_tag_links([vegan.id, vegan.id, vegan.id])
        self.assertEqual(
            MenuItemTag.objects.filter(menu_item=self.item).count(), 1,
        )

    def test_sync_with_empty_list_clears_all(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        vegan = RestaurantTag.objects.get(
            restaurant=self.restaurant, name='Vegan',
        )
        MenuItemTag.objects.create(menu_item=self.item, tag=vegan)
        self.item.sync_tag_links([])
        self.assertFalse(
            MenuItemTag.objects.filter(menu_item=self.item).exists()
        )


class PublicTableScanPresetTagsTests(TestCase):
    """
    The diner-side table-scan response must surface the restaurant's
    RestaurantTag rows as `preset_tags`, NOT the legacy
    Restaurant.preset_tags JSONField. The JSONField is never written at
    seed time, so reading it left the diner filter button hidden on every
    freshly-onboarded restaurant.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Scan', last_name='Owner',
            email='scan_owner@test.com', phone_number='256700000061',
            username='256700000061', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Scan Tags Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(
            number=901, restaurant=self.restaurant,
        )

    def test_table_scan_returns_restaurant_tag_rows_not_jsonfield(self):
        from restaurants_app.controllers.handle_diner_journey import handle_table_scan
        result = handle_table_scan(table_id=str(self.table.id))
        self.assertEqual(result['status'], 200)
        preset_tags = result['data']['restaurant']['preset_tags']
        self.assertEqual(len(preset_tags), 14)
        for tag in preset_tags:
            self.assertIn('id', tag)
            self.assertIn('name', tag)
            self.assertIn('category', tag)
            self.assertIn('filterable', tag)
            self.assertIn('colour', tag)
            self.assertIn('icon', tag)

    def test_table_scan_omits_soft_deleted_tags(self):
        from restaurants_app.models import RestaurantTag
        from restaurants_app.controllers.handle_diner_journey import handle_table_scan
        tag = RestaurantTag.objects.filter(restaurant=self.restaurant).first()
        tag.deleted = True
        tag.save(update_fields=['deleted'])
        result = handle_table_scan(table_id=str(self.table.id))
        ids = [t['id'] for t in result['data']['restaurant']['preset_tags']]
        self.assertNotIn(str(tag.id), ids)
        self.assertEqual(len(result['data']['restaurant']['preset_tags']), 13)

    def test_table_scan_ignores_legacy_jsonfield(self):
        from restaurants_app.controllers.handle_diner_journey import handle_table_scan
        self.restaurant.preset_tags = [{'id': 'stale-uuid', 'name': 'Stale'}]
        self.restaurant.save(update_fields=['preset_tags'])
        result = handle_table_scan(table_id=str(self.table.id))
        names = [t['name'] for t in result['data']['restaurant']['preset_tags']]
        self.assertNotIn('Stale', names)
        self.assertEqual(len(result['data']['restaurant']['preset_tags']), 14)


class AreaDeletionBlockTests(TestCase):
    """
    Leg 2 of the deletion model: a dining area that still contains a
    non-deleted table cannot be deleted. The rule lives on
    DiningArea.deletion_blockers() and is enforced (409) at the
    restaurant-setup DELETE endpoint, before the soft-delete. The old
    soft-cascade that silently deleted an area's tables has been removed.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Area', last_name='Owner',
            email='area_owner@test.com', phone_number='256700000210',
            username='256700000210', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Area Restaurant', location='loc-area',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        # Area that still holds a table -> must be undeletable.
        self.occupied_area = DiningArea.objects.create(
            name='Occupied Patio', restaurant=self.restaurant,
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            dining_area=self.occupied_area,
        )
        # Area with no tables -> deletable.
        self.empty_area = DiningArea.objects.create(
            name='Empty Balcony', restaurant=self.restaurant,
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _delete(self, user, config_detail, body):
        path = f'/api/v1/restaurant-setup/{config_detail}/'
        token = self._token_for(user)
        return self.client.delete(
            path, data=body, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    # -- model rule ----------------------------------------------------------

    def test_deletion_blockers_present_when_area_has_table(self):
        blocker = self.occupied_area.deletion_blockers()
        self.assertIsNotNone(blocker)
        self.assertIn('1 table', blocker)

    def test_deletion_blockers_none_for_empty_area(self):
        self.assertIsNone(self.empty_area.deletion_blockers())

    def test_deletion_blockers_ignores_already_deleted_tables(self):
        self.table.deleted = True
        self.table.save(update_fields=['deleted'])
        self.assertIsNone(self.occupied_area.deletion_blockers())

    # -- endpoint enforcement ------------------------------------------------

    def test_delete_area_with_table_is_blocked(self):
        response = self._delete(
            self.owner, 'diningareas',
            {'id': str(self.occupied_area.id), 'deletion_reason': 'test'},
        )
        self.assertEqual(response.status_code, 409)
        # both the area and its table survive the blocked delete
        self.occupied_area.refresh_from_db()
        self.table.refresh_from_db()
        self.assertFalse(self.occupied_area.deleted)
        self.assertFalse(self.table.deleted)

    def test_delete_empty_area_succeeds(self):
        response = self._delete(
            self.owner, 'diningareas',
            {'id': str(self.empty_area.id), 'deletion_reason': 'test'},
        )
        self.assertEqual(response.status_code, 200)
        self.empty_area.refresh_from_db()
        self.assertTrue(self.empty_area.deleted)

    # -- vacuum no longer cascades to tables ---------------------------------

    def test_vacuum_does_not_soft_delete_tables_under_deleted_area(self):
        from misc_app.management.commands.vacuum_deleted_records import (
            ConVacuumDeletedRecords,
        )
        # Simulate an area that ended up soft-deleted while still holding a
        # table (the pre-block state). The vacuum must NOT cascade-delete it.
        self.occupied_area.deleted = True
        self.occupied_area.save(update_fields=['deleted'])
        ConVacuumDeletedRecords().vacuum()
        self.table.refresh_from_db()
        self.assertFalse(self.table.deleted)


class TableDeletionBlockTests(TestCase):
    """
    Leg 3 of the deletion model: a table with a live (unsettled) order cannot
    be soft-deleted. 'Live' is payment-aware — terminal means paid, cancelled,
    or refunded; everything else (pending, failed, served-but-unpaid) blocks,
    so an open bill is never orphaned out of settle-up. The rule lives on
    Table.deletion_blockers() / Table.has_unsettled_orders() and is enforced
    (409) at the restaurant-setup DELETE endpoint.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Table', last_name='Owner',
            email='table_owner@test.com', phone_number='256700000310',
            username='256700000310', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Table Restaurant', location='loc-table',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self._next_number = 0

    def _make_table(self):
        self._next_number += 1
        return Table.objects.create(
            number=self._next_number, str_number=str(self._next_number),
            restaurant=self.restaurant,
        )

    def _make_order(self, table, payment_status='pending',
                    order_status='initiated', fulfilment_status='new'):
        from orders_app.models import Order
        return Order.objects.create(
            restaurant=self.restaurant, table=table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            payment_status=payment_status, order_status=order_status,
            fulfilment_status=fulfilment_status,
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _delete_table(self, table):
        path = '/api/v1/restaurant-setup/tables/'
        token = self._token_for(self.owner)
        return self.client.delete(
            path,
            data={'id': str(table.id), 'deletion_reason': 'test'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    # -- model rule: live (unsettled) states block ---------------------------

    def test_pending_order_blocks(self):
        table = self._make_table()
        self._make_order(table, payment_status='pending')
        self.assertIsNotNone(table.deletion_blockers())

    def test_served_but_unpaid_order_blocks(self):
        # the classic postpayment case the fulfilment-based helper would miss
        table = self._make_table()
        self._make_order(
            table, payment_status='pending',
            order_status='served', fulfilment_status='served',
        )
        self.assertTrue(table.has_unsettled_orders())
        self.assertIsNotNone(table.deletion_blockers())

    def test_failed_payment_blocks(self):
        table = self._make_table()
        self._make_order(table, payment_status='failed')
        self.assertIsNotNone(table.deletion_blockers())

    # -- model rule: terminal states do NOT block ----------------------------

    def test_paid_order_does_not_block(self):
        table = self._make_table()
        self._make_order(table, payment_status='paid', order_status='paid')
        self.assertIsNone(table.deletion_blockers())

    def test_cancelled_order_does_not_block(self):
        table = self._make_table()
        self._make_order(table, payment_status='pending', order_status='cancelled')
        self.assertIsNone(table.deletion_blockers())

    def test_refunded_order_does_not_block(self):
        table = self._make_table()
        self._make_order(table, payment_status='paid', order_status='refunded')
        self.assertIsNone(table.deletion_blockers())

    def test_no_orders_does_not_block(self):
        self.assertIsNone(self._make_table().deletion_blockers())

    def test_soft_deleted_order_does_not_block(self):
        table = self._make_table()
        order = self._make_order(table, payment_status='pending')
        order.deleted = True
        order.save(update_fields=['deleted'])
        self.assertIsNone(table.deletion_blockers())

    # -- endpoint enforcement ------------------------------------------------

    def test_delete_table_with_live_order_blocked(self):
        table = self._make_table()
        self._make_order(
            table, payment_status='pending',
            order_status='served', fulfilment_status='served',
        )
        response = self._delete_table(table)
        self.assertEqual(response.status_code, 409)
        table.refresh_from_db()
        self.assertFalse(table.deleted)
        # the open order stays reachable for settle-up
        from orders_app.models import Order
        self.assertTrue(Order.objects.filter(table=table, deleted=False).exists())

    def test_delete_table_all_terminal_orders_succeeds(self):
        table = self._make_table()
        self._make_order(table, payment_status='paid', order_status='paid')
        self._make_order(table, payment_status='pending', order_status='cancelled')
        response = self._delete_table(table)
        self.assertEqual(response.status_code, 200)
        table.refresh_from_db()
        self.assertTrue(table.deleted)

    def test_delete_table_with_no_orders_succeeds(self):
        table = self._make_table()
        response = self._delete_table(table)
        self.assertEqual(response.status_code, 200)
        table.refresh_from_db()
        self.assertTrue(table.deleted)
