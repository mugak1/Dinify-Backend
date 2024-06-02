from django.db.models import Count, Avg, Sum
from misc_app.controllers.clean_dates import clean_dates
from orders_app.models import Order


def generate_restaurant_diners_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    orders = None

    if date_from == date_to:
        orders = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__date=date_from
        )
    else:
        orders = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__gte=date_from,
            time_created__lte=date_to
        )

    new_diners = orders.values('customer').distinct().count()
    repeat_diners = orders.values('customer').annotate(order_count=Count('id')).filter(order_count__gt=1).count()  # noqa
    most_active_diner = orders.values('customer__first_name').annotate(order_count=Count('id')).order_by('-total_cost').first()  # noqa
    # exclude orders with no customer
    customer_orders = orders.exclude(customer__isnull=True)
    average_sales_amount_per_diner = customer_orders.exclude(
        customer__isnull=True
    ).aggregate(Avg('total_cost'))  # noqa

    stats = {
        'new_diners': new_diners,
        'repeat_diners': repeat_diners,
        'most_active_diner': most_active_diner,
        'average_sales_amount_per_diners': average_sales_amount_per_diner
    }

    return {
        'status': 200,
        'message': 'Diners summary generated successfully',
        'data': stats
    }


def generate_restaurant_diners_listing(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates

    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    if (date_to - date_from).days > 31:
        return {
            'status': 400,
            'message': 'Date range cannot be greater than 31 days.'
        }

    # get the list of diners
    orders = Order.objects.filter(
        restaurant=restaurant_id,
        time_created__date__gte=date_from,
        time_created__date__lte=date_to
    ).exclude(customer__isnull=True).values('customer').distinct()

    diners = []
    for order in orders:
        customer = order.get('customer')
        customer_orders = Order.objects.filter(
            restaurant=restaurant_id,
            customer=customer
        )
        diner = {
            'id': customer,
            'name': f"{customer.first_name} {customer.last_name}",
            'phone_number': customer.phone_number,
            'email': customer.email,
            'no_orders': customer_orders.count(),
            'total_spend': customer_orders.aggregate(total_spend=Sum('total_cost')).get('total_spend'),
            'average_spend': customer_orders.aggregate(average_spend=Avg('total_cost')).get('average_spend'),
        }
        diners.append(diner)

    return {
        'status': 200,
        'message': 'Diners listing generated successfully',
        'data': diners
    }
