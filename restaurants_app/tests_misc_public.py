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

from dinify_backend.configs import ROLES
from dinify_backend.configss.string_definitions import RestaurantStatus_Active
from restaurants_app.models import Restaurant, RestaurantEmployee, Table
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

    def test_soft_deleted_restaurant_hidden_and_deleted_param_cannot_reveal_it(self):
        # (PR5) A soft-deleted restaurant that is still status='active' must NEVER
        # leak into the anonymous public directory. Unlike the authenticated
        # catch-all, there is NO ?deleted opt-in here: any caller-supplied
        # `deleted` value is ignored, so it can never surface the deleted row.
        deleted_rest = Restaurant.objects.create(
            name='Soft Deleted Active', location='loc-del',
            status=RestaurantStatus_Active, owner=self.owner, deleted=True,
        )

        def visible_ids(params=None):
            response = self.client.get(RESTAURANTS_URL, params or {})
            self.assertEqual(response.status_code, 200)
            return [record['id'] for record in response.json()['data']['records']]

        # Default listing: the soft-deleted restaurant is hidden, the live one is
        # still present (positive control).
        ids = visible_ids()
        self.assertNotIn(str(deleted_rest.id), ids)
        self.assertIn(str(self.restaurant.id), ids)

        # No `deleted` value (true / false / garbage) may reveal it — the fix is
        # value-independent (presence no longer bypasses the guard), and the live
        # restaurant stays visible throughout.
        for value in ('true', 'True', 'false', 'zzz', '1'):
            ids = visible_ids({'deleted': value})
            self.assertNotIn(
                str(deleted_rest.id), ids,
                msg=f'?deleted={value} leaked the soft-deleted restaurant',
            )
            self.assertIn(str(self.restaurant.id), ids)

    def test_unknown_param_with_deleted_present_keeps_it_hidden(self):
        # (req 7) An unknown query param must not weaken the soft-delete filter.
        deleted_rest = Restaurant.objects.create(
            name='Soft Deleted Unknown Param', location='loc-del2',
            status=RestaurantStatus_Active, owner=self.owner, deleted=True,
        )
        response = self.client.get(RESTAURANTS_URL, {'foo': 'barbar'})
        self.assertEqual(response.status_code, 200)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertNotIn(str(deleted_rest.id), ids)
        self.assertIn(str(self.restaurant.id), ids)


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

    def test_deleted_table_not_reachable_even_with_deleted_param(self):
        # (req 4/5) The public tables listing is retired, so there is no anonymous
        # route to ANY table data — deleted or live — and a `?deleted=true` param
        # cannot re-open one. A soft-deleted table stays completely unreachable.
        deleted_table = Table.objects.create(
            number=7, str_number='7', restaurant=self.rest_a, deleted=True,
        )
        for params in ({'restaurant': str(self.rest_a.id)},
                       {'restaurant': str(self.rest_a.id), 'deleted': 'true'},
                       {'deleted': 'true'}):
            response = self.client.get(TABLES_URL, params)
            self.assertEqual(response.status_code, 404, msg=f'params={params}')
            self.assertNotIn(str(deleted_table.id), json.dumps(response.json()))


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


class AuthenticatedManagementDeletedAccessUnchangedTests(TestCase):
    """(req 6) The PR5 fix is scoped to the ANONYMOUS misc-public endpoint. The
    authenticated management catch-all (RestaurantSetupEndpoint) is a separate,
    IsAuthenticated + tenant-scoped surface that intentionally STILL honours
    ?deleted=true within the caller's own tenancy — that behaviour is preserved.
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
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.live_restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        # ... and a soft-deleted (still status='active') restaurant they also
        # manage. Module scope binds on restaurant STATUS, not the deleted flag,
        # so it stays within the owner's tenancy and is reachable via ?deleted=true.
        self.deleted_restaurant = Restaurant.objects.create(
            name='Mgmt Deleted Restaurant', location='loc-del',
            status=RestaurantStatus_Active, owner=self.owner, deleted=True,
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
        # The intentionally-supported management opt-in is unchanged by PR5.
        response = self.client.get(
            self.SETUP_RESTAURANTS_URL, {'deleted': 'true'}, **self._auth(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        ids = [record['id'] for record in response.json()['data']['records']]
        self.assertIn(str(self.deleted_restaurant.id), ids)
