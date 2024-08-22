from django.test import TestCase
from users_app.models import User
from users_app.tests import TEST_PHONE, seed_user
from finance_app.models import DinifyAccount
from restaurants_app.models import Restaurant, Table
from dinify_backend.configss.string_definitions import AccountType_Restaurant
from orders_app.tests import seed_order
from orders_app.models import Order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_TABLE_NUMBER1
)
from finance_app.controllers.initiate_order_payment import initiate_order_payment
from finance_app.controllers.initiate_refund import initiate_refund
from finance_app.controllers.process_payment_feedback import process_payment_feedback
from dinify_backend.configss.string_definitions import PaymentMode_MobileMoney
from dinify_backend.configss.messages import OK_ORDER_PAYMENT_PROCESSED


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

    def test_order_payment(self):
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
