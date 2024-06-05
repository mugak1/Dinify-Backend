from typing import Optional
from finance_app.models import DinifyTransaction
from finance_app.serializers import SerializerGetDinifyTransactionListing


def generate_dinify_transaction_report(
    date_from: Optional[str],
    date_to: Optional[str],
    restaurant_id: Optional[str],
    transaction_status: Optional[str],
    transaction_type: Optional[str],
) -> dict:
    filters = {}
    if date_from is not None:
        filters['time_created__date__gte'] = date_from
    if date_to is not None:
        filters['time_created__date__lte'] = date_to
    if restaurant_id is not None:
        filters['account__restaurant'] = restaurant_id
    if transaction_status is not None:
        filters['transaction_status'] = transaction_status
    if transaction_type is not None:
        filters['transaction_type'] = transaction_type

    transactions = DinifyTransaction.objects.filter(**filters)
    records = SerializerGetDinifyTransactionListing(transactions, many=True)

    return {
        'status': 200,
        'message': 'Successfully retrieved dinify transactions',
        'data': records.data
    }
