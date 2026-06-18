import logging
from typing import Optional
from datetime import timedelta
from django.db import transaction
from decimal import Decimal, ROUND_HALF_UP
from restaurants_app.models import Restaurant
from users_app.models import User
from finance_app.models import DinifyAccount, DinifyTransaction
from dinify_backend.configss.string_definitions import (
    AccountType_DinifyRevenue, ProcessingStatus_Confirmed, ProcessingStatus_Failed,
    ProcessingStatus_Pending,
    ProcessingStatus_Done, TransactionStatus_Success,
    TransactionType_Subscription,
    PaymentMode_MobileMoney, PaymentMode_Card,
)
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from payment_integrations_app.controllers.dpo import DpoIntegration

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

        if payment_mode == PaymentMode_MobileMoney:
            collection = YoIntegration().momo_collect(
                # UGX has no subunits; round to whole units for the gateway
                transaction_amount=int(transaction_amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                msisdn=msisdn,
                transaction_id=str(subscription_payment.id)
            )
            if collection:
                return {
                    'status': 200,
                    'message': 'The subscription payment has been initiated. Please confirm payment when prompted',  # noqa
                    'data': {
                        "transaction_id": str(subscription_payment.id)
                    }
                }
            else:
                return {
                    'status': 400,
                    'message': 'Sorry, an error occurred while initiating the payment. Please try again later',  # noqa
                    'data': {
                        "transaction_id": str(subscription_payment.id)
                    }
                }

        if payment_mode == PaymentMode_Card:
            dpo_token = DpoIntegration().create_token(
                # UGX has no subunits; round to whole units for the gateway
                amount=int(transaction_amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                currency=account.account_currency,
                transaction_reference=str(subscription_payment.id),
                timestamp=str(subscription_payment.time_created),
            )

            if dpo_token is not None:
                return {
                    'status': 200,
                    'message': 'The payment has been initiated successfully.',
                    'data': {
                        "transaction_id": str(subscription_payment.id),
                        "dpo_token": dpo_token,
                        "redirect_url": dpo_token
                    }
                }
            else:
                return {
                    'status': 400,
                    'message': 'Sorry, an error occurred while initiating the payment. Please try again later',
                    'data': {
                        "transaction_id": str(subscription_payment.id)
                    }
                }

        return {
            'status': 200,
            'message': 'The subscription payment has been initiated. Please confirm payment when promted',
            'data': {
                "transaction_id": str(subscription_payment.id)
            }
        }

    def process(self, transaction_id: str):
        with transaction.atomic():
            txs_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)
            restaurant = Restaurant.objects.select_for_update().get(id=txs_record.restaurant.id)

            if txs_record.processing_status == ProcessingStatus_Confirmed:
                logger.debug("Payment mode: %s", txs_record.payment_mode)
                if txs_record.payment_mode in [PaymentMode_MobileMoney, PaymentMode_Card]:
                    txs_record.transaction_status = TransactionStatus_Success
                    txs_record.processing_status = ProcessingStatus_Done
                    txs_record.amount_in = txs_record.transaction_amount
                    txs_record.save()

                    # extend the restaurant subscription_expiry_date
                    days = 30
                    if restaurant.preferred_subscription_method == 'yearly':
                        days = 365

                    current_expiry = restaurant.subscription_expiry_date
                    if current_expiry is None:
                        current_expiry = txs_record.time_created

                    new_expiry_date = current_expiry + timedelta(days=days)
                    restaurant.subscription_validity = True
                    restaurant.subscription_expiry_date = new_expiry_date
                    restaurant.save()

                else:
                    logger.debug("Payment mode not supported yet")
            elif txs_record.processing_status == ProcessingStatus_Failed:
                txs_record.transaction_status = TransactionStatus_Success
                txs_record.processing_status = ProcessingStatus_Done
                txs_record.save()
