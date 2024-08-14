from typing import Optional
from decimal import Decimal
from users_app.models import User
from orders_app.models import Order
from finance_app.models import DinifyAccount, DinifyTransaction
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant, TransactionStatus_Initiated, TransactionType_OrderPayment,
    TransactionPlatform_Web, PaymentMode_Cash, PaymentMode_Card,
    PaymentMode_MobileMoney, PaymentForm_Split, PaymentForm_Full
)
from dinify_backend.configss.messages import (
    OK_ORDER_PAYMENT_INITIATED,
    ERR_ORDER_PAYMENT_INITIATION
)
from payment_integrations_app.controllers.flutterwave import Flutterwave
from payment_integrations_app.controllers.dpo import DpoIntegration


def initiate_order_payment(
    order: Order,
    payment_mode: str,
    transaction_platform=TransactionPlatform_Web,
    payment_form=PaymentForm_Full,
    msisdn: Optional[str] = None,
    amount: Optional[int] = None,
    user: Optional[User] = None,
    manual_payment: Optional[bool] = False,
    manual_payment_details: Optional[dict] = None
) -> dict:
    """
    Initiates the payment process for an order
    """
    # get the account to consider for the transaction
    # if the restaurant doesnot have an account, then create one for it
    try:
        account = DinifyAccount.objects.get(restaurant=order.restaurant)
    except DinifyAccount.DoesNotExist:
        account = DinifyAccount.objects.create(
            account_type=AccountType_Restaurant,
            restaurant=order.restaurant
        )

    transaction_amount = clean_amount(Decimal(order.actual_cost))

    if payment_form is PaymentForm_Split:
        if amount is not None:
            transaction_amount = clean_amount(Decimal(amount))

    # determine the amount to collect based on the aggregator charges
    amount_collectable = transaction_amount
    if payment_mode is PaymentMode_MobileMoney:
        amount_collectable = transaction_amount

    # make a transaction record for the payment
    order_payment = DinifyTransaction.objects.create(
        account=account,
        order=order,
        restaurant=order.restaurant,
        transaction_type=TransactionType_OrderPayment,
        transaction_status=TransactionStatus_Initiated,
        transaction_platform=transaction_platform,
        transaction_amount=transaction_amount,
        transaction_collected_amount=amount_collectable,
        msisdn=msisdn,
        payment_mode=payment_mode,
        payment_form=payment_form,
        created_by=user,
        manual_payment=manual_payment,
        manual_payment_details=manual_payment_details
    )

    # once created, send out a payment prompt
    if payment_mode == PaymentMode_MobileMoney and not manual_payment:
        # send a mobile money prompt
        # flutterwave_response = Flutterwave(
        #     payment_channel=payment_mode,
        #     amount=int(amount_collectable),
        #     email=order.customer.email if order.customer else None,
        #     transaction_id=str(order_payment.id),
        #     msisdn=msisdn,
        #     restaurant_country=order.restaurant.country,
        #     currency=account.account_currency
        # ).collect()

        # if flutterwave_response.get('status') == 'error':
        #     print(f"Order initiation payment error: {flutterwave_response.get('message')}")
        #     return {
        #         'status': 400,
        #         'message': ERR_ORDER_PAYMENT_INITIATION,
        #         'data': {
        #             "transaction_id": str(order_payment.id)
        #         }
        #     }
        # else:
        #     return {
        #         'status': 200,
        #         'message': OK_ORDER_PAYMENT_INITIATED,
        #         'data': {
        #             "transaction_id": str(order_payment.id),
        #             "redirect_url": flutterwave_response.get('meta').get('authorization').get('redirect')  # noqa
        #         }
        #     }
        dpo_token = DpoIntegration(
            amount=int(amount_collectable),
            currency=account.account_currency,
            msisdn=msisdn,
            transaction_reference=str(order_payment.id),
            timestamp=str(order_payment.time_created)
        ).create_token()

        if dpo_token is not None:
            return {
                'status': 200,
                'message': 'The payment has been initiated successfully.',
                'data': {
                    "transaction_id": str(order_payment.id),
                    "dpo_token": dpo_token,
                    "redirect_url": dpo_token
                }
            }
        else:
            return {
                'status': 400,
                'message': 'Sorry, an error occurred while initiating the payment. Please try again.',  # noqa
                'data': {
                    "transaction_id": str(order_payment.id)
                }
            }

    message = OK_ORDER_PAYMENT_INITIATED
    if manual_payment:
        message = 'The transaction will be reflected shortly.'
    return {
        'status': 200,
        'message': message,
        'data': {
            "transaction_id": str(order_payment.id)
        }
    }
