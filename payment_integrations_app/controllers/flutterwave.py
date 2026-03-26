import logging
import requests
from typing import Optional
from decouple import config
from dataclasses import dataclass, field
from misc_app.controllers.determine_telecom import determine_telecom
from dinify_backend.configss.string_definitions import PaymentMode_Card, PaymentMode_MobileMoney

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


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
    momo_payout_endpoint: str = "https://api.flutterwave.com/v3/transfers"
    ug_momo_collection_endpoint: str = "https://api.flutterwave.com/v3/charges?type=mobile_money_uganda"

    momo_collection_endpoints: dict = field(default_factory=lambda: {
        "UG": "https://api.flutterwave.com/v3/charges?type=mobile_money_uganda"
    })

    @property
    def HEADERS(self):
        return {
            'Authorization': f"Bearer {config('FLUTTERWAVE_SECRET')}"
        }

    def collect(self) -> dict:
        if self.payment_channel == PaymentMode_MobileMoney:
            return self.collect_mobile_money()

    def collect_mobile_money(self):
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
        }

        url = self.momo_collection_endpoints.get(self.restaurant_country, None)
        try:
            response = requests.post(
                url,
                json=req_body,
                headers=self.HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response = response.json()
        except requests.RequestException as exc:
            logger.error("Flutterwave MoMo collection failed: %s", exc)
            return {"status": "error", "message": "Payment provider request failed"}
        logger.info("Flutterwave MoMo Collection: tx_ref=%s status=%s",
                     self.transaction_id, response.get("status"))
        return response

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
        try:
            response = requests.post(
                self.momo_payout_endpoint,
                json=req_body,
                headers=self.HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response = response.json()
        except requests.RequestException as exc:
            logger.error("Flutterwave MoMo payout failed: %s", exc)
            return {"status": "error", "message": "Payout provider request failed"}
        logger.info("Flutterwave MoMo Payout: ref=%s status=%s",
                     self.transaction_id, response.get("status"))
        return response
