"""
Tests for the ``dashboard-v2`` chart-granularity vocabulary.

``generate_restaurant_dashboard_v2`` historically resolved its bucketing through
``TRUNC_MAP`` — a map keyed on the caller's UI SELECTION rather than on a
granularity ('day' meant "the user picked Day, so bucket by HOUR") — with a
fail-OPEN ``.get(period, TruncHour)``. Nothing on this path caps the date range
(``clean_dates`` only parses and orders the dates), so an unrecognised value over a
long window silently returned an enormous hourly payload instead of an error.

DASH-PERIOD-00 added the honest ``bucket`` vocabulary
(``hour``/``day``/``week``/``month``/``year``, fail-CLOSED) beside it;
DASH-REMOVE-LEGACY-00 removed ``period`` once the frontend stopped sending it, and
with it the last fail-open path — ``bucket`` is now REQUIRED, so absent, empty,
whitespace-only and unknown are one 400.

The same PR removed the server-computed preceding-window comparison
(``previous_totals`` / ``previous_total`` / ``previous_series``); the frontend now
issues a second call for the basis the user selected. ``DashboardV2ResponseShapeTests``
guards against those fields creeping back.

``week`` joined the vocabulary in DASH-WEEK-00, once the frontend ladder started
emitting it. Its own cases live in ``DashboardV2WeekBucketTests`` below.

Granularity is asserted through the RETURNED SERIES, never by reading
``BUCKET_TRUNC``: a test that asserts a map's contents still passes when the
wiring is broken.

The shared fixture seeds five paid orders inside 2024 chosen so that the granularity
ladder separates — 5 hourly / 3 daily / 2 monthly / 1 yearly — and a wrong truncation
cannot pass by coincidence. ``week`` is the ONE value that does not separate by
count: it also gives 2 buckets on this fixture, so it is asserted by BOUNDARY
(Mondays 03-04 / 06-10, against monthly's 03-01 / 06-01) rather than by length, and
its behavioural cases get their own fixture window. ``time_created`` is
``auto_now_add``, so it is set via ``.update()`` after create; instants are UTC
(EAT is UTC+3, so 09:00 UTC == 12:00 EAT on the same calendar day).
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, Table, RestaurantEmployee
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, RESTAURANT_OWNER,
    OrderStatus_Paid, PaymentStatus_Paid,
    OrderStatus_Refunded, PaymentStatus_Pending,
)
from reports_app.controllers.restaurant.dashboard import (
    generate_restaurant_dashboard_v2,
)
from reports_app.controllers.common.bucketing import PERIOD_TRUNC

# Buckets are truncated on the LOCAL calendar (settings.TIME_ZONE), so the expected
# boundaries are EAT instants.
EAT = ZoneInfo('Africa/Nairobi')

RANGE_FROM = '2024-01-01'
RANGE_TO = '2024-12-31'


def make_user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


def utc(year, month, day, hour=9, minute=0):
    """A timezone-aware UTC instant (Order.time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


def eat(year, month, day, hour=0, minute=0):
    """The EAT instant a bucket boundary is expected to land on."""
    return datetime(year, month, day, hour, minute, tzinfo=EAT)


class DashboardV2Base(TestCase):
    """Paid 2024 orders whose granularity ladder is 5 / 3 / 2 / 1 buckets.

    ``week`` sits outside that ladder — it collapses to 2 buckets here, colliding
    with monthly, so it is pinned by boundary rather than by count. See the module
    docstring.
    """

    def setUp(self):
        self.owner = make_user('256700000500')
        self.restaurant = Restaurant.objects.create(
            name='Dashboard Restaurant', location='loc-dash',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # Three distinct EAT hours on ONE EAT day (12:00 / 14:00 / 16:00 EAT) ...
        self.make_order(when=utc(2024, 3, 4, 9))
        self.make_order(when=utc(2024, 3, 4, 11))
        self.make_order(when=utc(2024, 3, 4, 13))
        # ... a second day in the same month ...
        self.make_order(when=utc(2024, 3, 5, 9))
        # ... and a second month in the same year.
        self.make_order(when=utc(2024, 6, 10, 9))
        # One order OUTSIDE the range, so every count and total below is proof the
        # window filter bites rather than a total of everything seeded. (It was
        # originally seeded to make the removed `previous_totals` comparison
        # non-trivial; it earns its place on the primary window too.)
        self.make_order(when=utc(2023, 6, 15, 9))

    def make_order(self, when, order_status=OrderStatus_Paid,
                   payment_status=PaymentStatus_Paid):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_status=order_status,
            payment_status=payment_status,
            total_cost=Decimal('1000.00'),
            discounted_cost=Decimal('800.00'),
            savings=Decimal('200.00'),
            actual_cost=Decimal('750.00'),
        )
        Order.objects.filter(id=order.id).update(time_created=when)
        return order

    # --- probes ---------------------------------------------------------
    def dashboard(self, **kwargs):
        kwargs.setdefault('restaurant_id', self.restaurant.id)
        kwargs.setdefault('date_from', RANGE_FROM)
        kwargs.setdefault('date_to', RANGE_TO)
        return generate_restaurant_dashboard_v2(**kwargs)

    def order_buckets(self, **kwargs):
        """The ``at`` keys of the orders series — the granularity probe.

        ``_build_orders`` counts every order in range with no status filter, so the
        bucket count is unambiguous.
        """
        result = self.dashboard(**kwargs)
        self.assertEqual(result['status'], 200, result)
        return [row['at'] for row in result['data']['orders']['series']]

    def order_bucket_counts(self, **kwargs):
        """``(at, count)`` pairs — the probe for WHICH bucket an order landed in.

        ``order_buckets`` alone cannot see an order moving between two buckets that
        both already exist, which is exactly what the EAT-boundary case turns on.
        """
        result = self.dashboard(**kwargs)
        self.assertEqual(result['status'], 200, result)
        return [(row['at'], row['count'])
                for row in result['data']['orders']['series']]

    def revenue_buckets(self, **kwargs):
        result = self.dashboard(**kwargs)
        self.assertEqual(result['status'], 200, result)
        return [row['at'] for row in result['data']['revenue']['series']]

    # --- populated-only probes (BUCKETS-ZEROFILL-00) ---------------------
    #
    # Both series are now DENSE: every bucket in the window is emitted, zeroed if
    # it did not trade. So the FULL key list is a property of the window, and only
    # the POPULATED subset still probes the truncation — it is the set of distinct
    # boundaries the seeded orders fell on, which is what the granularity cases
    # were always really asserting. The dense axis itself is covered by
    # DashboardV2ZeroFillTests below.
    def populated_order_buckets(self, **kwargs):
        return [at for at, count in self.order_bucket_counts(**kwargs) if count]

    def populated_order_bucket_counts(self, **kwargs):
        return [(at, count)
                for at, count in self.order_bucket_counts(**kwargs) if count]

    def populated_revenue_buckets(self, **kwargs):
        result = self.dashboard(**kwargs)
        self.assertEqual(result['status'], 200, result)
        return [row['at'] for row in result['data']['revenue']['series']
                if row['gross'] != '0.00']


class DashboardV2BucketVocabularyTests(DashboardV2Base):
    """Each ``bucket`` value truncates to its own, honest granularity.

    Read through the POPULATED buckets: since BUCKETS-ZEROFILL-00 the series spans
    the whole window regardless of granularity, so the distinct boundaries the
    seeded orders landed on are what still distinguish one truncation from another.
    """

    def test_hour_bucket_gives_one_bucket_per_distinct_eat_hour(self):
        buckets = self.populated_order_buckets(bucket='hour')
        self.assertEqual(len(buckets), 5)
        # 09:00 UTC == 12:00 EAT on the same calendar day.
        self.assertEqual(datetime.fromisoformat(buckets[0]), eat(2024, 3, 4, 12))
        self.assertEqual(datetime.fromisoformat(buckets[1]), eat(2024, 3, 4, 14))
        self.assertEqual(datetime.fromisoformat(buckets[2]), eat(2024, 3, 4, 16))
        self.assertEqual(datetime.fromisoformat(buckets[-1]), eat(2024, 6, 10, 12))

    def test_day_bucket_collapses_the_three_hours_into_one_day(self):
        buckets = self.populated_order_buckets(bucket='day')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets],
            [eat(2024, 3, 4), eat(2024, 3, 5), eat(2024, 6, 10)],
        )

    def test_week_bucket_collapses_to_monday_boundaries(self):
        # 2024-03-04 is a Monday and 03-05 the Tuesday of the same week, so the four
        # March orders share ONE bucket; 2024-06-10 is a Monday of its own.
        #
        # Asserted by BOUNDARY, not by count, deliberately: weekly gives 2 populated
        # buckets on this fixture and so does monthly, so a length assertion would
        # pass under a TruncMonth wiring. The Monday keys are what distinguish them.
        buckets = self.populated_order_buckets(bucket='week')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets],
            [eat(2024, 3, 4), eat(2024, 6, 10)],
        )
        self.assertNotEqual(buckets, self.populated_order_buckets(bucket='month'))

    def test_month_bucket_collapses_to_month_boundaries(self):
        buckets = self.populated_order_buckets(bucket='month')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets],
            [eat(2024, 3, 1), eat(2024, 6, 1)],
        )

    def test_year_bucket_collapses_to_a_single_year_boundary(self):
        buckets = self.populated_order_buckets(bucket='year')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets], [eat(2024, 1, 1)],
        )

    def test_the_revenue_series_buckets_identically(self):
        # revenue and orders share ONE trunc_fn — the ladder must hold on both.
        self.assertEqual(
            [len(self.populated_revenue_buckets(bucket=b))
             for b in ('hour', 'day', 'month', 'year')],
            [5, 3, 2, 1],
        )

    def test_the_revenue_series_buckets_weekly_too(self):
        # 'week' is absent from the length ladder above because its count collides
        # with monthly's. Asserting the revenue KEYS equal the orders keys carries
        # the same "one trunc_fn drives both series" guarantee without that
        # ambiguity — and would fail if only one of the two series were rewired.
        self.assertEqual(
            self.populated_revenue_buckets(bucket='week'),
            self.populated_order_buckets(bucket='week'),
        )
        self.assertEqual(
            [datetime.fromisoformat(b)
             for b in self.populated_revenue_buckets(bucket='week')],
            [eat(2024, 3, 4), eat(2024, 6, 10)],
        )

    def test_both_series_share_one_dense_axis(self):
        # The zero-fill must not drift the two cards apart: whatever the window
        # yields, revenue and orders emit the SAME keys in the same order. The
        # populated-key equality above cannot see an axis that differs only in its
        # empty buckets, which is exactly what a per-card fill would produce.
        for bucket in ('hour', 'day', 'week', 'month', 'year'):
            with self.subTest(bucket=bucket):
                self.assertEqual(self.revenue_buckets(bucket=bucket),
                                 self.order_buckets(bucket=bucket))


class DashboardV2ZeroFillTests(DashboardV2Base):
    """Both series span the whole window; empty buckets are zeroed, not omitted.

    BUCKETS-ZEROFILL-00. Before this, a bucket with no orders was simply absent, so
    a chart joined the surrounding buckets into a straight line and implied trading
    that did not happen — and unlike Reports, this endpoint has no client-side fill
    behind it.
    """

    # 2024-03-04 .. 2024-03-08: the fixture's three 03-04 hours and one 03-05 order
    # sit at the front, then 03-06 / 03-07 / 03-08 trade nothing. A deliberate gap
    # in the MIDDLE of the seeded data, not merely a tail.
    GAP_FROM = '2024-03-04'
    GAP_TO = '2024-03-08'

    def test_a_day_that_traded_nothing_is_emitted_as_zero(self):
        series = self.dashboard(
            date_from=self.GAP_FROM, date_to=self.GAP_TO, bucket='day',
        )['data']['orders']['series']

        self.assertEqual([row['at'] for row in series],
                         [eat(2024, 3, d).isoformat() for d in range(4, 9)])
        self.assertEqual([row['count'] for row in series], [3, 1, 0, 0, 0])

    def test_the_revenue_series_zeroes_every_money_column(self):
        series = self.dashboard(
            date_from=self.GAP_FROM, date_to=self.GAP_TO, bucket='day',
        )['data']['revenue']['series']

        self.assertEqual(series[-1], {
            'at': eat(2024, 3, 8).isoformat(),
            'gross': '0.00', 'discounts': '0.00', 'refunds': '0.00',
        })

    def test_a_wholly_empty_window_is_a_full_zero_axis_not_an_empty_list(self):
        # January 2024 has no fixture orders at all.
        data = self.dashboard(
            date_from='2024-01-01', date_to='2024-01-31', bucket='day',
        )['data']

        self.assertEqual(len(data['orders']['series']), 31)
        self.assertEqual(len(data['revenue']['series']), 31)
        self.assertEqual({row['count'] for row in data['orders']['series']}, {0})
        self.assertEqual({row['gross'] for row in data['revenue']['series']},
                         {'0.00'})
        # Still reconciles: an all-zero series and zero totals, not a missing card.
        self.assertEqual(data['revenue']['totals']['gross'], '0.00')
        self.assertEqual(data['orders']['total'], 0)

    def test_the_fill_does_not_zero_over_a_refund_only_bucket(self):
        # WATCH THE BASIS. revenue.series is driven by PAID orders with refunds
        # joined in, so a bucket holding ONLY a refund has no paid row to fill
        # against. It was absent entirely before this change; the fill must insert
        # it carrying the real refund, never a 0.00 that would contradict
        # totals.refunds.
        self.make_order(
            when=utc(2024, 3, 7, 9),
            order_status=OrderStatus_Refunded, payment_status=PaymentStatus_Pending,
        )

        data = self.dashboard(
            date_from=self.GAP_FROM, date_to=self.GAP_TO, bucket='day',
        )['data']
        by_at = {row['at']: row for row in data['revenue']['series']}
        refund_only = by_at[eat(2024, 3, 7).isoformat()]

        self.assertEqual(refund_only['refunds'], '750.00')
        # No paid order that day, so the paid-side columns are genuinely zero.
        self.assertEqual(refund_only['gross'], '0.00')
        self.assertEqual(refund_only['discounts'], '0.00')
        # And the series still reconciles to the totals card.
        self.assertEqual(data['revenue']['totals']['refunds'], '750.00')
        self.assertEqual(
            sum(Decimal(row['refunds']) for row in data['revenue']['series']),
            Decimal(data['revenue']['totals']['refunds']),
        )

    def test_the_fill_never_overwrites_a_bucket_that_has_data(self):
        # The populated rows must be untouched by the fill: same keys, same
        # numbers, in the same order as the grouping alone produced. This is the
        # "zeros add nothing" claim stated as an assertion.
        series = self.dashboard(
            date_from=self.GAP_FROM, date_to=self.GAP_TO, bucket='day',
        )['data']['revenue']['series']
        populated = [row for row in series if row['gross'] != '0.00']

        self.assertEqual(populated, [
            {'at': eat(2024, 3, 4).isoformat(), 'gross': '3000.00',
             'discounts': '600.00', 'refunds': '0.00'},
            {'at': eat(2024, 3, 5).isoformat(), 'gross': '1000.00',
             'discounts': '200.00', 'refunds': '0.00'},
        ])

    def test_totals_are_unchanged_by_the_fill(self):
        # Totals come from their own aggregates, not from the series, so the fill
        # cannot move them — asserted across every granularity so a future fill
        # that summed the series would fail loudly here.
        baseline = self.dashboard(bucket='day')['data']
        for bucket in ('hour', 'week', 'month', 'year'):
            with self.subTest(bucket=bucket):
                data = self.dashboard(bucket=bucket)['data']
                self.assertEqual(data['revenue']['totals'],
                                 baseline['revenue']['totals'])
                self.assertEqual(data['orders']['total'],
                                 baseline['orders']['total'])
                self.assertEqual(data['orders']['breakdown'],
                                 baseline['orders']['breakdown'])
                # The dense series still sums to the gross total.
                self.assertEqual(
                    sum(Decimal(row['gross'])
                        for row in data['revenue']['series']),
                    Decimal(data['revenue']['totals']['gross']),
                )


class DashboardV2FailClosedTests(DashboardV2Base):
    """An unrecognised ``bucket`` is an error, never a silent hourly default."""

    def test_unknown_bucket_is_400_naming_the_accepted_values(self):
        # The message is the contract a caller debugs against, and it is DERIVED
        # from BUCKET_TRUNC — so this is what pins that adding a granularity also
        # advertises it.
        result = self.dashboard(bucket='nonsense')
        self.assertEqual(result['status'], 400)
        for accepted in ('hour', 'day', 'week', 'month', 'year'):
            self.assertIn(accepted, result['message'])
        self.assertNotIn('data', result)

    def test_the_vocabulary_is_not_the_sales_trends_one(self):
        # 'week' is now accepted here (DASH-WEEK-00) — but it was added because a
        # real caller emits it, NOT by unifying this map with PERIOD_TRUNC. The
        # two vocabularies still differ in both directions, and that asymmetry is
        # the boundary between them: 'quarter' is a PERIOD_TRUNC entry the
        # dashboard ladder has no caller for, so it must still 400; 'hour' is a
        # dashboard entry PERIOD_TRUNC does not carry at all.
        self.assertEqual(self.dashboard(bucket='quarter')['status'], 400)
        self.assertEqual(self.dashboard(bucket='hour')['status'], 200)
        # The one claim no dashboard response can express — the OTHER map's
        # contents. This is the guard against a future "tidy-up" merging them.
        self.assertNotIn('hour', PERIOD_TRUNC)

    def test_bucket_lookup_is_case_sensitive(self):
        self.assertEqual(self.dashboard(bucket='DAY')['status'], 400)


class DashboardV2RequiredBucketTests(DashboardV2Base):
    """``bucket`` is REQUIRED — absent, empty and whitespace-only are all a 400.

    Until DASH-REMOVE-LEGACY-00 these fell through to the legacy ``period`` path and
    its fail-open hourly default. With ``period`` gone there is nothing to fall back
    to, and this endpoint bounds neither the date range nor the bucket count, so it
    has no defensible default to invent. Absence is a caller bug, and it says so.
    """

    def test_omitting_bucket_entirely_is_400(self):
        result = self.dashboard()
        self.assertEqual(result['status'], 400)
        self.assertNotIn('data', result)

    def test_empty_string_is_400(self):
        self.assertEqual(self.dashboard(bucket='')['status'], 400)

    def test_whitespace_only_is_400(self):
        self.assertEqual(self.dashboard(bucket='   ')['status'], 400)

    def test_explicit_none_is_400(self):
        self.assertEqual(self.dashboard(bucket=None)['status'], 400)

    def test_the_missing_message_names_the_accepted_values_too(self):
        # Same envelope and same DERIVED accepted-value tail as an unknown value —
        # a caller who omitted the parameter learns what to send, not just that
        # something was wrong.
        result = self.dashboard(bucket='')
        for accepted in ('hour', 'day', 'week', 'month', 'year'):
            self.assertIn(accepted, result['message'])

    def test_missing_and_unknown_are_distinguishable(self):
        # One shape, two causes. The lead clause differs so a caller can tell
        # "you sent nothing" from "you sent something wrong".
        self.assertIn('Missing', self.dashboard(bucket='')['message'])
        self.assertIn('Unsupported', self.dashboard(bucket='nope')['message'])

    def test_a_padded_but_real_bucket_still_resolves(self):
        # Stripping is for padding, not for inventing a default. The stripped key
        # also has to reach the zero-fill, not just the truncation — a padded value
        # that resolved the trunc_fn but not the axis would raise, not 400.
        self.assertEqual(len(self.populated_order_buckets(bucket='  day  ')), 3)
        self.assertEqual(len(self.order_buckets(bucket='  day  ')), 366)


class DashboardV2WeekBucketTests(DashboardV2Base):
    """``bucket='week'`` behaviour at the ENDPOINT, not at the helper (DASH-WEEK-00).

    ``dashboard.py`` shares no code with ``common/bucketing.py`` — it rolls its own
    grouped queries — so ``tests_reports_foundations.test_bucket_by_week`` says
    nothing about this path. These cases cover it directly.

    ``TruncWeek`` is Monday-anchored, and these buckets truncate on the local
    calendar, so an ``at`` key is the MONDAY of its week in EAT. ``sales-trends``
    anchors weeks to the same Monday; only the emitted FORMAT differs
    ('YYYY-MM-DD' there, a full ISO datetime with +03:00 here).

    Calendar facts these cases rely on: 2024-09-08 is a Sunday, sitting in the week
    of Monday 2024-09-02; 2024-09-09 is a Monday; 2024-09-14 is the Saturday of the
    09-09 week.

    The window is September so the inherited base fixture (2024-03, 2024-06, 2023-06)
    falls entirely outside it. ``WEEK_TO`` is padded past the last seeded order on
    purpose: this controller date-filters with a raw ``time_created__lte=<date>``,
    which Django coerces to MIDNIGHT in the active timezone, so a window ending on
    2024-09-14 would silently drop that day's 12:00-EAT order.
    """

    WEEK_FROM = '2024-09-01'
    WEEK_TO = '2024-09-30'

    def setUp(self):
        super().setUp()
        self.make_order(when=utc(2024, 9, 8, 9))    # Sun 12:00 EAT -> week 09-02
        self.make_order(when=utc(2024, 9, 9, 9))    # Mon 12:00 EAT -> week 09-09
        self.make_order(when=utc(2024, 9, 14, 9))   # Sat 12:00 EAT -> week 09-09
        # 2024-09-08 23:30 UTC == 2024-09-09 02:30 EAT: Sunday in UTC, Monday in
        # EAT. Bucketed in UTC it would join the 09-02 week; in EAT it must not.
        self.make_order(when=utc(2024, 9, 8, 23, 30))

    def dashboard(self, **kwargs):
        # Override the January-December default so a case that forgets its dates
        # cannot silently mix these orders with the inherited base fixture.
        kwargs.setdefault('date_from', self.WEEK_FROM)
        kwargs.setdefault('date_to', self.WEEK_TO)
        return super().dashboard(**kwargs)

    def test_a_sunday_and_the_following_monday_are_different_buckets(self):
        buckets = [datetime.fromisoformat(b)
                   for b in self.populated_order_buckets(bucket='week')]
        self.assertEqual(buckets, [eat(2024, 9, 2), eat(2024, 9, 9)])

    def test_a_monday_and_the_saturday_of_its_week_share_one_bucket(self):
        # Both fall in the 09-09 week; the 02:30-EAT order joins them, so that
        # bucket holds three of the four September orders and the Sunday one is
        # alone in its own.
        self.assertEqual(
            self.populated_order_bucket_counts(bucket='week'),
            [(eat(2024, 9, 2).isoformat(), 1),
             (eat(2024, 9, 9).isoformat(), 3)],
        )

    def test_the_week_boundary_is_eat_not_utc(self):
        # The 2024-09-08 23:30 UTC order is 02:30 EAT on Monday 09-09 and must land
        # in the LATER week. No clock is mocked: this path reads no clock, so the
        # late-UTC instant IS the whole mechanism (the house pattern in
        # tests_timezone_clocks.py).
        #
        # Counts are what carry this. Both bucket KEYS exist either way — under a
        # UTC truncation the split would be (2, 2) instead of (1, 3), and the
        # boundary offsets would read +00:00 rather than +03:00.
        counts = dict(self.order_bucket_counts(bucket='week'))
        self.assertEqual(counts[eat(2024, 9, 9).isoformat()], 3)
        self.assertEqual(counts[eat(2024, 9, 2).isoformat()], 1)
        for at in counts:
            self.assertTrue(at.endswith('+03:00'), at)

    def test_weekly_is_coarser_than_daily_over_the_same_window(self):
        # Four orders on three distinct EAT days collapse into two weeks — proof
        # the truncation changed rather than the window.
        self.assertEqual(
            [datetime.fromisoformat(b)
             for b in self.populated_order_buckets(bucket='day')],
            [eat(2024, 9, 8), eat(2024, 9, 9), eat(2024, 9, 14)],
        )
        self.assertEqual(len(self.populated_order_buckets(bucket='week')), 2)
        # And the dense axes are coarser too — 30 September days, 6 weeks.
        self.assertEqual(len(self.order_buckets(bucket='day')), 30)
        self.assertEqual(len(self.order_buckets(bucket='week')), 6)

    def test_the_endpoint_serves_a_week_bucket(self):
        token = str(RefreshToken.for_user(self.owner).access_token)
        resp = self.client.get(
            f'/api/v1/reports/restaurant/dashboard-v2/'
            f'?restaurant={self.restaurant.id}'
            f'&from={self.WEEK_FROM}&to={self.WEEK_TO}&bucket=week',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        series = resp.json()['data']['orders']['series']
        # The exact strings the frontend enumerator will parse — now the WHOLE
        # September axis, not just the two weeks that traded. It opens on Monday
        # 08-26 because 09-01 is a Sunday: the window's first day has to have a
        # bucket to land in.
        self.assertEqual([row['at'] for row in series],
                         ['2024-08-26T00:00:00+03:00',
                          '2024-09-02T00:00:00+03:00',
                          '2024-09-09T00:00:00+03:00',
                          '2024-09-16T00:00:00+03:00',
                          '2024-09-23T00:00:00+03:00',
                          '2024-09-30T00:00:00+03:00'])
        self.assertEqual([row['count'] for row in series], [0, 1, 3, 0, 0, 0])


class DashboardV2ResponseShapeTests(DashboardV2Base):
    """The payload carries ONE window, and the primary window's numbers are pinned.

    DASH-REMOVE-LEGACY-00 removed the server-computed preceding-equal-length
    comparison. The frontend now issues a second call for the basis the user selected
    — which the server cannot infer — so re-adding these fields would restore a
    second aggregate pass over a second date window on every dashboard load, per
    card, for nobody.
    """

    LEGACY_FIELDS = ('previous_series', 'previous_totals', 'previous_total')

    def test_no_previous_period_fields_survive_anywhere(self):
        for bucket in ('hour', 'day', 'week', 'month', 'year'):
            data = self.dashboard(bucket=bucket)['data']
            for card in ('revenue', 'orders'):
                for field in self.LEGACY_FIELDS:
                    with self.subTest(bucket=bucket, card=card, field=field):
                        self.assertNotIn(field, data[card])

    def test_the_card_shapes_are_exactly_what_remains(self):
        data = self.dashboard(bucket='day')['data']
        self.assertEqual(set(data['revenue']), {'series', 'totals'})
        self.assertEqual(set(data['orders']), {'series', 'breakdown', 'total'})

    def test_the_primary_window_totals_are_unchanged(self):
        # Five in-range paid orders at 1000.00 gross / 200.00 savings / 750.00
        # actual, none refunded — and the sixth, 2023 order excluded by the window.
        # These are the pre-refactor values; the previous window never fed them.
        totals = self.dashboard(bucket='day')['data']['revenue']['totals']
        self.assertEqual(totals, {
            'gross': '5000.00',
            'discounts': '1000.00',
            'refunds': '0.00',
            'net': '4000.00',
        })

    def test_the_primary_window_order_total_is_unchanged(self):
        orders = self.dashboard(bucket='day')['data']['orders']
        self.assertEqual(orders['total'], 5)
        self.assertEqual(
            {row['status']: row['count'] for row in orders['breakdown']},
            {'paid': 5, 'open': 0, 'cancelled': 0, 'refunded': 0},
        )

    def test_the_totals_do_not_move_with_the_bucket(self):
        # Granularity reshapes the SERIES, never the totals.
        baseline = self.dashboard(bucket='hour')['data']
        for bucket in ('day', 'week', 'month', 'year'):
            with self.subTest(bucket=bucket):
                data = self.dashboard(bucket=bucket)['data']
                self.assertEqual(data['revenue']['totals'],
                                 baseline['revenue']['totals'])
                self.assertEqual(data['orders']['total'],
                                 baseline['orders']['total'])


class DashboardV2QueryCountTests(DashboardV2Base):
    """The point of removing the previous window: it halved the aggregation.

    Each card used to run its ENTIRE aggregation twice — once per window — so the
    second window cost 5 queries in revenue (paid buckets, refund buckets, three
    aggregates) and 2 in orders (bucket rows, count). Pinned because the shape of
    the deletion is invisible in a response assertion.
    """

    def test_revenue_and_orders_each_query_one_window(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from reports_app.controllers.restaurant import dashboard as d
        from misc_app.controllers.clean_dates import clean_dates

        dates = clean_dates(date_from=RANGE_FROM, date_to=RANGE_TO)
        args = (str(self.restaurant.id), dates['date_from'], dates['date_to'],
                d.BUCKET_TRUNC['day'], 'day')

        with CaptureQueriesContext(connection) as ctx:
            d._build_revenue(*args)
        self.assertEqual(len(ctx), 5)          # was 10

        with CaptureQueriesContext(connection) as ctx:
            d._build_orders(*args)
        self.assertEqual(len(ctx), 6)          # was 8 (4 are the breakdown counts)


class DashboardV2EndpointTests(DashboardV2Base):
    """The endpoint threads ``bucket`` through and maps the 400 to an HTTP status."""

    def url(self, query=''):
        return (
            f'/api/v1/reports/restaurant/dashboard-v2/'
            f'?restaurant={self.restaurant.id}'
            f'&from={RANGE_FROM}&to={RANGE_TO}&{query}'
        )

    def auth(self):
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def test_unknown_bucket_is_http_400(self):
        resp = self.client.get(self.url('bucket=nonsense'), **self.auth())
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('hour', resp.json()['message'])

    def test_a_valid_bucket_reaches_the_controller(self):
        resp = self.client.get(self.url('bucket=year'), **self.auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        # Proof the parameter was threaded: the window is one calendar year, so
        # yearly gives ONE bucket where daily would give 366.
        self.assertEqual(len(resp.json()['data']['orders']['series']), 1)

    def test_an_empty_bucket_query_parameter_is_http_400(self):
        # A stray `&bucket=` used to fall back to the legacy period default. There
        # is no fallback left, so it is now the same 400 as any other absence.
        resp = self.client.get(self.url('bucket='), **self.auth())
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('hour', resp.json()['message'])

    def test_no_bucket_parameter_is_http_400(self):
        resp = self.client.get(self.url(), **self.auth())
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    def test_a_stray_period_parameter_is_ignored_not_honoured(self):
        # `period` is gone. A caller still sending it gets the 400 for the absent
        # `bucket` — never the granularity the retired parameter used to select.
        resp = self.client.get(self.url('period=ytd'), **self.auth())
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_period_no_longer_selects_a_granularity(self):
        # The collision that forced `bucket` to be a NEW parameter rather than an
        # alias: 'day' meant hourly as a period and daily as a bucket. Only the
        # bucket meaning survives, so this is daily (366 buckets over the 2024
        # window) and never hourly (8784).
        resp = self.client.get(self.url('period=day&bucket=day'), **self.auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()['data']['orders']['series']), 366)
