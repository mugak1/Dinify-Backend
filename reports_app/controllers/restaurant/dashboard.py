from datetime import datetime, timedelta
from decimal import Decimal
import statistics

from misc_app.controllers.clean_dates import clean_dates
from django.db.models import Count, Sum, Avg, F, Q  # noqa
from django.db.models.functions import TruncHour, TruncDay, TruncMonth
from django.utils import timezone

from orders_app.models import Order, OrderItem, KitchenTicket
from restaurants_app.models import Table
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    OrderStatus_Served, PaymentStatus_Paid, OrderStatus_Cancelled,
    OrderStatus_Refunded, OrderStatus_Preparing, OrderStatus_Pending,
    PaymentStatus_Pending,
    TransactionType_OrderPayment, TransactionStatus_Success,
    KdsStatus_New, KdsStatus_InPrep, KdsStatus_Ready, KdsStatus_Fulfilled,
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

    orders = Order.objects.filter(
        restaurant=restaurant_id,
        time_created__gte=date_from,
        time_created__lte=date_to
    )
    order_items = OrderItem.objects.filter(
        order__restaurant=restaurant_id,
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
        payment_status=PaymentStatus_Paid
    )
    total_revenue = orders.aggregate(total_revenue=Sum('actual_cost'))['total_revenue']
    this_month_revenue = orders.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
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


def summarize_orders(restaurant_id: str):
    orders = Order.objects.filter(
        restaurant=restaurant_id
    )
    num_orders = orders.count()
    this_month_orders = orders.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).count()
    # last_month_orders = orders.filter(
    #     time_created__month=datetime.now().month - 1
    # ).count()

    closed_orders = orders.filter(
        order_status=OrderStatus_Served,
        payment_status=PaymentStatus_Paid
    )
    cancelled_orders = orders.filter(
        order_status__in=[OrderStatus_Cancelled, OrderStatus_Refunded]
    ).count()

    active_orders = orders.filter(
        order_status__in=[OrderStatus_Served, OrderStatus_Preparing, OrderStatus_Pending],
        payment_status=PaymentStatus_Pending
    )
    occupied_tables = active_orders.values('table').distinct().count()
    total_tables = Table.objects.filter(restaurant=restaurant_id).count()
    distinct_ordered_items = OrderItem.objects.filter(
        order__in=[order.id for order in active_orders]
    ).distinct().count()

    # orders | closed_orders
    order_items = OrderItem.objects.filter(
        order__in=[order.id for order in orders]
    )
    most_popular_items = order_items.values('item__name').annotate(
        total_quantity=Sum('quantity')
    ).order_by('-total_quantity')[:3]
    top_customers_by_revenue = orders.values('customer').annotate(
        total_spent=Sum('actual_cost')
    ).order_by('-total_spent')[:3]
    top_customers_by_orders = orders.values(
        'customer__first_name',
        'customer__last_name',
        'customer__username'
    ).exclude(
        customer__isnull=True
    ).annotate(
        total_orders=Count('id')
    ).order_by('-total_orders')[:3]
    # === end dummy content

    diners = closed_orders.values('customer').distinct().count()
    monthly_diners = closed_orders.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).values('customer').distinct().count()

    return {
        'num_orders': num_orders,
        'this_month_orders': this_month_orders,
        # 'last_month_orders': last_month_orders,
        'month_growth': 'up',  # if this_month_orders > last_month_orders else 'down'
        'order_count_overview': {
            'total': num_orders,
            'closed': closed_orders.count(),
            'cancelled': cancelled_orders,
        },
        'real_time': {
            'active': active_orders.count(),
            'occupied_tables': occupied_tables,
            'total_tables': total_tables,
            'distinct_order_items': distinct_ordered_items
        },
        'top_items': most_popular_items,
        'top_customers': {
            'by_revenue': top_customers_by_revenue,
            'by_orders': top_customers_by_orders
        },
        'diners': {
            'total': diners,
            'monthly': monthly_diners,
            'monthly_growth': 'up'
        }
    }


def get_restaurant_dashboard_1(restaurant_id: str):
    return {
        'status': 200,
        'message': 'Successfully retrieved the restaurant dashboard',
        'data': {
            'revenue': summarize_revenue(restaurant_id),
            'orders': summarize_orders(restaurant_id)
        }
    }


# ---------------------------------------------------------------------------
# Dashboard V2
# ---------------------------------------------------------------------------

TRUNC_MAP = {
    'day': TruncHour,
    'week': TruncDay,
    'month': TruncDay,
    'ytd': TruncMonth,
}

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


def _build_revenue(restaurant_id, date_from, date_to, trunc_fn,
                   prev_from, prev_to):
    def _series_and_totals(d_from, d_to):
        base = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__gte=d_from,
            time_created__lte=d_to,
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

        series = []
        for row in paid_buckets:
            at = row['bucket'].isoformat() if row['bucket'] else None
            series.append({
                'at': at,
                'gross': _dec(row['gross']),
                'discounts': _dec(row['discounts']),
                'refunds': _dec(refund_map.get(row['bucket'], _ZERO)),
            })

        gross_total = paid.aggregate(v=Sum('total_cost'))['v'] or _ZERO
        discounts_total = paid.aggregate(v=Sum('savings'))['v'] or _ZERO
        refunds_total = refunded.aggregate(v=Sum('actual_cost'))['v'] or _ZERO
        net = Decimal(gross_total) - Decimal(discounts_total) - Decimal(refunds_total)

        totals = {
            'gross': _dec(gross_total),
            'discounts': _dec(discounts_total),
            'refunds': _dec(refunds_total),
            'net': _dec(net),
        }
        return series, totals

    series, totals = _series_and_totals(date_from, date_to)
    prev_series, prev_totals = _series_and_totals(prev_from, prev_to)
    return {
        'series': series,
        'previous_series': prev_series,
        'totals': totals,
        'previous_totals': prev_totals,
    }


def _build_payment_methods(restaurant_id, date_from, date_to):
    rows = (
        DinifyTransaction.objects.filter(
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


def _build_orders(restaurant_id, date_from, date_to, trunc_fn,
                  prev_from, prev_to):
    def _series_and_total(d_from, d_to):
        base = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__gte=d_from,
            time_created__lte=d_to,
        )
        buckets = (
            base.annotate(bucket=trunc_fn('time_created'))
            .values('bucket')
            .annotate(count=Count('id'))
            .order_by('bucket')
        )
        series = [
            {'at': r['bucket'].isoformat() if r['bucket'] else None, 'count': r['count']}
            for r in buckets
        ]
        return series, base.count()

    series, total = _series_and_total(date_from, date_to)
    prev_series, prev_total = _series_and_total(prev_from, prev_to)

    base = Order.objects.filter(
        restaurant=restaurant_id,
        time_created__gte=date_from,
        time_created__lte=date_to,
    )
    breakdown = [
        {'status': 'paid', 'count': base.filter(payment_status=PaymentStatus_Paid).count()},
        {'status': 'open', 'count': base.filter(
            payment_status=PaymentStatus_Pending,
        ).exclude(
            order_status__in=[OrderStatus_Cancelled, OrderStatus_Refunded],
        ).count()},
        {'status': 'cancelled', 'count': base.filter(order_status=OrderStatus_Cancelled).count()},
        {'status': 'refunded', 'count': base.filter(order_status=OrderStatus_Refunded).count()},
    ]

    return {
        'series': series,
        'previous_series': prev_series,
        'breakdown': breakdown,
        'total': total,
        'previous_total': prev_total,
    }


def _build_popular_items(restaurant_id, date_from, date_to):
    rows = (
        OrderItem.objects.filter(
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

    active_orders = Order.objects.filter(
        restaurant=restaurant_id,
        payment_status=PaymentStatus_Pending,
    ).exclude(
        order_status__in=[OrderStatus_Cancelled, OrderStatus_Refunded],
    )
    occupied = active_orders.values('table').distinct().count()
    occupancy_pct = (
        str((Decimal(occupied) * Decimal(100) / Decimal(total)).quantize(Decimal('0.1')))
        if total > 0 else '0.0'
    )

    # Median visit duration for today's closed orders
    closed_today = Order.objects.filter(
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


def _build_kds(restaurant_id):
    now = timezone.now()
    today = now.date()

    open_tickets = KitchenTicket.objects.filter(
        restaurant=restaurant_id,
        status__in=[KdsStatus_New, KdsStatus_InPrep, KdsStatus_Ready],
        deleted=False,
    )
    open_count = open_tickets.count()

    over_sla = 0
    at_risk = 0
    oldest_minutes = 0
    oldest_ticket_number = None

    for ticket in open_tickets.only('placed_at', 'target_prep_minutes', 'ticket_number'):
        age_minutes = (now - ticket.placed_at).total_seconds() / 60
        if age_minutes > ticket.target_prep_minutes:
            over_sla += 1
        elif age_minutes > ticket.target_prep_minutes * Decimal('0.8'):
            at_risk += 1
        if age_minutes > oldest_minutes:
            oldest_minutes = age_minutes
            oldest_ticket_number = ticket.ticket_number

    # Avg fulfillment for today's fulfilled tickets
    fulfilled_today = KitchenTicket.objects.filter(
        restaurant=restaurant_id,
        status=KdsStatus_Fulfilled,
        fulfilled_at__date=today,
        deleted=False,
    )
    fulfillment_durations = []
    for t in fulfilled_today.only('placed_at', 'fulfilled_at'):
        if t.fulfilled_at and t.placed_at:
            mins = (t.fulfilled_at - t.placed_at).total_seconds() / 60
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
        'oldest_ticket_minutes': int(oldest_minutes) if oldest_ticket_number else 0,
        'oldest_ticket_number': oldest_ticket_number,
        'status': kds_status,
    }


def generate_restaurant_dashboard_v2(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    period: str = 'day',
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    trunc_fn = TRUNC_MAP.get(period, TruncHour)

    # Compute previous period of equal length
    delta = (date_to - date_from) + timedelta(days=1)
    prev_from = date_from - delta
    prev_to = date_from - timedelta(days=1)

    return {
        'status': 200,
        'data': {
            'revenue': _build_revenue(
                restaurant_id, date_from, date_to, trunc_fn, prev_from, prev_to,
            ),
            'payment_methods': _build_payment_methods(
                restaurant_id, date_from, date_to,
            ),
            'orders': _build_orders(
                restaurant_id, date_from, date_to, trunc_fn, prev_from, prev_to,
            ),
            'popular_items': _build_popular_items(
                restaurant_id, date_from, date_to,
            ),
            'tables': _build_tables(restaurant_id),
            'kds': _build_kds(restaurant_id),
        }
    }


# ---------------------------------------------------------------------------
# Reviews Summary
# ---------------------------------------------------------------------------

def generate_restaurant_reviews_summary(restaurant_id: str) -> dict:
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)

    reviewed_30d = Order.objects.filter(
        restaurant=restaurant_id,
        rating__isnull=False,
        time_created__gte=thirty_days_ago,
    )

    total_30d = reviewed_30d.count()

    # Average rating
    avg_raw = reviewed_30d.aggregate(v=Avg('rating'))['v']
    avg_rating = (
        str(Decimal(str(avg_raw)).quantize(Decimal('0.1')))
        if avg_raw is not None else '0.0'
    )

    # Distribution (ensure all 5 star levels present)
    dist_qs = (
        reviewed_30d.values('rating')
        .annotate(count=Count('id'))
    )
    dist_map = {r['rating']: r['count'] for r in dist_qs}
    distribution = [
        {'stars': s, 'count': dist_map.get(s, 0)}
        for s in range(5, 0, -1)
    ]

    # Recent reviews (last 3 with a rating, across all time)
    recent_qs = (
        Order.objects.filter(
            restaurant=restaurant_id,
            rating__isnull=False,
        )
        .order_by('-time_created')[:3]
    )
    recent_reviews = [
        {
            'order_id': str(o.id),
            'order_number': o.order_number,
            'rating': o.rating,
            'text': o.review or '',
            'created_at': o.time_created.isoformat() if o.time_created else None,
        }
        for o in recent_qs
    ]

    # Low rating share (1-2 stars)
    if total_30d > 0:
        low_count = reviewed_30d.filter(rating__lte=2).count()
        low_share = str(
            (Decimal(low_count) * Decimal(100) / Decimal(total_30d))
            .quantize(Decimal('0.1'))
        )
    else:
        low_share = '0.0'

    return {
        'status': 200,
        'data': {
            'avg_rating_30d': avg_rating,
            'total_reviews_30d': total_30d,
            'distribution': distribution,
            'recent_reviews': recent_reviews,
            'low_rating_share_30d': low_share,
        }
    }
