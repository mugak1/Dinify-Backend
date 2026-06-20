"""
Tests for the rebuilt Transactions reports (PR5): summary / listing.

These lock in the corrected semantics over the legacy bugs:
  * the summary runs in TWO grouped queries and 0-fills onto the complete known
    sets (statuses success/failed/pending/initiated; types order_payment/
    subscription) — the per-status / per-type loop is gone,
  * the listing emits RAW enums (no Title-casing, no 'momo'->'MoMo'), a single
    ``amount`` (not amount_in / amount_out), the real ``payment_mode``, and
    serialises in one query via ``select_related`` (no per-row order N+1).

``time_created`` is ``auto_now_add`` on both DinifyTransaction and Order, so it
is set via ``.update()`` after create. UTC instants are used (EAT is UTC+3, so
09:00 UTC is the same calendar day in EAT).
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import Restaurant, Table
from orders_app.models import Order
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active,
    TransactionType_OrderPayment, TransactionType_Subscription,
    TransactionStatus_Success, TransactionStatus_Failed,
    TransactionStatus_Pending, TransactionStatus_Initiated,
    TransactionPlatform_Web,
    PaymentMode_MobileMoney,
)
from reports_app.controllers.restaurant.transactions import (
    generate_restaurant_transaction_summary,
    generate_restaurant_transaction_listing,
)


def make_user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


def utc(year, month, day, hour=9, minute=0):
    """A timezone-aware UTC instant (time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


class TransactionsReportBase(TestCase):
    """Shared fixtures + Order / DinifyTransaction seeding helpers."""

    def setUp(self):
        self.owner = make_user('256700000500')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # A second tenant, to assert restaurant scoping.
        self.other_owner = make_user('256700000501')
        self.restaurant_b = Restaurant.objects.create(
            name='Other Restaurant', location='loc-b',
            status=RestaurantStatus_Active, owner=self.other_owner,
        )

    def make_order(self, when=None, order_number=None):
        # The transactions report only reads order_number; the money columns are
        # required by the model, so they are set to throwaway values.
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=Decimal('1000.00'), discounted_cost=Decimal('1000.00'),
            savings=Decimal('0.00'), actual_cost=Decimal('1000.00'),
        )
        updates = {}
        if when is not None:
            updates['time_created'] = when
        if order_number is not None:
            updates['order_number'] = order_number
        if updates:
            Order.objects.filter(id=order.id).update(**updates)
        return order

    def make_txn(self, restaurant=None, order=None,
                 txn_type=TransactionType_OrderPayment,
                 status=TransactionStatus_Success,
                 platform=TransactionPlatform_Web,
                 amount='1000.00',
                 payment_mode=PaymentMode_MobileMoney,
                 when=None):
        txn = DinifyTransaction.objects.create(
            restaurant=restaurant or self.restaurant, order=order,
            transaction_type=txn_type, transaction_status=status,
            transaction_platform=platform,
            transaction_amount=Decimal(amount), payment_mode=payment_mode,
        )
        if when is not None:
            DinifyTransaction.objects.filter(id=txn.id).update(time_created=when)
        return txn

    def seed_summary_mix(self, when=None):
        """A mix spanning both axes: success spans two types, order_payment spans
        two statuses — so by_status / by_type can't be a single-axis artefact."""
        when = when or utc(2024, 1, 10)
        self.make_txn(txn_type=TransactionType_OrderPayment,
                      status=TransactionStatus_Success, amount='1000.00', when=when)
        self.make_txn(txn_type=TransactionType_OrderPayment,
                      status=TransactionStatus_Success, amount='1000.00', when=when)
        self.make_txn(txn_type=TransactionType_Subscription,
                      status=TransactionStatus_Success, amount='300.00',
                      payment_mode=None, when=when)
        self.make_txn(txn_type=TransactionType_OrderPayment,
                      status=TransactionStatus_Failed, amount='500.00', when=when)
        self.make_txn(txn_type=TransactionType_Subscription,
                      status=TransactionStatus_Pending, amount='200.00',
                      payment_mode=None, when=when)


class TransactionsSummaryTests(TransactionsReportBase):

    def test_by_status_full_set_with_counts_amounts_and_zero_fill(self):
        self.seed_summary_mix()

        data = generate_restaurant_transaction_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        # Complete, ordered status domain — 'initiated' is 0-filled (no rows).
        self.assertEqual([b['status'] for b in data['by_status']],
                         [TransactionStatus_Success, TransactionStatus_Failed,
                          TransactionStatus_Pending, TransactionStatus_Initiated])
        by_status = {b['status']: b for b in data['by_status']}
        # success spans order_payment (2x1000) + subscription (1x300).
        self.assertEqual(by_status[TransactionStatus_Success]['count'], 3)
        self.assertEqual(by_status[TransactionStatus_Success]['amount'], Decimal('2300.00'))
        self.assertEqual(by_status[TransactionStatus_Failed]['count'], 1)
        self.assertEqual(by_status[TransactionStatus_Failed]['amount'], Decimal('500.00'))
        self.assertEqual(by_status[TransactionStatus_Pending]['count'], 1)
        self.assertEqual(by_status[TransactionStatus_Pending]['amount'], Decimal('200.00'))
        self.assertEqual(by_status[TransactionStatus_Initiated]['count'], 0)
        self.assertEqual(by_status[TransactionStatus_Initiated]['amount'], 0)

        # total_transactions == sum of the status counts.
        self.assertEqual(data['total_transactions'], 5)

    def test_by_type_full_set_with_counts_and_amounts(self):
        self.seed_summary_mix()

        data = generate_restaurant_transaction_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual([b['type'] for b in data['by_type']],
                         [TransactionType_OrderPayment, TransactionType_Subscription])
        by_type = {b['type']: b for b in data['by_type']}
        # order_payment spans success (2x1000) + failed (1x500).
        self.assertEqual(by_type[TransactionType_OrderPayment]['count'], 3)
        self.assertEqual(by_type[TransactionType_OrderPayment]['amount'], Decimal('2500.00'))
        # subscription spans success (300) + pending (200).
        self.assertEqual(by_type[TransactionType_Subscription]['count'], 2)
        self.assertEqual(by_type[TransactionType_Subscription]['amount'], Decimal('500.00'))

    def test_empty_range_is_zero_filled_not_empty(self):
        data = generate_restaurant_transaction_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['total_transactions'], 0)
        self.assertEqual([b['status'] for b in data['by_status']],
                         [TransactionStatus_Success, TransactionStatus_Failed,
                          TransactionStatus_Pending, TransactionStatus_Initiated])
        self.assertEqual([b['type'] for b in data['by_type']],
                         [TransactionType_OrderPayment, TransactionType_Subscription])
        for bucket in data['by_status'] + data['by_type']:
            self.assertEqual(bucket['count'], 0)
            self.assertEqual(bucket['amount'], 0)

    def test_summary_runs_in_two_grouped_queries(self):
        self.seed_summary_mix()

        # TWO grouped queries (group-by status, group-by type) and nothing else —
        # proving the per-status / per-type loop is gone.
        with self.assertNumQueries(2):
            result = generate_restaurant_transaction_summary(
                restaurant_id=self.restaurant.id,
                date_from='2024-01-10', date_to='2024-01-10',
            )
            self.assertEqual(result['data']['total_transactions'], 5)

    def test_summary_is_restaurant_scoped(self):
        self.make_txn(restaurant=self.restaurant, when=utc(2024, 1, 10))
        self.make_txn(restaurant=self.restaurant_b, when=utc(2024, 1, 10))

        data = generate_restaurant_transaction_summary(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-10', date_to='2024-01-10',
        )['data']

        self.assertEqual(data['total_transactions'], 1)


class TransactionsListingTests(TransactionsReportBase):

    def test_enums_are_raw_never_title_cased(self):
        self.make_txn(
            txn_type=TransactionType_OrderPayment,
            status=TransactionStatus_Success,
            platform=TransactionPlatform_Web,
            payment_mode=PaymentMode_MobileMoney,
            when=utc(2024, 2, 1),
        )

        data = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        row = data[0]
        self.assertEqual(row['transaction_type'], 'order_payment')   # not 'Order Payment'
        self.assertEqual(row['transaction_status'], 'success')       # not 'Success'
        self.assertEqual(row['transaction_platform'], 'web')         # not 'Web'
        self.assertEqual(row['payment_mode'], 'momo')                # raw, not 'MoMo'
        self.assertNotIn('MoMo', row.values())

    def test_amount_is_a_single_field_no_in_out(self):
        self.make_txn(amount='1234.00', when=utc(2024, 2, 1))

        data = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        row = data[0]
        self.assertEqual(row['amount'], Decimal('1234.00'))
        self.assertNotIn('amount_in', row)
        self.assertNotIn('amount_out', row)

    def test_payment_mode_is_real_or_null(self):
        order = self.make_order(when=utc(2024, 2, 1, 8))
        self.make_txn(order=order, payment_mode=PaymentMode_MobileMoney,
                      when=utc(2024, 2, 1, 8))
        # A subscription with no channel must surface as null, not a fabricated mode.
        self.make_txn(order=None, txn_type=TransactionType_Subscription,
                      payment_mode=None, when=utc(2024, 2, 1, 9))

        data = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        # Ordered by time_created: order txn (08:00), subscription (09:00).
        self.assertEqual(data[0]['payment_mode'], PaymentMode_MobileMoney)
        self.assertIsNone(data[1]['payment_mode'])

    def test_order_number_null_for_subscription_string_for_order(self):
        order = self.make_order(when=utc(2024, 2, 1, 8), order_number=7)
        self.make_txn(order=order, when=utc(2024, 2, 1, 8))
        self.make_txn(order=None, txn_type=TransactionType_Subscription,
                      payment_mode=None, when=utc(2024, 2, 1, 9))

        data = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(data[0]['order_number'], '7')   # string, order-linked
        self.assertIsNone(data[1]['order_number'])       # subscription

    def test_listing_is_a_single_query_regardless_of_rows(self):
        # Several order-linked transactions. If the per-row order N+1 were back,
        # this would be many queries instead of one.
        for i in range(3):
            order = self.make_order(when=utc(2024, 2, 1, 8 + i))
            self.make_txn(order=order, when=utc(2024, 2, 1, 8 + i))

        with self.assertNumQueries(1):
            result = generate_restaurant_transaction_listing(
                restaurant_id=self.restaurant.id,
                date_from='2024-02-01', date_to='2024-02-01',
            )
            data = result['data']
            self.assertEqual(len(data), 3)

    def test_31_day_cap_with_subscription_exemption(self):
        # > 31 days with no type filter is rejected.
        result = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-03-01',
        )
        self.assertEqual(result['status'], 400)

        # ...but type=subscription is exempt from the cap.
        result = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-01-01', date_to='2024-03-01',
            transaction_type=TransactionType_Subscription,
        )
        self.assertEqual(result['status'], 200)

    def test_type_and_status_filters_narrow_the_rows(self):
        self.make_txn(txn_type=TransactionType_OrderPayment,
                      status=TransactionStatus_Success, when=utc(2024, 2, 1))
        self.make_txn(txn_type=TransactionType_Subscription,
                      status=TransactionStatus_Failed, payment_mode=None,
                      when=utc(2024, 2, 1))

        by_type = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
            transaction_type=TransactionType_Subscription,
        )['data']
        self.assertEqual(len(by_type), 1)
        self.assertEqual(by_type[0]['transaction_type'], 'subscription')

        by_status = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
            transaction_status=TransactionStatus_Failed,
        )['data']
        self.assertEqual(len(by_status), 1)
        self.assertEqual(by_status[0]['transaction_status'], 'failed')

    def test_listing_is_restaurant_scoped(self):
        self.make_txn(restaurant=self.restaurant, when=utc(2024, 2, 1))
        self.make_txn(restaurant=self.restaurant_b, when=utc(2024, 2, 1))

        data = generate_restaurant_transaction_listing(
            restaurant_id=self.restaurant.id,
            date_from='2024-02-01', date_to='2024-02-01',
        )['data']

        self.assertEqual(len(data), 1)
