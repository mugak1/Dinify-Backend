from typing import Optional
from decimal import Decimal
from orders_app.models import Order
from finance_app.models import DinifyAccount, DinifyTransaction
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configs import (
    TransactionStatus_Initiated, TransactionType_OrderPayment,
    TransactionPlatform_Web, PaymentMode_Cash, PaymentMode_Card,
    PaymentMode_MobileMoney
)
from dinify_backend.string_messages import OK_ORDER_PAYMENT_INITIATED, ERR_ORDER_PAYMENT_INITIATION
from payment_integrations_app.controllers.flutterwave import Flutterwave


def initiate_order_payment(
    order: Order,
    payment_mode: str,
    transaction_platform=TransactionPlatform_Web,
    msisdn: Optional[str] = None,
) -> dict:
    """
    Initiates the payment process for an order
    """
    # get the account to consider for the transaction
    account = DinifyAccount.objects.get(restaurant=order.restaurant)

    amount = clean_amount(Decimal(order.actual_cost))

    # determine the amount to collect based on the aggregator charges
    amount_collectable = amount
    if payment_mode is PaymentMode_MobileMoney:
        amount_collectable = amount

    # make a transaction record for the payment
    order_payment = DinifyTransaction.objects.create(
        account=account,
        order=order,
        restaurant=order.restaurant,
        transaction_type=TransactionType_OrderPayment,
        transaction_status=TransactionStatus_Initiated,
        transaction_platform=transaction_platform,
        transaction_amount=amount,
        transaction_collected_amount=amount_collectable,
        msisdn=msisdn,
        payment_mode=payment_mode
    )

    # once created, send out a payment prompt
    if payment_mode == PaymentMode_MobileMoney:
        # send a mobile money prompt
        flutterwave_response = Flutterwave(
            payment_channel=payment_mode,
            amount=int(amount_collectable),
            email=order.customer.email if order.customer else None,
            transaction_id=str(order_payment.id),
            msisdn=msisdn,
            restaurant_country=order.restaurant.country,
            currency=account.account_currency
        ).collect()

        if flutterwave_response.get('status') == 'error':
            print(f"Order initiation payment error: {flutterwave_response.get('message')}")
            return {
                'status': 400,
                'message': ERR_ORDER_PAYMENT_INITIATION
            }
        else:
            return {
                'status': 200,
                'message': OK_ORDER_PAYMENT_INITIATED,
                'data': {
                    "transaction_id": str(order_payment.id),
                    "redirect_url": flutterwave_response.get('meta').get('authorization').get('redirect')  # noqa
                }
            }

    return {
        'status': 200,
        'message': OK_ORDER_PAYMENT_INITIATED,
        'data': {
            "transaction_id": str(order_payment.id)
        }
    }
