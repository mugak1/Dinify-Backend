from django.db import transaction
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    TransactionStatus_Success,
    PaymentStatus_Paid,
)
from dinify_backend.configss.messages import OK_ORDER_PAYMENT_PROCESSED


def process_order_payment(
    transaction_record: DinifyTransaction,
    transaction_status: str
) -> dict:
    """
    Process the status of the order payment
    """
    order = Order.objects.select_for_update().get(id=transaction_record.order.id)
    if transaction_status == TransactionStatus_Success:
        # TODO check if the cumulative amount paid is equal to the order amount
        order.total_paid = order

        
        order.payment_status = PaymentStatus_Paid
        if order.order_status == "Served":
            order.order_status = "Paid"
        order.save()
        return {
            'status': 200,
            'message': OK_ORDER_PAYMENT_PROCESSED
        }
