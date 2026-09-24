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
from orders_app.controllers.test_orders import counted_orders_q
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED, PRICING_VERSION_LEGACY,
)
from restaurants_app.models import Table
from finance_app.models import DinifyTransaction
from reports_app.controllers.common.bucketing import period_boundaries
from reports_app.controllers.common.sale_filters import SALE_STATUSES, revenue_sum
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


# Whether ANY code path in this backend records a captured payment.
#
# It does not. Order creation seeds ``payment_status='pending'``
# (orders_app/controllers/services/create_order.py) and nothing anywhere ever
# writes ``'paid'`` — the order-payment write path was deleted with the
# custodial teardown and will be rebuilt at PSP integration (REGULATORY_AUDIT.md);
# ``payment_status`` is absent from EDIT_INFORMATION and read-only on every
# serializer that names it. The payment rate below is therefore 0% for every
# restaurant, always, and the figure is a placeholder rather than a measurement.
#
# It ships as a FLAG rather than a silently-zero card because the backend should
# state what it knows and let the client decide whether to render, caveat or hide
# it. FLIP THIS TO True in the same PR that lands the PSP write path — nothing
# else in this module needs to change.
PAYMENT_TRACKING_ENABLED = False


# ---------------------------------------------------------------------------
# Metric definitions
#
# Each metric is defined ONCE, here, as a named predicate over the window's
# orders, and every figure in the payload is derived from that definition rather
# than restating a status list inline. The sale vocabulary is IMPORTED from
# reports_app.controllers.common.sale_filters — this module consumes that
# vocabulary, it never redefines it.
#
# THE DENOMINATOR FOR EVERY RATE IS "ORDERS PLACED", not "every row in the
# window". ``num_sales`` used to be a bare ``orders.count()``: it counted
# abandoned ``initiated`` drafts, cancellations and refunds as sales, and then
# divided the paid / cancelled / refunded percentages by that inflated figure.
# The name said "sales"; the value said "orders" (PHASE_0_5_CLOSURE.md,
# "Reported, not fixed"). Sharing one denominator is also what makes the
# cancellation and refund rates comparable to each other and keeps either from
# exceeding 100%.
# ---------------------------------------------------------------------------


def _orders_placed(orders):
    """Orders that were actually submitted — abandoned ``initiated`` drafts excluded."""
    return orders.exclude(order_status=OrderStatus_Initiated)


def _sales(placed):
    """Orders placed that are revenue-bearing — SALE_STATUSES ({served, paid})."""
    return placed.filter(order_status__in=SALE_STATUSES)


def _cancelled(placed):
    """Orders placed that were cancelled."""
    return placed.filter(order_status=OrderStatus_Cancelled)


def _refunded(placed):
    """Orders placed that were refunded."""
    return placed.filter(order_status=OrderStatus_Refunded)


def _payment_captured(placed):
    """Orders placed whose payment was captured — see PAYMENT_TRACKING_ENABLED."""
    return placed.filter(payment_status=PaymentStatus_Paid)


def _rate(count, placed_count):
    """A share of orders placed, as a percentage to 1dp — 0 when nothing was placed."""
    if not placed_count:
        return 0
    return round((count / placed_count) * 100, 1)


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

    # PRACTICE orders — test orders at a real restaurant, such as a rehearsal before it
    # went live — are excluded from every figure below: this whole report is
    # commercial history (`counted_orders_q`). At a TEST restaurant nothing is
    # excluded; its orders are flagged test and count exactly like a live
    # restaurant's. The live floor and KDS cards in dashboard-v2 include every order
    # anywhere, deliberately — see _build_tables / _build_kds.
    orders = Order.objects.filter(
        counted_orders_q(),
        restaurant=restaurant_id,
        time_created__gte=date_from,
        time_created__lte=date_to
    )
    order_items = OrderItem.objects.filter(
        counted_orders_q('order__'),
        order__restaurant=restaurant_id,
        order__time_created__gte=date_from,
        order__time_created__lte=date_to
    )

    # Every rate below divides by orders PLACED — see the metric definitions above.
    placed = _orders_placed(orders)
    num_orders_placed = placed.count()

    sales = _sales(placed)
    num_sales = sales.count()

    paid_orders = _payment_captured(placed)
    num_paid_orders = paid_orders.count()
    perc_paid_orders = _rate(num_paid_orders, num_orders_placed)

    cancelled_orders = _cancelled(placed)
    num_cancelled_orders = cancelled_orders.count()
    perc_cancelled_orders = _rate(num_cancelled_orders, num_orders_placed)

    refunded_orders = _refunded(placed)
    num_refunded_orders = refunded_orders.count()
    perc_refunded_orders = _rate(num_refunded_orders, num_orders_placed)

    # Revenue over SALES, on sale_filters' canonical basis (Sum('actual_cost') —
    # the amount actually payable, net of discounts). It used to be
    # Sum('total_cost') (pre-discount gross) over payment_status=Paid, which no
    # order ever reaches, so this figure was permanently null.
    sales_amount = sales.aggregate(total=revenue_sum())['total']

    # The diner / item / peak-hour figures below still read the unfiltered
    # `orders` and `order_items`, so they continue to include abandoned drafts.
    # That is DELIBERATE scope, not an oversight: this change defines the sale and
    # rate metrics only. Rebasing the diner counts is a different question (they
    # also collapse every anonymous QR guest into one phantom customer, which the
    # Diners report handles separately) and belongs with that surface.
    new_diners = orders.values('customer').distinct().count()
    repeat_diners = orders.values('customer').annotate(order_count=Count('id')).filter(order_count__gt=1).count()  # noqa
    most_active_diner = orders.values('customer__first_name').annotate(order_count=Count('id')).order_by('-order_count').first()  # noqa

    most_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('-total_quantity').first()  # noqa
    least_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('total_quantity').first()  # noqa

    most_liked_item = None
    least_liked_item = None

    peak_hour = orders.annotate(hour=F('time_created__hour')).values('hour').annotate(order_count=Count('id')).order_by('-order_count').first()  # noqa

    stats = {
        # Sales — revenue-bearing orders only. This key kept its name but CHANGED
        # MEANING: it counted every row in the window, drafts included.
        "num_sales": num_sales,
        # The denominator every percentage below is taken over. Added so the
        # figure the rates are computed against is visible rather than implied.
        "orders_placed": num_orders_placed,
        # False until the PSP write path lands — see PAYMENT_TRACKING_ENABLED.
        # `paid_orders` is a placeholder while this is False, not a measurement.
        "payment_tracking_enabled": PAYMENT_TRACKING_ENABLED,
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
        counted_orders_q(),
        restaurant=restaurant_id,
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
    # `month_growth` USED TO SIT HERE AS THE LITERAL STRING 'up', with the
    # comparison that would have justified it commented out beside it:
    #
    #     'month_growth': 'up'  # if this_month > last_month else 'down'
    #
    # So this helper reported growth for every restaurant in every month,
    # including one that had never taken an order (measured: a restaurant with
    # zero orders returned {'total': 0, 'this_month': 0, 'month_growth': 'up'}).
    # That is a direction asserted from no comparison at all — the same class
    # of claim D07 removed from the cards that read these figures.
    #
    # IT IS REMOVED RATHER THAN GIVEN AN HONEST VALUE, and the consumer search
    # is what decides that: `summarize_revenue` has NO production caller in
    # this repository — it is on no urlconf, in no serializer and in no
    # response, and is reached only from two test modules, neither of which
    # reads this key. There is no wire contract to keep compatible. A trend
    # that is genuinely wanted later gets built against a baseline that can be
    # ABSENT, which is exactly what a bare direction string cannot express.
    return {
        'total': total_revenue if total_revenue is not None else 0,
        'this_month': this_month_revenue if this_month_revenue is not None else 0,
        # THE SAME CONSTANT THE TWO DASHBOARDS PUBLISH, not a second literal.
        # Both figures above aggregate `payment_status='paid'`, so both are
        # permanently zero on live data for exactly the reason v1's
        # `paid_orders` and v2's `revenue` are — and a caller has to be able to
        # tell a zero that was measured from one that was not. Nothing here is
        # repriced, rebased or recomputed: the paid filter stays.
        'payment_tracking_enabled': PAYMENT_TRACKING_ENABLED,
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


#: What the notice says when a window straddles the D02 pricing correction. It
#: is a sentence about COMPARABILITY, deliberately not about correctness: every
#: row is right for the rules it was priced under, and none is rewritten.
MIXED_PRICING_NOTICE = (
    'This period contains orders priced under two different conventions. '
    'Gross and discounts are not directly comparable across the whole period: '
    'before the pricing correction an order\u2019s gross excluded paid modifier '
    'costs and its discount could be negative. The amount each diner actually '
    'paid is unaffected. No order has been repriced.'
)


def _pricing_conventions(legacy, corrected):
    """Disclose WHICH pricing convention produced the gross and discount above.

    THE DEFECT THIS CLOSES IS A REPORTING ONE, not a monetary one. D02 changed
    what two persisted columns MEAN: a CORRECTED order's ``total_cost`` includes
    paid modifier costs and its ``savings`` can never be negative, while a
    LEGACY order's ``total_cost`` excludes them and its ``savings`` could be
    negative (the reference excluded modifiers while the effective included
    them). ``gross`` and ``discounts`` above sum both kinds, and ``net`` is
    derived from both — so a window spanning the deployment reports three
    figures that mix two measurements, with nothing on the response saying so.
    Until now that boundary was described ONLY in ``BREAKING_CHANGES.md`` §13,
    which an operator reading a dashboard never sees.

    NO ORDER IS REPRICED, REWRITTEN OR EXCLUDED. Both conventions stay in the
    totals, because dropping the legacy half would silently understate a real
    trading period — which is a worse answer than a mixed one that says it is
    mixed. The disclosure is the whole remedy.

    ``notice`` is present ONLY when the window actually straddles the boundary.
    A period entirely on one side is not ambiguous and gets no warning: a notice
    that appeared on every response would be ignored on the one that mattered.
    """
    mixed = bool(legacy) and bool(corrected)
    return {
        'mixed': mixed,
        'legacy_orders': legacy,
        'corrected_orders': corrected,
        'notice': MIXED_PRICING_NOTICE if mixed else None,
    }


def _build_revenue(restaurant_id, date_from, date_to, trunc_fn, bucket):
    base = Order.objects.filter(
        counted_orders_q(),
        restaurant=restaurant_id,
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

    # ONE aggregate over `paid`, not three separate round trips. The two money
    # sums were already two queries; folding the convention counts in beside
    # them makes the disclosure below cost NOTHING — the card now runs one
    # FEWER query than before, over the same already tenant-, date- and
    # practice-filtered queryset. No second scan, no unfiltered read.
    totals = paid.aggregate(
        gross=Sum('total_cost'),
        discounts=Sum('savings'),
        legacy=Count('id', filter=Q(pricing_version=PRICING_VERSION_LEGACY)),
        corrected=Count('id', filter=Q(pricing_version=PRICING_VERSION_CORRECTED)),
    )
    gross_total = totals['gross'] or _ZERO
    discounts_total = totals['discounts'] or _ZERO
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
        'pricing_conventions': _pricing_conventions(
            totals['legacy'], totals['corrected']),
    }


def _build_payment_methods(restaurant_id, date_from, date_to):
    rows = (
        DinifyTransaction.objects.filter(
            counted_orders_q('order__'),
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            order__restaurant=restaurant_id,
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
    # ORDERS PLACED, on the same definition as the v1 dashboard: an abandoned
    # `initiated` draft was never submitted and is not an order. This base used to
    # include them, which inflated `total` and the chart series — and made the
    # breakdown unable to sum to `total`, since an initiated order (payment_status
    # 'pending') is excluded from 'open' below and fails 'paid', so it was counted
    # in the total while appearing in none of the four rows.
    base = _orders_placed(Order.objects.filter(
        counted_orders_q(),
        restaurant=restaurant_id,
        time_created__gte=date_from,
        time_created__lte=date_to,
    ))
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
            counted_orders_q('order__'),
            order__restaurant=restaurant_id,
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
        counted_orders_q(),
        restaurant=restaurant_id,
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
        counted_orders_q(),
        restaurant=restaurant_id,
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
        counted_orders_q(),
        restaurant=restaurant_id,
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
            # THE SAME DISCLOSURE v1 CARRIES, ON THE PAYLOAD THE PORTAL ACTUALLY
            # READS (D07). v1 has published `payment_tracking_enabled` since the
            # custodial teardown, but the restaurant dashboard reads v2, so the
            # statement reached nobody.
            #
            # IT GOVERNS THREE FIGURES BELOW, which is why it sits at the top
            # level rather than inside any one card:
            #   * `payment_methods`, which sums `DinifyTransaction` rows of type
            #     order_payment with status success. The order-payment WRITER was
            #     deleted with the custodial teardown, so no such row has been
            #     written since and none can be; the card is `[]` for every
            #     restaurant and every window, permanently.
            #   * `orders.breakdown.paid`, which counts `payment_status='paid'`.
            #     Order creation seeds 'pending' and nothing writes 'paid'.
            #   * `revenue` — THE HEADLINE, and the one most easily missed.
            #     `_build_revenue` aggregates `gross` and `discounts` over
            #     `paid = base.filter(payment_status=PaymentStatus_Paid)`, the
            #     SAME unwritten column, so both are permanently `0.00` in every
            #     bucket and in `totals`. `refunds` is NOT paid-gated
            #     (`order_status='refunded'` is reachable), and
            #     `net = gross - discounts - refunds`, so a window containing a
            #     single refund reports a NEGATIVE net against a zero gross.
            #     `pricing_conventions` counts `pricing_version` over that same
            #     empty set, which is why the D02/C mixed-pricing notice has
            #     never been able to render on a live payload.
            #
            # An empty card, a zero count and a zero-or-negative headline are not
            # measurements — read as ones they say "no payments were settled in
            # this period" and "this restaurant lost money", which are claims
            # about the restaurant's trade rather than about this platform's
            # instrumentation. The flag is what lets a client tell the two apart,
            # and the portal renders the distinction rather than the bare figure.
            #
            # NOTHING IS REPRICED, RECOMPUTED OR SUPPRESSED. The figures stay
            # exactly as they were — this states what they are worth, and does
            # not invent a revenue basis (that is the PSP write path's job, and
            # rebasing `_build_revenue` onto sale orders would be a reporting
            # contract change, not a disclosure).
            #
            # ONE CONSTANT, BOTH PAYLOADS: flip `PAYMENT_TRACKING_ENABLED` in the
            # same PR that lands the PSP write path and v1 and v2 move together.
            'payment_tracking_enabled': PAYMENT_TRACKING_ENABLED,
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
