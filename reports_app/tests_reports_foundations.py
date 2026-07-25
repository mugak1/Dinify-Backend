"""
Tests for the PR3 reporting foundations:

* ``reports_app.controllers.common.sale_filters`` — the canonical
  "what is a sale / what is revenue" definitions, and
* ``reports_app.controllers.common.bucketing`` — single-query, EAT-aligned
  period bucketing that replaces the legacy trends N+1.

The seeded orders use deliberately all-distinct money values
(total=1000, discounted=800, savings=200, actual=750) so the revenue/discount
assertions can prove revenue is ``actual_cost`` and discount is ``savings`` —
not ``total_cost`` (gross) and not ``discounted_cost`` (post-discount total).

Timestamps are written via ``Order.objects.filter(...).update(time_created=...)``
because ``time_created`` is ``auto_now_add`` and cannot be set on create. UTC
instants are used throughout; EAT is UTC+3, so 23:30 UTC is 02:30 EAT the next
day — the basis for the timezone-alignment assertions.
"""
from datetime import datetime, date, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import Restaurant, Table
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    OrderStatus_Initiated, OrderStatus_Pending, OrderStatus_Preparing,
    OrderStatus_Served, OrderStatus_Paid, OrderStatus_Refunded,
    OrderStatus_Cancelled,
)
from reports_app.controllers.common.sale_filters import (
    SALE_STATUSES, revenue_sum, discount_sum, sale_orders,
)
from reports_app.controllers.common.bucketing import bucket_sales, LOCAL_TZ


def make_user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


def utc(year, month, day, hour=12, minute=0):
    """A timezone-aware UTC instant (Order.time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


class ReportsFoundationsBase(TestCase):
    """Shared two-tenant fixtures + an Order-seeding helper."""

    def setUp(self):
        self.owner = make_user('256700000300')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # A second tenant, for restaurant-scoping assertions.
        self.other_owner = make_user('256700000301')
        self.restaurant_b = Restaurant.objects.create(
            name='Other Restaurant', location='loc-b',
            status=RestaurantStatus_Live, owner=self.other_owner,
        )
        self.table_b = Table.objects.create(
            number=1, restaurant=self.restaurant_b,
        )

    def make_order(self, status=OrderStatus_Paid, when=None, restaurant=None,
                   table=None, total='1000.00', discounted='800.00',
                   savings='200.00', actual='750.00'):
        order = Order.objects.create(
            restaurant=restaurant or self.restaurant,
            table=table or self.table,
            order_status=status,
            total_cost=Decimal(total),
            discounted_cost=Decimal(discounted),
            savings=Decimal(savings),
            actual_cost=Decimal(actual),
        )
        if when is not None:
            # time_created is auto_now_add; .update() bypasses save() to set it.
            Order.objects.filter(id=order.id).update(time_created=when)
        return order


class SaleFiltersTests(ReportsFoundationsBase):

    def test_sale_statuses_constant(self):
        self.assertEqual(SALE_STATUSES, [OrderStatus_Served, OrderStatus_Paid])
        for excluded in (OrderStatus_Initiated, OrderStatus_Pending,
                         OrderStatus_Preparing, OrderStatus_Cancelled,
                         OrderStatus_Refunded):
            self.assertNotIn(excluded, SALE_STATUSES)

    def test_sale_orders_includes_served_paid_excludes_others(self):
        day = date(2024, 1, 10)
        for status in (OrderStatus_Initiated, OrderStatus_Pending,
                       OrderStatus_Preparing, OrderStatus_Served,
                       OrderStatus_Paid, OrderStatus_Refunded,
                       OrderStatus_Cancelled):
            self.make_order(status=status, when=utc(2024, 1, 10, 9, 0))

        qs = sale_orders(self.restaurant.id, day, day)

        self.assertEqual(qs.count(), 2)
        self.assertEqual(
            set(qs.values_list('order_status', flat=True)),
            {OrderStatus_Served, OrderStatus_Paid},
        )

    def test_sale_orders_restaurant_scoped(self):
        day = date(2024, 1, 10)
        mine = self.make_order(when=utc(2024, 1, 10, 9, 0))
        theirs = self.make_order(
            when=utc(2024, 1, 10, 9, 0),
            restaurant=self.restaurant_b, table=self.table_b,
        )

        qs = sale_orders(self.restaurant.id, day, day)

        ids = set(qs.values_list('id', flat=True))
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids)
        self.assertEqual(qs.count(), 1)

    def test_sale_orders_local_day_alignment(self):
        # 2024-01-15 23:30 UTC == 2024-01-16 02:30 EAT -> belongs to the 16th.
        self.make_order(when=utc(2024, 1, 15, 23, 30))

        self.assertEqual(
            sale_orders(
                self.restaurant.id, date(2024, 1, 16), date(2024, 1, 16),
            ).count(),
            1,
        )
        self.assertEqual(
            sale_orders(
                self.restaurant.id, date(2024, 1, 15), date(2024, 1, 15),
            ).count(),
            0,
        )

    def test_revenue_sums_actual_cost_not_total_or_discounted(self):
        for _ in range(2):
            self.make_order(when=utc(2024, 1, 10, 9, 0))
        qs = sale_orders(self.restaurant.id, date(2024, 1, 10), date(2024, 1, 10))

        revenue = qs.aggregate(v=revenue_sum())['v']

        self.assertEqual(revenue, Decimal('1500.00'))      # 2 x actual 750
        self.assertNotEqual(revenue, Decimal('2000.00'))   # not total 1000
        self.assertNotEqual(revenue, Decimal('1600.00'))   # not discounted 800

    def test_discount_sums_savings_not_total_or_discounted(self):
        for _ in range(2):
            self.make_order(when=utc(2024, 1, 10, 9, 0))
        qs = sale_orders(self.restaurant.id, date(2024, 1, 10), date(2024, 1, 10))

        discount = qs.aggregate(v=discount_sum())['v']

        self.assertEqual(discount, Decimal('400.00'))      # 2 x savings 200
        self.assertNotEqual(discount, Decimal('2000.00'))  # not total 1000
        self.assertNotEqual(discount, Decimal('1600.00'))  # not discounted 800


class BucketingTests(ReportsFoundationsBase):

    def _period_date(self, row):
        """The EAT calendar date of a bucket's period boundary."""
        return row['period'].astimezone(LOCAL_TZ).date()

    def test_bucket_by_day_groups_per_day(self):
        for _ in range(2):
            self.make_order(when=utc(2024, 3, 1, 9, 0))
        self.make_order(when=utc(2024, 3, 2, 9, 0))
        for _ in range(3):
            self.make_order(when=utc(2024, 3, 3, 9, 0))

        qs = sale_orders(self.restaurant.id, date(2024, 3, 1), date(2024, 3, 3))
        rows = bucket_sales(qs, 'day')

        self.assertEqual(len(rows), 3)
        self.assertEqual([self._period_date(r) for r in rows],
                         [date(2024, 3, 1), date(2024, 3, 2), date(2024, 3, 3)])
        self.assertEqual([r['count'] for r in rows], [2, 1, 3])
        self.assertEqual([r['revenue'] for r in rows],
                         [Decimal('1500.00'), Decimal('750.00'), Decimal('2250.00')])
        self.assertEqual([r['discount'] for r in rows],
                         [Decimal('400.00'), Decimal('200.00'), Decimal('600.00')])

    def test_bucket_is_a_single_query(self):
        for _ in range(3):
            self.make_order(when=utc(2024, 3, 1, 9, 0))
        self.make_order(when=utc(2024, 3, 2, 9, 0))

        # qs is lazy; building it runs no query. Only bucket_sales should hit
        # the DB, and exactly once — proving the per-period loop is gone.
        qs = sale_orders(self.restaurant.id, date(2024, 3, 1), date(2024, 3, 2))
        with self.assertNumQueries(1):
            rows = bucket_sales(qs, 'day')

        self.assertEqual(len(rows), 2)

    def test_bucket_by_week(self):
        # 2024-03-04 is a Monday; 03-06 is the same ISO week; 03-11 the next.
        self.make_order(when=utc(2024, 3, 4, 9, 0))
        self.make_order(when=utc(2024, 3, 6, 9, 0))
        self.make_order(when=utc(2024, 3, 11, 9, 0))

        qs = sale_orders(self.restaurant.id, date(2024, 3, 1), date(2024, 3, 31))
        rows = bucket_sales(qs, 'week')

        self.assertEqual([self._period_date(r) for r in rows],
                         [date(2024, 3, 4), date(2024, 3, 11)])
        self.assertEqual([r['count'] for r in rows], [2, 1])

    def test_bucket_by_month_quarter_year(self):
        self.make_order(when=utc(2024, 1, 15, 9, 0))
        for _ in range(2):
            self.make_order(when=utc(2024, 2, 15, 9, 0))
        self.make_order(when=utc(2025, 1, 15, 9, 0))

        qs = sale_orders(self.restaurant.id, date(2024, 1, 1), date(2025, 12, 31))

        months = bucket_sales(qs, 'month')
        self.assertEqual([self._period_date(r) for r in months],
                         [date(2024, 1, 1), date(2024, 2, 1), date(2025, 1, 1)])
        self.assertEqual([r['count'] for r in months], [1, 2, 1])

        # Jan + Feb 2024 collapse into 2024-Q1.
        quarters = bucket_sales(qs, 'quarter')
        self.assertEqual([self._period_date(r) for r in quarters],
                         [date(2024, 1, 1), date(2025, 1, 1)])
        self.assertEqual([r['count'] for r in quarters], [3, 1])

        years = bucket_sales(qs, 'year')
        self.assertEqual([self._period_date(r) for r in years],
                         [date(2024, 1, 1), date(2025, 1, 1)])
        self.assertEqual([r['count'] for r in years], [3, 1])

    def test_bucket_tz_alignment_late_utc_lands_next_day_eat(self):
        # Order A: 2024-01-15 23:30 UTC == 2024-01-16 02:30 EAT.
        self.make_order(when=utc(2024, 1, 15, 23, 30))
        # Order B: unambiguously the 16th in EAT (12:00 EAT == 09:00 UTC).
        self.make_order(when=utc(2024, 1, 16, 9, 0))

        # Raw status-filtered qs (no date filter) isolates bucketing's tz logic.
        qs = Order.objects.filter(
            restaurant=self.restaurant, order_status__in=SALE_STATUSES,
        )
        rows = bucket_sales(qs, 'day')

        self.assertEqual(len(rows), 1)            # both land in ONE EAT day
        self.assertEqual(rows[0]['count'], 2)
        self.assertEqual(self._period_date(rows[0]), date(2024, 1, 16))
        # Not the UTC calendar day of order A.
        self.assertNotEqual(self._period_date(rows[0]), date(2024, 1, 15))

    def test_bucket_invalid_period_raises_valueerror(self):
        qs = Order.objects.filter(restaurant=self.restaurant)
        with self.assertRaises(ValueError):
            bucket_sales(qs, 'fortnight')
