import json
from django.db import transaction
from django.test import TestCase
from dinify_backend.configs import ROLES
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from users_app.tests import TEST_PHONE, seed_user
from users_app.models import User
from restaurants_app.controllers.create_restaurant import (
    admin_register_restaurant
)
from restaurants_app.controllers.dining_areas import create_dining_area
from restaurants_app.controllers.menu_sections import ConMenuSection
from restaurants_app.endpoints.restaurant_setup import normalize_ordered_section_ids
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea, Reservation, WaitlistEntry,
)


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
            owner=owner,
            status=RestaurantStatus_Live,
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
    # Seeded menus represent a PUBLISHED, diner-orderable menu: the diner menu
    # read path and the order publication gate both require approved + enabled,
    # so the shared happy-path fixtures must be published.
    MenuSection.objects.create(
        name=TEST_MENU_SECTION_NAME,
        restaurant=restaurant,
        approved=True,
        enabled=True,
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
        MenuItem(
            name=TEST_DISCOUNTED_MENU_ITEM_NAME,
            section=menu_section,
            primary_price=1000.0,
            # Canonical post-0042 shape (10% off 1000 = 900). The effective price
            # is recomputed from discount_details, not read from discounted_price.
            discounted_price=900.0,
            running_discount=True,
            consider_discount_object=True,
            discount_details={
                'discount_type': 'percentage',
                'discount_percentage': 10.0,
                'discount_amount': 0.0,
                'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '',
                'end_date': '',
                'start_time': '',
                'end_time': '',
            },
        ),  # noqa
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
            consider_discount_object=True,
            # Canonical post-0042 shape (10% off 1000 = 900); effective base is
            # recomputed from discount_details. 900 + Small 1100 = 2000.
            discount_details={
                'discount_type': 'percentage',
                'discount_percentage': 10.0,
                'discount_amount': 0.0,
                'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '',
                'end_date': '',
                'start_time': '',
                'end_time': '',
            },
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
    # Publish the seeded items so they satisfy the diner menu read path and the
    # order publication gate (both require approved + enabled). The deliberately
    # unavailable item keeps available=False to exercise stock reconciliation.
    for menu_item in menu_items:
        menu_item.approved = True
        menu_item.enabled = True
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
        # portal-access lifecycle filter in get_user_restaurant_roles.
        self.restaurant.status = RestaurantStatus_Live
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
        response = handle_show_menu(str(self.restaurant.id))
        self.assertEqual(response['status'], 200)
        self.assertIn('item_sort_mode', response)
        self.assertEqual(response['item_sort_mode'], 'price-high')

    def test_show_menu_defaults_to_manual(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_menu,
        )
        response = handle_show_menu(str(self.restaurant.id))
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

    def _put_serializer(self, item, start_date, end_date):
        from restaurants_app.serializers import SerializerPutMenuItem
        details = {
            'discount_type': 'percentage',
            'discount_percentage': 20.0,
            'discount_amount': 0.0,
            'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': start_date,
            'end_date': end_date,
            'start_time': '',
            'end_time': '',
        }
        return SerializerPutMenuItem(
            instance=item, data={'discount_details': details}, partial=True)

    def _window_item(self, name):
        from decimal import Decimal
        return self._make_item(
            name, Decimal('10000'),
            {**self._always_active_temporal(),
             'discount_type': 'percentage',
             'discount_percentage': 20.0,
             'discount_amount': 0.0},
            Decimal('8000.00'))

    def test_put_rejects_inverted_window(self):
        # Defense-in-depth parity with the admin form: end strictly before start
        # is rejected (surfaced as a 'discount_details' error → HTTP 400).
        serializer = self._put_serializer(self._window_item('Inverted Win'), '2026-06-24', '2026-06-19')
        self.assertFalse(serializer.is_valid())
        self.assertIn('discount_details', serializer.errors)

    def test_put_accepts_equal_start_end(self):
        # end == start is a valid one-day window (strict '<', never '<=').
        serializer = self._put_serializer(self._window_item('Equal Win'), '2026-06-24', '2026-06-24')
        serializer.is_valid()
        self.assertNotIn('discount_details', serializer.errors)

    def test_put_accepts_normal_window(self):
        serializer = self._put_serializer(self._window_item('Normal Win'), '2026-06-19', '2026-06-24')
        serializer.is_valid()
        self.assertNotIn('discount_details', serializer.errors)

    def test_put_accepts_empty_dates(self):
        # No window set → the guard must not fire.
        serializer = self._put_serializer(self._window_item('Empty Win'), '', '')
        serializer.is_valid()
        self.assertNotIn('discount_details', serializer.errors)

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

    def test_serializer_gates_on_active_window(self):
        # The diner serializer reports the discount as inactive (current_price
        # == primary, discount_percentage == 0) when the window has lapsed, and
        # active (current_price < primary, discount_percentage > 0) when live —
        # the same predicate the order/charge path uses.
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from restaurants_app.serializers import SerializerPublicGetMenuItem

        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        expired = self._make_item(
            'Serializer Expired Item', Decimal('10000'),
            {
                'discount_type': 'percentage', 'discount_percentage': 20.0,
                'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '', 'end_date': yesterday,
                'start_time': '', 'end_time': '',
            },
            Decimal('8000.00'),
        )
        data = SerializerPublicGetMenuItem(expired).data
        self.assertFalse(data['is_discount_active'])
        self.assertEqual(data['current_price'], '10000.00')
        self.assertEqual(data['discount_percentage'], 0)

        active = self._make_item(
            'Serializer Active Item', Decimal('10000'),
            {
                'discount_type': 'percentage', 'discount_percentage': 20.0,
                'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
                'start_date': '', 'end_date': '',
                'start_time': '', 'end_time': '',
            },
            Decimal('8000.00'),
        )
        data = SerializerPublicGetMenuItem(active).data
        self.assertTrue(data['is_discount_active'])
        self.assertEqual(data['current_price'], '8000.00')
        self.assertLess(Decimal(data['current_price']), Decimal('10000'))
        self.assertEqual(data['discount_percentage'], 20.0)


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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
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

    # -- section-tables verb retirement (BUG-P3-10) --------------------------

    def test_section_tables_verb_is_retired(self):
        """BUG-P3-10: the dead 'section-tables' POST verb is retired. A dinify
        admin (the only caller RBAC ever let past the gate) no longer creates
        tables — the request falls through to the generic unmapped-verb
        handling. Guards against anyone re-adding a live section-tables verb."""
        # An unmapped verb reaching a dinify admin now 500s in Secretary (None
        # serializer); capture it as a response rather than letting it propagate.
        self.client.raise_request_exception = False
        before = Table.objects.filter(restaurant=self.restaurant_a).count()
        response = self._request(
            self.dinify_admin, 'post', 'section-tables',
            {'restaurant': str(self.restaurant_a.id), 'number': 3,
             'consideration': 'count'},
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(
            Table.objects.filter(restaurant=self.restaurant_a).count(), before,
        )

    def test_create_dining_area_still_creates_section_tables(self):
        """Regression guard for the surviving create_tables_in_section caller:
        create_dining_area(create_tables=True) must still populate the area."""
        result = create_dining_area(
            restaurant_id=str(self.restaurant_a.id),
            dining_area_name='Rooftop',
            smoking_zone=False,
            outdoor_seating=True,
            user=self.owner_a,
            create_tables=True,
            consideration='count',
            no_tables=3,
        )
        self.assertEqual(result['status'], 200)
        area = DiningArea.objects.get(name='Rooftop', restaurant=self.restaurant_a)
        self.assertEqual(
            Table.objects.filter(dining_area=area, deleted=False).count(), 3,
        )

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

    # -- last-owner deactivation guard, live PUT path (DC-BE-011) ------------

    def test_put_deactivate_last_owner_is_blocked_409(self):
        # The guard that used to sit on the dead DELETE-employees branch now
        # runs on the live PUT {active:'false'} path: the sole active owner
        # cannot be deactivated. 409, never 403 (a 403 force-logs-out).
        response = self._request(
            self.owner_a, 'put', 'employees',
            {'id': str(self.employment_a.id), 'active': 'false'},
        )
        self.assertEqual(response.status_code, 409)
        self.employment_a.refresh_from_db()
        self.assertTrue(self.employment_a.active)

    def test_put_deactivate_non_last_owner_succeeds(self):
        # A second active owner exists, so deactivating one is allowed.
        second_owner = User.objects.create_user(
            first_name='Owner', last_name='A2',
            email='owner_a2@test.com', phone_number='256700000011',
            username='256700000011', country='Uganda', password='password',
            roles=[],
        )
        second_employment = RestaurantEmployee.objects.create(
            user=second_owner, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        response = self._request(
            self.owner_a, 'put', 'employees',
            {'id': str(second_employment.id), 'active': 'false'},
        )
        self.assertEqual(response.status_code, 200)
        second_employment.refresh_from_db()
        self.assertFalse(second_employment.active)

    def test_put_deactivate_non_owner_employee_succeeds(self):
        # A non-owner (kitchen) employee is never subject to the last-owner
        # guard, so deactivation goes straight through.
        staff = User.objects.create_user(
            first_name='Kitchen', last_name='Staff',
            email='kitchen_a@test.com', phone_number='256700000012',
            username='256700000012', country='Uganda', password='password',
            roles=[],
        )
        staff_employment = RestaurantEmployee.objects.create(
            user=staff, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_KITCHEN')],
        )
        response = self._request(
            self.owner_a, 'put', 'employees',
            {'id': str(staff_employment.id), 'active': 'false'},
        )
        self.assertEqual(response.status_code, 200)
        staff_employment.refresh_from_db()
        self.assertFalse(staff_employment.active)

    def test_put_deactivate_employee_cross_tenant_is_rejected(self):
        # Owner A cannot deactivate an employee of restaurant B — the module
        # gate resolves B from the id and denies (403) before the guard runs.
        b_employee = RestaurantEmployee.objects.get(
            user=self.owner_b, restaurant=self.restaurant_b,
        )
        response = self._request(
            self.owner_a, 'put', 'employees',
            {'id': str(b_employee.id), 'active': 'false'},
        )
        self.assertEqual(response.status_code, 403)
        b_employee.refresh_from_db()
        self.assertTrue(b_employee.active)

    # -- upsell reorder DELETE verb guard (DC-BE-004) -----------------------

    def test_delete_on_upsell_reorder_url_is_405(self):
        # The reorder URL injects action='reorder'; DELETE is not valid there
        # and used to silently act as a plain item delete. It now returns 405.
        response = self._request(
            self.owner_a, 'delete', 'upsell-config/items/reorder', {},
        )
        self.assertEqual(response.status_code, 405)

    def test_delete_on_upsell_items_plain_url_is_not_405(self):
        # The plain item-delete route injects no action, so the 405 guard must
        # not fire there — a missing id yields the ordinary 400.
        response = self._request(
            self.owner_a, 'delete', 'upsell-config/items', {},
        )
        self.assertNotEqual(response.status_code, 405)

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
    menu items, employees (staff PII), and menu sections — while its own reads
    keep working and a dinify admin retains its
    legitimate cross-restaurant access.

    Headline property: the returned queryset is authoritatively bound to the
    caller's owner/manager restaurants server-side; the client ?restaurant=
    can only narrow within that set, never widen it.
    """

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='read_owner_a@test.com', phone_number='256700000110',
            username='256700000110', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Read Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
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

    def test_orders_vocab_is_retired(self):
        # The `orders` record type was retired from the setup catch-all, so the
        # endpoint no longer lists orders at all — a GET falls through to the
        # generic unmapped-resource rejection (403). Full retirement SUBSUMES the
        # old cross-tenant read-isolation guarantee this test used to assert
        # (owner_a could not see B's orders): with no orders listing, none leak.
        r = self._get(self.owner_a, f'{self.BASE}/orders/?restaurant={self.restaurant_b.id}')
        self.assertEqual(r.status_code, 403)

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

    def test_unknown_query_param_does_not_500(self):
        # A stray/unknown query param must be ignored, not crash the list (500);
        # the known ?restaurant= scoping still returns the caller's own table.
        r = self._get(
            self.owner_a,
            f'{self.BASE}/tables/?restaurant={self.restaurant_a.id}&foo=barbar',
        )
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
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        return real_dt(year, month, day, hour, minute, tzinfo=ZoneInfo('Africa/Nairobi'))

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
        # handle_show_menu captures ONE local `now` via timezone.localtime() and
        # threads it into every schedule decision, so pin THAT seam. (Patching
        # schedule_utils.datetime no longer works — the function does not sample
        # the clock when `now` is supplied.)
        with patch(
            'restaurants_app.controllers.handle_diner_journey.timezone'
        ) as mock_tz:
            mock_tz.localtime.return_value = monday_10am
            response = handle_show_menu(str(restaurant.id))

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
            status=RestaurantStatus_Live, owner=self.owner,
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
            status=RestaurantStatus_Live, owner=self.owner,
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
    - users_app.controllers.permissions_check.can_user_access_module (the module
      gate the dedicated endpoints + first-time-menu-review now route through)

    These gate every dedicated endpoint (preset_tags, upsell_config,
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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
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

    # -- unit: can_user_access_module (the `menu` gate first-time-menu-review
    #    and the dedicated endpoints now route through) -------------------

    def test_module_gate_excludes_deactivated_employee(self):
        from users_app.controllers.permissions_check import (
            can_user_access_module,
        )
        from dinify_backend.configss.string_definitions import MODULE_MENU
        self.employment_a.active = False
        self.employment_a.save(update_fields=['active'])
        self.assertFalse(can_user_access_module(
            self.owner_a, str(self.restaurant_a.id), MODULE_MENU,
        ))

    def test_module_gate_excludes_soft_deleted_employee(self):
        from users_app.controllers.permissions_check import (
            can_user_access_module,
        )
        from dinify_backend.configss.string_definitions import MODULE_MENU
        self.employment_a.deleted = True
        self.employment_a.save(update_fields=['deleted'])
        self.assertFalse(can_user_access_module(
            self.owner_a, str(self.restaurant_a.id), MODULE_MENU,
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
        # Extras must be published to surface via get_extras (the read path now
        # mirrors the order path's publication contract); this suite is about
        # the discount_details shape, not publication.
        discounted_extra = MenuItem.objects.create(
            name='Discounted Extra', section=section, primary_price=1000,
            running_discount=True, discount_details=discount,
            approved=True, enabled=True, is_extra=True,
        )
        plain_extra = MenuItem.objects.create(
            name='Plain Extra', section=section, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )
        parent = MenuItem.objects.create(
            name='Parent With Extras', section=section, primary_price=5000,
            has_extras=True,
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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
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

    # -- reorder (POST restaurant-tags/reorder/) ----------------------------

    def test_reorder_persists_display_order(self):
        from restaurants_app.models import RestaurantTag
        vegan = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        spicy = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Spicy')
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/reorder/',
            data={'order': [
                {'id': str(vegan.id), 'display_order': 7},
                {'id': str(spicy.id), 'display_order': 3},
            ]},
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        vegan.refresh_from_db()
        spicy.refresh_from_db()
        self.assertEqual(vegan.display_order, 7)
        self.assertEqual(spicy.display_order, 3)

    def test_reorder_rejects_mixed_restaurant_payload(self):
        # A foreign-restaurant tag anywhere in the payload rejects the WHOLE
        # request and writes nothing (not even the caller's own rows).
        from restaurants_app.models import RestaurantTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        b_tag = RestaurantTag.objects.get(restaurant=self.restaurant_b, name='Vegan')
        a_before, b_before = a_tag.display_order, b_tag.display_order
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/reorder/',
            data={'order': [
                {'id': str(a_tag.id), 'display_order': 40},
                {'id': str(b_tag.id), 'display_order': 41},
            ]},
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        a_tag.refresh_from_db()
        b_tag.refresh_from_db()
        self.assertEqual(a_tag.display_order, a_before)
        self.assertEqual(b_tag.display_order, b_before)

    def test_reorder_foreign_only_denied(self):
        # Only foreign tags → single (foreign) restaurant → module gate 403.
        from restaurants_app.models import RestaurantTag
        b_tag = RestaurantTag.objects.get(restaurant=self.restaurant_b, name='Spicy')
        b_before = b_tag.display_order
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/reorder/',
            data={'order': [{'id': str(b_tag.id), 'display_order': 42}]},
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)
        b_tag.refresh_from_db()
        self.assertEqual(b_tag.display_order, b_before)

    def test_reorder_rejects_unknown_id(self):
        from restaurants_app.models import RestaurantTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        a_before = a_tag.display_order
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/reorder/',
            data={'order': [
                {'id': str(a_tag.id), 'display_order': 3},
                {'id': '00000000-0000-0000-0000-000000000000', 'display_order': 4},
            ]},
            content_type='application/json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        a_tag.refresh_from_db()
        self.assertEqual(a_tag.display_order, a_before)

    def test_reorder_unauthenticated_rejected(self):
        from restaurants_app.models import RestaurantTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        response = self.client.post(
            '/api/v1/restaurant-setup/restaurant-tags/reorder/',
            data={'order': [{'id': str(a_tag.id), 'display_order': 0}]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    # -- usage-count (GET restaurant-tags/<id>/usage-count/) ----------------

    def _link_items(self, tag, count):
        from restaurants_app.models import MenuItemTag
        section = MenuSection.objects.create(
            name='Mains', restaurant=tag.restaurant,
        )
        items = []
        for i in range(count):
            item = MenuItem.objects.create(
                name=f'Dish {i}', section=section, primary_price=1000,
            )
            MenuItemTag.objects.create(menu_item=item, tag=tag)
            items.append(item)
        return items

    def test_usage_count_returns_number(self):
        from restaurants_app.models import RestaurantTag
        tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Spicy')
        self._link_items(tag, 2)
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/usage-count/',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['count'], 2)

    def test_usage_count_excludes_soft_deleted_items(self):
        from restaurants_app.models import RestaurantTag
        tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Spicy')
        items = self._link_items(tag, 2)
        items[0].deleted = True
        items[0].save(update_fields=['deleted'])
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/usage-count/',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['count'], 1)

    def test_usage_count_rejects_cross_restaurant(self):
        from restaurants_app.models import RestaurantTag
        b_tag = RestaurantTag.objects.get(restaurant=self.restaurant_b, name='Spicy')
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/{b_tag.id}/usage-count/',
            **self._auth(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_usage_count_unauthenticated_rejected(self):
        from restaurants_app.models import RestaurantTag
        tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Spicy')
        response = self.client.get(
            f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/usage-count/'
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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
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
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.table = Table.objects.create(
            number=901, restaurant=self.restaurant,
        )

    def _scan(self, table_id):
        # The scan is credential-only: mint a QR credential for this table and
        # present it in the X-Diner-Credential header (the only entry path).
        from rest_framework.test import APIRequestFactory
        from restaurants_app.controllers.handle_diner_journey import (
            handle_table_scan,
        )
        from restaurants_app.controllers.diner_capability import (
            issue_qr_credential,
        )
        cred = issue_qr_credential(self.restaurant.id, table_id, 1)
        request = APIRequestFactory().get(
            '/api/v1/orders/journey/table-scan/',
            HTTP_X_DINER_CREDENTIAL=cred,
        )
        return handle_table_scan(request)

    def test_table_scan_returns_restaurant_tag_rows_not_jsonfield(self):
        result = self._scan(str(self.table.id))
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
        tag = RestaurantTag.objects.filter(restaurant=self.restaurant).first()
        tag.deleted = True
        tag.save(update_fields=['deleted'])
        result = self._scan(str(self.table.id))
        ids = [t['id'] for t in result['data']['restaurant']['preset_tags']]
        self.assertNotIn(str(tag.id), ids)
        self.assertEqual(len(result['data']['restaurant']['preset_tags']), 13)

    def test_table_scan_ignores_legacy_jsonfield(self):
        self.restaurant.preset_tags = [{'id': 'stale-uuid', 'name': 'Stale'}]
        self.restaurant.save(update_fields=['preset_tags'])
        result = self._scan(str(self.table.id))
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
            status=RestaurantStatus_Live, owner=self.owner,
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
            status=RestaurantStatus_Live, owner=self.owner,
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


class EditInformationTableCleanupTests(TestCase):
    """
    Phase-4 hygiene: the `table` block of EDIT_INFORMATION dropped the phantom
    `available` (its column was removed in migration 0023), the deprecated
    `room_name`/`smoking_zone`/`outdoor_seating` keys, and the misleading
    `min_length` on `number`.

    - A PUT that carries only a now-removed key must no longer report a spurious
      "updated": Secretary sees no editable change and returns 400 "No changes
      detected". The phantom `available` previously forced a false 200 because a
      key with no backing column always compared unequal to the serialized
      (absent) old value.
    - A legitimate editable field still updates through the same PUT path.
    - Creating a table with a normal short `number` still succeeds (NV-04): the
      create path validates against REQUIRED_INFORMATION (min_length 1), never
      the removed EDIT_INFORMATION value.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='EI', last_name='Owner',
            email='ei_table_owner@test.com', phone_number='256700000410',
            username='256700000410', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='EI Table Restaurant', location='loc-ei-table',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _put(self, data):
        return self.client.put(
            '/api/v1/restaurant-setup/tables/',
            data=data,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(self.owner)}',
        )

    def _post(self, data):
        return self.client.post(
            '/api/v1/restaurant-setup/tables/',
            data=data,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(self.owner)}',
        )

    def test_put_only_removed_available_reports_no_changes(self):
        # `available` is no longer EDIT_INFORMATION-editable (its column was
        # dropped in 0023), so a PUT carrying only it must not fake a change.
        response = self._put({'id': str(self.table.id), 'available': True})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('No changes detected', response.json().get('message', ''))

    def test_put_only_deprecated_key_reports_no_changes(self):
        # Same guarantee for a deprecated key removed from the table block.
        response = self._put({'id': str(self.table.id), 'smoking_zone': True})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('No changes detected', response.json().get('message', ''))

    def test_put_legitimate_field_still_updates(self):
        response = self._put(
            {'id': str(self.table.id), 'display_name': 'Patio 1'}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.table.refresh_from_db()
        self.assertEqual(self.table.display_name, 'Patio 1')

    def test_create_table_with_short_number_succeeds(self):
        # NV-04: the removed EDIT_INFORMATION min_length never gated create; a
        # normal short number validates against REQUIRED_INFORMATION (>= 1).
        response = self._post(
            {'number': 5, 'restaurant': str(self.restaurant.id)}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            Table.objects.filter(
                restaurant=self.restaurant, str_number='5', deleted=False,
            ).exists()
        )


class DinerTableScanTests(TestCase):
    """
    Hardening + perf regression tests for the public diner QR table-scan
    (handle_table_scan / OrderJourneyEndpoint). The endpoint is AllowAny, so
    the protections under test are input validation and table-state gating —
    not authorization — plus the collapsed serializer queries.
    """

    SCAN_PATH = '/api/v1/orders/journey/table-scan/'

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.table = Table.objects.create(
            number=101, restaurant=self.restaurant,
        )

    def _scan(self, table_id=None, credential=None):
        # The scan is credential-only. Mint a QR credential for the requested
        # table id (generation 1, matching a freshly-created table) and present
        # it in the X-Diner-Credential header — the ONLY entry path. State-gating
        # cases mutate the table AFTER minting, so the live re-check denies them.
        from rest_framework.test import APIRequestFactory
        from restaurants_app.controllers.handle_diner_journey import (
            handle_table_scan,
        )
        from restaurants_app.controllers.diner_capability import (
            issue_qr_credential,
        )
        extra = {}
        if credential is not None:
            extra['HTTP_X_DINER_CREDENTIAL'] = credential
        elif table_id is not None:
            extra['HTTP_X_DINER_CREDENTIAL'] = issue_qr_credential(
                self.restaurant.id, table_id, 1,
            )
        request = APIRequestFactory().get(self.SCAN_PATH, **extra)
        return handle_table_scan(request)

    # ── input validation: never 500 ───────────────────────
    def test_missing_credential_returns_400_not_500(self):
        # No credential at all -> clean 400 (never 500), no session minted.
        response = self._scan()
        self.assertEqual(response['status'], 400)
        self.assertNotIn('data', response)

    def test_malformed_credential_returns_400(self):
        response = self._scan(credential='not-a-real-token')
        self.assertEqual(response['status'], 400)
        self.assertNotIn('data', response)

    def test_endpoint_missing_credential_does_not_500(self):
        response = self.client.get(self.SCAN_PATH)
        self.assertEqual(response.status_code, 400)

    def test_endpoint_raw_table_query_is_ignored(self):
        # A raw ?table= (real uuid or garbage) is not authority — with no
        # credential the scan cleanly 400s and mints no session, either way.
        for raw in (str(self.table.id), 'not-a-uuid'):
            response = self.client.get(self.SCAN_PATH + '?table=' + raw)
            self.assertEqual(response.status_code, 400, msg=raw)
            self.assertNotIn('data', response.json())

    # ── state gating: dead tables must not resolve ────────
    def test_unknown_uuid_returns_404(self):
        response = self._scan('00000000-0000-0000-0000-000000000000')
        self.assertEqual(response['status'], 404)
        self.assertNotIn('data', response)

    def test_deleted_table_returns_404(self):
        self.table.deleted = True
        self.table.save(update_fields=['deleted'])
        self.assertEqual(self._scan(str(self.table.id))['status'], 404)

    def test_disabled_table_returns_404(self):
        self.table.enabled = False
        self.table.save(update_fields=['enabled'])
        self.assertEqual(self._scan(str(self.table.id))['status'], 404)

    def test_inactive_table_returns_404(self):
        self.table.is_active = False
        self.table.save(update_fields=['is_active'])
        self.assertEqual(self._scan(str(self.table.id))['status'], 404)

    def test_out_of_service_table_returns_404(self):
        self.table.status = 'out_of_service'
        self.table.save(update_fields=['status'])
        self.assertEqual(self._scan(str(self.table.id))['status'], 404)

    def test_endpoint_disabled_table_returns_404(self):
        self.table.enabled = False
        self.table.save(update_fields=['enabled'])
        from restaurants_app.controllers.diner_capability import (
            issue_qr_credential,
        )
        cred = issue_qr_credential(
            self.restaurant.id, self.table.id, self.table.qr_version,
        )
        response = self.client.get(self.SCAN_PATH, HTTP_X_DINER_CREDENTIAL=cred)
        self.assertEqual(response.status_code, 404)

    # ── happy path / occupancy / reserved ─────────────────
    def test_available_table_returns_200(self):
        response = self._scan(str(self.table.id))
        self.assertEqual(response['status'], 200)
        data = response['data']
        for key in ('id', 'available', 'current_order', 'restaurant'):
            self.assertIn(key, data)
        self.assertEqual(
            data['current_order'], {'ongoing': False, 'order_id': None},
        )
        self.assertTrue(data['available']['available'])

    def test_scan_restaurant_payload_includes_socials(self):
        # socials rides through to the diner scan payload as the raw dict —
        # no normalization (empty handles stay '' for the frontend to filter).
        self.restaurant.socials = {
            'instagram': 'dinify', 'facebook': '', 'x': '', 'tiktok': '',
        }
        self.restaurant.save(update_fields=['socials'])
        response = self._scan(str(self.table.id))
        self.assertEqual(response['status'], 200)
        socials = response['data']['restaurant']['socials']
        self.assertEqual(
            set(socials.keys()), {'instagram', 'facebook', 'x', 'tiktok'},
        )
        self.assertEqual(socials['instagram'], 'dinify')
        self.assertEqual(socials['facebook'], '')  # empty rides through raw

    def test_occupied_table_still_scannable(self):
        # An ongoing (SUBMITTED) order must NOT block a scan — the diner resumes
        # it. order_status is 'pending' because an 'initiated' draft no longer
        # occupies the table (it claims the table only at submit).
        from orders_app.models import Order
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            payment_status='pending', order_status='pending',
            fulfilment_status='new',
        )
        response = self._scan(str(self.table.id))
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['data']['current_order']['ongoing'])
        self.assertEqual(
            str(response['data']['current_order']['order_id']), str(order.id),
        )

    def test_served_order_is_not_ongoing(self):
        # Regression: a served order frees the table even though diner payment is
        # unwired (payment_status stays 'pending'). current_order must agree with
        # the kitchen board, which drops served tickets off the active feed — the
        # old order_status/payment_status check wrongly flagged this as ongoing.
        from orders_app.models import Order
        Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            payment_status='pending', order_status='served',
            fulfilment_status='served',
        )
        current = self._scan(str(self.table.id))['data']['current_order']
        self.assertFalse(current['ongoing'])
        self.assertIsNone(current['order_id'])

    def test_cancelled_order_is_not_ongoing(self):
        from orders_app.models import Order
        Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            payment_status='pending', order_status='cancelled',
            fulfilment_status='new',
        )
        current = self._scan(str(self.table.id))['data']['current_order']
        self.assertFalse(current['ongoing'])

    def test_reserved_table_returns_400(self):
        self.table.reserved = True
        self.table.save(update_fields=['reserved'])
        response = self._scan(str(self.table.id))
        self.assertEqual(response['status'], 400)
        self.assertIn('reserved', response['message'].lower())

    # ── model helper ──────────────────────────────────────
    def test_is_available_for_scan_truth_table(self):
        self.assertTrue(self.table.is_available_for_scan())
        variants = [
            ('deleted', True), ('enabled', False),
            ('is_active', False), ('status', 'out_of_service'),
        ]
        for number, (field, value) in enumerate(variants, start=210):
            t = Table.objects.create(
                number=number, restaurant=self.restaurant, **{field: value},
            )
            self.assertFalse(
                t.is_available_for_scan(), msg=f'{field}={value}',
            )

    # ── perf: collapsed/redundant queries, identical output ──
    def test_scan_read_query_count_and_output(self):
        # The redundant-query refactor (single .first() in get_current_order,
        # reuse of the loaded instance in get_available, select_related on the
        # fetch) must keep the query count low AND leave the serialized output
        # byte-for-byte identical.
        from restaurants_app.serializers import (
            SerializerPublicGetTableDetails,
        )
        from restaurants_app.models import RestaurantTag

        # Make preset_tags deterministic for the snapshot (the seed signal adds
        # a default catalog on restaurant creation).
        RestaurantTag.objects.filter(restaurant=self.restaurant).update(deleted=True)

        def serialize():
            table = (
                Table.objects
                .select_related('restaurant', 'dining_area')
                .get(id=self.table.id)
            )
            return SerializerPublicGetTableDetails(table, many=False).data

        # Warm any one-off caches outside the query-count assertion.
        serialize()
        with self.assertNumQueries(4):
            data = serialize()

        self.assertEqual(data, {
            'id': str(self.table.id),
            'number': self.table.number,
            'room_name': self.table.room_name,
            'prepayment_required': False,
            'available': {'available': True, 'message': 'Available'},
            'current_order': {'ongoing': False, 'order_id': None},
            'restaurant': {
                'id': str(self.restaurant.id),
                'name': self.restaurant.name,
                'logo': None,
                'cover_photo': None,
                'branding_configuration': self.restaurant.branding_configuration,
                'socials': self.restaurant.socials,
                'menu_approval_status':
                    self.restaurant.first_time_menu_approval_decision,
                'preset_tags': [],
            },
            'reserved': False,
            'dining_area': None,
            'display_name': '',
            'min_capacity': 1,
            'max_capacity': 4,
            'shape': 'square',
            'status': 'available',
            'tags': [],
            'qr_mode': 'order_pay',
        })

    def test_scan_read_query_count_flat_with_multiple_orders(self):
        # Guard against a *future* per-order N+1: get_current_order and
        # get_available each issue a single .first(), so adding more orders to
        # the table must NOT add queries. assertNumQueries only catches a
        # per-item regression when the seed has 2+ of the related row, so seed
        # several orders here (the snapshot test above runs with zero).
        from restaurants_app.serializers import (
            SerializerPublicGetTableDetails,
        )
        from orders_app.models import Order
        for _ in range(3):
            Order.objects.create(
                restaurant=self.restaurant, table=self.table,
                total_cost=1000, discounted_cost=1000, savings=0,
                actual_cost=1000, payment_status='pending',
                order_status='initiated', fulfilment_status='new',
            )

        def serialize():
            table = (
                Table.objects
                .select_related('restaurant', 'dining_area')
                .get(id=self.table.id)
            )
            return SerializerPublicGetTableDetails(table, many=False).data

        serialize()  # warm any one-off caches outside the assertion
        with self.assertNumQueries(4):
            serialize()

    def test_get_table_availability_accepts_instance(self):
        # Regression: the new table= path matches the legacy table_id= path.
        from restaurants_app.controllers.tables import get_table_availability
        by_id = get_table_availability(table_id=str(self.table.id))
        by_instance = get_table_availability(table=self.table)
        self.assertEqual(by_id, by_instance)
        self.assertTrue(by_instance['available'])


class UpdateFloorPlanEndpointTests(TestCase):
    """
    Coverage for the atomic floor-plan batch endpoint
    (table-actions/update-floor-plan): full-geometry persistence, all-or-nothing
    rollback on a mid-batch failure, and tenant scoping. The frontend now wires
    onto this endpoint as a single request; these lock in that the server half
    is genuinely atomic and owner/manager-scoped.
    """

    PATH = '/api/v1/restaurant-setup/table-actions/update-floor-plan/'

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='fp_owner_a@test.com', phone_number='256700000210',
            username='256700000210', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Floor A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.t1 = Table.objects.create(number=1, restaurant=self.restaurant_a)
        self.t2 = Table.objects.create(number=2, restaurant=self.restaurant_a)

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='fp_owner_b@test.com', phone_number='256700000220',
            username='256700000220', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Floor B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.t_b = Table.objects.create(number=1, restaurant=self.restaurant_b)

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _post(self, user, body, raise_exception=True):
        # raise_exception=False lets the test inspect a 500 response (rollback
        # path) instead of having the test client re-raise.
        self.client.raise_request_exception = raise_exception
        return self.client.post(
            self.PATH, data=json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(user)}',
        )

    def test_persists_full_geometry_in_one_request(self):
        resp = self._post(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'tables': [
                {'id': str(self.t1.id), 'floor_x': 11, 'floor_y': 22,
                 'floor_width': 5, 'floor_height': 6},
                {'id': str(self.t2.id), 'floor_x': 33, 'floor_y': 44,
                 'floor_width': 7, 'floor_height': 8},
            ],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['updated_count'], 2)
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(
            [self.t1.floor_x, self.t1.floor_y,
             self.t1.floor_width, self.t1.floor_height],
            [11.0, 22.0, 5.0, 6.0],
        )
        self.assertEqual(
            [self.t2.floor_x, self.t2.floor_y,
             self.t2.floor_width, self.t2.floor_height],
            [33.0, 44.0, 7.0, 8.0],
        )

    def test_partial_failure_rolls_back_the_whole_batch(self):
        # t1 is valid and processed first; t2 carries a non-numeric coord that
        # raises mid-loop. The transaction.atomic() wrapper must roll back t1's
        # already-applied write too — no partial desync moved server-side.
        original_x = self.t1.floor_x
        resp = self._post(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'tables': [
                {'id': str(self.t1.id), 'floor_x': 99, 'floor_y': 99},
                {'id': str(self.t2.id), 'floor_x': 'not-a-number'},
            ],
        }, raise_exception=False)
        self.assertEqual(resp.status_code, 500)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.floor_x, original_x)  # rolled back, not 99

    def test_cross_tenant_restaurant_is_forbidden(self):
        # Owner A cannot target restaurant B at all.
        original_x = self.t_b.floor_x
        resp = self._post(self.owner_a, {
            'restaurant': str(self.restaurant_b.id),
            'tables': [{'id': str(self.t_b.id), 'floor_x': 77}],
        })
        self.assertEqual(resp.status_code, 403)
        self.t_b.refresh_from_db()
        self.assertEqual(self.t_b.floor_x, original_x)

    def test_foreign_table_under_own_restaurant_is_skipped(self):
        # Owner A is authorised for A, but a B-owned table id won't match the
        # per-table restaurant filter — silently skipped, never written.
        original_x = self.t_b.floor_x
        resp = self._post(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'tables': [{'id': str(self.t_b.id), 'floor_x': 77}],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['updated_count'], 0)
        self.t_b.refresh_from_db()
        self.assertEqual(self.t_b.floor_x, original_x)

    def test_unauthenticated_request_is_rejected(self):
        resp = self.client.post(
            self.PATH,
            data=json.dumps({
                'restaurant': str(self.restaurant_a.id),
                'tables': [{'id': str(self.t1.id), 'floor_x': 1}],
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 401)


class SubscriptionDetailsGateTests(TestCase):
    """
    Authorization + validation for the subscription-details capability at
    ``/api/v1/restaurant-setup/subscription-details/``.

    WRITE (PUT) is Dinify-admin ONLY — subscription validity/expiry is
    system/billing state that no restaurant user (owner included) may self-set;
    the gate lives inside ``RestaurantSubscription.update``. READ (GET) stays
    gated upstream on the settings module (owner + manager + admin, 404 on
    denial); these tests also cover the controller's input hardening (missing
    restaurant -> 400, unknown restaurant -> 404, never a 500).
    """

    BASE = '/api/v1/restaurant-setup'

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Owner', last_name='A',
            email='sub_owner_a@test.com', phone_number='256700000210',
            username='256700000210', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Sub Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        self.manager_a = User.objects.create_user(
            first_name='Manager', last_name='A',
            email='sub_manager_a@test.com', phone_number='256700000211',
            username='256700000211', country='Uganda', password='password',
            roles=[],
        )
        RestaurantEmployee.objects.create(
            user=self.manager_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_MANAGER')],
        )

        self.owner_b = User.objects.create_user(
            first_name='Owner', last_name='B',
            email='sub_owner_b@test.com', phone_number='256700000220',
            username='256700000220', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Sub Restaurant B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Independent dinify admin, employed at neither restaurant.
        self.dinify_admin = User.objects.create_user(
            first_name='Dinify', last_name='Admin',
            email='sub_admin@test.com', phone_number='256700000240',
            username='256700000240', country='Uganda', password='password',
            roles=['dinify_admin'],
        )

    # -- helpers --------------------------------------------------------------

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _put(self, user, body):
        return self.client.put(
            f'{self.BASE}/subscription-details/',
            data=json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(user)}',
        )

    def _get(self, user, restaurant_id):
        return self.client.get(
            f'{self.BASE}/subscription-details/?restaurant={restaurant_id}',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(user)}',
        )

    def _valid_body(self, restaurant_id):
        return {
            'restaurant': str(restaurant_id),
            'subscription_validity': True,
            'subscription_expiry_date': '2030-12-31',
        }

    # -- PUT: write gate (the P0) --------------------------------------------

    def test_put_owner_of_target_restaurant_forbidden(self):
        response = self._put(self.owner_a, self._valid_body(self.restaurant_a.id))
        self.assertEqual(response.status_code, 403)

    def test_put_user_of_different_restaurant_forbidden(self):
        response = self._put(self.owner_b, self._valid_body(self.restaurant_a.id))
        self.assertEqual(response.status_code, 403)

    def test_put_dinify_admin_succeeds_and_persists(self):
        self.assertTrue(self.restaurant_a.subscription_validity)
        response = self._put(self.dinify_admin, {
            'restaurant': str(self.restaurant_a.id),
            'subscription_validity': False,
            'subscription_expiry_date': '2031-01-15T10:00:00Z',
        })
        self.assertEqual(response.status_code, 200)
        self.restaurant_a.refresh_from_db()
        self.assertFalse(self.restaurant_a.subscription_validity)
        self.assertIsNotNone(self.restaurant_a.subscription_expiry_date)
        self.assertEqual(self.restaurant_a.subscription_expiry_date.year, 2031)

    def test_put_owner_forbidden_before_any_db_write(self):
        original = self.restaurant_a.subscription_validity
        self._put(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'subscription_validity': not original,
            'subscription_expiry_date': '2030-12-31',
        })
        self.restaurant_a.refresh_from_db()
        self.assertEqual(self.restaurant_a.subscription_validity, original)

    def test_put_admin_missing_field_returns_400(self):
        response = self._put(self.dinify_admin, {
            'restaurant': str(self.restaurant_a.id),
            'subscription_validity': True,
            # subscription_expiry_date deliberately omitted
        })
        self.assertEqual(response.status_code, 400)

    def test_put_admin_non_boolean_validity_returns_400(self):
        response = self._put(self.dinify_admin, {
            'restaurant': str(self.restaurant_a.id),
            'subscription_validity': 'yes',
            'subscription_expiry_date': '2030-12-31',
        })
        self.assertEqual(response.status_code, 400)

    def test_put_admin_nonexistent_restaurant_returns_404(self):
        import uuid
        response = self._put(self.dinify_admin, self._valid_body(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    # -- GET: read gate preserved (settings) + input hardening ---------------

    def test_get_owner_reads_own_succeeds(self):
        response = self._get(self.owner_a, self.restaurant_a.id)
        self.assertEqual(response.status_code, 200)

    def test_get_manager_reads_own_succeeds(self):
        # Managers keep subscription read access (settings module) under the
        # chosen "keep read as-is" approach.
        response = self._get(self.manager_a, self.restaurant_a.id)
        self.assertEqual(response.status_code, 200)

    def test_get_user_of_different_restaurant_denied(self):
        response = self._get(self.owner_b, self.restaurant_a.id)
        self.assertEqual(response.status_code, 404)

    def test_get_dinify_admin_reads_any(self):
        response = self._get(self.dinify_admin, self.restaurant_a.id)
        self.assertEqual(response.status_code, 200)

    def test_get_admin_missing_restaurant_returns_400(self):
        response = self.client.get(
            f'{self.BASE}/subscription-details/',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(self.dinify_admin)}',
        )
        self.assertEqual(response.status_code, 400)

    def test_get_admin_nonexistent_restaurant_returns_404(self):
        import uuid
        response = self._get(self.dinify_admin, uuid.uuid4())
        self.assertEqual(response.status_code, 404)


class AdminRegisterRestaurantAuthorizationTests(TestCase):
    """
    Endpoint-level authorization for the admin restaurant-registration
    capability at ``POST /api/v1/restaurant-setup/admin-register-restaurant/``.

    This branch creates a restaurant, mints an owner ``User`` account and
    dispatches credential SMS/email (``self_register`` with ``skip_otp=True`` —
    no phone-ownership check), so it is Dinify-admin ONLY. The gate lives at the
    endpoint (the trust boundary, and the only place ``request.user`` exists —
    the controller receives an ``auth_info`` dict). Any other authenticated
    user (a restaurant owner or a plain diner) must get a side-effect-free 403.

    The existing controller-unit test ``test_admin_register_restaurant`` calls
    the controller directly and is unaffected — the gate is at the endpoint.
    """

    BASE = '/api/v1/restaurant-setup'

    # A restaurant/owner that the admin-register call would mint on success.
    NEW_RESTAURANT_NAME = 'Admin Registered Restaurant'
    NEW_OWNER_PHONE = '256788888888'

    def setUp(self):
        # Dinify admin, employed at no restaurant.
        self.dinify_admin = User.objects.create_user(
            first_name='Dinify', last_name='Admin',
            email='ar_admin@test.com', phone_number='256700000310',
            username='256700000310', country='Uganda', password='password',
            roles=['dinify_admin'],
        )

        # A restaurant owner (non-admin): a plain user plus an active
        # owner employment on their own restaurant.
        self.owner = User.objects.create_user(
            first_name='Owner', last_name='AR',
            email='ar_owner@test.com', phone_number='256700000320',
            username='256700000320', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='AR Existing Restaurant', location='ar-loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # A plain authenticated user: no admin role, no employment.
        self.plain_user = User.objects.create_user(
            first_name='Plain', last_name='User',
            email='ar_plain@test.com', phone_number='256700000330',
            username='256700000330', country='Uganda', password='password',
            roles=[],
        )

    # -- helpers --------------------------------------------------------------

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _post(self, user, body):
        return self.client.post(
            f'{self.BASE}/admin-register-restaurant/',
            data=json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token_for(user)}',
        )

    def _valid_body(self):
        return {
            'name': self.NEW_RESTAURANT_NAME,
            'location': 'AR Test Location',
            'first_name': 'New',
            'last_name': 'Owner',
            'email': 'ar_new_owner@test.com',
            'phone_number': self.NEW_OWNER_PHONE,
            'country': 'UG',
        }

    def _assert_nothing_minted(self):
        self.assertFalse(
            Restaurant.objects.filter(name=self.NEW_RESTAURANT_NAME).exists()
        )
        self.assertFalse(
            User.objects.filter(phone_number=self.NEW_OWNER_PHONE).exists()
        )

    # -- the gate (the P1) ----------------------------------------------------

    def test_restaurant_owner_forbidden(self):
        response = self._post(self.owner, self._valid_body())
        self.assertEqual(response.status_code, 403)
        # The real harm is minting an account + credential SMS: assert the
        # denial happened with no side effects.
        self._assert_nothing_minted()

    def test_plain_user_forbidden(self):
        response = self._post(self.plain_user, self._valid_body())
        self.assertEqual(response.status_code, 403)
        self._assert_nothing_minted()

    def test_dinify_admin_succeeds(self):
        response = self._post(self.dinify_admin, self._valid_body())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            Restaurant.objects.filter(name=self.NEW_RESTAURANT_NAME).exists()
        )
        self.assertTrue(
            User.objects.filter(phone_number=self.NEW_OWNER_PHONE).exists()
        )


class TestDinerJourneyDetailHardening(TestCase):
    """(BUG-P2-3d + PR 7A) The public order-details / transaction-details journey
    controllers now require an opaque diner table SESSION (not raw id knowledge)
    and scope every lookup to the session's restaurant+table. A malformed
    (non-UUID), nonexistent or foreign id must return a clean, NON-DISCLOSING 404
    dict — the endpoint maps the dict's status straight to the HTTP code — instead
    of letting ValidationError / DoesNotExist surface as a 500 (and instead of the
    old distinct 400-for-malformed, which would confirm the id was junk). The
    None-guards ("please provide ...") still fire once the session resolves, and a
    valid id on the session's own table still succeeds."""

    # A syntactically valid UUID that is never seeded -> DoesNotExist -> 404.
    NONEXISTENT_ID = '00000000-0000-4000-8000-000000000000'
    # Not a UUID at all -> UUIDField ValidationError, folded into the 404.
    MALFORMED_ID = 'not-a-uuid'

    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_tables()
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER1)

    def _request(self, **params):
        # A GET request carrying a valid diner session for self.table in the
        # X-Diner-Session header, plus any query params the controller reads.
        from rest_framework.test import APIRequestFactory
        from restaurants_app.controllers.diner_capability import (
            issue_table_session,
        )
        # Drop None-valued params so a "missing id" case sends no query key.
        query = {k: v for k, v in params.items() if v is not None}
        return APIRequestFactory().get(
            '/api/v1/orders/journey/', query,
            HTTP_X_DINER_SESSION=issue_table_session(self.table),
        )

    # --- order-details ---

    def test_order_details_malformed_id_folds_into_404(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_order_details,
        )
        self.assertEqual(
            handle_show_order_details(
                self._request(order=self.MALFORMED_ID)
            )['status'],
            404,
        )

    def test_order_details_nonexistent_id_returns_404(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_order_details,
        )
        self.assertEqual(
            handle_show_order_details(
                self._request(order=self.NONEXISTENT_ID)
            )['status'],
            404,
        )

    def test_order_details_missing_id_returns_400(self):
        # None short-circuits at the existing "please provide" guard (which runs
        # AFTER the session resolves).
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_order_details,
        )
        self.assertEqual(
            handle_show_order_details(self._request(order=None))['status'], 400
        )

    def test_order_details_valid_id_succeeds(self):
        from orders_app.models import Order
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_order_details,
        )
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            prepayment_required=False,
            payment_status='pending', order_status='initiated',
        )
        result = handle_show_order_details(self._request(order=str(order.id)))
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['data']['id'], str(order.id))

    # --- transaction-details ---

    def test_transaction_details_malformed_id_folds_into_404(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_transaction_details,
        )
        self.assertEqual(
            handle_show_transaction_details(
                self._request(transaction=self.MALFORMED_ID)
            )['status'],
            404,
        )

    def test_transaction_details_nonexistent_id_returns_404(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_transaction_details,
        )
        self.assertEqual(
            handle_show_transaction_details(
                self._request(transaction=self.NONEXISTENT_ID)
            )['status'],
            404,
        )

    def test_transaction_details_missing_id_returns_400(self):
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_transaction_details,
        )
        self.assertEqual(
            handle_show_transaction_details(
                self._request(transaction=None)
            )['status'],
            400,
        )

    def test_transaction_details_valid_id_succeeds(self):
        from decimal import Decimal
        from orders_app.models import Order
        from finance_app.models import DinifyTransaction
        from dinify_backend.configss.string_definitions import (
            TransactionType_OrderPayment, TransactionStatus_Success,
            TransactionPlatform_Web,
        )
        from restaurants_app.controllers.handle_diner_journey import (
            handle_show_transaction_details,
        )
        # The transaction is diner-visible only through its order, which must be
        # on the session's table.
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            payment_status='pending', order_status='served',
        )
        txn = DinifyTransaction.objects.create(
            restaurant=self.restaurant, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_platform=TransactionPlatform_Web,
            transaction_amount=Decimal('1000.00'),
        )
        result = handle_show_transaction_details(
            self._request(transaction=str(txn.id))
        )
        self.assertEqual(result['status'], 200)
        self.assertEqual(str(result['data']['id']), str(txn.id))


class RestaurantAdminOnlyFieldGuardTests(TestCase):
    """
    BUG-P3-2 + PR-5: platform-owned restaurant fields on the restaurant-setup PUT.

    `flat_fee` (the Dinify subscription price billed by finance_app
    tx_subscription) keeps the original contract: a tenant (owner/manager) PUT
    carrying it has it silently stripped — matching how Secretary ignores
    non-applicable fields — while the rest of the edit still applies, and a Dinify
    admin retains write access.

    `status` is now stricter than "admin-only". PR-5 made the lifecycle a
    constrained axis with exactly ONE writer (restaurants_app.controllers.lifecycle,
    behind the elevation-gated admin transition endpoint): the field left
    EDIT_INFORMATION and is read_only on SerializerPutRestaurant, so NOBODY writes
    it here — not a tenant, and not a Dinify admin. The legacy admin
    changeApprovalStatus PUT is retired.

    Reachability: module access (the write gate) requires a lifecycle state that
    grants portal access (onboarding or live), so a tenant at a suspended
    restaurant is denied 403 at the gate before any field handling. Note the PR-5
    widening — an ONBOARDING restaurant now passes that gate, where `pending` used
    to fail it — which is why the denied fixture below is suspended, not onboarding.
    """

    def setUp(self):
        from decimal import Decimal
        # Active restaurant + owner: the reachable case (owner has settings-module
        # access precisely because the restaurant is active).
        self.owner = User.objects.create_user(
            first_name='Owner', last_name='Active',
            email='owner_active_p3@test.com', phone_number='256700000210',
            username='256700000210', country='Uganda', password='password',
            roles=[],
        )
        self.active_restaurant = Restaurant.objects.create(
            name='Active Bistro', location='loc-active',
            status=RestaurantStatus_Live, owner=self.owner,
            flat_fee=Decimal('2500.00'),
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.active_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Onboarding restaurant + owner: PR-5 GRANTS this owner portal access,
        # so their PUT reaches the field handling rather than dying at the gate.
        self.onboarding_owner = User.objects.create_user(
            first_name='Owner', last_name='Onboarding',
            email='owner_pending_p3@test.com', phone_number='256700000211',
            username='256700000211', country='Uganda', password='password',
            roles=[],
        )
        self.onboarding_restaurant = Restaurant.objects.create(
            name='Onboarding Bistro', location='loc-onboarding',
            status=RestaurantStatus_Onboarding, owner=self.onboarding_owner,
        )
        RestaurantEmployee.objects.create(
            user=self.onboarding_owner, restaurant=self.onboarding_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Suspended restaurant + owner: the enforcement lever; write blocked at gate.
        self.suspended_owner = User.objects.create_user(
            first_name='Owner', last_name='Suspended',
            email='owner_blocked_p3@test.com', phone_number='256700000212',
            username='256700000212', country='Uganda', password='password',
            roles=[],
        )
        self.suspended_restaurant = Restaurant.objects.create(
            name='Suspended Bistro', location='loc-suspended',
            status=RestaurantStatus_Suspended, owner=self.suspended_owner,
        )
        RestaurantEmployee.objects.create(
            user=self.suspended_owner, restaurant=self.suspended_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # Independent Dinify admin (no employment anywhere).
        self.dinify_admin = User.objects.create_user(
            first_name='Dinify', last_name='Admin',
            email='admin_p3@test.com', phone_number='256700000213',
            username='256700000213', country='Uganda', password='password',
            roles=['dinify_admin'],
        )

    def _token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _put_restaurant(self, user, body):
        token = self._token_for(user)
        return self.client.put(
            '/api/v1/restaurant-setup/restaurants/',
            data=body,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    # -- tenant strip on an active restaurant (the reachable case) -----------

    def test_tenant_status_and_flat_fee_stripped_name_applies(self):
        """Field-level strip: a normal field applies; status + flat_fee do not."""
        from decimal import Decimal
        response = self._put_restaurant(self.owner, {
            'id': str(self.active_restaurant.id),
            'name': 'Renamed Bistro',
            'status': 'inactive',
            'flat_fee': '0.00',
        })
        self.assertEqual(response.status_code, 200)
        self.active_restaurant.refresh_from_db()
        self.assertEqual(self.active_restaurant.name, 'Renamed Bistro')
        self.assertEqual(self.active_restaurant.status, RestaurantStatus_Live)
        self.assertEqual(self.active_restaurant.flat_fee, Decimal('2500.00'))

    def test_tenant_status_only_write_is_stripped_noop(self):
        """
        A status-only tenant PUT is fully stripped, so Secretary finds no
        applicable field and returns 400 'No changes detected' — crucially,
        status is NOT applied. Tenants never send status-only in the real
        portal; realistic edits carry other fields (see the test above).
        """
        response = self._put_restaurant(self.owner, {
            'id': str(self.active_restaurant.id),
            'status': 'inactive',
        })
        self.assertEqual(response.status_code, 400)
        self.active_restaurant.refresh_from_db()
        self.assertEqual(self.active_restaurant.status, RestaurantStatus_Live)

    def test_tenant_flat_fee_only_write_is_stripped_noop(self):
        """flat_fee-only tenant PUT is stripped: 400 no-op, subscription price unchanged."""
        from decimal import Decimal
        response = self._put_restaurant(self.owner, {
            'id': str(self.active_restaurant.id),
            'flat_fee': '0.00',
        })
        self.assertEqual(response.status_code, 400)
        self.active_restaurant.refresh_from_db()
        self.assertEqual(self.active_restaurant.flat_fee, Decimal('2500.00'))

    def test_tenant_normal_edit_without_admin_fields_unaffected(self):
        """Regression: an edit carrying no admin-only field behaves exactly as before."""
        response = self._put_restaurant(self.owner, {
            'id': str(self.active_restaurant.id),
            'name': 'Just A Rename',
        })
        self.assertEqual(response.status_code, 200)
        self.active_restaurant.refresh_from_db()
        self.assertEqual(self.active_restaurant.name, 'Just A Rename')

    # -- dinify admin retains full write (the legitimate path) ---------------

    def test_admin_status_write_is_now_ignored_too(self):
        """
        The retired changeApprovalStatus flow: an admin PUT {id, status} no longer
        moves the lifecycle. Status-only, so nothing applicable remains and
        Secretary returns 400 — the point is that the state does not change.
        """
        response = self._put_restaurant(self.dinify_admin, {
            'id': str(self.onboarding_restaurant.id),
            'status': 'live',
        })
        self.assertEqual(response.status_code, 400)
        self.onboarding_restaurant.refresh_from_db()
        self.assertEqual(
            self.onboarding_restaurant.status, RestaurantStatus_Onboarding,
        )

    def test_admin_status_write_alongside_real_edit_is_dropped(self):
        """The companion case: the legitimate field applies, the lifecycle does not."""
        response = self._put_restaurant(self.dinify_admin, {
            'id': str(self.onboarding_restaurant.id),
            'name': 'Admin Renamed Bistro',
            'status': 'live',
        })
        self.assertEqual(response.status_code, 200)
        self.onboarding_restaurant.refresh_from_db()
        self.assertEqual(self.onboarding_restaurant.name, 'Admin Renamed Bistro')
        self.assertEqual(
            self.onboarding_restaurant.status, RestaurantStatus_Onboarding,
        )

    def test_admin_flat_fee_write_applies(self):
        """Admin retains flat_fee (subscription price) write access."""
        from decimal import Decimal
        response = self._put_restaurant(self.dinify_admin, {
            'id': str(self.active_restaurant.id),
            'flat_fee': '1500.00',
        })
        self.assertEqual(response.status_code, 200)
        self.active_restaurant.refresh_from_db()
        self.assertEqual(self.active_restaurant.flat_fee, Decimal('1500.00'))

    # -- existing gate already blocks non-active restaurants (documentation) -

    def test_tenant_put_on_onboarding_restaurant_reaches_the_gate(self):
        """
        THE PR-5 WIDENING at the write gate: an ONBOARDING owner is no longer 403'd
        (the resolver used to demand `active`), so a real edit applies — while the
        self-promotion to `live` is still refused, now by the read_only field rather
        than by the gate. Both halves matter: access widened, authority did not.
        """
        response = self._put_restaurant(self.onboarding_owner, {
            'id': str(self.onboarding_restaurant.id),
            'name': 'Owner Renamed Bistro',
            'status': 'live',
        })
        self.assertEqual(response.status_code, 200)
        self.onboarding_restaurant.refresh_from_db()
        self.assertEqual(self.onboarding_restaurant.name, 'Owner Renamed Bistro')
        self.assertEqual(
            self.onboarding_restaurant.status, RestaurantStatus_Onboarding,
        )

    def test_tenant_put_on_suspended_restaurant_forbidden_at_gate(self):
        """The enforcement lever holds: a suspended restaurant denies at the gate."""
        response = self._put_restaurant(self.suspended_owner, {
            'id': str(self.suspended_restaurant.id),
            'status': 'live',
        })
        self.assertEqual(response.status_code, 403)
        self.suspended_restaurant.refresh_from_db()
        self.assertEqual(
            self.suspended_restaurant.status, RestaurantStatus_Suspended,
        )


class MenuFkTenantBoundaryTests(TestCase):
    """
    Cross-tenant section / section_group reassignment on menu writes (TENANT-P1-03).

    The restaurant-setup UPDATE gate authorizes against the record's CURRENT tenant
    (existing section__restaurant) and never evaluates the DESTINATION restaurant
    implied by a reassigned FK. SerializerPutMenuItem.validate() previously reached
    the section->restaurant resolution only BELOW an ``if tag_ids is None: return
    attrs`` early-return, so a menu-item PUT that omitted tag_ids moved section /
    section_group across tenants unchecked. Those two guards are LOAD-BEARING — they
    close a live exploit.

    SerializerPutSectionGroup.validate() is DEFENSE-IN-DEPTH: ``section`` is not in
    EI_SECTION_GROUP, so Secretary strips it before the serializer runs and the
    endpoint path is already closed. It is exercised only by a direct serializer
    test (test_sectiongroup_serializer_rejects_foreign_section); the EI-contract
    tripwire (test_section_group_section_is_not_editable) is what actually protects
    that path today. A green section-group ENDPOINT test is NOT evidence an exploit
    was closed.
    """

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Fk', last_name='OwnerA',
            email='fk_owner_a@test.com', phone_number='256700000410',
            username='256700000410', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='FK Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.owner_b = User.objects.create_user(
            first_name='Fk', last_name='OwnerB',
            email='fk_owner_b@test.com', phone_number='256700000420',
            username='256700000420', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='FK Restaurant B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # A-side menu graph: two sections + two groups (distinct same-tenant
        # destinations for the positive-control moves), plus the item under attack.
        self.section_a = MenuSection.objects.create(
            name='A Mains', restaurant=self.restaurant_a, listing_position=0,
        )
        self.section_a2 = MenuSection.objects.create(
            name='A Sides', restaurant=self.restaurant_a, listing_position=1,
        )
        self.group_a = SectionGroup.objects.create(
            name='A Group', section=self.section_a,
        )
        self.group_a2 = SectionGroup.objects.create(
            name='A Group 2', section=self.section_a,
        )
        self.item_a = MenuItem.objects.create(
            name='A Item', section=self.section_a,
            section_group=self.group_a, primary_price=1000,
        )

        # B-side menu graph — the cross-tenant targets owner_a will try to reach.
        self.section_b = MenuSection.objects.create(
            name='B Mains', restaurant=self.restaurant_b, listing_position=0,
        )
        self.group_b = SectionGroup.objects.create(
            name='B Group', section=self.section_b,
        )

    # --- helpers --------------------------------------------------------
    def _auth(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _put(self, user, config_detail, body):
        return self.client.put(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    def _post(self, user, config_detail, body):
        return self.client.post(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    # --- 1. LOAD-BEARING: foreign section on menu-item PUT rejected ------
    def test_foreign_section_on_menuitem_put_rejected(self):
        # tag_ids OMITTED — the exact bypass path (the old early-return skipped
        # the only section resolution). The new guard runs above it.
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section': str(self.section_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_id, self.section_a.id)

    # --- 2. LOAD-BEARING: foreign section_group on menu-item PUT rejected -
    def test_foreign_section_group_on_menuitem_put_rejected(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section_group': str(self.group_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_group_id, self.group_a.id)

    # --- 3a. section-group current behaviour (truthful; NOT via the guard) -
    def test_sectiongroup_foreign_section_put_current_behaviour(self):
        # `section` is not in EI_SECTION_GROUP, so Secretary strips it before the
        # serializer runs; the PUT is a no-op (400 "No changes detected"). This
        # pins CURRENT behaviour — closed today by EI field-filtering, NOT by
        # SerializerPutSectionGroup.validate() (which 3b tests directly).
        resp = self._put(
            self.owner_a, 'sectiongroups',
            {'id': str(self.group_a.id), 'section': str(self.section_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.group_a.refresh_from_db()
        self.assertEqual(self.group_a.section_id, self.section_a.id)

    # --- 3b. section-group guard — DIRECT serializer test (exercises the code) -
    def test_sectiongroup_serializer_rejects_foreign_section(self):
        # The ONLY test that reaches SerializerPutSectionGroup.validate() — the
        # endpoint can't (see 3a). Proves the defense-in-depth guard fires.
        from restaurants_app.serializers import SerializerPutSectionGroup
        serializer = SerializerPutSectionGroup(
            instance=self.group_a,
            data={'section': str(self.section_b.id)},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('section', serializer.errors)
        self.group_a.refresh_from_db()
        self.assertEqual(self.group_a.section_id, self.section_a.id)

    # --- 3c. EI-contract tripwire (what actually protects the path today) -
    def test_section_group_section_is_not_editable(self):
        """Tripwire. SectionGroup.section is the tenancy path (section__restaurant).
        Secretary filters PUT data to EI keys, so `section` being ABSENT from
        EI_SECTION_GROUP is what prevents a cross-tenant group move today. If you
        add it, SerializerPutSectionGroup.validate() becomes load-bearing — prove
        it fires end-to-end before changing this test."""
        from dinify_backend.configss.edit_information import EI_SECTION_GROUP
        self.assertNotIn('section', {k['key'] for k in EI_SECTION_GROUP})

    # --- 4a/4b. same-tenant reassignment still works (positive control) --
    def test_same_tenant_section_move_succeeds(self):
        # item_a carries group_a (which belongs to the OLD section), so a bare
        # section move now trips the section/group cohesion invariant (PR3). A
        # same-tenant move is still allowed — it just has to clear (or replace) the
        # now-incompatible group; clear it here as the positive control.
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section': str(self.section_a2.id),
             'section_group': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_id, self.section_a2.id)
        self.assertIsNone(self.item_a.section_group_id)

    def test_same_tenant_section_group_move_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section_group': str(self.group_a2.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_group_id, self.group_a2.id)

    # --- 5. clearing section_group still works --------------------------
    def test_clearing_section_group_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section_group': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item_a.refresh_from_db()
        self.assertIsNone(self.item_a.section_group_id)

    # --- 6a/6b. tag_ids scoping unregressed (block below the early-return) -
    def test_foreign_tag_ids_still_rejected(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        b_tag = RestaurantTag.objects.get(restaurant=self.restaurant_b, name='Vegan')
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'tag_ids': [str(b_tag.id)]},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(MenuItemTag.objects.filter(menu_item=self.item_a).exists())

    def test_same_tenant_tag_ids_still_succeeds(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'tag_ids': [str(a_tag.id)]},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        tagged = set(
            MenuItemTag.objects.filter(menu_item=self.item_a).values_list('tag_id', flat=True)
        )
        self.assertEqual(tagged, {a_tag.id})

    # --- 7a/7b. create path already safe (pin it; do NOT "fix" create) --
    def test_create_menuitem_with_foreign_section_denied(self):
        # _resolve_menuitems('create') authorizes against the SUPPLIED section's
        # restaurant (B), so owner_a is denied at the gate — create was already safe.
        before = MenuItem.objects.filter(section=self.section_b).count()
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Injected', 'section': str(self.section_b.id),
             'primary_price': '1000.00'},
        )
        self.assertIn(resp.status_code, (401, 403), resp.content)
        self.assertEqual(MenuItem.objects.filter(section=self.section_b).count(), before)

    def test_create_sectiongroup_with_foreign_section_denied(self):
        before = SectionGroup.objects.filter(section=self.section_b).count()
        resp = self._post(
            self.owner_a, 'sectiongroups',
            {'name': 'Injected Group', 'section': str(self.section_b.id)},
        )
        self.assertIn(resp.status_code, (401, 403), resp.content)
        self.assertEqual(SectionGroup.objects.filter(section=self.section_b).count(), before)

    # --- 8. malformed / unknown section id -> clean 4xx, never a 500 ----
    def test_malformed_and_unknown_section_are_4xx_not_500(self):
        import uuid
        for bad in ['not-a-uuid', str(uuid.uuid4())]:
            resp = self._put(
                self.owner_a, 'menuitems',
                {'id': str(self.item_a.id), 'section': bad},
            )
            self.assertEqual(resp.status_code, 400, f'{bad}: {resp.content}')
            self.item_a.refresh_from_db()
            self.assertEqual(self.item_a.section_id, self.section_a.id)

    # --- adversarial: both FKs foreign together; and with tag_ids present -
    def test_foreign_section_and_group_together_rejected(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section': str(self.section_b.id),
             'section_group': str(self.group_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_id, self.section_a.id)
        self.assertEqual(self.item_a.section_group_id, self.group_a.id)

    def test_foreign_section_rejected_even_with_valid_tag_ids(self):
        # The guard fires ABOVE the tag_ids early-return, so a present same-tenant
        # tag_ids does not let a foreign section slip through.
        from restaurants_app.models import RestaurantTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item_a.id), 'section': str(self.section_b.id),
             'tag_ids': [str(a_tag.id)]},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.section_id, self.section_a.id)


class MenuItemSectionGroupCohesionTests(TestCase):
    """
    Section-group cohesion on menu-item CREATE and UPDATE (TENANT-P1-03, PR 3).

    Invariant: a MenuItem.section_group, when present, must belong to the EXACT
    MenuSection assigned to the item (``SectionGroup.section == MenuItem.section``).
    Enforced in ``SerializerPutMenuItem.validate()`` on BOTH paths. The rule is
    stronger than same-restaurant and subsumes it — if the group's section is the
    item's section, they share a restaurant by definition.

    Prior gap: the FK guard was gated behind ``if self.instance is not None`` so it
    never ran on create, and even on update it only required the group to resolve
    to the same RESTAURANT (not the same section). The create-path endpoint gate
    (``_resolve_menuitems('create')``) authorizes on the submitted ``section``
    ONLY, so a foreign / wrong-section ``section_group`` could be injected on
    create (it resolves globally via the auto ``PrimaryKeyRelatedField``).

    Fixtures: ``section_1`` / ``section_2`` both belong to ``restaurant_a``;
    ``group_1a`` and ``group_1b`` live in ``section_1`` and ``group_2a`` lives in
    ``section_2`` (the same-restaurant / different-section target). ``restaurant_b``'s
    ``section_b`` / ``group_b`` are the cross-tenant targets ``owner_a`` will reach for.
    """

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Cohesion', last_name='OwnerA',
            email='cohesion_owner_a@test.com', phone_number='256700000510',
            username='256700000510', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Cohesion Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.owner_b = User.objects.create_user(
            first_name='Cohesion', last_name='OwnerB',
            email='cohesion_owner_b@test.com', phone_number='256700000520',
            username='256700000520', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Cohesion Restaurant B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # restaurant_a: two sections; two groups in section_1, one in section_2.
        self.section_1 = MenuSection.objects.create(
            name='A Section 1', restaurant=self.restaurant_a, listing_position=0,
        )
        self.section_2 = MenuSection.objects.create(
            name='A Section 2', restaurant=self.restaurant_a, listing_position=1,
        )
        self.group_1a = SectionGroup.objects.create(
            name='Group 1A', section=self.section_1,
        )
        self.group_1b = SectionGroup.objects.create(
            name='Group 1B', section=self.section_1,
        )
        self.group_2a = SectionGroup.objects.create(
            name='Group 2A', section=self.section_2,
        )
        self.item = MenuItem.objects.create(
            name='Cohesion Item', section=self.section_1,
            section_group=self.group_1a, primary_price=1000,
        )

        # restaurant_b: the foreign section + group owner_a will try to reach.
        self.section_b = MenuSection.objects.create(
            name='B Section', restaurant=self.restaurant_b, listing_position=0,
        )
        self.group_b = SectionGroup.objects.create(
            name='B Group', section=self.section_b,
        )

    # --- helpers --------------------------------------------------------
    def _auth(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _put(self, user, config_detail, body):
        return self.client.put(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    def _post(self, user, config_detail, body):
        return self.client.post(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    # --- 1. create with a group from the SUBMITTED section succeeds ------
    def test_create_with_group_from_submitted_section_succeeds(self):
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Grouped Create', 'section': str(self.section_1.id),
             'section_group': str(self.group_1a.id), 'primary_price': '1000.00'},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        item = MenuItem.objects.get(name='Grouped Create', section=self.section_1)
        self.assertEqual(item.section_group_id, self.group_1a.id)

    # --- 2. create with a group from another SAME-restaurant section fails -
    def test_create_with_group_from_other_section_same_restaurant_fails(self):
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Wrong Section Group', 'section': str(self.section_1.id),
             'section_group': str(self.group_2a.id), 'primary_price': '1000.00'},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(MenuItem.objects.filter(name='Wrong Section Group').exists())

    # --- 3. create with a group from ANOTHER restaurant fails ------------
    def test_create_with_group_from_another_restaurant_fails(self):
        # The endpoint gate authorizes on section_1 (restaurant_a), so owner_a
        # passes the gate; validate() is what rejects the foreign group — the exact
        # create-path hole this PR closes (section_group resolves globally).
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Foreign Group Create', 'section': str(self.section_1.id),
             'section_group': str(self.group_b.id), 'primary_price': '1000.00'},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(MenuItem.objects.filter(name='Foreign Group Create').exists())

    # --- 4. create WITHOUT a group succeeds -----------------------------
    def test_create_without_group_succeeds(self):
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Groupless Create', 'section': str(self.section_1.id),
             'primary_price': '1000.00'},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        item = MenuItem.objects.get(name='Groupless Create', section=self.section_1)
        self.assertIsNone(item.section_group_id)

    # --- 5. update to a VALID group succeeds ----------------------------
    def test_update_to_valid_group_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section_group': str(self.group_1b.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.section_group_id, self.group_1b.id)

    # --- 6. update to a same-restaurant / different-section group fails --
    def test_update_to_same_restaurant_different_section_group_fails(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section_group': str(self.group_2a.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.section_group_id, self.group_1a.id)

    # --- 7. update to a FOREIGN group fails -----------------------------
    def test_update_to_foreign_group_fails(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section_group': str(self.group_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.section_group_id, self.group_1a.id)

    # --- 8. CLEARING the group succeeds ---------------------------------
    def test_clearing_group_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section_group': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.section_group_id)

    # --- 9. MOVE the item + supply a group from the NEW section succeeds -
    def test_move_item_and_supply_group_from_new_section_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section': str(self.section_2.id),
             'section_group': str(self.group_2a.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.section_id, self.section_2.id)
        self.assertEqual(self.item.section_group_id, self.group_2a.id)

    # --- 10. MOVE the item but RETAIN a group from the OLD section fails -
    def test_move_item_retaining_group_from_old_section_fails(self):
        # Effective section becomes section_2 (submitted); group_1a still points at
        # section_1, so the cohesion invariant rejects the whole PUT.
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'section': str(self.section_2.id),
             'section_group': str(self.group_1a.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.section_id, self.section_1.id)
        self.assertEqual(self.item.section_group_id, self.group_1a.id)

    # --- 11. tag scoping still enforced ALONGSIDE the cohesion invariant -
    def test_valid_group_with_same_tenant_tag_ids_succeeds(self):
        from restaurants_app.models import RestaurantTag, MenuItemTag
        a_tag = RestaurantTag.objects.get(restaurant=self.restaurant_a, name='Vegan')
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Group And Tag', 'section': str(self.section_1.id),
             'section_group': str(self.group_1a.id), 'primary_price': '1000.00',
             'tag_ids': [str(a_tag.id)]},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        item = MenuItem.objects.get(name='Group And Tag', section=self.section_1)
        self.assertEqual(item.section_group_id, self.group_1a.id)
        self.assertTrue(MenuItemTag.objects.filter(menu_item=item, tag=a_tag).exists())

    def test_valid_group_with_foreign_tag_ids_rejected(self):
        # Cohesion passes (group_1a is in section_1) but the foreign tag is still
        # rejected by the tag-scoping block below the guard — proving the two checks
        # compose and neither short-circuits the other.
        from restaurants_app.models import RestaurantTag, MenuItemTag
        b_tag = RestaurantTag.objects.get(restaurant=self.restaurant_b, name='Vegan')
        resp = self._post(
            self.owner_a, 'menuitems',
            {'name': 'Group And Foreign Tag', 'section': str(self.section_1.id),
             'section_group': str(self.group_1a.id), 'primary_price': '1000.00',
             'tag_ids': [str(b_tag.id)]},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(MenuItem.objects.filter(name='Group And Foreign Tag').exists())
        self.assertFalse(MenuItemTag.objects.filter(tag=b_tag).exists())

    # --- 12. a failed request does NOT partially change the item --------
    def test_failed_update_does_not_partially_change_item(self):
        # A rejected foreign-group PUT that ALSO renames the item must persist
        # neither change — Secretary only saves when the serializer is valid.
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.item.id), 'name': 'Should Not Persist',
             'section_group': str(self.group_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Cohesion Item')
        self.assertEqual(self.item.section_group_id, self.group_1a.id)


class TablesNestedFkTenantBoundaryTests(TestCase):
    """
    Cross-tenant nested-FK isolation for tables-domain writes (TENANT-P2-01).

    The reservations / waitlist / tables write paths gate the PARENT record's
    restaurant and never re-scope the nested FKs (reservation table/server,
    waitlist seated_table, table dining_area) or the parent `restaurant` on a
    reassigning PUT. A shared validator (assert_fks_belong_to_restaurant) wired
    into the three serializers' validate(), plus a scoped reservation fetch in
    _seat, binds every nested FK to the gated restaurant.

    LOAD-BEARING: the reservation/waitlist `restaurant` pin, all nested FKs, and
    the _seat scope. DEFENSE-IN-DEPTH: the SerializerPutTable `restaurant` pin —
    Secretary strips `restaurant` (not an EDIT_INFORMATION['table'] key), so it is
    exercised only by the direct serializer test + the EI tripwire, never a table
    endpoint PUT. A green table-endpoint test is NOT evidence the table pin fired.
    """

    def setUp(self):
        from django.utils import timezone
        # --- Tenant A ---
        self.owner_a = User.objects.create_user(
            first_name='TFk', last_name='OwnerA',
            email='tfk_owner_a@test.com', phone_number='256700000530',
            username='256700000530', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='TFK Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.server_user_a = User.objects.create_user(
            first_name='TFk', last_name='ServerA',
            email='tfk_server_a@test.com', phone_number='256700000531',
            username='256700000531', country='Uganda', password='password',
            roles=[],
        )
        self.server_a = RestaurantEmployee.objects.create(
            user=self.server_user_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_WAITER')],
        )
        self.dining_area_a = DiningArea.objects.create(
            name='A Patio', restaurant=self.restaurant_a,
        )
        self.dining_area_a2 = DiningArea.objects.create(
            name='A Garden', restaurant=self.restaurant_a,
        )
        self.table_a = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_a,
            dining_area=self.dining_area_a,
        )
        self.table_a2 = Table.objects.create(
            number=2, str_number='2', restaurant=self.restaurant_a,
        )
        self.reservation_a = Reservation.objects.create(
            restaurant=self.restaurant_a, guest_name='Alice A',
            date_time=timezone.now(), party_size=2,
            table=self.table_a, server=self.server_a,
        )
        self.waitlist_a = WaitlistEntry.objects.create(
            restaurant=self.restaurant_a, guest_name='Wait A', party_size=3,
            seated_table=self.table_a,
        )

        # --- Tenant B (the cross-tenant targets) ---
        self.owner_b = User.objects.create_user(
            first_name='TFk', last_name='OwnerB',
            email='tfk_owner_b@test.com', phone_number='256700000540',
            username='256700000540', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='TFK Restaurant B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.server_user_b = User.objects.create_user(
            first_name='TFk', last_name='ServerB',
            email='tfk_server_b@test.com', phone_number='256700000541',
            username='256700000541', country='Uganda', password='password',
            roles=[],
        )
        self.server_b = RestaurantEmployee.objects.create(
            user=self.server_user_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_WAITER')],
        )
        self.dining_area_b = DiningArea.objects.create(
            name='B Patio', restaurant=self.restaurant_b,
        )
        self.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_b,
        )
        self.reservation_b = Reservation.objects.create(
            restaurant=self.restaurant_b, guest_name='Bob B',
            date_time=timezone.now(), party_size=2,
        )
        self.waitlist_b = WaitlistEntry.objects.create(
            restaurant=self.restaurant_b, guest_name='Wait B',
        )

    # --- helpers --------------------------------------------------------
    def _token(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user).access_token)

    def _request(self, user, method, config_detail, body):
        return getattr(self.client, method)(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._token(user)}',
        )

    # --- 1. reservation row-move (LOAD-BEARING parent pin) --------------
    def test_reservation_put_foreign_restaurant_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.restaurant_id, self.restaurant_a.id)

    # --- 2. reservation foreign table (POST + PUT) ---------------------
    def test_reservation_post_foreign_table_rejected(self):
        resp = self._request(
            self.owner_a, 'post', 'reservations',
            {'restaurant': str(self.restaurant_a.id), 'guest_name': 'NewTbl',
             'date_time': '2026-07-14T19:00:00Z', 'table': str(self.table_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Reservation.objects.filter(guest_name='NewTbl').exists())

    def test_reservation_put_foreign_table_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': str(self.table_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.table_id, self.table_a.id)

    # --- 3. reservation foreign server (POST + PUT) --------------------
    def test_reservation_post_foreign_server_rejected(self):
        resp = self._request(
            self.owner_a, 'post', 'reservations',
            {'restaurant': str(self.restaurant_a.id), 'guest_name': 'NewSrv',
             'date_time': '2026-07-14T19:00:00Z', 'server': str(self.server_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Reservation.objects.filter(guest_name='NewSrv').exists())

    def test_reservation_put_foreign_server_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'server': str(self.server_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.server_id, self.server_a.id)

    # --- 4. waitlist row-move (LOAD-BEARING parent pin) ----------------
    def test_waitlist_put_foreign_restaurant_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'waitlist',
            {'id': str(self.waitlist_a.id), 'restaurant': str(self.restaurant_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.waitlist_a.refresh_from_db()
        self.assertEqual(self.waitlist_a.restaurant_id, self.restaurant_a.id)

    # --- 5. waitlist foreign seated_table (POST + PUT) ----------------
    def test_waitlist_post_foreign_seated_table_rejected(self):
        resp = self._request(
            self.owner_a, 'post', 'waitlist',
            {'restaurant': str(self.restaurant_a.id), 'guest_name': 'WNew',
             'seated_table': str(self.table_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(WaitlistEntry.objects.filter(guest_name='WNew').exists())

    def test_waitlist_put_foreign_seated_table_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'waitlist',
            {'id': str(self.waitlist_a.id), 'seated_table': str(self.table_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.waitlist_a.refresh_from_db()
        self.assertEqual(self.waitlist_a.seated_table_id, self.table_a.id)

    # --- 6. table foreign dining_area on PUT (LOAD-BEARING) -----------
    def test_table_put_foreign_dining_area_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'tables',
            {'id': str(self.table_a.id), 'dining_area': str(self.dining_area_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.dining_area_id, self.dining_area_a.id)

    # --- 7. table foreign dining_area on CREATE ----------------------
    def test_table_post_foreign_dining_area_rejected(self):
        resp = self._request(
            self.owner_a, 'post', 'tables',
            {'number': 99, 'restaurant': str(self.restaurant_a.id),
             'dining_area': str(self.dining_area_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            Table.objects.filter(number=99, restaurant=self.restaurant_a).exists())

    # --- 8. _seat with a foreign reservation leaves B untouched -------
    def test_seat_foreign_reservation_leaves_it_untouched(self):
        original_status = self.reservation_b.status
        resp = self._request(
            self.owner_a, 'post', 'table-actions/seat',
            {'table_id': str(self.table_a.id),
             'reservation_id': str(self.reservation_b.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.reservation_b.refresh_from_db()
        self.assertEqual(self.reservation_b.status, original_status)
        self.assertNotEqual(self.reservation_b.status, 'seated')
        self.assertIsNone(self.reservation_b.seated_at)
        self.assertIsNone(self.reservation_b.table_id)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.status, 'seated')  # table still seats

    # --- 9. _seat with a same-tenant reservation still works ----------
    def test_seat_same_tenant_reservation_succeeds(self):
        resp = self._request(
            self.owner_a, 'post', 'table-actions/seat',
            {'table_id': str(self.table_a.id),
             'reservation_id': str(self.reservation_a.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.status, 'seated')
        self.assertIsNotNone(self.reservation_a.seated_at)
        self.assertEqual(self.reservation_a.table_id, self.table_a.id)

    # --- 10. positive controls: same-tenant FKs + own-restaurant PUT --
    def test_reservation_post_same_tenant_table_and_server_succeeds(self):
        resp = self._request(
            self.owner_a, 'post', 'reservations',
            {'restaurant': str(self.restaurant_a.id), 'guest_name': 'OkRes',
             'date_time': '2026-07-14T19:00:00Z',
             'table': str(self.table_a2.id), 'server': str(self.server_a.id)},
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        created = Reservation.objects.get(guest_name='OkRes')
        self.assertEqual(created.table_id, self.table_a2.id)
        self.assertEqual(created.server_id, self.server_a.id)

    def test_reservation_put_same_tenant_table_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': str(self.table_a2.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.table_id, self.table_a2.id)

    def test_waitlist_put_same_tenant_seated_table_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'waitlist',
            {'id': str(self.waitlist_a.id), 'seated_table': str(self.table_a2.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.waitlist_a.refresh_from_db()
        self.assertEqual(self.waitlist_a.seated_table_id, self.table_a2.id)

    def test_table_put_same_tenant_dining_area_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'tables',
            {'id': str(self.table_a.id), 'dining_area': str(self.dining_area_a2.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.dining_area_id, self.dining_area_a2.id)

    def test_reservation_put_own_restaurant_still_succeeds(self):
        # Guards the "reject on presence" mistake: the frontend re-sends the row's
        # own `restaurant` on PUT; an EQUAL value must be a no-op that still passes.
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id),
             'restaurant': str(self.restaurant_a.id), 'party_size': 5},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.party_size, 5)
        self.assertEqual(self.reservation_a.restaurant_id, self.restaurant_a.id)

    # --- 11. clearing SET_NULL FKs to None still works ----------------
    def test_reservation_put_clear_table_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertIsNone(self.reservation_a.table_id)

    def test_waitlist_put_clear_seated_table_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'waitlist',
            {'id': str(self.waitlist_a.id), 'seated_table': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.waitlist_a.refresh_from_db()
        self.assertIsNone(self.waitlist_a.seated_table_id)

    def test_table_put_clear_dining_area_succeeds(self):
        resp = self._request(
            self.owner_a, 'put', 'tables',
            {'id': str(self.table_a.id), 'dining_area': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.table_a.refresh_from_db()
        self.assertIsNone(self.table_a.dining_area_id)

    # --- 12. table restaurant pin: tripwire + DIRECT serializer test --
    def test_table_restaurant_is_not_editable(self):
        """Tripwire. Table.restaurant is the tenancy anchor. Secretary filters PUT
        data to EI keys, so `restaurant` being ABSENT from EDIT_INFORMATION['table']
        is what prevents a cross-tenant table move today. If you add it, the
        SerializerPutTable restaurant pin becomes load-bearing — prove it fires
        end-to-end before changing this test."""
        from dinify_backend.configss.edit_information import EDIT_INFORMATION
        self.assertNotIn('restaurant', {k['key'] for k in EDIT_INFORMATION['table']})

    def test_table_serializer_rejects_foreign_restaurant(self):
        # The ONLY test that reaches the SerializerPutTable restaurant pin — the
        # endpoint strips `restaurant` (see the tripwire). Defense-in-depth.
        # Use table_a2 (number=2, str_number='2'): restaurant B has no matching
        # (number, str_number), so the unique-together validator passes and it is
        # the restaurant pin — not an incidental unique clash — that rejects.
        from restaurants_app.serializers import SerializerPutTable
        serializer = SerializerPutTable(
            instance=self.table_a2,
            data={'restaurant': str(self.restaurant_b.id)},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('restaurant', serializer.errors)
        self.table_a2.refresh_from_db()
        self.assertEqual(self.table_a2.restaurant_id, self.restaurant_a.id)

    # --- 13. foreign vs unknown/malformed id: equivalent, no enumeration
    def test_foreign_and_unknown_table_both_rejected_no_enumeration(self):
        import uuid
        foreign = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': str(self.table_b.id)},
        )
        unknown = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': str(uuid.uuid4())},
        )
        self.assertEqual(foreign.status_code, 400, foreign.content)
        self.assertEqual(unknown.status_code, 400, unknown.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.table_id, self.table_a.id)
        # No tenant enumeration: the foreign rejection reveals neither B's id nor name.
        body = foreign.content.decode()
        self.assertNotIn(str(self.restaurant_b.id), body)
        self.assertNotIn('TFK Restaurant B', body)

    def test_malformed_table_uuid_is_4xx_not_500(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id), 'table': 'not-a-uuid'},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.table_id, self.table_a.id)

    # --- adversarial: both nested FKs foreign; POST restaurant gate ----
    def test_reservation_put_foreign_table_and_server_together_rejected(self):
        resp = self._request(
            self.owner_a, 'put', 'reservations',
            {'id': str(self.reservation_a.id),
             'table': str(self.table_b.id), 'server': str(self.server_b.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.reservation_a.refresh_from_db()
        self.assertEqual(self.reservation_a.table_id, self.table_a.id)
        self.assertEqual(self.reservation_a.server_id, self.server_a.id)

    def test_reservation_post_into_foreign_restaurant_denied_at_gate(self):
        # POST gates on the body restaurant, so owner_a can't create into B at all
        # (403 at the gate, before the serializer). Pins the existing behaviour.
        resp = self._request(
            self.owner_a, 'post', 'reservations',
            {'restaurant': str(self.restaurant_b.id), 'guest_name': 'GateX',
             'date_time': '2026-07-14T19:00:00Z'},
        )
        self.assertIn(resp.status_code, (401, 403), resp.content)
        self.assertFalse(Reservation.objects.filter(guest_name='GateX').exists())


class AuthenticatedManagementDeletedAccessTests(TestCase):
    """The authenticated management catch-all (RestaurantSetupEndpoint) is an
    IsAuthenticated + tenant-scoped surface that hides soft-deleted restaurants by
    default but STILL honours ?deleted=true within the caller's own tenancy.

    Relocated from tests_misc_public.py when the anonymous misc-public endpoint
    was retired: it exercises a SEPARATE live endpoint (the authenticated setup
    catch-all), so its coverage must survive that file's deletion.
    """

    SETUP_RESTAURANTS_URL = '/api/v1/restaurant-setup/restaurants/'

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Mgmt', last_name='Owner',
            email='mgmt_owner@example.com', phone_number='256700000904',
            username='256700000904', country='Uganda', password='password',
            roles=[],
        )
        # A live restaurant the owner manages ...
        self.live_restaurant = Restaurant.objects.create(
            name='Mgmt Live Restaurant', location='loc-live',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.live_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        # ... and a soft-deleted (still status='live') restaurant they also
        # manage. Module scope binds on restaurant STATUS, not the deleted flag,
        # so it stays within the owner's tenancy and is reachable via ?deleted=true.
        self.deleted_restaurant = Restaurant.objects.create(
            name='Mgmt Deleted Restaurant', location='loc-del',
            status=RestaurantStatus_Live, owner=self.owner, deleted=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.deleted_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

    def _auth(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def test_default_hides_deleted_for_authenticated_owner(self):
        response = self.client.get(self.SETUP_RESTAURANTS_URL, **self._auth())
        self.assertEqual(response.status_code, 200, response.content)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.live_restaurant.id), ids)
        self.assertNotIn(str(self.deleted_restaurant.id), ids)

    def test_deleted_true_still_reveals_deleted_for_authenticated_owner(self):
        # The intentionally-supported management opt-in is unchanged.
        response = self.client.get(
            self.SETUP_RESTAURANTS_URL, {'deleted': 'true'}, **self._auth(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.deleted_restaurant.id), ids)
