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
    RestaurantStatus_Active, RESTAURANT_OWNER,
    OrderStatus_Paid, PaymentStatus_Paid,
)
from reports_app.controllers.restaurant.dashboard import summarize_revenue
from reports_app.controllers.dinify.dashboard import (
    summarize_orders as summarize_dinify_orders,
)

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
            status=RestaurantStatus_Active, owner=self.owner,
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
            status=RestaurantStatus_Active, owner=self.owner,
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


class DinifyDashboardMonthBoundaryTests(TestCase):
    """Same-shape assertion on the admin (dinify) dashboard summarize_orders."""

    def setUp(self):
        self.owner = make_user('256700000902')
        self.restaurant = Restaurant.objects.create(
            name='TZ Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

    @mock.patch('django.utils.timezone.now', return_value=FROZEN_UTC)
    def test_boundary_order_is_current_month(self, _now):
        order = make_paid_order(self.restaurant, self.table)
        Order.objects.filter(id=order.id).update(time_created=BOUNDARY_ORDER_UTC)
        summary = summarize_dinify_orders()
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['monthly'], 1)
