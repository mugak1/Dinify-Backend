"""
Tests for the rebuilt Sales reports (PR4): summary / listing / trends.

These lock in the corrected semantics over the legacy bugs:
  * revenue is ``Sum('actual_cost')`` and discount is ``Sum('savings')`` — NOT
    ``total_cost`` (gross) or ``discounted_cost`` (post-discount total),
  * the "sale" set is {served, paid} (a paid-not-served order counts; a
    cancelled / pending order does not),
  * the listing reads the real ``payment_mode`` (no hardcoded 'MoMo'), counts
    items without a per-row query, and serialises in a single query,
  * trends bucket in ONE grouped query with the documented period labels.

Distinct money values (total=1000, discounted=800, savings=200, actual=750) let
the assertions distinguish which column each figure comes from. ``time_created``
is ``auto_now_add``, so it is set via ``.update()`` after create; UTC instants
are used (EAT is UTC+3, so 09:00 UTC == 12:00 EAT, same calendar day).
"""
from datetime import datetime, date, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import Restaurant, Table, MenuSection, MenuItem
from orders_app.models import Order, OrderItem
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active,
    OrderStatus_Served, OrderStatus_Paid, OrderStatus_Pending,
    OrderStatus_Cancelled,
    TransactionType_OrderPayment,
    TransactionStatus_Success, TransactionStatus_Failed,
    TransactionPlatform_Web,
    PaymentMode_MobileMoney, PaymentMode_Cash,
)
from reports_app.controllers.restaurant.sales import (
    generate_restaurant_sales_summary,
    generate_restaurant_sales_listing,
    generate_restaurant_sales_trends,
    generate_restaurant_sales_hourly,
)


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


class SalesReportBase(TestCase):
    """Shared fixtures + Order / OrderItem / transaction seeding helpers."""

    def setUp(self):
        self.owner = make_user('256700000400')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # A second tenant, to assert restaurant scoping.
        self.other_owner = make_user('256700000401')
        self.restaurant_b = Restaurant.objects.create(
            name='Other Restaurant', location='loc-b',
            status=RestaurantStatus_Active, owner=self.other_owner,
        )
        self.table_b = Table.objects.create(number=1, restaurant=self.restaurant_b)

        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, listing_position=0,
        )
        self.item = MenuItem.objects.create(
            name='Burger', section=self.section,
            primary_price=Decimal('10.00'), listing_position=0,
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
            Order.objects.filter(id=order.id).update(time_created=when)
        return order

    def add_items(self, order, count):
        for _ in range(count):
            OrderItem.objects.create(
                order=order, item=self.item, quantity=1,
                unit_price=Decimal('10.00'), discounted_price=Decimal('10.00'),
                total_cost=Decimal('10.00'), discounted_cost=Decimal('10.00'),
                savings=Decimal('0.00'), actual_cost=Decimal('10.00'),
            )

    def add_txn(self, order, payment_mode=PaymentMode_MobileMoney,
                amount='750.00', status=TransactionStatus_Success,
                txn_type=TransactionType_OrderPayment):
        return DinifyTransaction.objects.create(
            order=order, restaurant=order.restaurant,
            transaction_type=txn_type,
            transaction_status=status,
            transaction_platform=TransactionPlatform_Web,
            transaction_amount=Decimal(amount),
            payment_mode=payment_mode,
        )


class SalesSummaryTests(SalesReportBase):

    def test_revenue_is_actual_cost_not_total_or_discounted(self):
        # Two sale orders (one served, one paid) on the same day.
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10))
        # Non-sale orders that must be excluded.
        self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 1, 10))
        self.make_order(status=OrderStatus_Pending, when=utc(2024, 1, 10))

        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['number_of_sales'], 2)
        # revenue == Sum(actual_cost) == 2 x 750 — NOT total_cost / discounted_cost.
        self.assertEqual(data['revenue'], Decimal('1500.00'))
        self.assertNotEqual(data['revenue'], Decimal('2000.00'))   # not 2 x total 1000
        self.assertNotEqual(data['revenue'], Decimal('1600.00'))   # not 2 x discounted 800
        # gross_sales is the pre-discount list total, correctly labelled.
        self.assertEqual(data['gross_sales'], Decimal('2000.00'))
        # total_discounts == Sum(savings) == 2 x 200 — NOT discounted_cost.
        self.assertEqual(data['total_discounts'], Decimal('400.00'))
        self.assertNotEqual(data['total_discounts'], Decimal('1600.00'))

    def test_avg_max_min_over_actual_cost(self):
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10),
                        actual='600.00')
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10),
                        actual='900.00')

        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['average_order_value'], Decimal('750.00'))
        self.assertEqual(data['max_order_value'], Decimal('900.00'))
        self.assertEqual(data['min_order_value'], Decimal('600.00'))

    def test_paid_not_served_order_is_counted(self):
        # A single paid (never served) order proves the {served, paid} set.
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10))

        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['number_of_sales'], 1)
        self.assertEqual(data['revenue'], Decimal('750.00'))

    def test_empty_range_returns_zero_not_null(self):
        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['number_of_sales'], 0)
        self.assertEqual(data['revenue'], 0)
        self.assertEqual(data['gross_sales'], 0)
        self.assertEqual(data['total_discounts'], 0)
        self.assertEqual(data['average_order_value'], 0)
        self.assertEqual(data['payment_channels'], [])

    def test_payment_channels_read_real_payment_mode(self):
        momo_order = self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10))
        cash_order = self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10))
        self.add_txn(momo_order, payment_mode=PaymentMode_MobileMoney, amount='750.00')
        self.add_txn(cash_order, payment_mode=PaymentMode_Cash, amount='750.00')

        # A successful txn on a CANCELLED (non-sale) order must be excluded.
        cancelled = self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 1, 10))
        self.add_txn(cancelled, payment_mode=PaymentMode_MobileMoney, amount='999.00')

        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # Ordered by payment_mode: 'cash' then 'momo'.
        self.assertEqual(data['payment_channels'], [
            {'channel': PaymentMode_Cash, 'count': 1, 'amount': Decimal('750.00')},
            {'channel': PaymentMode_MobileMoney, 'count': 1, 'amount': Decimal('750.00')},
        ])

    def test_restaurant_scoped(self):
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10))
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10),
                        restaurant=self.restaurant_b, table=self.table_b)

        data = generate_restaurant_sales_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['number_of_sales'], 1)
        self.assertEqual(data['revenue'], Decimal('750.00'))


class SalesListingTests(SalesReportBase):

    def test_rows_match_the_sale_set(self):
        self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 2, 1))
        self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 2, 1))
        self.make_order(status=OrderStatus_Pending, when=utc(2024, 2, 1))

        data = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(len(data), 2)  # only served + paid

    def test_item_count_and_money_not_fanned_out(self):
        order = self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1))
        self.add_items(order, 3)

        data = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        row = data[0]
        self.assertEqual(row['item_count'], 3)
        # The row's own money columns, NOT multiplied by the item count.
        self.assertEqual(row['gross'], Decimal('1000.00'))
        self.assertEqual(row['revenue'], Decimal('750.00'))
        self.assertEqual(row['discount'], Decimal('200.00'))

    def test_item_count_zero_when_no_items(self):
        self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1))

        data = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(data[0]['item_count'], 0)

    def test_payment_mode_is_real_or_null_never_hardcoded(self):
        with_txn = self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1, 8))
        without_txn = self.make_order(status=OrderStatus_Paid, when=utc(2024, 2, 1, 9))
        failed_only = self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1, 10))
        self.add_txn(with_txn, payment_mode=PaymentMode_MobileMoney)
        # A failed txn must NOT surface as the payment_mode.
        self.add_txn(failed_only, payment_mode=PaymentMode_Cash,
                     status=TransactionStatus_Failed)

        data = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        # Ordered by time_created: with_txn, without_txn, failed_only.
        self.assertEqual(data[0]['payment_mode'], PaymentMode_MobileMoney)
        self.assertIsNone(data[1]['payment_mode'])
        self.assertIsNone(data[2]['payment_mode'])
        self.assertNotIn('MoMo', [r['payment_mode'] for r in data])

    def test_order_number_is_string_and_status_is_raw(self):
        order = self.make_order(status=OrderStatus_Paid, when=utc(2024, 2, 1))
        Order.objects.filter(id=order.id).update(order_number=7)

        data = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(data[0]['order_number'], '7')
        self.assertEqual(data[0]['payment_status'], 'pending')  # raw, not 'Pending'

    def test_listing_is_a_single_query_regardless_of_rows(self):
        # Several orders, each with several items and a transaction. If the
        # per-row N+1 were back, this would be many queries.
        for i in range(3):
            order = self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 1, 8 + i))
            self.add_items(order, 2)
            self.add_txn(order, payment_mode=PaymentMode_MobileMoney)

        with self.assertNumQueries(1):
            result = generate_restaurant_sales_listing(
                restaurant_id=self.restaurant.id,
                date_from='2024-02-01', date_to='2024-02-01',
            )
            data = result['data']
            self.assertEqual(len(data), 3)

        for row in data:
            self.assertEqual(row['item_count'], 2)

    def test_31_day_cap(self):
        result = generate_restaurant_sales_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-03-01',
        )
        self.assertEqual(result['status'], 400)


class SalesTrendsTests(SalesReportBase):

    def test_daily_table_per_period_revenue_and_count(self):
        for _ in range(2):
            self.make_order(status=OrderStatus_Served, when=utc(2024, 3, 1))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 3, 2))
        for _ in range(3):
            self.make_order(status=OrderStatus_Served, when=utc(2024, 3, 3))

        table = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-03-01', date_to='2024-03-03',
            trend_category='daily', trend_result='table',
        )['data']

        self.assertEqual([r['period'] for r in table],
                         ['2024-03-01', '2024-03-02', '2024-03-03'])
        self.assertEqual([r['count'] for r in table], [2, 1, 3])
        self.assertEqual([r['revenue'] for r in table],
                         [Decimal('1500.00'), Decimal('750.00'), Decimal('2250.00')])
        self.assertEqual([r['discount'] for r in table],
                         [Decimal('400.00'), Decimal('200.00'), Decimal('600.00')])

    def test_trends_is_a_single_grouped_query(self):
        for _ in range(2):
            self.make_order(status=OrderStatus_Served, when=utc(2024, 3, 1))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 3, 2))

        with self.assertNumQueries(1):
            result = generate_restaurant_sales_trends(
                restaurant_id=self.restaurant.id,
                date_from='2024-03-01', date_to='2024-03-02',
                trend_category='daily', trend_result='table',
            )
            self.assertEqual(len(result['data']), 2)

    def test_period_labels_month_quarter_year(self):
        # Monthly.
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 15))
        for _ in range(2):
            self.make_order(status=OrderStatus_Served, when=utc(2024, 2, 15))
        months = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-02-29',
            trend_category='monthly', trend_result='table',
        )['data']
        self.assertEqual([r['period'] for r in months], ['Jan-24', 'Feb-24'])
        self.assertEqual([r['count'] for r in months], [1, 2])

        # Quarterly — Jan (Q1) and Apr (Q2) 2024.
        self.make_order(status=OrderStatus_Served, when=utc(2024, 4, 15))
        quarters = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-04-30',
            trend_category='quarterly', trend_result='table',
        )['data']
        self.assertEqual([r['period'] for r in quarters], ['Q1-2024', 'Q2-2024'])

        # Annual — 2024 and 2025.
        self.make_order(status=OrderStatus_Served, when=utc(2025, 6, 15))
        years = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2025-12-31',
            trend_category='annual', trend_result='table',
        )['data']
        self.assertEqual([r['period'] for r in years], ['2024', '2025'])

    def test_graph_result_is_a_series_object(self):
        for _ in range(2):
            self.make_order(status=OrderStatus_Served, when=utc(2024, 3, 1))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 3, 2))

        data = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-03-01', date_to='2024-03-02',
            trend_category='daily', trend_result='graph',
        )['data']

        self.assertEqual(data['xaxis']['categories'], ['2024-03-01', '2024-03-02'])
        self.assertEqual(data['xaxis']['title']['text'], 'Days')
        series_by_name = {s['name']: s['data'] for s in data['series']}
        self.assertIn('Revenue', series_by_name)
        self.assertEqual(series_by_name['Count'], [2, 1])

    def test_invalid_category_returns_400(self):
        result = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-03-01', date_to='2024-03-02',
            trend_category='fortnightly', trend_result='table',
        )
        self.assertEqual(result['status'], 400)

    def test_daily_31_day_cap(self):
        result = generate_restaurant_sales_trends(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-03-01',
            trend_category='daily', trend_result='table',
        )
        self.assertEqual(result['status'], 400)


class SalesHourlyTests(SalesReportBase):
    """The hour-of-day ("when orders land") sale distribution.

    EAT is UTC+3, so 09:00 UTC == 12:00 EAT (hour 12), 16:00 UTC == 19:00 EAT
    (hour 19), and 23:30 UTC == 02:30 EAT the next day (hour 2). The hour must
    be extracted in EAT, never UTC.
    """

    def test_hour_is_bucketed_in_eat_not_utc(self):
        # 23:30 UTC is 02:30 EAT the next day -> hour 2, NOT hour 23.
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10, 23, 30))

        # Window spans both calendar days so the order is included regardless
        # of the date-edge timezone.
        data = generate_restaurant_sales_hourly(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-11',
        )['data']

        self.assertEqual(len(data), 24)
        self.assertEqual(data[2]['hour'], 2)
        self.assertEqual(data[2]['count'], 1)
        self.assertEqual(data[23]['count'], 0)   # NOT bucketed in UTC

    def test_zero_filled_to_continuous_24_hour_axis(self):
        # Seed only EAT hours 12 and 19.
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10, 9, 0))
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10, 16, 0))

        data = generate_restaurant_sales_hourly(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # A stable 0..23 axis, in order.
        self.assertEqual(len(data), 24)
        self.assertEqual([row['hour'] for row in data], list(range(24)))

        self.assertEqual(data[12]['count'], 1)
        self.assertEqual(data[19]['count'], 1)
        # Every untouched hour is present and zeroed (not absent).
        for hour, row in enumerate(data):
            if hour not in (12, 19):
                self.assertEqual(row['count'], 0)
                self.assertEqual(row['revenue'], 0)

    def test_only_sale_status_orders_are_counted(self):
        # Both sale statuses ({served, paid}) count; cancelled / pending do not.
        self.make_order(status=OrderStatus_Served, when=utc(2024, 1, 10, 9, 0))
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10, 9, 0))
        self.make_order(status=OrderStatus_Cancelled, when=utc(2024, 1, 10, 9, 0))
        self.make_order(status=OrderStatus_Pending, when=utc(2024, 1, 10, 9, 0))

        data = generate_restaurant_sales_hourly(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data[12]['count'], 2)   # served + paid only

    def test_revenue_is_actual_cost_not_total_or_discounted(self):
        # Defaults: total=1000, discounted=800, savings=200, actual=750.
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10, 9, 0))

        data = generate_restaurant_sales_hourly(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # revenue == Sum(actual_cost), NOT total_cost / discounted_cost.
        self.assertEqual(data[12]['revenue'], Decimal('750.00'))
        self.assertNotEqual(data[12]['revenue'], Decimal('1000.00'))
        self.assertNotEqual(data[12]['revenue'], Decimal('800.00'))
        # discount == Sum(savings).
        self.assertEqual(data[12]['discount'], Decimal('200.00'))

    def test_runs_as_a_single_grouped_query(self):
        self.make_order(status=OrderStatus_Paid, when=utc(2024, 1, 10, 9, 0))

        with self.assertNumQueries(1):
            generate_restaurant_sales_hourly(
                restaurant_id=self.restaurant.id,
                date_from='2024-01-10', date_to='2024-01-10',
            )
