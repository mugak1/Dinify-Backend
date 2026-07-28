from datetime import timedelta
from decimal import Decimal
import statistics

from misc_app.controllers.clean_dates import clean_dates
from django.db.models import Count, Sum, Avg, F, Q  # noqa
from django.db.models.functions import (
    TruncHour, TruncDay, TruncWeek, TruncMonth, TruncYear,
)
from django.utils import timezone

from orders_app.models import Order, OrderItem
from restaurants_app.models import Table
from finance_app.models import DinifyTransaction
from reports_app.controllers.common.bucketing import period_boundaries
from dinify_backend.configss.string_definitions import (
    PaymentStatus_Paid, OrderStatus_Cancelled,
    OrderStatus_Refunded,
    OrderStatus_Initiated,
    PaymentStatus_Pending,
    TransactionType_OrderPayment, TransactionStatus_Success,
)


# Total number of sales
# Paid orders (number and percentage)
# Cancelled orders (number and percentage)
# Refunded orders (number and percentage)
# Gross sales amount
# New diners
# Repeat diners
# Most ordered item
# Least ordered item
# Most liked item i.e. based on the ratings
# Least liked item i.e. based on the ratings
# Most active diner
# Peak hour


def generate_restaurant_dashboard_details(
    restaurant_id: str,
    date_from: str,
    date_to: str
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    # Pre-go-live rehearsal orders are excluded from every figure below: this whole
    # report is commercial history. The live floor and KDS cards in dashboard-v2 are
    # the deliberate exception — see _build_tables / _build_kds.
    orders = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        time_created__gte=date_from,
        time_created__lte=date_to
    )
    order_items = OrderItem.objects.filter(
        order__restaurant=restaurant_id,
        order__is_test=False,
        order__time_created__gte=date_from,
        order__time_created__lte=date_to
    )

    num_sales = orders.count()
    paid_orders = orders.filter(payment_status=PaymentStatus_Paid)
    num_paid_orders = paid_orders.count()
    perc_paid_orders = (num_paid_orders / num_sales) * 100 if num_sales else 0
    perc_paid_orders = round(perc_paid_orders, 1)

    cancelled_orders = orders.filter(order_status=OrderStatus_Cancelled)
    num_cancelled_orders = cancelled_orders.count()
    perc_cancelled_orders = (num_cancelled_orders / num_sales) * 100 if num_sales else 0
    perc_cancelled_orders = round(perc_cancelled_orders, 1)

    refunded_orders = orders.filter(order_status=OrderStatus_Refunded)
    num_refunded_orders = refunded_orders.count()
    perc_refunded_orders = (num_refunded_orders / num_sales) * 100 if num_sales else 0
    perc_refunded_orders = round(perc_refunded_orders, 1)

    sales_amount = paid_orders.aggregate(total_cost=Sum('total_cost'))['total_cost']

    new_diners = orders.values('customer').distinct().count()
    repeat_diners = orders.values('customer').annotate(order_count=Count('id')).filter(order_count__gt=1).count()  # noqa
    most_active_diner = orders.values('customer__first_name').annotate(order_count=Count('id')).order_by('-order_count').first()  # noqa

    most_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('-total_quantity').first()  # noqa
    least_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('total_quantity').first()  # noqa

    most_liked_item = None
    least_liked_item = None

    peak_hour = orders.annotate(hour=F('time_created__hour')).values('hour').annotate(order_count=Count('id')).order_by('-order_count').first()  # noqa

    stats = {
        "num_sales": num_sales,
        "paid_orders": {
            "number": num_paid_orders,
            "percentage": perc_paid_orders,
        },
        "cancelled_orders": {
            "number": num_cancelled_orders,
            "percentage": perc_cancelled_orders,
        },
        "refunded_orders": {
            "number": num_refunded_orders,
            "percentage": perc_refunded_orders,
        },
        "sales_amount": sales_amount,
        "new_diners": new_diners,
        "repeat_diners": repeat_diners,
        "most_ordered_item": most_ordered_item['item__name'] if most_ordered_item else '',
        "least_ordered_item": least_ordered_item['item__name'] if least_ordered_item else '',
        "most_liked_item": most_liked_item['item__name'] if most_liked_item else '',
        "least_liked_item": least_liked_item['item__name'] if least_liked_item else '',
        "most_active_diner": most_active_diner['customer__first_name'] if most_active_diner else '',
        "peak_hour": peak_hour['hour'] if peak_hour else '',
    }

    return {
        'status': 200,
        'message': 'Successfully retrieved the restaurant dashboard',
        'data': stats
    }


def summarize_revenue(restaurant_id: str):
    orders = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        payment_status=PaymentStatus_Paid
    )
    total_revenue = orders.aggregate(total_revenue=Sum('actual_cost'))['total_revenue']
    # Resolve "this month" in EAT — the DB extracts time_created in EAT, so the
    # comparison values must be EAT too (naive datetime.now() is UTC wall clock).
    local_now = timezone.localtime(timezone.now())
    this_month_revenue = orders.filter(
        time_created__month=local_now.month,
        time_created__year=local_now.year
    ).aggregate(total_revenue=Sum('actual_cost'))['total_revenue']
    # last_month_revenue = orders.filter(
    #     time_created__month=datetime.now().month - 1
    # ).aggregate(total_revenue=Sum('actual_cost'))['total_revenue']
    return {
        'total': total_revenue if total_revenue is not None else 0,
        'this_month': this_month_revenue if this_month_revenue is not None else 0,
        # 'last_month': last_month_revenue,
        'month_growth': 'up'  # if this_month_revenue > last_month_revenue else 'down'
    }


# ---------------------------------------------------------------------------
# Dashboard V2
# ---------------------------------------------------------------------------

# The granularity vocabulary — keyed on the GRANULARITY itself, resolved fail-CLOSED,
# and the ONLY one this endpoint has. It replaced a legacy `period` map keyed on the
# caller's UI SELECTION ('day' meant "the user picked Day, so bucket by HOUR") which
# DASH-PERIOD-00 deprecated and DASH-REMOVE-LEGACY-00 removed once no caller sent it.
#
# 'week' was added in DASH-WEEK-00 for a real caller: the frontend timeframe ladder
# jumped 'day' (<=31 days) straight to 'month', rendering a 60-day range as two points
# instead of about nine. DASH-PERIOD-00 declined it on the grounds that unused
# vocabulary is surface to keep correct for no caller — that reasoning expired the
# moment the ladder started emitting it. Because `bucket` fails CLOSED, the ladder
# change would otherwise have 400'd every 32-to-90-day range.
#
# `TruncWeek` is Monday-anchored, so a weekly `at` key is the MONDAY of its week in
# EAT. The tz comes from the truncation itself: the `trunc_fn(...)` calls below pass
# no `tzinfo=`, so Django truncates in — and returns a datetime aware in — the active
# timezone (`TIME_ZONE = 'Africa/Nairobi'`), which is where every `at` key's +03:00
# offset comes from. (`common/bucketing.py` reaches the same place by passing
# `tzinfo=LOCAL_TZ` explicitly; different mechanism, same zone.)
# `sales-trends` (`weekly`, TRENDS-WEEKLY-00) anchors to the same Monday. The two
# endpoints emit it in different FORMATS — 'YYYY-MM-DD' there, a full ISO datetime
# with the +03:00 offset here — but agreeing on the anchor is what lets one frontend
# enumerator serve both, so do not move either boundary independently.
#
# This is still NOT a unification with
# `reports_app.controllers.common.bucketing.PERIOD_TRUNC`, and the overlap is now
# wide enough to look like an invitation to merge them. It is not: that map has no
# 'hour', this one has no 'quarter', and each omission is a deliberate "no caller
# needs it" rather than an oversight. The asymmetry IS the boundary between them.
# No 'quarter' entry here for exactly the reason 'week' had none until now — the
# dashboard ladder does not emit one.
#
# Both endpoints DO now share `common.bucketing.period_boundaries` to enumerate the
# zero-fill axis, and that is not the unification disclaimed above: it maps nothing
# to a truncation function and carries no endpoint's vocabulary opinion, so it spans
# both ('hour' AND 'quarter') without either map gaining an entry. This map is still
# the sole authority on what `bucket` values this endpoint accepts.
BUCKET_TRUNC = {
    'hour': TruncHour,
    'day': TruncDay,
    'week': TruncWeek,
    'month': TruncMonth,
    'year': TruncYear,
}


def _bucket_error(lead):
    """The one 400 envelope both bucket failures share.

    The accepted-value list is DERIVED from ``BUCKET_TRUNC``, never written out
    separately, so adding a granularity cannot leave the message stale — and because
    the map is ordered by coarseness, the message reads in granularity order.
    """
    return {
        'status': 400,
        'message': f"{lead}; expected one of {', '.join(BUCKET_TRUNC)}",
    }


def _resolve_bucket_trunc(bucket):
    """Resolve the chart truncation from the ``bucket`` granularity.

    :returns: ``(trunc_fn, key, None)`` on success, or ``(None, None, error)`` where
        ``error`` is the ``{'status': 400, 'message': ...}`` envelope the endpoint
        turns into an HTTP 400.

    The NORMALISED key is returned alongside the truncation because the series is
    zero-filled: the builders need the granularity itself to enumerate the bucket
    axis (``period_boundaries``), not just the function that truncates to it. It is
    returned from here rather than re-derived at the call site so the strip below
    happens in exactly one place — a caller stripping its own copy could disagree
    with the value that was actually validated.

    ``bucket`` is REQUIRED. There is no default granularity to fall back to and no
    defensible one to invent: this endpoint caps neither the date range nor the bucket
    count, so guessing wrong returns an enormous payload rather than an error the
    caller can see. Absent, empty and whitespace-only are one case — they strip to the
    same nothing — and all three 400 alongside unknown values. Lookup is exact ('DAY'
    is not 'day') so a mismatch surfaces here rather than downstream.
    """
    key = (bucket or '').strip()
    if not key:
        return None, None, _bucket_error('Missing bucket')
    try:
        return BUCKET_TRUNC[key], key, None
    except KeyError:
        return None, None, _bucket_error(f"Unsupported bucket '{key}'")


PAYMENT_LABEL_MAP = {
    'momo': 'Mobile Money',
    'cash': 'Cash',
    'card': 'Card',
    'ova': 'OVA',
    'bank': 'Bank',
}

_ZERO = Decimal('0.00')


def _dec(value):
    """Return a string-formatted Decimal, defaulting to '0.00'."""
    if value is None:
        return str(_ZERO)
    return str(Decimal(value).quantize(Decimal('0.01')))


def _build_revenue(restaurant_id, date_from, date_to, trunc_fn, bucket):
    base = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        time_created__gte=date_from,
        time_created__lte=date_to,
    )
    paid = base.filter(payment_status=PaymentStatus_Paid)
    refunded = base.filter(order_status=OrderStatus_Refunded)

    paid_buckets = (
        paid.annotate(bucket=trunc_fn('time_created'))
        .values('bucket')
        .annotate(gross=Sum('total_cost'), discounts=Sum('savings'))
        .order_by('bucket')
    )
    refund_buckets = (
        refunded.annotate(bucket=trunc_fn('time_created'))
        .values('bucket')
        .annotate(refunds=Sum('actual_cost'))
        .order_by('bucket')
    )
    refund_map = {r['bucket']: r['refunds'] or _ZERO for r in refund_buckets}
    paid_map = {r['bucket']: r for r in paid_buckets}

    # Zero-fill onto the complete window axis so a bucket that traded nothing is
    # reported as a zero rather than omitted — otherwise the chart joins its
    # neighbours into a straight line and implies revenue that did not happen.
    #
    # WATCH THE BASIS. This series is driven by PAID orders, with refunds joined
    # in; the fill INSERTS only where that basis has no bucket and never
    # overwrites a real row. It also does not widen the axis to paid ∪ refunded:
    # the axis is the window. Refunds keep exactly the lookup they always had, so
    # a bucket holding a refund but no paid order — previously absent entirely —
    # now surfaces its real refund instead of a 0.00 that would contradict
    # totals.refunds below.
    series = []
    for boundary in period_boundaries(date_from, date_to, bucket):
        row = paid_map.get(boundary)
        series.append({
            'at': boundary.isoformat(),
            'gross': _dec(row['gross'] if row else None),
            'discounts': _dec(row['discounts'] if row else None),
            'refunds': _dec(refund_map.get(boundary, _ZERO)),
        })

    gross_total = paid.aggregate(v=Sum('total_cost'))['v'] or _ZERO
    discounts_total = paid.aggregate(v=Sum('savings'))['v'] or _ZERO
    refunds_total = refunded.aggregate(v=Sum('actual_cost'))['v'] or _ZERO
    net = Decimal(gross_total) - Decimal(discounts_total) - Decimal(refunds_total)

    return {
        'series': series,
        'totals': {
            'gross': _dec(gross_total),
            'discounts': _dec(discounts_total),
            'refunds': _dec(refunds_total),
            'net': _dec(net),
        },
    }


def _build_payment_methods(restaurant_id, date_from, date_to):
    rows = (
        DinifyTransaction.objects.filter(
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            order__restaurant=restaurant_id,
            order__is_test=False,
            order__time_created__gte=date_from,
            order__time_created__lte=date_to,
        )
        .values('payment_mode')
        .annotate(amount=Sum('transaction_amount'), tx_count=Count('id'))
        .order_by('-amount')
    )
    return [
        {
            'method': r['payment_mode'] or 'other',
            'label': PAYMENT_LABEL_MAP.get(r['payment_mode'], r['payment_mode'] or 'Other'),
            'amount': _dec(r['amount']),
            'tx_count': r['tx_count'],
        }
        for r in rows
    ]


def _build_orders(restaurant_id, date_from, date_to, trunc_fn, bucket):
    base = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        time_created__gte=date_from,
        time_created__lte=date_to,
    )
    buckets = (
        base.annotate(bucket=trunc_fn('time_created'))
        .values('bucket')
        .annotate(count=Count('id'))
        .order_by('bucket')
    )
    # Zero-filled onto the same window axis as the revenue series above, so the
    # two cards stay key-for-key aligned and a bucket with no orders reports 0.
    by_bucket = {r['bucket']: r for r in buckets}
    series = [
        {
            'at': boundary.isoformat(),
            'count': (by_bucket.get(boundary) or {}).get('count', 0),
        }
        for boundary in period_boundaries(date_from, date_to, bucket)
    ]

    breakdown = [
        {'status': 'paid', 'count': base.filter(payment_status=PaymentStatus_Paid).count()},
        {'status': 'open', 'count': base.filter(
            payment_status=PaymentStatus_Pending,
        ).exclude(
            order_status__in=[
                OrderStatus_Initiated, OrderStatus_Cancelled, OrderStatus_Refunded,
            ],
        ).count()},
        {'status': 'cancelled', 'count': base.filter(order_status=OrderStatus_Cancelled).count()},
        {'status': 'refunded', 'count': base.filter(order_status=OrderStatus_Refunded).count()},
    ]

    return {
        'series': series,
        'breakdown': breakdown,
        'total': base.count(),
    }


def _build_popular_items(restaurant_id, date_from, date_to):
    rows = (
        OrderItem.objects.filter(
            order__restaurant=restaurant_id,
            order__is_test=False,
            order__time_created__gte=date_from,
            order__time_created__lte=date_to,
        )
        .values('item__id', 'item__name', 'item__image')
        .annotate(revenue=Sum('actual_cost'), qty=Sum('quantity'))
        .order_by('-revenue')[:10]
    )
    results = []
    for r in rows:
        image = r['item__image']
        image_url = f'/media/{image}' if image else None
        results.append({
            'item_id': str(r['item__id']),
            'name': r['item__name'],
            'image_url': image_url,
            'revenue': _dec(r['revenue']),
            'qty': r['qty'] or 0,
        })
    return results


def _build_tables(restaurant_id):
    now = timezone.now()
    today = now.date()
    yesterday = today - timedelta(days=1)

    total = Table.objects.filter(
        restaurant=restaurant_id, enabled=True, deleted=False,
    ).count()

    # DELIBERATELY includes test orders. This is LIVE FLOOR STATE, not history: a
    # rehearsal order really does occupy its table, and this card must agree with the
    # occupancy helpers and the table cards, which also count it. Excluding it here
    # would show a table free while a ticket for it sat on the kitchen board.
    active_orders = Order.objects.filter(
        restaurant=restaurant_id,
        payment_status=PaymentStatus_Pending,
    ).exclude(
        order_status__in=[
            OrderStatus_Initiated, OrderStatus_Cancelled, OrderStatus_Refunded,
        ],
    )
    occupied = active_orders.values('table').distinct().count()
    occupancy_pct = (
        str((Decimal(occupied) * Decimal(100) / Decimal(total)).quantize(Decimal('0.1')))
        if total > 0 else '0.0'
    )

    # Median visit duration for today's closed orders
    closed_today = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        payment_status=PaymentStatus_Paid,
        time_created__date=today,
    )
    durations = []
    for o in closed_today.only('time_created', 'time_last_updated'):
        delta = (o.time_last_updated - o.time_created).total_seconds() / 60
        durations.append(delta)
    median_visit = (
        str(Decimal(str(statistics.median(durations))).quantize(Decimal('0.1')))
        if durations else None
    )

    # Turns
    closed_today_count = closed_today.count()
    closed_yesterday_count = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        payment_status=PaymentStatus_Paid,
        time_created__date=yesterday,
    ).count()
    turns_today = (
        str((Decimal(closed_today_count) / Decimal(total)).quantize(Decimal('0.1')))
        if total > 0 else '0.0'
    )
    turns_yesterday = (
        str((Decimal(closed_yesterday_count) / Decimal(total)).quantize(Decimal('0.1')))
        if total > 0 else '0.0'
    )

    # Avg ticket
    avg_today = closed_today.aggregate(v=Avg('actual_cost'))['v']
    avg_yesterday = Order.objects.filter(
        restaurant=restaurant_id,
        is_test=False,
        payment_status=PaymentStatus_Paid,
        time_created__date=yesterday,
    ).aggregate(v=Avg('actual_cost'))['v']

    return {
        'total': total,
        'occupied': occupied,
        'occupancy_pct': occupancy_pct,
        'median_visit_minutes': median_visit,
        'turns_today': turns_today,
        'turns_yesterday': turns_yesterday,
        'avg_ticket_today': _dec(avg_today),
        'avg_ticket_yesterday': _dec(avg_yesterday),
    }


# Escalation thresholds (minutes) that replace the retired per-ticket
# target_prep_minutes when deriving kitchen stats from the Order fulfilment axis.
KDS_WARNING_MINUTES = 8
KDS_OVERDUE_MINUTES = 15


def _build_kds(restaurant_id):
    now = timezone.now()
    today = now.date()

    # Open = orders still moving through the kitchen fulfilment axis.
    # DELIBERATELY includes test orders, for the same reason as _build_tables: the
    # kitchen genuinely is working a rehearsal ticket, and this card must agree with
    # the kitchen board itself. The fulfilment metrics below share that basis, so the
    # whole KDS card is coherent — open tickets and the time taken to clear them are
    # measured over the same set.
    open_orders = Order.objects.filter(
        restaurant=restaurant_id,
        fulfilment_status__in=['new', 'preparing', 'ready'],
        deleted=False,
    ).exclude(
        order_status__in=[OrderStatus_Initiated, OrderStatus_Cancelled],
    )
    open_count = open_orders.count()

    over_sla = 0
    at_risk = 0
    oldest_minutes = 0
    oldest_ticket_number = None

    for order in open_orders.only('time_created', 'order_number'):
        age_minutes = (now - order.time_created).total_seconds() / 60
        if age_minutes > KDS_OVERDUE_MINUTES:
            over_sla += 1
        elif age_minutes > KDS_WARNING_MINUTES:
            at_risk += 1
        if age_minutes > oldest_minutes:
            oldest_minutes = age_minutes
            oldest_ticket_number = order.order_number

    # Avg fulfillment for orders served today (created -> served duration).
    fulfilled_today = Order.objects.filter(
        restaurant=restaurant_id,
        fulfilment_status='served',
        served_at__date=today,
        deleted=False,
    )
    fulfillment_durations = []
    for o in fulfilled_today.only('time_created', 'served_at'):
        if o.served_at and o.time_created:
            mins = (o.served_at - o.time_created).total_seconds() / 60
            fulfillment_durations.append(mins)

    avg_fulfillment = None
    if fulfillment_durations:
        avg_val = sum(fulfillment_durations) / len(fulfillment_durations)
        avg_fulfillment = str(Decimal(str(avg_val)).quantize(Decimal('0.1')))

    if over_sla >= 3:
        kds_status = 'in_weeds'
    elif over_sla >= 1 or at_risk >= 2:
        kds_status = 'at_risk'
    else:
        kds_status = 'on_track'

    return {
        'open_tickets': open_count,
        'over_sla': over_sla,
        'at_risk': at_risk,
        'avg_fulfillment_minutes': avg_fulfillment,
        # legacy orders may carry order_number=None, so guard on the open count
        'oldest_ticket_minutes': int(oldest_minutes) if open_count else 0,
        'oldest_ticket_number': oldest_ticket_number,
        'status': kds_status,
    }


def generate_restaurant_dashboard_v2(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    bucket: str | None = None,
) -> dict:
    """Build the v2 dashboard payload over ``[date_from, date_to]``.

    :param bucket: REQUIRED — the chart granularity, one of ``hour``, ``day``,
        ``week``, ``month``, ``year`` (see ``BUCKET_TRUNC``). Unknown, absent, empty
        and whitespace-only values are all a 400 naming the accepted values; there is
        no default granularity, because this endpoint bounds neither the date range
        nor the bucket count and so has no defensible one. A ``week`` bucket is keyed
        on the MONDAY of its week in EAT.

    The ``revenue`` and ``orders`` series are DENSE: one row per bucket in the
    requested window, empty ones zeroed, both cards on the same axis. A period that
    traded nothing is a reportable zero, not an absence — omitted, it would make the
    chart join its neighbours into a straight line and imply trading that did not
    happen, and nothing downstream fills the gap. Note this interacts with the
    uncapped bucket count above: the fill makes the bucket count a floor rather than
    a worst case (see REPORTS_CONTRACT_AUDIT.md §9).

    The payload carries ONE window. The preceding-equal-length comparison this used to
    compute server-side (``previous_totals`` / ``previous_total`` / ``previous_series``)
    was removed in DASH-REMOVE-LEGACY-00: the frontend now issues a second call for the
    basis the user actually selected, which the server cannot infer.
    """
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    trunc_fn, bucket_key, bucket_error = _resolve_bucket_trunc(bucket)
    if bucket_error is not None:
        return bucket_error

    return {
        'status': 200,
        'data': {
            'revenue': _build_revenue(
                restaurant_id, date_from, date_to, trunc_fn, bucket_key,
            ),
            'payment_methods': _build_payment_methods(
                restaurant_id, date_from, date_to,
            ),
            'orders': _build_orders(
                restaurant_id, date_from, date_to, trunc_fn, bucket_key,
            ),
            'popular_items': _build_popular_items(
                restaurant_id, date_from, date_to,
            ),
            'tables': _build_tables(restaurant_id),
            'kds': _build_kds(restaurant_id),
        }
    }
