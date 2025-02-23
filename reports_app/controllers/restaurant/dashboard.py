from datetime import datetime
from misc_app.controllers.clean_dates import clean_dates
from django.db.models import Count, Sum, Avg, F  # noqa
from orders_app.models import Order, OrderItem
from restaurants_app.models import Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Served, PaymentStatus_Paid, OrderStatus_Cancelled,
    OrderStatus_Refunded, OrderStatus_Preparing, OrderStatus_Pending,
    PaymentStatus_Pending
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
    # this_month_orders = orders.filter(
    #     time_created__month=datetime.now().month
    # ).count()
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

    order_items = OrderItem.objects.filter(
        order__in=[order.id for order in closed_orders]
    )
    most_popular_items = order_items.values('item__name').annotate(
        total_quantity=Sum('quantity')
    ).order_by('-total_quantity')[:3]
    top_customers_by_revenue = closed_orders.values('customer').annotate(
        total_spent=Sum('actual_cost')
    ).order_by('-total_spent')[:3]
    top_customers_by_orders = closed_orders.values('customer').annotate(
        total_orders=Count('id')
    ).order_by('-total_orders')[:3]

    diners = closed_orders.values('customer').distinct().count()
    monthly_diners = closed_orders.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).values('customer').distinct().count()

    return {
        'num_orders': num_orders,
        # 'this_month_orders': this_month_orders,
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
            'monthly': monthly_diners
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
