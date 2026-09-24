"""
Restaurant Transactions reports — summary and listing.

Transactions are ``DinifyTransaction``-based, so the axis here is
``transaction_status`` / ``transaction_type`` — NOT the Order-based
``sale_filters`` / ``SALE_STATUSES`` used by the sales reports. The
``{status, message, data}`` envelope and the two public entrypoints
(``generate_restaurant_transaction_summary`` / ``_listing``) are kept so the
endpoint dispatch (``reports_app/endpoints/restaurant_reports.py``) is unchanged.

Corrected over the legacy bugs:
  * the summary runs in TWO grouped queries (group-by status, group-by type),
    then 0-fills onto the complete known sets so the shape is stable even when a
    bucket is empty — no per-status / per-type loop,
  * the listing emits RAW enum values (the frontend owns formatting), a single
    ``amount`` (direction is derivable from ``transaction_type``), the real
    ``payment_mode``, and ``select_related('order')`` so ``order_number`` adds no
    per-row query,
  * the date filter always uses ``time_created__date__gte/__lte`` (EAT-aligned
    under USE_TZ) — no single-day ``==`` special case.
"""
from typing import Optional

from django.db.models import Count, Q, Sum

from finance_app.models import DinifyTransaction
from finance_app.serializers import SerializerGetRestaurantTransactionListing
from misc_app.controllers.clean_dates import clean_dates
from orders_app.controllers.test_orders import counted_orders_q
from dinify_backend.configss.string_definitions import (
    TransactionStatus_Success,
    TransactionStatus_Failed,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    TransactionType_OrderPayment,
    TransactionType_Subscription,
)

# The summary 0-fills onto these known sets so the shape is stable even when a
# bucket has no rows. Statuses are the full transaction_status domain; types are
# the live set after the 8a non-custodial trim (order_payment + subscription) —
# any legacy type still present in the data is intentionally not surfaced here.
SUMMARY_STATUSES = [
    TransactionStatus_Success,
    TransactionStatus_Failed,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
]
SUMMARY_TYPES = [
    TransactionType_OrderPayment,
    TransactionType_Subscription,
]


def generate_restaurant_transaction_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    # This report is scoped by `restaurant_id` directly rather than through the order
    # FK, so it does NOT inherit sale_filters' practice-order exclusion — it applies
    # the same rule itself (`counted_orders_q`). The Q() form is load-bearing: `order`
    # is nullable and subscription rows carry no order at all, so a bare
    # `.exclude(order__is_test=True)` would be a join-shaped filter that silently
    # drops them. Keep rows with no order, and order-backed rows unless the order is
    # a PRACTICE order (a test order at a real restaurant) — at a TEST restaurant
    # every row counts, exactly as it would at a live one.
    base = DinifyTransaction.objects.filter(
        Q(order__isnull=True) | counted_orders_q('order__'),
        restaurant_id=restaurant_id,
        time_created__date__gte=date_from,
        time_created__date__lte=date_to,
    )

    # TWO grouped queries — replaces the legacy per-status and per-type loops.
    status_rows = {
        row['transaction_status']: row
        for row in base.values('transaction_status').annotate(
            count=Count('id'), amount=Sum('transaction_amount'),
        )
    }
    type_rows = {
        row['transaction_type']: row
        for row in base.values('transaction_type').annotate(
            count=Count('id'), amount=Sum('transaction_amount'),
        )
    }

    by_status = [
        {
            'status': status,
            'count': (status_rows.get(status) or {}).get('count', 0),
            'amount': (status_rows.get(status) or {}).get('amount') or 0,
        }
        for status in SUMMARY_STATUSES
    ]
    by_type = [
        {
            'type': txn_type,
            'count': (type_rows.get(txn_type) or {}).get('count', 0),
            'amount': (type_rows.get(txn_type) or {}).get('amount') or 0,
        }
        for txn_type in SUMMARY_TYPES
    ]
    # Sum of the status counts — no extra count query. (Different lens from
    # by_type, which projects only the live set, so the two need not reconcile.)
    total_transactions = sum(bucket['count'] for bucket in by_status)

    return {
        'status': 200,
        'message': 'Transaction summary generated successfully',
        'data': {
            'total_transactions': total_transactions,
            'by_status': by_status,
            'by_type': by_type,
        },
    }


def generate_restaurant_transaction_listing(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    transaction_type: Optional[str] = None,
    transaction_status: Optional[str] = None,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    # 31-day cap, exempting subscriptions (sparse, worth viewing over long ranges).
    if (date_to - date_from).days > 31 and \
            transaction_type != TransactionType_Subscription:
        return {
            'status': 400,
            'message': 'Date range should not exceed 31 days',
        }

    filters = {
        'restaurant_id': restaurant_id,
        'time_created__date__gte': date_from,
        'time_created__date__lte': date_to,
    }
    if transaction_type is not None:
        filters['transaction_type'] = transaction_type
    if transaction_status is not None:
        filters['transaction_status'] = transaction_status

    transactions = (
        DinifyTransaction.objects
        .filter(**filters)
        .select_related('order')
        .order_by('time_created')
    )
    records = SerializerGetRestaurantTransactionListing(transactions, many=True)
    return {
        'status': 200,
        'message': 'Transaction listing generated successfully',
        'data': records.data,
    }
