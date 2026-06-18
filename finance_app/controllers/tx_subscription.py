import logging
from typing import Optional
from restaurants_app.models import Restaurant
from users_app.models import User
from finance_app.models import DinifyAccount, DinifyTransaction
from dinify_backend.configss.string_definitions import (
    AccountType_DinifyRevenue,
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
        restaurant = Restaurant.objects.get(id=restaurant_id)
        if restaurant.preferred_subscription_method == 'per_order':
            return {
                'status': 400,
                'message': 'Subscription payment not supported for per order subscription'
            }

        account = None
        transaction_amount = restaurant.flat_fee

        account = DinifyAccount.objects.get(account_type=AccountType_DinifyRevenue)

        if account is None:
            return {
                'status': 400,
                'message': 'An error occurred while determining the account'
            }

        # TODO require OTP if the number used is new to the platform
        # make a transaction record for the payment
        processing_status = ProcessingStatus_Pending
        subscription_payment = DinifyTransaction.objects.create(
            account=account,
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
