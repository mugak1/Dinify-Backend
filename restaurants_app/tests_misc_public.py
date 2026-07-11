"""
Tests for the anonymous (AllowAny) misc-public listing endpoint
(GET /api/v1/restaurant-setup/misc-public/<config_detail>/).

These lock in the BUG-P1-4/5 fix:
  * the restaurants listing must not leak the owner's personal PII, and
  * the tables listing must be restaurant-scoped (never an unscoped
    cross-tenant dump) and expose only diner-safe fields.
"""
import json

from django.test import TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Active
from restaurants_app.models import Restaurant, Table, DiningArea
from users_app.models import User


RESTAURANTS_URL = '/api/v1/restaurant-setup/misc-public/restaurants/'
TABLES_URL = '/api/v1/restaurant-setup/misc-public/tables/'

# Distinctive owner PII we assert never appears in the public payload.
OWNER_FIRST_NAME = 'Ownerfirst'
OWNER_LAST_NAME = 'Ownerlast'
OWNER_EMAIL = 'owner_pii_secret@example.com'
OWNER_PHONE = '256700000901'

# Internal-only Table columns that must never reach an anonymous caller.
INTERNAL_TABLE_FIELDS = (
    'restaurant', 'prepayment_required', 'room_name', 'smoking_zone',
    'outdoor_seating', 'enabled', 'is_active', 'shape', 'tags',
    'has_qr', 'qr_mode', 'qr_regenerated_at',
    'floor_x', 'floor_y', 'floor_width', 'floor_height',
    'time_created', 'time_last_updated', 'time_deleted', 'created_by',
    'deleted', 'deletion_reason', 'deleted_by', 'archived', 'vacuumed',
    'eod_last_date', 'eod_record_date',
)

# The exact diner-safe field set the tables listing is expected to expose.
PUBLIC_TABLE_FIELDS = {
    'id', 'number', 'str_number', 'display_name',
    'min_capacity', 'max_capacity', 'status', 'reserved', 'dining_area',
}


class MiscPublicRestaurantsTests(TestCase):
    """The restaurants listing exposes the restaurant, never owner PII."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name=OWNER_FIRST_NAME, last_name=OWNER_LAST_NAME,
            email=OWNER_EMAIL, phone_number=OWNER_PHONE, username=OWNER_PHONE,
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='PII Test Restaurant', location='loc-pii',
            status=RestaurantStatus_Active, owner=self.owner,
        )

    def test_listing_is_anonymous_and_returns_the_restaurant(self):
        # No Authorization header: the endpoint is AllowAny.
        response = self.client.get(RESTAURANTS_URL)
        self.assertEqual(response.status_code, 200)
        records = response.json()['data']['records']
        ids = [record['id'] for record in records]
        self.assertIn(str(self.restaurant.id), ids)

    def test_owner_pii_is_absent_from_payload(self):
        response = self.client.get(RESTAURANTS_URL)
        self.assertEqual(response.status_code, 200)
        records = response.json()['data']['records']
        self.assertTrue(records)
        # The owner block is gone entirely from every record ...
        for record in records:
            self.assertNotIn('owner', record)
        # ... and none of the owner's personal PII appears anywhere in the
        # serialized payload (guards against a nested re-introduction).
        raw = json.dumps(response.json())
        for leaked in (OWNER_FIRST_NAME, OWNER_LAST_NAME, OWNER_EMAIL, OWNER_PHONE):
            self.assertNotIn(leaked, raw)

    def test_unknown_query_param_is_ignored_not_500(self):
        # A stray/unknown query param must be silently skipped by
        # define_filter_params, never crash the listing with a 500.
        response = self.client.get(RESTAURANTS_URL, {'foo': 'barbar'})
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.restaurant.id), ids)

    def test_known_name_param_filters(self):
        # A known param (name -> name__icontains) still maps and filters.
        other = Restaurant.objects.create(
            name='Zeta Public Grill', location='loc-z',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        response = self.client.get(RESTAURANTS_URL, {'name': 'Zeta'})
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(other.id), ids)
        # 'PII Test Restaurant' does not match name__icontains='Zeta'.
        self.assertNotIn(str(self.restaurant.id), ids)


class MiscPublicTablesTests(TestCase):
    """The tables listing is restaurant-scoped and diner-safe."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Tbl', last_name='Owner',
            email='tbl_owner@example.com', phone_number='256700000902',
            username='256700000902', country='Uganda', password='password',
            roles=[],
        )
        self.rest_a = Restaurant.objects.create(
            name='Tables Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.rest_b = Restaurant.objects.create(
            name='Tables Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        area_a = DiningArea.objects.create(name='Patio', restaurant=self.rest_a)
        # Restaurant A: tables 1 and 2 (table 1 carries a dining area).
        Table.objects.create(
            number=1, str_number='1', restaurant=self.rest_a, dining_area=area_a,
        )
        Table.objects.create(number=2, str_number='2', restaurant=self.rest_a)
        # Restaurant B: a distinctive table that must never appear in A's listing.
        Table.objects.create(number=99, str_number='99', restaurant=self.rest_b)

    def test_missing_restaurant_param_returns_400_and_no_dump(self):
        response = self.client.get(TABLES_URL)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['status'], 400)
        self.assertEqual(body['message'], 'restaurant is required')

    def test_scoped_to_requested_restaurant_only(self):
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id)}
        )
        self.assertEqual(response.status_code, 200)
        records = response.json()['data']['records']
        numbers = sorted(record['number'] for record in records)
        # Only restaurant A's tables (1, 2); restaurant B's #99 must be absent.
        self.assertEqual(numbers, [1, 2])

    def test_payload_exposes_only_diner_safe_fields(self):
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id)}
        )
        self.assertEqual(response.status_code, 200)
        records = response.json()['data']['records']
        self.assertTrue(records)
        for record in records:
            keys = set(record.keys())
            # No internal-only column leaks ...
            leaked = keys & set(INTERNAL_TABLE_FIELDS)
            self.assertEqual(leaked, set(), f'internal fields leaked: {leaked}')
            # ... and exactly the diner-safe field set is present.
            self.assertEqual(keys, PUBLIC_TABLE_FIELDS)

    def test_dining_area_is_expanded_when_present(self):
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id)}
        )
        records = response.json()['data']['records']
        by_number = {record['number']: record for record in records}
        # Table 1 has a dining area -> nested summary dict; table 2 has none.
        self.assertIsNotNone(by_number[1]['dining_area'])
        self.assertEqual(by_number[1]['dining_area']['name'], 'Patio')
        self.assertIsNone(by_number[2]['dining_area'])
