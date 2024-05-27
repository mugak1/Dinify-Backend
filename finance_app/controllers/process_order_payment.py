from decimal import Decimal
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    TransactionStatus_Success,
    PaymentStatus_Paid,
    OrderItemStatus_Served
)
from misc_app.controllers.clean_amount import clean_amount
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
        total_paid = order.total_paid
        total_paid += transaction_record.transaction_amount
        balance_payable = clean_amount(Decimal(order.total_cost)) - total_paid
        order.total_paid = total_paid
        order.balance_payable = balance_payable

        if order.balance_payable <= clean_amount(Decimal(1.00)):
            order.payment_status = PaymentStatus_Paid
            if order.order_status == OrderItemStatus_Served:
                order.order_status = "Paid"
        order.save()
        return {
            'status': 200,
            'message': OK_ORDER_PAYMENT_PROCESSED
        }
