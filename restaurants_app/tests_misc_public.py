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

    def test_soft_deleted_restaurant_hidden_but_deleted_override_returns_it(self):
        # (P3-03) A soft-deleted restaurant that is still status='active' must
        # NOT leak into the public directory; the ?deleted=true override still
        # surfaces it (mirrors the authenticated catch-all's default).
        deleted_rest = Restaurant.objects.create(
            name='Soft Deleted Active', location='loc-del',
            status=RestaurantStatus_Active, owner=self.owner, deleted=True,
        )
        # Default listing: the soft-deleted restaurant is hidden, the live one
        # is still present (positive control).
        response = self.client.get(RESTAURANTS_URL)
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertNotIn(str(deleted_rest.id), ids)
        self.assertIn(str(self.restaurant.id), ids)
        # ?deleted=true override: the soft-deleted restaurant IS returned.
        response = self.client.get(RESTAURANTS_URL, {'deleted': 'true'})
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(deleted_rest.id), ids)


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

    def test_soft_deleted_table_hidden_but_deleted_override_returns_it(self):
        # (P3-03) A soft-deleted table must not leak into the public tables
        # listing; the ?deleted=true override still surfaces it.
        Table.objects.create(
            number=7, str_number='7', restaurant=self.rest_a, deleted=True,
        )
        # Default: the soft-deleted table (7) is excluded; the live tables
        # (1, 2) are still present (positive control).
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id)},
        )
        self.assertEqual(response.status_code, 200)
        numbers = sorted(r['number'] for r in response.json()['data']['records'])
        self.assertEqual(numbers, [1, 2])
        # ?deleted=true override: the soft-deleted table (7) is returned too.
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id), 'deleted': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        numbers = sorted(r['number'] for r in response.json()['data']['records'])
        self.assertIn(7, numbers)
        self.assertIn(1, numbers)


class MiscPublicUnknownConfigTests(TestCase):
    """(BUG-P2-3g) An unrecognised config_detail returns a clean 404, never a
    500. Covers the removed 'details' value — whose handler (self.get_detail)
    never existed, so it raised AttributeError -> 500 — and any other unknown
    value, which used to fall through into Secretary with a None serializer. The
    two real listings still resolve."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Cfg', last_name='Owner',
            email='cfg_owner@example.com', phone_number='256700000903',
            username='256700000903', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Config Guard Restaurant', location='loc-cfg',
            status=RestaurantStatus_Active, owner=self.owner,
        )

    def test_details_config_detail_returns_404_not_500(self):
        # 'details' used to call an undefined self.get_detail -> AttributeError
        # -> 500. It has no frontend caller and is now a plain 404.
        response = self.client.get(
            '/api/v1/restaurant-setup/misc-public/details/'
        )
        self.assertEqual(response.status_code, 404)

    def test_unrelated_unknown_config_detail_returns_404(self):
        # Any other unknown value hits the same whitelist fallback (used to fall
        # through into Secretary with a None serializer).
        response = self.client.get(
            '/api/v1/restaurant-setup/misc-public/frobnicate/'
        )
        self.assertEqual(response.status_code, 404)

    def test_known_restaurants_listing_still_resolves(self):
        # The whitelist guard must not break the two real listings.
        response = self.client.get(RESTAURANTS_URL)
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.restaurant.id), ids)
