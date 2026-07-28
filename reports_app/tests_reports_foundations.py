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
from reports_app.controllers.common.bucketing import (
    bucket_sales, period_boundaries, BOUNDARY_PERIODS, PERIOD_TRUNC, LOCAL_TZ,
)


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


class PeriodBoundariesTests(TestCase):
    """The zero-fill axis (BUCKETS-ZEROFILL-00).

    ``bucket_sales`` returns only the periods that HAVE orders; this is the
    complete set of periods that SHOULD appear, so a caller can join the two and
    emit the empty ones as zeros. Pure calendar arithmetic — no fixtures, no DB.

    Calendar facts these rely on: 2024-03-04, 2024-03-11 and 2024-02-26 are
    Mondays; 2024-03-01 is a Friday and 2024-03-03 a Sunday, both in the week of
    2024-02-26; 2024 is a leap year.
    """

    def dates(self, date_from, date_to, period):
        """Boundaries as EAT calendar dates, which is what the labels key on."""
        return [b.astimezone(LOCAL_TZ).date()
                for b in period_boundaries(date_from, date_to, period)]

    def test_daily_axis_is_every_day_inclusive_of_both_ends(self):
        self.assertEqual(
            self.dates(date(2024, 3, 1), date(2024, 3, 4), 'day'),
            [date(2024, 3, 1), date(2024, 3, 2),
             date(2024, 3, 3), date(2024, 3, 4)],
        )

    def test_a_single_day_window_is_one_bucket(self):
        self.assertEqual(self.dates(date(2024, 3, 1), date(2024, 3, 1), 'day'),
                         [date(2024, 3, 1)])

    def test_weekly_axis_opens_on_the_monday_of_the_window_s_first_week(self):
        # THE case this whole helper turns on. 2024-03-01 is a Friday, so the
        # window's first days belong to the week of Monday 2024-02-26 — a key that
        # falls BEFORE date_from. Clip it and those days have nowhere to land, and
        # a frontend enumerating from that Monday finds no row for its first key.
        self.assertEqual(
            self.dates(date(2024, 3, 1), date(2024, 3, 14), 'week'),
            [date(2024, 2, 26), date(2024, 3, 4), date(2024, 3, 11)],
        )

    def test_weekly_axis_from_a_monday_opens_on_that_monday(self):
        self.assertEqual(
            self.dates(date(2024, 3, 4), date(2024, 3, 12), 'week'),
            [date(2024, 3, 4), date(2024, 3, 11)],
        )

    def test_weekly_axis_from_a_sunday_opens_on_the_preceding_monday(self):
        # A Sunday is the LAST day of its ISO week, so this is the widest the
        # partial edge ever gets: six days before date_from.
        self.assertEqual(
            self.dates(date(2024, 3, 3), date(2024, 3, 10), 'week'),
            [date(2024, 2, 26), date(2024, 3, 4)],
        )

    def test_monthly_axis_covers_partial_months_at_both_ends(self):
        self.assertEqual(
            self.dates(date(2024, 1, 15), date(2024, 4, 3), 'month'),
            [date(2024, 1, 1), date(2024, 2, 1),
             date(2024, 3, 1), date(2024, 4, 1)],
        )

    def test_monthly_axis_crosses_a_year_boundary(self):
        self.assertEqual(
            self.dates(date(2024, 11, 15), date(2025, 2, 3), 'month'),
            [date(2024, 11, 1), date(2024, 12, 1),
             date(2025, 1, 1), date(2025, 2, 1)],
        )

    def test_quarterly_axis_snaps_to_quarter_starts(self):
        self.assertEqual(
            self.dates(date(2024, 2, 15), date(2024, 12, 31), 'quarter'),
            [date(2024, 1, 1), date(2024, 4, 1),
             date(2024, 7, 1), date(2024, 10, 1)],
        )

    def test_annual_axis_snaps_to_january(self):
        self.assertEqual(
            self.dates(date(2024, 6, 15), date(2026, 2, 1), 'year'),
            [date(2024, 1, 1), date(2025, 1, 1), date(2026, 1, 1)],
        )

    def test_hourly_axis_covers_the_whole_local_day(self):
        # The window is inclusive of date_to's LAST instant, not its midnight, so
        # a one-day hourly window is 24 buckets rather than 1.
        boundaries = period_boundaries(date(2024, 3, 1), date(2024, 3, 1), 'hour')
        self.assertEqual(len(boundaries), 24)
        self.assertEqual([b.astimezone(LOCAL_TZ).hour for b in boundaries],
                         list(range(24)))

    def test_boundaries_are_aware_and_on_the_eat_offset(self):
        for boundary in period_boundaries(date(2024, 3, 1), date(2024, 3, 3), 'day'):
            self.assertIsNotNone(boundary.tzinfo)
            self.assertEqual(boundary.utcoffset().total_seconds(), 3 * 3600)
            # Midnight LOCAL, which is 21:00 UTC the previous day.
            self.assertEqual(boundary.astimezone(LOCAL_TZ).hour, 0)

    def test_boundaries_equal_the_keys_the_grouped_query_emits(self):
        # The join is by EQUALITY on aware datetimes, so an axis that merely
        # LOOKED right but carried a different instant would silently fill over
        # every real bucket. Proven against the truncation itself.
        owner = make_user('256700000390')
        restaurant = Restaurant.objects.create(
            name='Axis Restaurant', location='loc-axis',
            status=RestaurantStatus_Live, owner=owner,
        )
        table = Table.objects.create(number=1, restaurant=restaurant)
        order = Order.objects.create(
            restaurant=restaurant, table=table,
            order_status=OrderStatus_Served,
            total_cost=Decimal('1000.00'), discounted_cost=Decimal('800.00'),
            savings=Decimal('200.00'), actual_cost=Decimal('750.00'),
        )
        Order.objects.filter(id=order.id).update(
            time_created=datetime(2024, 3, 6, 9, 0, tzinfo=dt_timezone.utc),
        )
        qs = sale_orders(restaurant.id, date(2024, 3, 1), date(2024, 3, 31))

        for period in PERIOD_TRUNC:
            with self.subTest(period=period):
                # Exactly the lookup both controllers perform: index the grouped
                # rows by their period, then read them back by axis boundary. It
                # depends on HASH equality, not just ==, which is the part an
                # axis built from the wrong tzinfo would quietly get wrong.
                by_period = {r['period']: r for r in bucket_sales(qs, period)}
                axis = period_boundaries(
                    date(2024, 3, 1), date(2024, 3, 31), period,
                )
                hits = [b for b in axis if b in by_period]
                self.assertEqual(len(hits), 1, f'{period}: {axis}')
                self.assertEqual(by_period[hits[0]]['count'], 1)

    def test_an_inverted_window_is_empty_rather_than_unbounded(self):
        self.assertEqual(
            period_boundaries(date(2024, 3, 5), date(2024, 3, 1), 'day'), [],
        )

    def test_every_supported_period_is_enumerable(self):
        # BOUNDARY_PERIODS spans BOTH bucketing vocabularies, so neither endpoint
        # can request a granularity the axis cannot produce.
        for period in BOUNDARY_PERIODS:
            with self.subTest(period=period):
                self.assertTrue(
                    period_boundaries(date(2024, 3, 1), date(2024, 3, 31), period),
                )

    def test_an_unsupported_period_raises_valueerror(self):
        with self.assertRaises(ValueError):
            period_boundaries(date(2024, 3, 1), date(2024, 3, 31), 'fortnight')

    def test_adding_hour_to_the_axis_did_not_add_it_to_period_trunc(self):
        # The axis spans both vocabularies precisely so the two TRUNCATION maps do
        # not have to be merged. sales-trends must still reject 'hour'.
        self.assertIn('hour', BOUNDARY_PERIODS)
        self.assertNotIn('hour', PERIOD_TRUNC)
