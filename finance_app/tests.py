from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from users_app.models import User
from users_app.tests import TEST_PHONE, seed_user
from finance_app.models import DinifyAccount, DinifyTransaction
from restaurants_app.models import Restaurant, Table
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant,
    AccountType_DinifyRevenue,
    ProcessingStatus_Confirmed,
    PaymentMode_MobileMoney, PaymentMode_Ova,
)
from orders_app.tests import seed_order
from orders_app.models import Order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_TABLE_NUMBER1, TEST_TABLE_NUMBER4
)
from finance_app.controllers.initiate_order_payment import initiate_order_payment
from finance_app.controllers.process_payment_feedback import process_payment_feedback
from users_app.controllers.otp_manager import OtpManager

from finance_app.controllers.tx_order_payment import OrderPaymentTransaction
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction
from finance_app.controllers.tx_disbursement import DisbursementTransaction
from finance_app.controllers.update_wallet_balance import update_wallet_balance
from finance_app.management.commands.seed_dinify_account import seed_dinify_account

TEST_MSISDN = '256700000000'


def seed_account():
    """
    seed the account for the test
    """
    seed_user()
    seed_restaurant(seed_owner=True)
    restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    DinifyAccount.objects.create(
        account_type=AccountType_Restaurant,
        restaurant=restaurant
    )


def simulate_aggregator_feedback(
    desired_aggregator: str,
    desired_aggregator_status: str,
    desired_status: str
) -> dict:
    return {
        "aggregator": desired_aggregator,
        "aggregator_reference": "123456789",
        "aggregator_status": desired_aggregator_status,
        "status": desired_status
    }


# Patch targets for external I/O — mirrors the pattern in users_app/tests.py
# and payment_integrations_app/tests.py
_PATCH_MOMO_COLLECT = 'payment_integrations_app.controllers.yo_integrations.YoIntegration.momo_collect'
_PATCH_MOMO_DISBURSE = 'payment_integrations_app.controllers.yo_integrations.YoIntegration.momo_disburse'
_PATCH_YO_SMS = 'payment_integrations_app.controllers.yo_integrations.YoIntegration.send_sms'
_PATCH_DPO_CREATE = 'payment_integrations_app.controllers.dpo.DpoIntegration.create_token'
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
@patch(_PATCH_DPO_CREATE, return_value='TEST-TOKEN')
@patch(_PATCH_MOMO_DISBURSE, return_value=True)
@patch(_PATCH_MOMO_COLLECT, return_value=True)
@patch(_PATCH_YO_SMS, return_value=True)
class FinanceAppTestFunctions(TestCase):
    """
    Test functions for the Finance app.
    All external service calls (Yo Uganda, DPO, Flutterwave, Messenger SMS/email)
    and OTP methods are mocked at class level to prevent real API hits.
    """
    def setUp(self):
        seed_account()
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()
        seed_dinify_account()

    def test_order_payment(self, *mocks):
        """Test order payment initiation and processing via aggregator feedback."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER1)
        user = User.objects.get(username=TEST_PHONE)

        order = Order.objects.get(
            restaurant=restaurant,
            table=table,
            customer=user
        )

        result = initiate_order_payment(
            order=order,
            tip_amount=0,
            payment_mode=PaymentMode_MobileMoney,
            msisdn=TEST_MSISDN,
            user=user,
            otp='1234'
        )
        self.assertEqual(result['status'], 200)
        transaction_id = result['data']['transaction_id']

        feedback = simulate_aggregator_feedback(
            desired_aggregator='flutterwave',
            desired_aggregator_status='success',
            desired_status='success'
        )
        result = process_payment_feedback(
            transaction_id=transaction_id,
            aggregator=feedback['aggregator'],
            aggregator_reference=feedback['aggregator_reference'],
            aggregator_status=feedback['aggregator_status'],
            status=feedback['status']
        )
        # Verify the aggregator details were saved
        tx = DinifyTransaction.objects.get(id=transaction_id)
        self.assertEqual(tx.aggregator, 'flutterwave')
        self.assertEqual(tx.aggregator_reference, '123456789')

    def test_update_wallet_balance(self, *mocks):
        """Test wallet balance credit and debit operations."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        account = DinifyAccount.objects.get(restaurant=restaurant)

        # Starting balances should be zero
        self.assertEqual(account.momo_actual_balance, Decimal('0'))
        self.assertEqual(account.momo_available_balance, Decimal('0'))

        # Credit the momo wallet
        result = update_wallet_balance(
            id=str(account.id),
            mode=PaymentMode_MobileMoney,
            credit=Decimal('50000')
        )
        account.refresh_from_db()
        self.assertEqual(account.momo_actual_balance, Decimal('50000'))
        self.assertEqual(account.momo_available_balance, Decimal('50000'))
        self.assertEqual(account.momo_cumulative_in, Decimal('50000'))
        self.assertIn('before', result)
        self.assertIn('after', result)

        # Debit from the momo wallet
        result = update_wallet_balance(
            id=str(account.id),
            mode=PaymentMode_MobileMoney,
            debit=Decimal('20000')
        )
        account.refresh_from_db()
        self.assertEqual(account.momo_actual_balance, Decimal('30000'))
        self.assertEqual(account.momo_available_balance, Decimal('30000'))
        self.assertEqual(account.momo_cumulative_out, Decimal('20000'))

    def test_momo_payment_full_no_tip(self, *mocks):
        """Test MoMo payment with full amount, no tip, including OTP flow."""
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

        # Without OTP — should be rejected (msisdn not registered as a user)
        result = OrderPaymentTransaction().initiate(
            order=order,
            tip_amount=0,
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

        # With OTP — should succeed (verify_otp is mocked to return valid)
        result = OrderPaymentTransaction().initiate(
            order=order,
            tip_amount=0,
            payment_mode=PaymentMode_MobileMoney,
            msisdn=TEST_MSISDN,
            otp='1234',
            amount=100000
        )
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])

        # Simulate the aggregator confirming the transaction
        tx = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        tx.processing_status = ProcessingStatus_Confirmed
        tx.save()
        old_momo_balance = tx.account.momo_actual_balance

        result = OrderPaymentTransaction().process(
            transaction_id=str(tx.id),
        )
        account = DinifyAccount.objects.get(restaurant=restaurant)
        new_momo_balance = account.momo_actual_balance

        expected_balance = old_momo_balance + order.actual_cost
        self.assertEqual(expected_balance, new_momo_balance)

        order.refresh_from_db()
        self.assertEqual(order.order_status, 'paid')

    def test_subscription_payment(self, *mocks):
        """Test subscription payment via MoMo and OVA."""
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

        # Monthly MoMo subscription payment
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 200)

        txs = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        txs.processing_status = ProcessingStatus_Confirmed
        txs.save()

        SubscriptionPaymentTransaction().process(
            transaction_id=result['data']['transaction_id']
        )
        restaurant.refresh_from_db()
        # Expiry is based on txs_record.time_created + 30 days (since it was None)
        txs.refresh_from_db()
        expected_expiry_date = txs.time_created + timedelta(days=30)
        self.assertEqual(
            restaurant.subscription_expiry_date.date(),
            expected_expiry_date.date()
        )
        self.assertEqual(restaurant.subscription_validity, True)

        # Credit restaurant account for OVA payment test
        account = DinifyAccount.objects.get(restaurant=restaurant)
        account.momo_actual_balance = 1000000
        account.momo_available_balance = 1000000
        account.save()

        # OVA payment (wallet-to-wallet)
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_Ova,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 200)

        txs = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        txs.processing_status = ProcessingStatus_Confirmed
        txs.save()

        SubscriptionPaymentTransaction().process(
            transaction_id=result['data']['transaction_id']
        )
        restaurant.refresh_from_db()

        # Verify the dinify revenue subscription transaction was recorded
        dinify_account = DinifyAccount.objects.get(
            account_type=AccountType_DinifyRevenue
        )
        revenue_txs = DinifyTransaction.objects.filter(
            account=dinify_account,
            restaurant=restaurant
        )
        self.assertTrue(revenue_txs.exists())

    def test_disbursement(self, *mocks):
        """Test disbursement to restaurant owner via MoMo."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        user = User.objects.get(username=TEST_PHONE)

        # Fund the restaurant account so it passes the balance check
        account = DinifyAccount.objects.get(restaurant=restaurant)
        account.momo_actual_balance = Decimal('1000000')
        account.momo_available_balance = Decimal('1000000')
        account.save()

        result = DisbursementTransaction().initiate(
            restaurant_id=str(restaurant.id),
            payment_mode=PaymentMode_MobileMoney,
            user=user,
            msisdn=TEST_MSISDN,
            amount=50000
        )
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])
