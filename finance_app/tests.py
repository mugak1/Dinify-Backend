from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from users_app.models import User
from users_app.tests import TEST_PHONE, seed_user
from finance_app.models import DinifyTransaction
from restaurants_app.models import Restaurant, Table
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    PaymentMode_MobileMoney,
    PaymentMode_Card,
)
from orders_app.tests import seed_order
from orders_app.models import Order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_TABLE_NUMBER4
)
from users_app.controllers.otp_manager import OtpManager

from finance_app.controllers.tx_order_payment import OrderPaymentTransaction
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction

TEST_MSISDN = '256700000000'


# Patch targets for external I/O — mirrors the pattern in users_app/tests.py
# and payment_integrations_app/tests.py. The payment aggregators (Yo momo / DPO)
# were retired in 8a; only the SMS gateway and OTP mocks remain.
_PATCH_YO_SMS = 'payment_integrations_app.controllers.yo_integrations.YoIntegration.send_sms'
_PATCH_MESSENGER_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_MESSENGER_SMS = 'notifications_app.controllers.messenger.Messenger.send_sms'
# OTP mocks — resend_otp is mocked to avoid the user=None crash where
# resend_otp tries to access user.phone_number when identification='msisdn'
_PATCH_OTP_RESEND = 'users_app.controllers.otp_manager.OtpManager.resend_otp'
_PATCH_OTP_MAKE = 'users_app.controllers.otp_manager.OtpManager.make_otp'
_PATCH_OTP_VERIFY = 'users_app.controllers.otp_manager.OtpManager.verify_otp'


@patch(_PATCH_OTP_VERIFY, return_value={
    'status': 200, 'message': 'Valid OTP', 'data': {'valid': True}
})
@patch(_PATCH_OTP_RESEND, return_value={
    'status': 200, 'message': 'OTP sent successfully'
})
@patch(_PATCH_OTP_MAKE, return_value=True)
@patch(_PATCH_MESSENGER_SMS, return_value=True)
@patch(_PATCH_MESSENGER_EMAIL, return_value=True)
@patch(_PATCH_YO_SMS, return_value=True)
class FinanceAppTestFunctions(TestCase):
    """
    Test functions for the Finance app.
    The payment-initiation flows are aggregator-free stubs (8a): initiate
    records a pending DinifyTransaction and returns a pending response with no
    aggregator call. SMS/OTP and Messenger calls are mocked at class level.
    """
    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

    def test_momo_payment_full_no_tip(self, *mocks):
        """MoMo initiate now stubs to a pending transaction (no aggregator call)."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER4)
        user = User.objects.get(username=TEST_PHONE)

        order = Order.objects.create(
            restaurant=restaurant,
            table=table,
            customer=user,
            total_cost=100000,
            discounted_cost=100000,
            savings=0,
            actual_cost=100000,
            prepayment_required=True,
            order_status='served'
        )

        # Without OTP/amount — should be rejected
        result = OrderPaymentTransaction().initiate(
            order=order,
            payment_mode=PaymentMode_MobileMoney,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 400)

        # Request OTP — mocked to avoid the user=None crash in resend_otp
        # (the bug: resend_otp with identification='msisdn' leaves user=None,
        #  then tries to access user.phone_number on line 183)
        OtpManager().resend_otp(
            identification='msisdn',
            identifier=TEST_MSISDN
        )

        # With OTP — initiate returns a plain pending response and records the
        # transaction; the aggregator collection call was retired in 8a.
        result = OrderPaymentTransaction().initiate(
            order=order,
            payment_mode=PaymentMode_MobileMoney,
            msisdn=TEST_MSISDN,
            otp='1234',
            amount=100000
        )
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])

        tx = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        self.assertEqual(tx.processing_status, ProcessingStatus_Pending)

        # Card path now returns the same plain pending response — no DPO
        # redirect/token (deliberate contract change; frontend follow-up).
        card_result = OrderPaymentTransaction().initiate(
            order=order,
            payment_mode=PaymentMode_Card,
            amount=100000
        )
        self.assertEqual(card_result['status'], 200)
        self.assertNotIn('redirect_url', card_result['data'])
        self.assertNotIn('dpo_token', card_result['data'])

    def test_subscription_payment(self, *mocks):
        """Subscription initiate now stubs to a pending transaction (no aggregator call)."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        restaurant.subscription_validity = False
        restaurant.save()

        # Per-order subscription — should reject direct payment
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 400)

        # Switch to monthly subscription with flat fee
        restaurant.preferred_subscription_method = 'monthly'
        restaurant.flat_fee = Decimal('50000')
        restaurant.save()

        # Monthly MoMo subscription payment — pending stub
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])

        txs = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        self.assertEqual(txs.processing_status, ProcessingStatus_Pending)

        # The transaction is recorded against the restaurant (record-only
        # DinifyTransaction; the dinify_revenue account was removed in 8a).
        revenue_txs = DinifyTransaction.objects.filter(
            restaurant=restaurant
        )
        self.assertTrue(revenue_txs.exists())
