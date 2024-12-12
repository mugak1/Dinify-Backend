from typing import Optional
from datetime import timedelta
from django.db import transaction
from psycopg import Transaction
from restaurants_app.models import Restaurant
from users_app.models import User
from finance_app.models import DinifyAccount, DinifyTransaction
from dinify_backend.configss.string_definitions import (
    AccountType_DinifyRevenue, ProcessingStatus_Confirmed, ProcessingStatus_Failed,
    ProcessingStatus_Done, TransactionStatus_Success,
    AccountType_Restaurant,
    TransactionType_Subscription,
    PaymentMode_MobileMoney, PaymentMode_Card, PaymentMode_Ova,
)
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from payment_integrations_app.controllers.dpo import DpoIntegration
from finance_app.controllers.update_wallet_balance import update_wallet_balance


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

        if payment_mode == PaymentMode_Ova:
            account = DinifyAccount.objects.get(restaurant=restaurant)
            try:
                account = DinifyAccount.objects.get(restaurant=restaurant)
            except DinifyAccount.DoesNotExist:
                account = DinifyAccount.objects.create(
                    account_type=AccountType_Restaurant,
                    restaurant=restaurant
                )

            # check if the account has enough momo_balance

            if account.momo_available_balance < transaction_amount and account.card_available_balance < transaction_amount:  # noqa
                return {
                    'status': 400,
                    'message': 'Sorry, you have insufficient funds to make the payment'
                }
        else:
            account = DinifyAccount.objects.get(account_type=AccountType_DinifyRevenue)

        if account is None:
            return {
                'status': 400,
                'message': 'An error occurred while determining the account'
            }

        # TODO if payment via ova, check for any pending disbursements

        # TODO require OTP if the number used is new to the platform

        # make a transaction record for the payment
        subscription_payment = DinifyTransaction.objects.create(
            account=account,
            restaurant=restaurant,
            transaction_type=TransactionType_Subscription,
            transaction_platform=transaction_platform,
            transaction_amount=transaction_amount,
            msisdn=msisdn,
            payment_mode=payment_mode,
            created_by=user,
        )

        if payment_mode == PaymentMode_MobileMoney:
            collection = YoIntegration().momo_collect(
                transaction_amount=int(transaction_amount),
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
            dpo_token = DpoIntegration(
                amount=int(transaction_amount),
                currency=account.account_currency,
                msisdn=msisdn,
                transaction_reference=str(subscription_payment.id),
                timestamp=str(subscription_payment.time_created),
                dpo_transaction_token=None
            ).create_token()

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
                if txs_record.payment_mode in [PaymentMode_MobileMoney, PaymentMode_Card]:
                    # TODO update account balances
                    balance_update = update_wallet_balance(
                        id=str(txs_record.account.id),
                        mode=txs_record.payment_mode,
                        credit=txs_record.transaction_amount
                    )
                    txs_record.account_balances = balance_update
                    txs_record.transaction_status = TransactionStatus_Success
                    txs_record.processing_status = ProcessingStatus_Done
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
                    print("Payment mode  not supported yet")
            elif txs_record.processing_status == ProcessingStatus_Failed:
                # TODO update account balances
                balance_update = update_wallet_balance(
                    id=str(txs_record.account.id),
                    mode=txs_record.payment_mode,
                    credit=0
                )
                txs_record.account_balances = balance_update
                txs_record.transaction_status = TransactionStatus_Success
                txs_record.processing_status = ProcessingStatus_Done
                txs_record.save()
