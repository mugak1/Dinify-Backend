from datetime import datetime, timedelta
from restaurants_app.models import Restaurant, RestaurantEmployee
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from users_app.models import User
from django.db.models import Sum
from dinify_backend.configss.string_definitions import (
    TransactionType_Subscription,
    TransactionType_OrderCharge,

    RestaurantStatus_Pending, RestaurantStatus_Active,
    RestaurantStatus_Inactive, RestaurantStatus_Blocked,
    RestaurantStatus_Rejected,

    OrderStatus_Served, OrderStatus_Preparing,
    PaymentStatus_Paid, PaymentStatus_Pending,
)


def summarize_restaurants():
    restaurants = Restaurant.objects.all()
    statuses = [
        RestaurantStatus_Pending, RestaurantStatus_Active,
        RestaurantStatus_Inactive, RestaurantStatus_Blocked,
        RestaurantStatus_Rejected
    ]
    summary = {'total': restaurants.count()}
    summary['monthly'] = Restaurant.objects.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).count()
    summary['month_growth'] = 'up'
    status_breakdown = {}
    for status in statuses:
        status_breakdown[status] = restaurants.filter(status=status).count()
    summary['status_breakdown'] = status_breakdown

    return summary


def summarize_orders():
    orders = Order.objects.all()
    summary = {'total': orders.count()}
    summary['monthly'] = orders.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).count()
    summary['month_growth'] = 'up'
    status_breakdown = {}
    status_breakdown['closed'] = orders.filter(
        order_status=OrderStatus_Served,
        payment_status=PaymentStatus_Paid
    ).count()
    status_breakdown['open'] = orders.filter(
        order_status__in=[OrderStatus_Served, OrderStatus_Preparing],
        payment_status=PaymentStatus_Pending
    ).count()
    summary['status_breakdown'] = status_breakdown

    return summary


def summarize_users():
    users = User.objects.all()
    summary = {'total': users.count()}
    summary['monthly'] = users.filter(
        date_joined__month=datetime.now().month,
        date_joined__year=datetime.now().year
    ).count()
    summary['month_growth'] = 'up'
    # filter for users where the roles have the word 'dinify' in them
    summary['dinify_staff'] = users.filter(roles__icontains='dinify').count()
    summary['restaurant_staff'] = RestaurantEmployee.objects.filter(active=True).count()
    summary['diners'] = Order.objects.values('customer').distinct().count()
    return summary


def summarize_dinify_earnings():
    dinify_revenue = DinifyTransaction.objects.filter(
        transaction_type__in=[TransactionType_Subscription, TransactionType_OrderCharge]
    )

    cum_dinify_revenue = dinify_revenue.aggregate(Sum('transaction_amount'))['transaction_amount__sum']
    monthly_dinify_revenue = dinify_revenue.filter(
        time_created__month=datetime.now().month,
        time_created__year=datetime.now().year
    ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']

    summary = {'total': cum_dinify_revenue if cum_dinify_revenue else 0.0}
    summary['monthly'] = monthly_dinify_revenue if monthly_dinify_revenue else 0.0
    summary['month_growth'] = 'up'
    cum_subscriptions = dinify_revenue.filter(
        transaction_type=TransactionType_Subscription
    ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']
    cum_surcharge = dinify_revenue.filter(
        transaction_type=TransactionType_OrderCharge
    ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']
    summary['subscriptions'] = cum_subscriptions if cum_subscriptions else 0.0
    summary['surcharge'] = cum_surcharge if cum_surcharge else 0.0

    outstanding_subscriptions = Restaurant.objects.filter(
        status=RestaurantStatus_Blocked,
        preferred_subscription_method__in=['monthly', 'yearly']
    ).aggregate(Sum('flat_fee'))['flat_fee__sum']
    summary['outstanding'] = outstanding_subscriptions if outstanding_subscriptions else 0.0

    return summary


def get_top_restaurants():
    restaurants = Restaurant.objects.all()
    top_restaurants = []
    for restaurant in restaurants:
        orders = Order.objects.filter(restaurant=restaurant)
        total_orders = orders.count()
        total_amount = orders.aggregate(Sum('total_cost'))['total_cost__sum']
        top_restaurants.append({
            'restaurant': restaurant.name,
            'orders': total_orders,
            'amount': total_amount
        })
    top_restaurants = sorted(top_restaurants, key=lambda x: x['orders'], reverse=True)
    return top_restaurants[:5]


def generate_dinify_dashboard() -> dict:
    # restaurants = Restaurant.objects.all()
    # orders = Order.objects.all()
    # dinify_revenue = DinifyTransaction.objects.filter(
    #     transaction_type__in=[TransactionType_Subscription, TransactionType_OrderCharge]
    # ).aggregate(Sum('transaction_amount'))['transaction_amount__sum']

    # cum_num_orders = orders.count()
    # cum_order_amount = orders.aggregate(Sum('total_cost'))['total_cost__sum']

    # no_diners = orders.values('customer').distinct().count()
    # monthly_active_diners = orders.filter(
    #     time_created__gte=datetime.now() - timedelta(days=30)
    # ).values('customer').distinct().count()

    stats = {
        'restaurant_summary': summarize_restaurants(),
        'orders_summary': summarize_orders(),
        'users_summary': summarize_users(),
        'dinify_earnings': summarize_dinify_earnings(),
        'top_restaurants': get_top_restaurants(),

        # 'cum_num_orders': cum_num_orders,
        # 'cum_order_amount': cum_order_amount,
        # 'dinify_revenue': dinify_revenue if dinify_revenue else 0.0,
        # 'no_diners': no_diners,
        # 'monthly_active_diners': monthly_active_diners
    }
    return {
        'status': 200,
        'message': 'Dinify Dashboard details generated successfully',
        'data': {
            'stats': stats,
            'trend': generate_dinify_dashboard_trend()
        }
    }


def generate_dinify_dashboard_trend() -> dict:
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
