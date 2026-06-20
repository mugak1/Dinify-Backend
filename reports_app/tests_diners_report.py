"""
Tests for the rebuilt Diners reports (PR6): summary / listing.

These lock in the corrected NULL-customer semantics over the legacy bugs:
  * Dinify is anonymous-QR-first, so most sale orders are guests
    (``customer IS NULL``). Identified-diner metrics operate strictly on
    ``sale_orders(...).exclude(customer__isnull=True)`` — the NULL bucket is
    NEVER collapsed into one phantom diner and never counted as a repeat
    (the old inflation bug); guests surface as a separate ``guest_orders`` count,
  * spend is ``Sum('actual_cost')`` (net revenue, consistent with Sales) — NOT
    ``total_cost`` — and aggregates are over the date range, NOT lifetime,
  * ``most_active_diner`` is by sale COUNT (not spend) and is null with no diners,
  * the listing is ONE grouped+joined query (no UUID-as-User crash, no per-row
    N+1), identified diners only,
  * ``diners-trends`` is gone — removed from the endpoint dispatch.

``time_created`` is ``auto_now_add`` on Order, so it is set via ``.update()``
after create. UTC instants are used (EAT is UTC+3, so 09:00 UTC is the same
calendar day in EAT).
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from users_app.models import User
from restaurants_app.models import Restaurant, Table
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active,
    OrderStatus_Served, OrderStatus_Paid, OrderStatus_Pending,
    OrderStatus_Cancelled,
    DINIFY_ADMIN,
)
from reports_app.controllers.restaurant.diners import (
    generate_restaurant_diners_summary,
    generate_restaurant_diners_listing,
)
from reports_app.endpoints.restaurant_reports import RestaurantReportsEndpoint


def utc(year, month, day, hour=9, minute=0):
    """A timezone-aware UTC instant (Order.time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


class DinersReportBase(TestCase):
    """Shared fixtures + Order seeding (with an optional identified customer)."""

    def setUp(self):
        self.owner = self.make_diner('256700000600', 'Owner', 'One')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # A second tenant, to assert restaurant scoping.
        self.other_owner = self.make_diner('256700000610', 'Owner', 'Two')
        self.restaurant_b = Restaurant.objects.create(
            name='Other Restaurant', location='loc-b',
            status=RestaurantStatus_Active, owner=self.other_owner,
        )
        self.table_b = Table.objects.create(number=1, restaurant=self.restaurant_b)

        # Identified diners (named) + one deliberately unnamed (both names NULL).
        self.diner_a = self.make_diner('256700000601', 'Aaron', 'Active')
        self.diner_b = self.make_diner('256700000602', 'Bella', 'Big')
        self.diner_c = self.make_diner('256700000603', 'Cara', 'Casual')
        self.unnamed_diner = self.make_diner('256700000604', None, None)

        # A dinify admin — passes can_read_restaurant for the endpoint test.
        self.admin = self.make_diner('256700000699', 'Admin', 'User',
                                     roles=[DINIFY_ADMIN])

    def make_diner(self, phone, first_name='', last_name='', roles=None):
        return User.objects.create_user(
            first_name=first_name, last_name=last_name,
            email=f'{phone}@test.com', phone_number=phone,
            username=phone, country='Uganda', password='password',
            roles=roles or [],
        )

    def make_order(self, status=OrderStatus_Served, when=None, customer=None,
                   restaurant=None, table=None, total='1000.00',
                   discounted='800.00', savings='200.00', actual='750.00'):
        order = Order.objects.create(
            restaurant=restaurant or self.restaurant,
            table=table or self.table,
            order_status=status,
            customer=customer,
            total_cost=Decimal(total),
            discounted_cost=Decimal(discounted),
            savings=Decimal(savings),
            actual_cost=Decimal(actual),
        )
        if when is not None:
            Order.objects.filter(id=order.id).update(time_created=when)
        return order


class DinersSummaryTests(DinersReportBase):

    def test_null_customers_are_not_collapsed_into_a_diner(self):
        # N guest (null-customer) sale orders + M identified diners.
        for _ in range(3):
            self.make_order(when=utc(2024, 1, 10), customer=None)
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_a)
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10),
                        customer=self.diner_b)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # M identified diners, NOT M+1 — the NULL bucket is not a diner.
        self.assertEqual(data['identified_diners'], 2)
        # The 3-strong NULL bucket is NOT a repeat diner (kills the inflation bug).
        self.assertEqual(data['repeat_diners'], 0)
        # Guests surfaced honestly as their own count.
        self.assertEqual(data['guest_orders'], 3)

    def test_repeat_diners_counts_identified_only(self):
        # diner_a: 2 sales (a repeat). diner_b: 1 sale. 4 guest sales.
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_a)
        self.make_order(when=utc(2024, 1, 11), customer=self.diner_a)
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_b)
        for _ in range(4):
            self.make_order(when=utc(2024, 1, 10), customer=None)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-01-31',
        )['data']

        self.assertEqual(data['identified_diners'], 2)
        self.assertEqual(data['repeat_diners'], 1)   # diner_a only; guests excluded
        self.assertEqual(data['guest_orders'], 4)

    def test_most_active_diner_is_by_order_count_not_spend(self):
        # diner_a: 3 small sales. diner_b: 1 large sale (more spend, fewer orders).
        for _ in range(3):
            self.make_order(when=utc(2024, 1, 10), customer=self.diner_a, actual='100.00')
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_b, actual='5000.00')
        # Guests must never be the most active, however many.
        for _ in range(10):
            self.make_order(when=utc(2024, 1, 10), customer=None)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        most_active = data['most_active_diner']
        # diner_a wins by COUNT (3), despite diner_b's higher spend (5000).
        self.assertEqual(most_active['name'], 'Aaron Active')
        self.assertEqual(most_active['order_count'], 3)
        self.assertEqual(most_active['total_spend'], Decimal('300.00'))

    def test_most_active_diner_is_null_without_identified_diners(self):
        # Only guest orders — no identified diner exists.
        for _ in range(5):
            self.make_order(when=utc(2024, 1, 10), customer=None)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertIsNone(data['most_active_diner'])
        self.assertEqual(data['identified_diners'], 0)
        self.assertEqual(data['average_spend_per_identified_diner'], 0)
        self.assertEqual(data['guest_orders'], 5)

    def test_average_spend_uses_actual_cost_over_date_range_not_lifetime(self):
        # diner_a: 2 in-range sales (actual 750) + 1 OUT-of-range sale (actual 9999).
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_a,
                        actual='750.00', total='1000.00')
        self.make_order(when=utc(2024, 1, 11), customer=self.diner_a,
                        actual='750.00', total='1000.00')
        # Out of the queried range — must not leak into the in-range figures.
        self.make_order(when=utc(2024, 2, 20), customer=self.diner_a,
                        actual='9999.00', total='9999.00')

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-01-31',
        )['data']

        self.assertEqual(data['identified_diners'], 1)
        # avg per diner = Sum(actual over in-range identified) / diners = 1500 / 1.
        # NOT total_cost (would be 2000) and NOT inflated by the out-of-range 9999.
        self.assertEqual(data['average_spend_per_identified_diner'], Decimal('1500.00'))
        self.assertEqual(data['most_active_diner']['total_spend'], Decimal('1500.00'))
        self.assertEqual(data['most_active_diner']['order_count'], 2)

    def test_average_spend_per_diner_divides_by_diner_count(self):
        # diner_a: 3 sales x 100 = 300. diner_b: 1 sale x 700 = 700. total = 1000.
        for _ in range(3):
            self.make_order(when=utc(2024, 1, 10), customer=self.diner_a, actual='100.00')
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_b, actual='700.00')

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # 1000 total identified spend / 2 identified diners = 500.00 per diner.
        self.assertEqual(data['average_spend_per_identified_diner'], Decimal('500.00'))

    def test_only_sale_status_orders_count(self):
        # diner_a: 1 served sale + a cancelled + a pending (non-sales).
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10), customer=self.diner_a)
        self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 1, 10), customer=self.diner_a)
        self.make_order(status=OrderStatus_Pending, when=utc(2024, 1, 10), customer=self.diner_a)
        # Guests: 1 served sale + 1 cancelled (non-sale).
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10), customer=None)
        self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 1, 10), customer=None)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['identified_diners'], 1)
        self.assertEqual(data['repeat_diners'], 0)             # only 1 served sale
        self.assertEqual(data['guest_orders'], 1)              # only the served guest
        self.assertEqual(data['most_active_diner']['order_count'], 1)

    def test_empty_range_returns_zeroed_summary(self):
        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['identified_diners'], 0)
        self.assertEqual(data['repeat_diners'], 0)
        self.assertEqual(data['guest_orders'], 0)
        self.assertEqual(data['average_spend_per_identified_diner'], 0)
        self.assertIsNone(data['most_active_diner'])

    def test_summary_is_restaurant_scoped(self):
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_a)
        self.make_order(when=utc(2024, 1, 10), customer=self.diner_b,
                        restaurant=self.restaurant_b, table=self.table_b)

        data = generate_restaurant_diners_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['identified_diners'], 1)


class DinersListingTests(DinersReportBase):

    def test_listing_returns_identified_diners_only(self):
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_a)
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_b)
        # Guests must not appear in the listing.
        for _ in range(3):
            self.make_order(when=utc(2024, 2, 1), customer=None)

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(len(data), 2)   # only identified diners, no guest row
        self.assertEqual(
            {row['customer_id'] for row in data},
            {self.diner_a.id, self.diner_b.id},
        )

    def test_listing_does_not_raise_and_aggregates_correctly(self):
        # The legacy code did ``customer.first_name`` on a UUID -> crash. Prove
        # the rebuilt listing runs and joins the User fields through the ORM, and
        # that the money/aggregates are over the date range, not lifetime.
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_a, actual='100.00')
        self.make_order(when=utc(2024, 2, 2), customer=self.diner_a, actual='300.00')
        # Out-of-range sale for the same diner — excluded from the aggregates.
        self.make_order(when=utc(2024, 3, 15), customer=self.diner_a, actual='9999.00')

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-28',
        )['data']

        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row['customer_id'], self.diner_a.id)
        self.assertEqual(row['name'], 'Aaron Active')         # joined, not UUID.first_name
        self.assertEqual(row['phone_number'], self.diner_a.phone_number)
        self.assertEqual(row['no_orders'], 2)                 # in-range only
        self.assertEqual(row['total_spend'], Decimal('400.00'))   # actual_cost sum
        self.assertEqual(row['average_spend'], Decimal('200.00'))  # actual_cost avg
        self.assertEqual(row['last_order_date'], utc(2024, 2, 2))  # latest in range

    def test_listing_orders_by_order_count_desc(self):
        # diner_b: 1 order. diner_a: 3 orders. diner_a must come first.
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_b)
        for _ in range(3):
            self.make_order(when=utc(2024, 2, 1), customer=self.diner_a)

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual([row['no_orders'] for row in data], [3, 1])
        self.assertEqual(data[0]['customer_id'], self.diner_a.id)

    def test_listing_name_falls_back_to_phone_when_unnamed(self):
        # A diner with both names NULL falls back to the phone number, never 'None'.
        self.make_order(when=utc(2024, 2, 1), customer=self.unnamed_diner)

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(data[0]['name'], self.unnamed_diner.phone_number)
        self.assertNotIn('None', data[0]['name'])

    def test_listing_is_a_single_query(self):
        # Several identified diners, each with several orders. A per-customer N+1
        # would blow this up; the rebuilt listing is ONE grouped+joined query.
        for diner in (self.diner_a, self.diner_b, self.diner_c):
            for i in range(3):
                self.make_order(when=utc(2024, 2, 1, 8 + i), customer=diner)

        with self.assertNumQueries(1):
            result = generate_restaurant_diners_listing(
                restaurant_id=self.restaurant.id,
                date_from='2024-02-01', date_to='2024-02-01',
            )
            data = result['data']
            self.assertEqual(len(data), 3)

    def test_listing_excludes_guests_even_when_only_guests_exist(self):
        for _ in range(4):
            self.make_order(when=utc(2024, 2, 1), customer=None)

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(data, [])   # guests are never listed

    def test_listing_is_restaurant_scoped(self):
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_a)
        self.make_order(when=utc(2024, 2, 1), customer=self.diner_b,
                        restaurant=self.restaurant_b, table=self.table_b)

        data = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['customer_id'], self.diner_a.id)

    def test_listing_31_day_cap(self):
        result = generate_restaurant_diners_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-03-01',
        )
        self.assertEqual(result['status'], 400)


class DinersTrendsRemovedTests(DinersReportBase):

    def test_diners_trends_controller_is_removed(self):
        import reports_app.controllers.restaurant.diners as diners_module
        self.assertFalse(hasattr(diners_module, 'generate_restaurant_diners_trends'))

    def test_diners_trends_endpoint_is_no_longer_served(self):
        # Authorised (admin) request for the retired report -> 'Invalid report
        # name', proving the dispatch branch was removed (not a 404 auth reject).
        factory = APIRequestFactory()
        request = factory.get(
            '/api/v1/reports/restaurant/diners-trends/',
            {'restaurant': str(self.restaurant.id),
             'from': '2024-01-01', 'to': '2024-01-31',
             'category': 'daily', 'result': 'table'},
        )
        force_authenticate(request, user=self.admin)
        response = RestaurantReportsEndpoint.as_view()(request, report_name='diners-trends')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['message'], 'Invalid report name')
