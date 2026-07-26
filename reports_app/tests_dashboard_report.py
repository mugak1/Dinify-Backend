"""
Tests for the ``dashboard-v2`` chart-granularity vocabulary (DASH-PERIOD-00).

``generate_restaurant_dashboard_v2`` historically resolved its bucketing through
``TRUNC_MAP`` — a map keyed on the caller's UI SELECTION rather than on a
granularity ('day' meant "the user picked Day, so bucket by HOUR") — with a
fail-OPEN ``.get(period, TruncHour)``. Nothing on this path caps the date range
(``clean_dates`` only parses and orders the dates), so an unrecognised value over a
long window silently returned an enormous hourly payload instead of an error.

These cover the honest ``bucket`` vocabulary that replaces it
(``hour``/``day``/``month``/``year``, fail-CLOSED) and — just as importantly — pin
that the legacy ``period`` path is unchanged, since the deployed frontend still
sends it and this backend auto-deploys on merge, before the frontend's
TIMEFRAME-01B ships.

Granularity is asserted through the RETURNED SERIES, never by reading ``TRUNC_MAP``
/ ``BUCKET_TRUNC``: a test that asserts a map's contents still passes when the
wiring is broken.

The shared fixture seeds five paid orders inside 2024 chosen so that every
granularity collapses to a DIFFERENT bucket count — 5 hourly / 3 daily / 2 monthly
/ 1 yearly — so a wrong truncation cannot pass by coincidence. ``time_created`` is
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
)
from reports_app.controllers.restaurant.dashboard import (
    generate_restaurant_dashboard_v2,
)

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
    """Paid 2024 orders whose granularity ladder is 5 / 3 / 2 / 1 buckets."""

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
        # One order in the PREVIOUS window (2022-12-31..2023-12-31 for this range),
        # so previous_totals is non-zero and the comparison-window test is not
        # trivially satisfied by a pair of zeros.
        self.make_order(when=utc(2023, 6, 15, 9))

    def make_order(self, when):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_status=OrderStatus_Paid,
            payment_status=PaymentStatus_Paid,
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

    def revenue_buckets(self, **kwargs):
        result = self.dashboard(**kwargs)
        self.assertEqual(result['status'], 200, result)
        return [row['at'] for row in result['data']['revenue']['series']]


class DashboardV2BucketVocabularyTests(DashboardV2Base):
    """Each ``bucket`` value truncates to its own, honest granularity."""

    def test_hour_bucket_gives_one_bucket_per_distinct_eat_hour(self):
        buckets = self.order_buckets(bucket='hour')
        self.assertEqual(len(buckets), 5)
        # 09:00 UTC == 12:00 EAT on the same calendar day.
        self.assertEqual(datetime.fromisoformat(buckets[0]), eat(2024, 3, 4, 12))
        self.assertEqual(datetime.fromisoformat(buckets[1]), eat(2024, 3, 4, 14))
        self.assertEqual(datetime.fromisoformat(buckets[2]), eat(2024, 3, 4, 16))
        self.assertEqual(datetime.fromisoformat(buckets[-1]), eat(2024, 6, 10, 12))

    def test_day_bucket_collapses_the_three_hours_into_one_day(self):
        buckets = self.order_buckets(bucket='day')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets],
            [eat(2024, 3, 4), eat(2024, 3, 5), eat(2024, 6, 10)],
        )

    def test_month_bucket_collapses_to_month_boundaries(self):
        buckets = self.order_buckets(bucket='month')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets],
            [eat(2024, 3, 1), eat(2024, 6, 1)],
        )

    def test_year_bucket_collapses_to_a_single_year_boundary(self):
        buckets = self.order_buckets(bucket='year')
        self.assertEqual(
            [datetime.fromisoformat(b) for b in buckets], [eat(2024, 1, 1)],
        )

    def test_the_revenue_series_buckets_identically(self):
        # revenue and orders share ONE trunc_fn — the ladder must hold on both.
        self.assertEqual(
            [len(self.revenue_buckets(bucket=b))
             for b in ('hour', 'day', 'month', 'year')],
            [5, 3, 2, 1],
        )


class DashboardV2FailClosedTests(DashboardV2Base):
    """An unrecognised ``bucket`` is an error, never a silent hourly default."""

    def test_unknown_bucket_is_400_naming_the_accepted_values(self):
        result = self.dashboard(bucket='nonsense')
        self.assertEqual(result['status'], 400)
        for accepted in ('hour', 'day', 'month', 'year'):
            self.assertIn(accepted, result['message'])
        self.assertNotIn('data', result)

    def test_the_vocabulary_is_not_the_sales_trends_one(self):
        # 'week' / 'quarter' are PERIOD_TRUNC (sales-trends) entries. The dashboard
        # ladder emits neither, and unused vocabulary is surface we would have to
        # keep correct for no caller — so they must 400 here rather than quietly
        # working. This pins the deliberate NON-unification of the two maps.
        for absent in ('week', 'quarter'):
            with self.subTest(bucket=absent):
                self.assertEqual(self.dashboard(bucket=absent)['status'], 400)

    def test_bucket_lookup_is_case_sensitive(self):
        self.assertEqual(self.dashboard(bucket='DAY')['status'], 400)

    def test_a_legacy_period_typo_still_fails_open_to_hourly(self):
        # The LEGACY parameter deliberately keeps its fail-open default — tightening
        # it is exactly the breakage the new parameter exists to avoid.
        self.assertEqual(len(self.order_buckets(period='nonsense')), 5)


class DashboardV2LegacyPeriodCompatibilityTests(DashboardV2Base):
    """With ``bucket`` absent, every legacy ``period`` behaves exactly as before.

    This is the compatibility guarantee that lets this backend merge and deploy
    BEFORE the frontend's TIMEFRAME-01B ships: legacy 'day' -> hourly,
    'week' -> daily, 'month' -> daily, 'ytd' -> monthly.
    """

    LEGACY_BUCKET_COUNTS = {'day': 5, 'week': 3, 'month': 3, 'ytd': 2}

    def test_each_legacy_period_keeps_its_granularity(self):
        for period, expected in self.LEGACY_BUCKET_COUNTS.items():
            with self.subTest(period=period):
                self.assertEqual(len(self.order_buckets(period=period)), expected)

    def test_legacy_week_and_month_are_both_daily(self):
        # The legacy map points 'week' AND 'month' at the same truncation
        # (TruncDay). Pinned because it looks like a bug and is not one.
        self.assertEqual(
            self.order_buckets(period='week'), self.order_buckets(period='month'),
        )

    def test_the_default_period_is_hourly(self):
        # Neither parameter supplied — the signature default ('day') resolves
        # through the legacy map to TruncHour, as it always has.
        self.assertEqual(len(self.order_buckets()), 5)

    def test_omitting_bucket_matches_passing_none_explicitly(self):
        self.assertEqual(
            self.order_buckets(period='ytd'),
            self.order_buckets(period='ytd', bucket=None),
        )


class DashboardV2AbsentBucketTests(DashboardV2Base):
    """Empty / whitespace-only ``bucket`` is ABSENT, not an unknown granularity.

    A stray ``&bucket=`` in a URL must not 400.
    """

    def test_empty_string_falls_back_to_the_legacy_path(self):
        result = self.dashboard(period='ytd', bucket='')
        self.assertEqual(result['status'], 200)
        self.assertEqual(len(result['data']['orders']['series']), 2)  # monthly

    def test_whitespace_only_falls_back_to_the_legacy_path(self):
        result = self.dashboard(period='ytd', bucket='   ')
        self.assertEqual(result['status'], 200)
        self.assertEqual(len(result['data']['orders']['series']), 2)

    def test_a_padded_but_real_bucket_still_resolves(self):
        self.assertEqual(len(self.order_buckets(bucket='  day  ')), 3)


class DashboardV2PrecedenceTests(DashboardV2Base):
    """When both are supplied ``bucket`` wins — it never errors on the conflict."""

    def test_bucket_overrides_a_disagreeing_period(self):
        # period='ytd' alone is monthly (2 buckets); bucket='hour' must win (5).
        self.assertEqual(len(self.order_buckets(period='ytd')), 2)
        self.assertEqual(len(self.order_buckets(period='ytd', bucket='hour')), 5)

    def test_bucket_overrides_in_the_coarsening_direction_too(self):
        # period='day' alone is hourly (5); bucket='year' must win (1).
        self.assertEqual(len(self.order_buckets(period='day')), 5)
        self.assertEqual(len(self.order_buckets(period='day', bucket='year')), 1)

    def test_an_unknown_bucket_400s_even_beside_a_valid_period(self):
        # Precedence is not a fallback: `bucket` winning means a BAD `bucket` is an
        # error, not a quiet demotion to the legacy path.
        result = self.dashboard(period='day', bucket='nonsense')
        self.assertEqual(result['status'], 400)


class DashboardV2VocabularyCollisionTests(DashboardV2Base):
    """THE reason ``bucket`` is a new parameter rather than an alias of ``period``.

    The same string means DIFFERENT granularities in the two vocabularies::

        'day'    period -> TruncHour     bucket -> TruncDay
        'month'  period -> TruncDay      bucket -> TruncMonth

    so no alias table could express both, and a hard cutover would have broken the
    deployed frontend in the window between this backend auto-deploying on merge and
    TIMEFRAME-01B shipping. If you are reading this because you want to merge
    ``TRUNC_MAP`` and ``BUCKET_TRUNC`` into one map: this is what would break.
    """

    def test_day_means_hourly_as_a_period_and_daily_as_a_bucket(self):
        self.assertEqual(len(self.order_buckets(period='day')), 5)   # hourly
        self.assertEqual(len(self.order_buckets(bucket='day')), 3)   # daily
        self.assertNotEqual(
            self.order_buckets(period='day'), self.order_buckets(bucket='day'),
        )

    def test_month_means_daily_as_a_period_and_monthly_as_a_bucket(self):
        self.assertEqual(len(self.order_buckets(period='month')), 3)  # daily
        self.assertEqual(len(self.order_buckets(bucket='month')), 2)  # monthly
        self.assertNotEqual(
            self.order_buckets(period='month'), self.order_buckets(bucket='month'),
        )


class DashboardV2PreviousWindowTests(DashboardV2Base):
    """The comparison window is a pure function of the DATES, never the bucketing.

    ``previous_totals`` / ``previous_total`` come from ``.aggregate()`` /
    ``.count()`` over a window derived only from ``(date_to - date_from)``, so they
    must be identical across every granularity. (``previous_series`` is deliberately
    NOT asserted here — it runs through the same ``trunc_fn`` and is
    bucket-dependent by design.)
    """

    def test_previous_totals_are_identical_across_every_bucket(self):
        results = {
            b: self.dashboard(bucket=b)['data']
            for b in ('hour', 'day', 'month', 'year')
        }
        baseline = results['hour']['revenue']['previous_totals']
        # The 2023 order makes this a real comparison, not a pair of zeros.
        self.assertEqual(baseline['gross'], '1000.00')
        for name, data in results.items():
            with self.subTest(bucket=name):
                self.assertEqual(data['revenue']['previous_totals'], baseline)
                self.assertEqual(data['orders']['previous_total'], 1)

    def test_previous_totals_are_identical_across_legacy_periods_too(self):
        baseline = self.dashboard(period='day')['data']['revenue']['previous_totals']
        for period in ('week', 'month', 'ytd'):
            with self.subTest(period=period):
                data = self.dashboard(period=period)['data']
                self.assertEqual(data['revenue']['previous_totals'], baseline)


class DashboardV2EndpointTests(DashboardV2Base):
    """The endpoint threads ``bucket`` through and maps the 400 to an HTTP status.

    ``bucket`` is read with NO default, so absence stays distinguishable from an
    empty string at the HTTP layer too.
    """

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
        # Proof the parameter was threaded: yearly collapses the fixture to ONE
        # bucket, where the endpoint's default period ('day') would give five.
        self.assertEqual(len(resp.json()['data']['orders']['series']), 1)

    def test_an_empty_bucket_query_parameter_is_http_200(self):
        # A stray `&bucket=` must not 400; it falls back to the period default.
        resp = self.client.get(self.url('bucket='), **self.auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()['data']['orders']['series']), 5)

    def test_no_bucket_parameter_keeps_the_legacy_period_behaviour(self):
        resp = self.client.get(self.url('period=ytd'), **self.auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()['data']['orders']['series']), 2)
