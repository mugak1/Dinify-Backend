from django.db import transaction
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    TransactionStatus_Success,
    PaymentStatus_Paid,
)
from finance_app.controllers.process_order_payment import process_order_payment
from dinify_backend.configss.messages import OK_ORDER_PAYMENT_PROCESSED


def process_payment_feedback(
    transaction_id: str,
    aggregator: str,
    aggregator_reference: str,
    aggregator_status: str,
    status: str
) -> dict:
    """
    Process feedback from the aggregator on the status of the transaction
    """

    # get the transaction
    # save the aggregator details
    # call the specific function to process the status of the transaction

    with transaction.atomic():
        transaction_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)

        # TODO check if the transaction status is already success

        if transaction_record.aggregator is None:
            transaction_record.aggregator = aggregator
            transaction_record.aggregator_reference = aggregator_reference

        transaction_record.aggregator_status = aggregator_status
        transaction_record.transaction_status = status
        transaction_record.save()

        result = {
            'status': 200,
            'message': 'Transaction status has been updated'
        }

        # update the wallet balance


        if transaction_record.transaction_type == TransactionType_OrderPayment:
            result = process_order_payment(
                transaction_record=transaction_record,
                transaction_status=status
            )

        return result

