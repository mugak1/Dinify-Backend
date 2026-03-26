import logging
from decimal import Decimal
from orders_app.models import Order
from finance_app.models import DinifyAccount, DinifyTransaction
from users_app.models import User
from django.db.models import Sum
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderRefund, TransactionStatus_Initiated,
    TransactionPlatform_Web, PaymentMode_MobileMoney
)
from payment_integrations_app.controllers.flutterwave import Flutterwave

logger = logging.getLogger(__name__)


def initiate_refund(
    order: Order,
    amount: Decimal,
    user: User,
    payment_mode: str
) -> dict:
    """
    Initiate a refund transaction to a customer
    """
    # get the order payment transactions associated with the order
    order_payments = DinifyTransaction.objects.filter(order=order)
    net_order_payments = order_payments.aggregate(
        total_payments=Sum('transaction_amount')
    )
    # check that the refund amount is not greater than the amount paid
    if amount > net_order_payments['total_payments']:
        return {
            'status': 400,
            'message': 'The refund amount is greater than the net order amount paid'
        }
    account = DinifyAccount.objects.get(restaurant=order.restaurant)
    refund_transaction = DinifyTransaction.objects.create(
        account=account,
        order=order,
        transaction_type=TransactionType_OrderRefund,
        transaction_status=TransactionStatus_Initiated,
        transaction_platform=TransactionPlatform_Web,
        transaction_amount=amount,
        transaction_collected_amount=amount,
        msisdn=order.customer.phone_number,
        created_by=user,
        payment_mode=payment_mode
    )
    # determine the next undertaking based on the payment mode
    if payment_mode == PaymentMode_MobileMoney:
        # send a mobile money prompt
        flutterwave_response = Flutterwave(
            payment_channel=payment_mode,
            amount=int(amount),
            email=order.customer.email if order.customer else None,
            transaction_id=str(refund_transaction.id),
            msisdn=order.customer.phone_number,
            restaurant_country=order.restaurant.country,
            currency=account.account_currency
        ).send_mobile_money()
        logger.debug("Response: %s", flutterwave_response)
    return {
        'status': 200,
        'message': 'Refund transaction has been initiated',
        'transaction_id': refund_transaction.id,
        # 'flutterwave_response': flutterwave_response
    }
