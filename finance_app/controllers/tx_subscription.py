import logging
from typing import Optional
from django.core.exceptions import ValidationError
from restaurants_app.models import Restaurant
from users_app.models import User
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    TransactionType_Subscription,
)

logger = logging.getLogger(__name__)


class SubscriptionPaymentTransaction:
    def __init__(self):
        pass

    def initiate(
        self,
        restaurant_id: str,
        transaction_platform: str,
        payment_mode: str,
        user: User,
        msisdn: Optional[str] = None,
        otp: Optional[str] = None,
    ) -> dict:
        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValidationError):
            # A well-formed-but-unknown UUID (DoesNotExist) or a malformed id
            # (ValidationError from UUIDField conversion) both resolve to the
            # same 404 the endpoint gate emits — a bad id never 500s, and an
            # authorized caller's unknown-id 404 is byte-for-byte identical to a
            # non-member's gate 404.
            return {'status': 404, 'message': 'Not found'}
        if restaurant.preferred_subscription_method == 'per_order':
            return {
                'status': 400,
                'message': 'Subscription payment not supported for per order subscription'
            }

        transaction_amount = restaurant.flat_fee

        # TODO require OTP if the number used is new to the platform
        # make a transaction record for the payment
        processing_status = ProcessingStatus_Pending
        subscription_payment = DinifyTransaction.objects.create(
            restaurant=restaurant,
            transaction_type=TransactionType_Subscription,
            transaction_platform=transaction_platform,
            transaction_amount=transaction_amount,
            msisdn=msisdn,
            payment_mode=payment_mode,
            created_by=user,
            processing_status=processing_status
        )

        # [8b] provider collection call goes here

        return {
            'status': 200,
            'message': 'The subscription payment has been initiated. Please confirm payment when promted',
            'data': {
                "transaction_id": str(subscription_payment.id)
            }
        }
