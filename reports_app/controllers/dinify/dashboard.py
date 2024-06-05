from datetime import datetime, timedelta
from restaurants_app.models import Restaurant
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from django.db.models import Sum
from dinify_backend.configss.string_definitions import TransactionType_Subscription, TransactionType_OrderCharge
from misc_app.controllers.report_support_functions import make_graph_series_data


def generate_dinify_dashboard():
    restaurants = Restaurant.objects.all()
    orders = Order.objects.all()
    dinify_revenue = DinifyTransaction.objects.filter(
        transaction_type__in=[TransactionType_Subscription, TransactionType_OrderCharge]
    ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']

    new_restaurants = Restaurant.objects.filter(
        time_created__month=datetime.now().month
    )
    cum_num_orders = orders.count()
    cum_order_amount = orders.aggregate(Sum('total_cost'))['total_cost__sum']

    no_diners = orders.values('customer').distinct().count()
    monthly_active_diners = orders.filter(
        time_created__gte=datetime.now() - timedelta(days=30)
    ).values('customer').distinct().count()

    stats = {
        'no_restaurants': restaurants.count(),
        'monthly_new_restaurants': new_restaurants.count(),
        'cum_num_orders': cum_num_orders,
        'cum_order_amount': cum_order_amount,
        'dinify_revenue': dinify_revenue if dinify_revenue else 0.0,
        'no_diners': no_diners,
        'monthly_active_diners': monthly_active_diners
    }
    return {
        'status': 200,
        'message': 'Dinify Dashboard details generated successfully',
        'data': {
            'stats': stats,
            'trend': generate_dinify_dashboard_trend()
        }
    }


def generate_dinify_dashboard_trend():
    # get the last 7 days
    date_today = datetime.now().date()
    date_from = date_today - timedelta(days=7)
    date_to = date_today
    days = []
    x_categories = []
    day0 = date_from

    while day0 <= date_to:
        days.append(day0)
        x_categories.append(str(day0))
        day0 += timedelta(days=1)

    new_restaurants = []
    new_diners = []
    num_orders = []
    order_amounts = []
    dinify_revenue = []

    for day in days:
        new_restaurants.append(
            Restaurant.objects.filter(
                time_created__date=day
            ).count()
        )
        new_diners.append(
            Order.objects.filter(
                time_created__date=day
            ).values('customer').distinct().count()
        )
        num_orders.append(
            Order.objects.filter(
                time_created__date=day
            ).count()
        )
        order_amount = Order.objects.filter(
            time_created__date=day
        ).aggregate(Sum('total_cost'))['total_cost__sum']
        if order_amount is None:
            order_amount = 0.0
        order_amounts.append(order_amount)

        revenue = DinifyTransaction.objects.filter(
            time_created__date=day,
            transaction_type__in=[TransactionType_Subscription, TransactionType_OrderCharge]
        ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']
        if revenue is None:
            revenue = 0.0
        dinify_revenue.append(revenue)

    trend_data = {
        'series': [
            {'name': 'New Restaurants', 'data': new_restaurants},
            {'name': 'New Diners', 'data': new_diners},
            {'name': 'Number of Orders', 'data': num_orders},
            {'name': 'Order Amounts', 'data': order_amounts},
            {'name': 'Dinify Revenue', 'data': dinify_revenue}
        ],
        'xaxis': {
            'categories': x_categories,
            'title': 'Date'
        }
    }

    return trend_data
