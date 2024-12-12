from datetime import datetime, timedelta
from django.test import TestCase
from users_app.models import User
from users_app.tests import TEST_PHONE, seed_user
from finance_app.models import DinifyAccount, DinifyTransaction
from restaurants_app.models import Restaurant, Table
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant,
    ProcessingStatus_Confirmed
)
from orders_app.tests import seed_order
from orders_app.models import Order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_TABLE_NUMBER1, TEST_TABLE_NUMBER4
)
from finance_app.controllers.initiate_order_payment import initiate_order_payment
from finance_app.controllers.initiate_refund import initiate_refund
from finance_app.controllers.process_payment_feedback import process_payment_feedback
from dinify_backend.configss.string_definitions import PaymentMode_MobileMoney
from dinify_backend.configss.messages import OK_ORDER_PAYMENT_PROCESSED
from users_app.controllers.otp_manager import OtpManager

from finance_app.controllers.tx_order_payment import OrderPaymentTransaction
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction
from finance_app.controllers.tx_disbursement import DisbursementTransaction
from finance_app.management.commands.seed_dinify_account import seed_dinify_account

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


# Create your tests here.
class FinanceAppTestFunctions(TestCase):
    """
    the test functions for the Misc app
    """
    def setUp(self):
        """
        setup the test
        """
        seed_account()
        # seed_user()
        # seed_restaurant(seed_owner=False)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()
        seed_dinify_account()

    def otest_order_payment(self):
        self.transaction_id = None

        def test_initiate():
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
                msisdn='256706087495'
            )
            # self.assertEqual(result['status'], 200)
            # self.assertEqual(result['message'], OK_ORDER_PAYMENT_INITIATED)
            self.transaction_id = result['data']['transaction_id']

            # test a refund
            # result = initiate_refund(
            #     order=order,
            #     amount=order.actual_cost,
            #     user=user,
            #     payment_mode=PaymentMode_MobileMoney
            # )
            # self.assertEqual(result['status'], 200)

        def test_process_payment_feedback():
            feedback = simulate_aggregator_feedback(
                desired_aggregator='flutterwave',
                desired_aggregator_status='success',
                desired_status='success'
            )
            result = process_payment_feedback(
                transaction_id=self.transaction_id,
                aggregator=feedback['aggregator'],
                aggregator_reference=feedback['aggregator_reference'],
                aggregator_status=feedback['aggregator_status'],
                status=feedback['status']
            )
            self.assertEqual(result, True)

        test_initiate()
        test_process_payment_feedback()

    def test_update_wallet_balance(self):
        pass

    def test_momo_payment_full_no_tip(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER4)
        user = User.objects.get(username=TEST_PHONE)

        # order for table 4
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

        # test when OTP is provided
        result = OrderPaymentTransaction().initiate(
            order=order,
            tip_amount=0,
            payment_mode=PaymentMode_MobileMoney,
            msisdn='256706087495'
        )
        self.assertEqual(result['status'], 400)

        # ask for the OTP
        OtpManager().resend_otp(
            identification='msisdn',
            identifier='256706087495'
        )
        result = OrderPaymentTransaction().initiate(
            order=order,
            tip_amount=0,
            payment_mode=PaymentMode_MobileMoney,
            msisdn='256706087495',
            otp='1234'
        )
        # print(f'\n\n{result}\n\n')
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])

        # simulate processing the transaction
        tx = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        tx.processing_status = ProcessingStatus_Confirmed
        tx.save()
        print(f"account balances: {tx.account.momo_actual_balance} | {tx.account.card_actual_balance} | {tx.account.cash_actual_balance}")
        old_momo_balance = tx.account.momo_actual_balance

        result = OrderPaymentTransaction().process(
            transaction_id=result['data']['transaction_id'],
        )
        account = DinifyAccount.objects.get(restaurant=restaurant)
        print(f"account balances: {account.momo_actual_balance} | {account.card_actual_balance} | {account.cash_actual_balance}")
        new_momo_balance = account.momo_actual_balance

        expected_balance = old_momo_balance + order.actual_cost
        self.assertEqual(expected_balance, new_momo_balance)

        order.refresh_from_db()
        self.assertEqual(order.order_status, 'Paid')

    def test_subscription_payment(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        restaurant.subscription_validity = False
        restaurant.save()

        # when the restaurant is charged per order
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn='256706087495'
        )
        print(result)

        restaurant.preferred_subscription_method = 'monthly'
        restaurant.flat_fee = 50000
        restaurant.save()

        # when the restaurant is charged monthly
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn='256706087495'
        )
        print(result)
        self.assertEqual(result['status'], 200)

        txs = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        txs.processing_status = ProcessingStatus_Confirmed
        txs.save()

        result = SubscriptionPaymentTransaction().process(
            transaction_id=result['data']['transaction_id']
        )
        restaurant.refresh_from_db()
        expected_expiry_date = datetime.now() + timedelta(days=30)
        print(f"expiry date: {restaurant.subscription_expiry_date}")
        self.assertEqual(restaurant.subscription_expiry_date.date(), expected_expiry_date.date())
        self.assertEqual(restaurant.subscription_validity, True)

    def otest_disbursement(self):
        seed_dinify_account()
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        user = User.objects.get(username=TEST_PHONE)

        result = DisbursementTransaction().initiate(
            restaurant_id=restaurant.id,
            payment_mode=PaymentMode_MobileMoney,
            user=user,
            msisdn='256706087495',
            amount=50000
        )
        print(result)
        # self.assertEqual(result['status'], 200)
