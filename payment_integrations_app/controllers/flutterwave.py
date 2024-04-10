import requests
import json
from typing import Optional
from decouple import config
from dataclasses import dataclass
from misc_app.controllers.determine_telecom import determine_telecom
from dinify_backend.configss.string_definitions import PaymentMode_Card, PaymentMode_MobileMoney


@dataclass
class Flutterwave:
    amount: int
    transaction_id: str
    msisdn: str
    restaurant_country: str
    payment_channel: str = PaymentMode_MobileMoney
    email: Optional[str] = None
    currency: str = "UGX"
    customer_name: str = "Dinify Customer"
    momo_payout_endpoint = "https://api.flutterwave.com/v3/transfers"
    ug_momo_collection_endpoint = "https://api.flutterwave.com/v3/charges?type=mobile_money_uganda"

    momo_collection_endpoints = {
        "UG": ug_momo_collection_endpoint
    }

    HEADERS = {
        'Authorization': f"Bearer {config('FLUTTERWAVE_SECRET')}"
    }

    def collect(self) -> dict:
        if self.payment_channel == PaymentMode_MobileMoney:
            return self.collect_mobile_money()

    def collect_mobile_money(self):
        # if self.email is None:
        self.email = config('DEFAULT_PAYMENT_EMAIL')
        network = determine_telecom(self.msisdn)
        if network == "airtelug":
            network = "AIRTEL"
        elif network == "mtnug":
            network = "MTN"
        else:
            return {
                "status": "error",
                "message": "Unsupported telecom network",
            }

        req_body = {
            "tx_ref": self.transaction_id,
            "amount": self.amount,
            "currency": self.currency,
            "email": self.email,
            "phone_number": self.msisdn,
            "network": network,
            "redirect_url": "https://dinify.com/order-payment/complete/",
            # "meta": {}
        }

        # print(req_body)
        # return {"status": "error"}

        url = self.momo_collection_endpoints.get(self.restaurant_country, None)
        response = requests.post(
            url,
            data=req_body,
            headers=self.HEADERS,
            # timeout=60000
        )
        response = response.json()
        print(f"\n===Flutterwave MoMo Collection\n...Request...\n{req_body}\n...Response...\n{response}")  # noqa
        return response

    # {'status': 'error', 'message': 'str.replace is not a function', 'data': None}
    # {
    #     'status': 'success',
    #     'message': 'Charge initiated',
    #     'meta': {
    #         'authorization': {
    #             'redirect': 'https://checkout.flutterwave.com/captcha/verify/lang-en/5748431:e08b9226a98d3da138b090f890432905', 
    #             'mode': 'redirect'
    #         }
    #     }
    # }

    # webhook

    def send_mobile_money(self):
        req_body = {
            "account_bank": "MPS",
            "account_number": self.msisdn,
            "amount": self.amount,
            "narration": "Dinify Refund",
            "currency": "UGX",
            "reference": self.transaction_id,
            "beneficiary_name": self.customer_name
        }
        response = requests.post(
            self.momo_payout_endpoint,
            data=req_body,
            headers=self.HEADERS
        )
        response = response.json()
        print(f"\n===Flutterwave MoMo Payout\n...Request...\n{req_body}\n...Response...\n{response}")  # noqa
        return response
