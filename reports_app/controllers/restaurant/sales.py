"""
Restaurant Sales reports — listing and trends.

Built on the PR3 reporting foundations so the two panes agree:
  * the "sale" set is ``SALE_STATUSES`` ({served, paid}) via ``sale_orders``,
  * revenue is ``Sum('actual_cost')`` and discount is ``Sum('savings')`` — never
    ``total_cost`` (gross) or ``discounted_cost`` (post-discount total),
  * trends are ONE grouped query via ``bucket_sales`` (no per-period loop),
    zero-filled onto ``period_boundaries`` so every period in the window is
    reported — the same policy the hourly pane applies with ``range(24)``.

The public entrypoints are ``generate_restaurant_sales_listing`` / ``_trends``
(plus the hour-of-day ``generate_restaurant_sales_hourly``), each returning the
``{status, message, data}`` envelope dispatched from
``reports_app/endpoints/restaurant_reports.py``.
"""
from django.db.models import (
    Count, Sum, Subquery, OuterRef, IntegerField,
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
    sale_orders, SALE_STATUSES,
)
from reports_app.controllers.common.bucketing import (
    bucket_sales, bucket_sales_by_hour, period_boundaries, LOCAL_TZ,
)
from reports_app.serializers import SerializerOrderListingReport


# trend_category (the public API param) -> bucketing period granularity.
TREND_PERIODS = {
    'daily': 'day',
    'weekly': 'week',
    'monthly': 'month',
    'quarterly': 'quarter',
    'annual': 'year',
}
# Per-category date-range caps (retained from the legacy controller). The daily
# cap also bounds the listing; the wider caps bound the number of buckets.
TREND_CAPS = {
    'daily': (31, 'Date range should not be greater than 31 days.'),
    # 371 days is 53 weeks exactly, so this bounds a weekly request to <=54
    # buckets for every start-day alignment (the 54th is the partial edge week).
    # It sits well above any span the timeframe ladder selects weekly for — it
    # exists to bound payload size and query cost, as the other caps do.
    'weekly': (371, 'Date range should not be greater than 1 year.'),
    'monthly': (731, 'Date range should not be greater than 2 years.'),
    'quarterly': (731, 'Date range should not be greater than 2 years.'),
    'annual': (1850, 'Date range should not be greater than 5 years.'),
}
# x-axis title for the graph series, keyed by bucketing period.
TREND_AXIS_TITLES = {
    'day': 'Days',
    'week': 'Weeks',
    'month': 'Months',
    'quarter': 'Quarters',
    'year': 'Years',
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
    by_period = {row['period']: row for row in buckets}

    # Zero-fill onto the complete window axis, the same policy the hourly pane
    # applies with range(24). A period that traded nothing is a reportable zero,
    # not an absence: dropping it makes the chart join its neighbours into a
    # straight line and imply trading that did not happen.
    table = [
        {
            'period': _period_label(boundary, period),
            'count': (by_period.get(boundary) or {}).get('count', 0),
            'revenue': (by_period.get(boundary) or {}).get('revenue') or 0,
            'discount': (by_period.get(boundary) or {}).get('discount') or 0,
        }
        for boundary in period_boundaries(date_from, date_to, period)
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
    """ISO, sortable-as-text period key for a bucket boundary, on the EAT calendar.

    The Reports contract is that the BACKEND returns RAW, SORTABLE values and the
    FRONTEND owns display formatting (it parses these keys as ISO dates). Every
    bucket therefore emits a key that sorts correctly as a plain string.

    day      -> 'YYYY-MM-DD'   (2024-03-01)
    week     -> 'YYYY-MM-DD'   (2024-03-04)  the MONDAY boundary of the bucket
    month    -> 'YYYY-MM'      (2024-03)
    quarter  -> 'YYYY-Qn'      (2024-Q1)   year-first so it sorts as text
    year     -> 'YYYY'         (2024)

    A week deliberately emits its Monday DATE, not an ISO week string. An ISO
    week (``2026-W30``) would sort correctly but breaks ``parseISO()`` on the
    frontend; the Monday boundary date preserves the "raw sortable value the
    frontend parses as an ISO date" contract above. ``TruncWeek`` is
    Monday-anchored and ``bucket_sales`` truncates with ``tzinfo=LOCAL_TZ``, so
    the boundary is the Monday in EAT.

    NOTE the partial-edge week: a range whose first day is mid-week produces a
    first bucket labelled with the PRECEDING Monday — a key that can fall before
    the requested ``date_from`` — while containing only the in-range days. That
    is correct and intended (the bucket is named by its week, not clipped to the
    window), but it is an easy thing to misread as an off-by-one. Since the
    series is zero-filled onto ``period_boundaries``, that bucket is now emitted
    whether or not it traded — the axis has to start where the data can, or the
    window's first days would have nowhere to land.
    """
    local_date = period_dt.astimezone(LOCAL_TZ).date()
    if period in ('day', 'week'):
        return local_date.strftime('%Y-%m-%d')
    if period == 'month':
        return local_date.strftime('%Y-%m')
    if period == 'quarter':
        quarter = (local_date.month - 1) // 3 + 1
        return f"{local_date.year}-Q{quarter}"
    return str(local_date.year)
