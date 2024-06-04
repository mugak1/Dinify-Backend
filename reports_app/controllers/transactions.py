from misc_app.controllers.clean_dates import clean_dates
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionStatus_Success,
    TransactionStatus_Failed,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    TransactionType_OrderPayment,
    TransactionType_OrderRefund,
    TransactionType_OrderCharge,
    TransactionType_Disbursement,
    TransactionType_Subscription
)
from django.db.models import Sum

TRANSACTION_STATUSES = [
    TransactionStatus_Success,
    TransactionStatus_Failed,
    TransactionStatus_Pending,
    TransactionStatus_Initiated
]

TRANSACTION_TYPES = [
    TransactionType_OrderPayment,
    TransactionType_OrderRefund,
    TransactionType_OrderCharge,
    TransactionType_Disbursement,
    TransactionType_Subscription
]


def generate_restaurant_transaction_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    transactions = None
    filters = {
        'restaurant_id': restaurant_id
    }
    if date_to == date_from:
        filters['time_created__date'] = date_from
    else:
        filters['time_created__date__gte'] = date_from
        filters['time_created__date__lte'] = date_to

    transactions = DinifyTransaction.objects.filter(**filters)

    # get the transaction count by status
    transaction_status_overview = []
    transaction_type_overview = []

    for status in TRANSACTION_STATUSES:
        status_transactions = transactions.filter(transaction_status=status)
        amount = status_transactions.aggregate(
                Sum('transaction_amount')
            )['transaction_amount__sum']
        transaction_status_overview.append({
            'status': status,
            'count': status_transactions.count(),
            'amount': amount if amount else 0
        })
    for transaction_type in TRANSACTION_TYPES:
        type_transactions = transactions.filter(transaction_type=transaction_type)
        transaction_type_overview.append({
            'transaction_type': transaction_type,
            'count': type_transactions.count()
        })
    return {
        'status': 200,
        'message': 'Transaction summary generated successfully',
        'data': {
            'no_of_transactions': transactions.count(),
            'transaction_status_overview': transaction_status_overview,
            'transaction_type_overview': transaction_type_overview,
        }
    }
