from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from users_app.tests import seed_user
from finance_app.models import DinifyTransaction
from restaurants_app.models import Restaurant
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    PaymentMode_MobileMoney,
)
from orders_app.tests import seed_order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME,
)
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


class RetiredOrderPaymentRouteTests(TestCase):
    """The anonymous ``initiate-order-payment`` write path (OrderPaymentsEndpoint
    + OrderPaymentTransaction) was retired. The route must no longer resolve — a
    POST returns 404 (no route), not 500/200.
    """

    def test_initiate_order_payment_route_returns_404(self):
        response = self.client.post(
            '/api/v1/finances/initiate-order-payment/', {},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
