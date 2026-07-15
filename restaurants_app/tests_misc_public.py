"""
Tests for the anonymous (AllowAny) misc-public listing endpoint
(GET /api/v1/restaurant-setup/misc-public/<config_detail>/).

These lock in:
  * the restaurants listing must not leak the owner's personal PII
    (BUG-P1-4/5), and
  * the tables listing is RETIRED (PR2 tenant isolation) — it now 404s and no
    longer dispenses the table UUIDs that double as the order-journey
    table-scan token, closing the anonymous enumeration chain at its root.
"""
import json

from django.test import TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Active
from restaurants_app.models import Restaurant, Table
from users_app.models import User


RESTAURANTS_URL = '/api/v1/restaurant-setup/misc-public/restaurants/'
TABLES_URL = '/api/v1/restaurant-setup/misc-public/tables/'

# Distinctive owner PII we assert never appears in the public payload.
OWNER_FIRST_NAME = 'Ownerfirst'
OWNER_LAST_NAME = 'Ownerlast'
OWNER_EMAIL = 'owner_pii_secret@example.com'
OWNER_PHONE = '256700000901'


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


class MiscPublicTablesRetiredTests(TestCase):
    """The anonymous public tables listing is RETIRED (PR2 tenant isolation).

    It used to hand out table UUIDs in bulk, and a table UUID is itself the
    order-journey table-scan token — so the listing let an anonymous caller
    enumerate a restaurant's tables and chain table-scan -> active order UUID
    -> order-details / review submission without ever holding a QR. The route
    now 404s and no table data is reachable through it. The authenticated
    management table listing (RestaurantSetupEndpoint) is a separate endpoint
    and is unaffected.
    """

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
        # A real table in each restaurant; none of its identifiers may be
        # reachable through the retired public listing.
        self.table_a = Table.objects.create(
            number=1, str_number='1', restaurant=self.rest_a,
        )
        self.table_b = Table.objects.create(
            number=99, str_number='99', restaurant=self.rest_b,
        )

    def test_scoped_tables_listing_returns_404_and_no_table_uuid(self):
        # Even with a valid restaurant scope the listing is gone (404), and the
        # body carries no table id/UUID.
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_a.id)},
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(self.table_a.id), json.dumps(response.json()))

    def test_unscoped_tables_listing_returns_404(self):
        # Without ?restaurant= it is also 404 — the retirement takes precedence
        # over the old 'restaurant is required' 400.
        response = self.client.get(TABLES_URL)
        self.assertEqual(response.status_code, 404)

    def test_public_directory_cannot_be_chained_to_table_uuids(self):
        # (#11) Restaurant B's UUID is publicly listable via the restaurants
        # directory, but there is NO public route to turn that restaurant UUID
        # into B's table UUIDs — so B's tables (and thus B's active orders)
        # cannot be discovered from public directory data alone.
        directory = self.client.get(RESTAURANTS_URL)
        self.assertEqual(directory.status_code, 200)
        ids = [r['id'] for r in directory.json()['data']['records']]
        self.assertIn(str(self.rest_b.id), ids)          # restaurant UUID is public
        self.assertNotIn(str(self.table_b.id), json.dumps(directory.json()))
        # The only route that mapped restaurant -> table UUIDs is retired (404).
        tables = self.client.get(
            TABLES_URL, {'restaurant': str(self.rest_b.id)},
        )
        self.assertEqual(tables.status_code, 404)


class MiscPublicUnknownConfigTests(TestCase):
    """(BUG-P2-3g) An unrecognised config_detail returns a clean 404, never a
    500. Covers the removed 'details' value — whose handler (self.get_detail)
    never existed, so it raised AttributeError -> 500 — the retired 'tables'
    value (PR2 tenant isolation), and any other unknown value (which used to
    fall through into Secretary with a None serializer). The one remaining real
    listing — restaurants — still resolves."""

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
        # The whitelist guard must not break the one remaining real listing.
        response = self.client.get(RESTAURANTS_URL)
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.restaurant.id), ids)
