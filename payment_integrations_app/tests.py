import uuid
from typing import Any
from venv import create
from django.test import TestCase
from payment_integrations_app.controllers.dpo import DpoIntegration
from payment_integrations_app.controllers.pesapal import Pesapal
from payment_integrations_app.controllers.yo_integrations import YoIntegration


# Create your tests here.
class PaymentIntegrationsTestFunctions(TestCase):
    """
    the test functions for the payment integrations app
    """
    # def test_verify_token(self):
    #     dpo_response = DpoIntegration(
    #         amount=None,
    #         currency=None,
    #         msisdn=None,
    #         transaction_reference=None,
    #         timestamp=None,
    #         dpo_transaction_token="CF63B58B-C2C7-473F-A938-50FA956DB346"
    #     ).verify_token()
    #     self.assertEquals(dpo_response['status'], 200)

    def ttest_pesapal_authenticate(self):
        pesapal_response = Pesapal().authenticate()
        self.assertEquals(pesapal_response['status'], 200)

    def test_yo(self):
        # momo_collect = YoIntegration().momo_collect(
        #     transaction_amount=100,
        #     msisdn='256706087495',
        #     transaction_id=str(uuid.uuid4())
        # )
        # self.assertTrue(momo_collect)

        # momo_check_transaction = YoIntegration().momo_check_transaction(
        #     yo_transaction_reference='0BOvvK1loqGauhReTUfzH7MlVVmSALv6RpXtI0ZLYLIUTBdiXhOx0FIgmMlLTuxqc372b6adb2ee2084d56f73966afe38a0'
        # )
        # self.assertTrue(momo_check_transaction)

        # momo_disburse = YoIntegration().momo_disburse(
        #     transaction_amount=100,
        #     msisdn='256706087495',
        #     transaction_id=str(uuid.uuid4())
        # )
        # self.assertTrue(momo_disburse)

        bank_create_account = YoIntegration().bank_create_verified_account(
            arg_account_name='ESAU  LWANGA',
            arg_account_number='9030017868518',
            arg_bank_name='Stanbic Bank Uganda Limited',
            arg_address_line1='KIRA',
            arg_address_line2='KAMPALA',
            arg_city='KAMPALA',
            arg_country='UGANDA',
        )
        self.assertTrue(bank_create_account)
