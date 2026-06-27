"""
Restaurant Sales reports — summary, listing, and trends.

Built on the PR3 reporting foundations so the three panes agree:
  * the "sale" set is ``SALE_STATUSES`` ({served, paid}) via ``sale_orders``,
  * revenue is ``Sum('actual_cost')`` and discount is ``Sum('savings')`` via
    ``revenue_sum`` / ``discount_sum`` — never ``total_cost`` (gross) or
    ``discounted_cost`` (post-discount total),
  * trends are ONE grouped query via ``bucket_sales`` (no per-period loop).

The ``{status, message, data}`` envelope and the three public entrypoints
(``generate_restaurant_sales_summary`` / ``_listing`` / ``_trends``) are kept so
the endpoint dispatch (``reports_app/endpoints/restaurant_reports.py``) is
unchanged.
"""
import calendar
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import (
    Count, Sum, Avg, Max, Min, Subquery, OuterRef, IntegerField,
)
from django.db.models.functions import Coalesce

from orders_app.models import OrderItem
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    TransactionStatus_Success,
)
from misc_app.controllers.clean_dates import clean_dates
from misc_app.controllers.report_support_functions import make_graph_series_data
from reports_app.controllers.common.sale_filters import (
    sale_orders, revenue_sum, discount_sum, SALE_STATUSES,
)
from reports_app.controllers.common.bucketing import (
    bucket_sales, bucket_sales_by_hour, LOCAL_TZ,
)
from reports_app.serializers import SerializerOrderListingReport


# trend_category (the public API param) -> bucketing period granularity.
TREND_PERIODS = {
    'daily': 'day',
    'monthly': 'month',
    'quarterly': 'quarter',
    'annual': 'year',
}
# Per-category date-range caps (retained from the legacy controller). The daily
# cap also bounds the listing; the wider caps bound the number of buckets.
TREND_CAPS = {
    'daily': (31, 'Date range should not be greater than 31 days.'),
    'monthly': (731, 'Date range should not be greater than 2 years.'),
    'quarterly': (731, 'Date range should not be greater than 2 years.'),
    'annual': (1850, 'Date range should not be greater than 5 years.'),
}
# x-axis title for the graph series, keyed by bucketing period.
TREND_AXIS_TITLES = {
    'day': 'Days',
    'month': 'Months',
    'quarter': 'Quarters',
    'year': 'Years',
}


def generate_restaurant_sales_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    orders = sale_orders(restaurant_id, date_from, date_to)

    # ONE aggregate row. revenue/discount use the canonical bases; avg/max/min
    # are over actual_cost (net order value), NOT total_cost.
    agg = orders.aggregate(
        number_of_sales=Count('id'),
        revenue=revenue_sum(),
        gross_sales=Sum('total_cost'),
        total_discounts=discount_sum(),
        average_order_value=Avg('actual_cost'),
        max_order_value=Max('actual_cost'),
        min_order_value=Min('actual_cost'),
    )

    average_order_value = agg['average_order_value']
    if average_order_value is not None:
        # Avg over a numeric column can carry extra places; money is 2dp.
        average_order_value = average_order_value.quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

    # Payment channels: successful order-payment transactions over the SAME sale
    # set, grouped by the real payment_mode. Genuinely sparse until 8b — not
    # fabricated, and (different lens) it need not reconcile with the order
    # count/revenue above (a served order may have no on-system transaction).
    channel_rows = (
        DinifyTransaction.objects
        .filter(
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            order__restaurant=restaurant_id,
            order__order_status__in=SALE_STATUSES,
            order__time_created__date__gte=date_from,
            order__time_created__date__lte=date_to,
        )
        .values('payment_mode')
        .annotate(count=Count('id'), amount=Sum('transaction_amount'))
        .order_by('payment_mode')
    )
    payment_channels = [
        {
            'channel': row['payment_mode'],
            'count': row['count'],
            'amount': row['amount'] if row['amount'] is not None else 0,
        }
        for row in channel_rows
    ]

    data = {
        'number_of_sales': agg['number_of_sales'] or 0,
        'revenue': agg['revenue'] if agg['revenue'] is not None else 0,
        'gross_sales': agg['gross_sales'] if agg['gross_sales'] is not None else 0,
        'total_discounts': agg['total_discounts'] if agg['total_discounts'] is not None else 0,
        'average_order_value': average_order_value if average_order_value is not None else 0,
        'max_order_value': agg['max_order_value'] if agg['max_order_value'] is not None else 0,
        'min_order_value': agg['min_order_value'] if agg['min_order_value'] is not None else 0,
        'payment_channels': payment_channels,
    }
    return {
        'status': 200,
        'message': 'Successfully retrieved the sales summary',
        'data': data,
    }


def generate_restaurant_sales_listing(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    if (date_to - date_from).days > 31:
        return {
            'status': 400,
            'message': 'Date range cannot be greater than 31 days.',
        }

    # item_count as a correlated COUNT subquery (no JOIN/GROUP BY, so the row's
    # own money columns can never be fan-out-inflated); 0 for an item-less order.
    item_count_subquery = (
        OrderItem.objects
        .filter(order=OuterRef('pk'))
        .values('order')
        .annotate(c=Count('id'))
        .values('c')
    )
    # The order's latest successful order-payment mode (real value, or NULL) —
    # replaces the hardcoded 'MoMo' and the per-row query.
    payment_mode_subquery = (
        DinifyTransaction.objects
        .filter(
            order=OuterRef('pk'),
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
        )
        .order_by('-time_created')
        .values('payment_mode')[:1]
    )

    orders = (
        sale_orders(restaurant_id, date_from, date_to)
        .annotate(
            item_count=Coalesce(
                Subquery(item_count_subquery, output_field=IntegerField()), 0,
            ),
            payment_mode=Subquery(payment_mode_subquery),
        )
        .order_by('time_created')
    )

    records = SerializerOrderListingReport(orders, many=True)
    return {
        'status': 200,
        'message': 'Successfully retrieved the sales listings',
        'data': records.data,
    }


def generate_restaurant_sales_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_category: str,
    trend_result: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    period = TREND_PERIODS.get(trend_category)
    if period is None:
        return {
            'status': 400,
            'message': 'Invalid trend category',
        }

    max_days, cap_message = TREND_CAPS[trend_category]
    if (date_to - date_from).days > max_days:
        return {
            'status': 400,
            'message': cap_message,
        }

    # ONE grouped query — replaces the legacy per-period summary loop.
    buckets = bucket_sales(
        sale_orders(restaurant_id, date_from, date_to), period,
    )
    table = [
        {
            'period': _period_label(row['period'], period),
            'count': row['count'],
            'revenue': row['revenue'] if row['revenue'] is not None else 0,
            'discount': row['discount'] if row['discount'] is not None else 0,
        }
        for row in buckets
    ]

    if trend_result == 'graph':
        graph_input = [
            {
                'period': row['period'],
                'revenue': row['revenue'],
                'count': row['count'],
            }
            for row in table
        ]
        data = make_graph_series_data(
            x_title=TREND_AXIS_TITLES[period],
            y_values=graph_input,
            x_detail='period',
        )
        return {
            'status': 200,
            'message': 'Successfully retrieved the sales trend graph series.',
            'data': data,
        }

    return {
        'status': 200,
        'message': 'Successfully retrieved the sales trend table.',
        'data': table,
    }


def generate_restaurant_sales_hourly(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    """Hour-of-day ("when orders land") sale distribution, 0–23 in EAT.

    Shares the Sales section's sale set (``{served, paid}``) and revenue basis
    (``Sum('actual_cost')``) via :func:`sale_orders` / ``bucket_sales_by_hour``,
    so the figures agree with the other Sales panes. Returns a stable 24-row
    axis (zero-filled for hours without orders) of RAW hours — the frontend
    owns any display window / peak labelling.
    """
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    # ONE grouped query; only hours that had orders are returned.
    buckets = bucket_sales_by_hour(
        sale_orders(restaurant_id, date_from, date_to),
    )
    by_hour = {row['hour']: row for row in buckets}

    # Zero-fill to a continuous 0–23 axis (the transactions-summary idiom).
    data = [
        {
            'hour': hour,
            'count': (by_hour.get(hour) or {}).get('count', 0),
            'revenue': (by_hour.get(hour) or {}).get('revenue') or 0,
            'discount': (by_hour.get(hour) or {}).get('discount') or 0,
        }
        for hour in range(24)
    ]

    return {
        'status': 200,
        'message': 'Successfully retrieved the hourly sales distribution.',
        'data': data,
    }


def _period_label(period_dt, period: str) -> str:
    """Human label for a bucket's period boundary, on the EAT calendar.

    day      -> 'YYYY-MM-DD'   (2024-03-01)
    month    -> 'Mon-YY'       (Mar-24)
    quarter  -> 'Qn-YYYY'      (Q1-2024)
    year     -> 'YYYY'         (2024)
    """
    local_date = period_dt.astimezone(LOCAL_TZ).date()
    if period == 'day':
        return local_date.strftime('%Y-%m-%d')
    if period == 'month':
        return f"{calendar.month_abbr[local_date.month]}-{local_date.year % 100:02d}"
    if period == 'quarter':
        quarter = (local_date.month - 1) // 3 + 1
        return f"Q{quarter}-{local_date.year}"
    return str(local_date.year)
