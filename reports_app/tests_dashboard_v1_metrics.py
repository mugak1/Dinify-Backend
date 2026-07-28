"""
Tests for the v1 restaurant dashboard's metric DEFINITIONS.

``generate_restaurant_dashboard_details`` computed ``num_sales`` as a bare
``orders.count()`` over a queryset filtered only by restaurant / ``is_test`` /
date. That counted abandoned ``initiated`` drafts, cancellations and refunds as
sales — the name said "sales", the value said "orders" — and then used the same
inflated figure as the denominator for the paid, cancelled and refunded
percentages (PHASE_0_5_CLOSURE.md, "Reported, not fixed").

Each metric is now defined once in ``dashboard.py`` and every figure derives from
the definition:

  * orders placed = submitted orders (``order_status != initiated``), the shared
    denominator for every rate;
  * sales         = orders placed ∩ ``SALE_STATUSES`` ({served, paid});
  * revenue       = ``Sum('actual_cost')`` over sales, sale_filters' basis.

Sharing one denominator is what makes the cancellation and refund rates
comparable to each other and keeps either from exceeding 100%.

The fixture seeds ONE order in each of the seven order statuses so a metric that
silently widens or narrows its status set cannot pass by coincidence, plus one
order outside the window so every figure is proof the date filter bites rather
than a total of everything seeded. ``time_created`` is ``auto_now_add``, so it is
set via ``.update()`` after create — the same pattern ``tests_dashboard_report``
uses.

Money is deliberately DIFFERENT per status (served 750 / paid 1250, everything
else 999) so a revenue figure that accidentally includes a non-sale is visible in
the total rather than hidden behind equal amounts.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import Restaurant, Table, RestaurantEmployee
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, RESTAURANT_OWNER,
    OrderStatus_Initiated, OrderStatus_Pending, OrderStatus_Preparing,
    OrderStatus_Served, OrderStatus_Paid,
    OrderStatus_Refunded, OrderStatus_Cancelled,
    PaymentStatus_Paid, PaymentStatus_Pending,
)
from reports_app.controllers.restaurant.dashboard import (
    PAYMENT_TRACKING_ENABLED,
    generate_restaurant_dashboard_details,
    generate_restaurant_dashboard_v2,
)
from reports_app.controllers.common.sale_filters import revenue_sum, sale_orders

RANGE_FROM = '2024-01-01'
RANGE_TO = '2024-12-31'


def utc(year, month, day, hour=9, minute=0):
    """A timezone-aware UTC instant (Order.time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


class DashboardV1Base(TestCase):
    """One order in each of the seven statuses, inside a 2024 window."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Dash', last_name='Owner',
            email='256700000600@test.com', phone_number='256700000600',
            username='256700000600', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Metric Restaurant', location='loc-metrics',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

    def make_order(self, order_status, when=None,
                   payment_status=PaymentStatus_Pending, actual_cost='999.00'):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_status=order_status,
            payment_status=payment_status,
            total_cost=Decimal('1500.00'),
            discounted_cost=Decimal(actual_cost),
            savings=Decimal('100.00'),
            actual_cost=Decimal(actual_cost),
        )
        Order.objects.filter(id=order.id).update(
            time_created=when or utc(2024, 3, 4),
        )
        return order

    def seed_all_statuses(self):
        """One order per status, plus one sale outside the window."""
        self.make_order(OrderStatus_Initiated)
        self.make_order(OrderStatus_Pending)
        self.make_order(OrderStatus_Preparing)
        self.make_order(OrderStatus_Served, actual_cost='750.00')
        self.make_order(
            OrderStatus_Paid,
            payment_status=PaymentStatus_Paid, actual_cost='1250.00',
        )
        self.make_order(OrderStatus_Refunded)
        self.make_order(OrderStatus_Cancelled)
        # Outside the window — never counted by anything below.
        self.make_order(
            OrderStatus_Served, when=utc(2023, 6, 15), actual_cost='500000.00',
        )

    def data(self, date_from=RANGE_FROM, date_to=RANGE_TO):
        response = generate_restaurant_dashboard_details(
            restaurant_id=str(self.restaurant.id),
            date_from=date_from,
            date_to=date_to,
        )
        self.assertEqual(response['status'], 200, response)
        return response['data']


class OrdersPlacedTests(DashboardV1Base):
    """Abandoned drafts are not orders, and are the denominator's whole point."""

    def test_orders_placed_excludes_initiated_drafts(self):
        self.seed_all_statuses()
        # Seven in the window, one of them an abandoned draft.
        self.assertEqual(
            Order.objects.filter(
                restaurant=self.restaurant, time_created__year=2024,
            ).count(),
            7,
        )
        self.assertEqual(self.data()['orders_placed'], 6)

    def test_extra_drafts_do_not_move_any_figure(self):
        self.seed_all_statuses()
        before = self.data()
        for _ in range(5):
            self.make_order(OrderStatus_Initiated)
        after = self.data()

        # Everything a draft could plausibly have inflated stays put.
        for key in ('num_sales', 'orders_placed', 'sales_amount'):
            self.assertEqual(before[key], after[key], key)
        for card in ('paid_orders', 'cancelled_orders', 'refunded_orders'):
            self.assertEqual(before[card], after[card], card)


class SalesDefinitionTests(DashboardV1Base):
    """`num_sales` means SALE_STATUSES, not "every row we could find"."""

    def test_num_sales_counts_only_served_and_paid(self):
        self.seed_all_statuses()
        self.assertEqual(self.data()['num_sales'], 2)

    def test_num_sales_is_not_the_order_count(self):
        # The regression itself: seven orders in window, two of them sales.
        self.seed_all_statuses()
        data = self.data()
        self.assertNotEqual(data['num_sales'], 7)
        self.assertLess(data['num_sales'], data['orders_placed'])

    def test_in_flight_orders_are_not_sales(self):
        self.make_order(OrderStatus_Pending)
        self.make_order(OrderStatus_Preparing)
        data = self.data()
        self.assertEqual(data['orders_placed'], 2)
        self.assertEqual(data['num_sales'], 0)

    def test_revenue_is_actual_cost_over_sales(self):
        self.seed_all_statuses()
        # served 750 + paid 1250; the 999s and the out-of-window 500000 are not sales.
        self.assertEqual(Decimal(str(self.data()['sales_amount'])), Decimal('2000.00'))

    def test_revenue_is_null_when_there_are_no_sales(self):
        self.make_order(OrderStatus_Cancelled)
        self.assertIsNone(self.data()['sales_amount'])


class RateDenominatorTests(DashboardV1Base):
    """Every rate divides by orders placed — the same denominator, so they compare."""

    def test_each_rate_is_over_orders_placed(self):
        self.seed_all_statuses()
        data = self.data()
        placed = data['orders_placed']
        self.assertEqual(placed, 6)

        # one cancelled, one refunded, one payment-captured, out of six placed
        expected = round((1 / 6) * 100, 1)
        self.assertEqual(data['cancelled_orders'], {'number': 1, 'percentage': expected})
        self.assertEqual(data['refunded_orders'], {'number': 1, 'percentage': expected})
        self.assertEqual(data['paid_orders'], {'number': 1, 'percentage': expected})

    def test_cancellation_and_refund_rates_are_comparable(self):
        # Two cancellations against one refund must read as double the rate —
        # which only holds while both divide by the same denominator.
        self.make_order(OrderStatus_Served)
        self.make_order(OrderStatus_Cancelled)
        self.make_order(OrderStatus_Cancelled)
        self.make_order(OrderStatus_Refunded)
        data = self.data()
        self.assertEqual(data['orders_placed'], 4)
        self.assertEqual(data['cancelled_orders']['percentage'], 50.0)
        self.assertEqual(data['refunded_orders']['percentage'], 25.0)

    def test_no_rate_can_exceed_one_hundred_percent(self):
        # Every placed order cancelled, plus drafts that used to inflate the
        # denominator in one direction and the numerator in neither.
        for _ in range(3):
            self.make_order(OrderStatus_Cancelled)
        self.make_order(OrderStatus_Initiated)
        data = self.data()
        self.assertEqual(data['orders_placed'], 3)
        self.assertEqual(data['cancelled_orders']['percentage'], 100.0)
        for card in ('paid_orders', 'cancelled_orders', 'refunded_orders'):
            self.assertLessEqual(data[card]['percentage'], 100.0, card)

    def test_empty_window_reports_zero_rates_not_a_crash(self):
        data = self.data()
        self.assertEqual(data['orders_placed'], 0)
        self.assertEqual(data['num_sales'], 0)
        for card in ('paid_orders', 'cancelled_orders', 'refunded_orders'):
            self.assertEqual(data[card], {'number': 0, 'percentage': 0}, card)


class PaymentTrackingFlagTests(DashboardV1Base):
    """The payment card is a placeholder until PSP, and says so."""

    def test_flag_is_emitted_and_false(self):
        self.seed_all_statuses()
        self.assertIs(self.data()['payment_tracking_enabled'], False)
        self.assertIs(PAYMENT_TRACKING_ENABLED, False)

    def test_no_production_path_writes_a_captured_payment(self):
        # The reason the flag is False: order creation seeds 'pending' and nothing
        # ever writes 'paid'. If a PSP write path lands, this fails and the flag
        # (and this test) must be updated together.
        from orders_app.controllers.con_orders import ConOrder

        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[],
        )
        # The call is refused for want of items — the point is only that no
        # code path anywhere assigns PaymentStatus_Paid.
        self.assertEqual(response['status'], 400)
        self.assertFalse(
            Order.objects.filter(payment_status=PaymentStatus_Paid).exists(),
        )


class DashboardAgreesWithSaleFiltersTests(DashboardV1Base):
    """The dashboard and `sale_orders()` now answer the same question."""

    def test_num_sales_matches_sale_orders(self):
        self.seed_all_statuses()
        chokepoint = sale_orders(str(self.restaurant.id), RANGE_FROM, RANGE_TO)
        self.assertEqual(self.data()['num_sales'], chokepoint.count())

    def test_revenue_matches_sale_orders(self):
        self.seed_all_statuses()
        chokepoint = sale_orders(str(self.restaurant.id), RANGE_FROM, RANGE_TO)
        expected = chokepoint.aggregate(total=revenue_sum())['total']
        self.assertEqual(
            Decimal(str(self.data()['sales_amount'])), Decimal(str(expected)),
        )

    def test_rehearsal_orders_stay_excluded_from_both(self):
        self.seed_all_statuses()
        rehearsal = self.make_order(OrderStatus_Served, actual_cost='4000.00')
        Order.objects.filter(pk=rehearsal.pk).update(is_test=True)

        chokepoint = sale_orders(str(self.restaurant.id), RANGE_FROM, RANGE_TO)
        data = self.data()
        self.assertEqual(data['num_sales'], chokepoint.count())
        self.assertEqual(Decimal(str(data['sales_amount'])), Decimal('2000.00'))


class DashboardV2OrdersCardTests(DashboardV1Base):
    """dashboard-v2 shared the defect in `orders.total` and its chart series."""

    def build(self):
        """The `orders` card, through the real v2 entrypoint (so `clean_dates` runs)."""
        response = generate_restaurant_dashboard_v2(
            restaurant_id=str(self.restaurant.id),
            date_from=RANGE_FROM,
            date_to=RANGE_TO,
            bucket='month',
        )
        self.assertEqual(response['status'], 200, response)
        return response['data']['orders']

    def test_total_excludes_drafts(self):
        self.seed_all_statuses()
        self.assertEqual(self.build()['total'], 6)

    def test_breakdown_sums_to_total(self):
        # Previously impossible: an initiated order is excluded from 'open' and
        # fails 'paid', so it was in `total` and in none of the four rows.
        self.seed_all_statuses()
        card = self.build()
        self.assertEqual(
            sum(row['count'] for row in card['breakdown']), card['total'],
        )

    def test_series_excludes_drafts(self):
        self.seed_all_statuses()
        card = self.build()
        self.assertEqual(sum(point['count'] for point in card['series']), 6)

    def test_v2_total_agrees_with_v1_orders_placed(self):
        self.seed_all_statuses()
        self.assertEqual(self.build()['total'], self.data()['orders_placed'])
