"""
Timezone-correctness tests for the "now / today / this-month" clock reads
(BUG-P3-3).

``settings.USE_TZ=True`` and ``TIME_ZONE='Africa/Nairobi'`` (EAT, UTC+3), and the
DB stores UTC — so a naive ``datetime.now()`` (the server's UTC wall clock) lands
on the wrong calendar day/month in the 00:00-03:00 EAT window, while aware field
lookups (``__month``/``__year``/``__date``) convert stored UTC to EAT. These tests
freeze ``django.utils.timezone.now`` at a fixed aware-UTC instant that is already
the NEXT day (and month) in EAT and assert every boundary resolves in EAT.

No freezegun: ``mock.patch('django.utils.timezone.now', ...)`` also drives
``timezone.localtime`` / ``timezone.localdate``, which is exactly the surface
under test.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, Table, RestaurantEmployee
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, RESTAURANT_OWNER,
    OrderStatus_Paid, PaymentStatus_Paid,
)
from reports_app.controllers.restaurant.dashboard import summarize_revenue

# 2026-07-31 22:30 UTC == 2026-08-01 01:30 EAT — an instant whose EAT calendar day
# (and month) is one ahead of its UTC day (and month).
FROZEN_UTC = datetime(2026, 7, 31, 22, 30, tzinfo=dt_timezone.utc)
# 2026-07-31 21:30 UTC == 2026-08-01 00:30 EAT — an order created "in August" EAT
# but still "July" in UTC.
BOUNDARY_ORDER_UTC = datetime(2026, 7, 31, 21, 30, tzinfo=dt_timezone.utc)


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


def make_paid_order(restaurant, table):
    """A paid sale order; ``time_created`` is auto_now_add so the caller forces it
    via ``.update()`` afterwards."""
    return Order.objects.create(
        restaurant=restaurant, table=table,
        order_status=OrderStatus_Paid, payment_status=PaymentStatus_Paid,
        total_cost=Decimal('1000.00'), discounted_cost=Decimal('800.00'),
        savings=Decimal('200.00'), actual_cost=Decimal('750.00'),
    )


class RestaurantReportsDefaultWindowTests(TestCase):
    """THE HEADLINE: with no from/to, the default report window is *today in EAT*."""

    def setUp(self):
        self.owner = make_user('256700000900')
        self.restaurant = Restaurant.objects.create(
            name='TZ Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
        )

    def _auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    @mock.patch('django.utils.timezone.now', return_value=FROZEN_UTC)
    @mock.patch(
        'reports_app.endpoints.restaurant_reports.generate_restaurant_dashboard_details'
    )
    def test_dashboard_default_window_is_eat_today(self, mock_dashboard, _now):
        # At 2026-08-01 01:30 EAT the default from/to must be '2026-08-01', not the
        # UTC-wall-clock '2026-07-31'.
        mock_dashboard.return_value = {'status': 200, 'message': 'ok', 'data': {}}
        resp = self.client.get(
            f'/api/v1/reports/restaurant/dashboard/?restaurant={self.restaurant.id}',
            **self._auth(self.owner),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        mock_dashboard.assert_called_once()
        kwargs = mock_dashboard.call_args.kwargs
        self.assertEqual(kwargs['date_from'], '2026-08-01')
        self.assertEqual(kwargs['date_to'], '2026-08-01')


class RestaurantDashboardMonthBoundaryTests(TestCase):
    """summarize_revenue counts a 00:30-EAT order as the current (EAT) month."""

    def setUp(self):
        self.owner = make_user('256700000901')
        self.restaurant = Restaurant.objects.create(
            name='TZ Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

    @mock.patch('django.utils.timezone.now', return_value=FROZEN_UTC)
    def test_boundary_order_is_current_month(self, _now):
        order = make_paid_order(self.restaurant, self.table)
        Order.objects.filter(id=order.id).update(time_created=BOUNDARY_ORDER_UTC)
        result = summarize_revenue(self.restaurant.id)
        # "now" resolves to 2026-08-01 EAT (month 8); the order is EAT-August too,
        # so its actual_cost lands in this_month. Under the naive bug (UTC month 7)
        # it would be excluded and this_month would be 0.
        self.assertEqual(result['this_month'], Decimal('750.00'))


class DinifyDashboardRetiredTests(TestCase):
    """
    The admin (dinify) dashboard used to carry a second copy of the same EAT
    month-boundary logic, asserted here alongside the restaurant one.

    The whole `reports_app.controllers.dinify` package went with ambient
    administrator authority (PR-A) — it served cross-tenant revenue on the strength
    of a role string. The boundary behaviour it shared is still covered above by
    `RestaurantDashboardMonthBoundaryTests` against the LIVE controller; this class
    just pins the retirement so the import cannot quietly come back.
    """

    def test_the_dinify_reports_controllers_are_gone(self):
        with self.assertRaises(ImportError):
            import reports_app.controllers.dinify  # noqa: F401


class DashboardV2TodayIsEatTodayTests(TestCase):
    """
    The dashboard-v2 Tables and KDS cards count "today" in EAT, like every other
    clock read in this file.

    Both builders took ``today`` from ``timezone.now().date()`` — the UTC calendar
    day — and compared it against ``time_created__date`` / ``served_at__date``, which
    Django resolves in EAT. From 00:00 to 03:00 EAT (21:00-24:00 UTC) the two differ
    by a day, so the cards reported YESTERDAY's turns, average ticket and median visit
    as today's, the day before as yesterday's, and no fulfilment time at all. That is
    also why ``tests_test_restaurant_parity``'s Tables-card test failed every night
    between 21:00 and 24:00 UTC and passed the rest of the day.

    The fixture separates the two days by COUNT (one order today, two yesterday, one
    table), so the defect cannot pass by coincidence: under the UTC read, "today"
    picks up yesterday's two orders.
    """

    def setUp(self):
        self.owner = make_user('256700000902')
        self.restaurant = Restaurant.objects.create(
            name='TZ Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # 2026-08-01 00:30 EAT: today, in EAT. Served fifteen minutes later.
        today = make_paid_order(self.restaurant, self.table)
        Order.objects.filter(id=today.id).update(
            time_created=BOUNDARY_ORDER_UTC,
            time_last_updated=datetime(2026, 7, 31, 21, 50, tzinfo=dt_timezone.utc),
            fulfilment_status='served',
            served_at=datetime(2026, 7, 31, 21, 45, tzinfo=dt_timezone.utc),
        )
        # 2026-07-31 00:30 and 12:00 EAT: yesterday, in EAT (and "today" in UTC at
        # the frozen instant, which is the whole trap).
        for created in (datetime(2026, 7, 30, 21, 30, tzinfo=dt_timezone.utc),
                        datetime(2026, 7, 31, 9, 0, tzinfo=dt_timezone.utc)):
            order = make_paid_order(self.restaurant, self.table)
            Order.objects.filter(id=order.id).update(
                time_created=created, time_last_updated=created,
            )

    @mock.patch('django.utils.timezone.now', return_value=FROZEN_UTC)
    def test_the_tables_card_counts_today_in_eat(self, _now):
        from reports_app.controllers.restaurant.dashboard import _build_tables
        tables = _build_tables(self.restaurant.id)
        self.assertEqual(tables['turns_today'], '1.0')
        self.assertEqual(tables['turns_yesterday'], '2.0')
        self.assertEqual(tables['avg_ticket_today'], '750.00')
        self.assertEqual(tables['median_visit_minutes'], '20.0')

    @mock.patch('django.utils.timezone.now', return_value=FROZEN_UTC)
    def test_the_kds_card_counts_orders_served_today_in_eat(self, _now):
        from reports_app.controllers.restaurant.dashboard import _build_kds
        self.assertEqual(_build_kds(self.restaurant.id)['avg_fulfillment_minutes'], '15.0')

    def test_CONTROL_the_same_instant_outside_the_window_already_agreed(self):
        # 2026-08-01 12:00 UTC == 15:00 EAT: both calendars say 1 August, so the old
        # UTC read and the EAT read give the same answer. The fixture is right; only
        # the 21:00-24:00 UTC window ever exposed the defect.
        noon = datetime(2026, 8, 1, 12, 0, tzinfo=dt_timezone.utc)
        from reports_app.controllers.restaurant.dashboard import _build_tables
        with mock.patch('django.utils.timezone.now', return_value=noon):
            tables = _build_tables(self.restaurant.id)
        self.assertEqual(tables['turns_today'], '1.0')
        self.assertEqual(tables['turns_yesterday'], '2.0')
