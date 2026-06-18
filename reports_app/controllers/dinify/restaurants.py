import logging
from typing import Optional

logger = logging.getLogger(__name__)

from restaurants_app.models import Restaurant
from orders_app.models import Order


def generate_dinify_restaurant_report(
    date_from: Optional[str],
    date_to: Optional[str],
    name: Optional[str],
) -> dict:
    filters = {}
    if date_from:
        filters['time_created__date__gte'] = date_from
    if date_to:
        filters['time_created__date__lte'] = date_to
    if name:
        filters['name__icontains'] = name

    restaurants = Restaurant.objects.all()
    data = []

    for restaurant in restaurants:
        orders = Order.objects.filter(restaurant=restaurant)

        data.append({
            'id': str(restaurant.id),
            'name': restaurant.name,
            'cum_num_orders': orders.count(),
            'cum_num_diners': orders.values('customer').distinct().count(),
            'cum_order_amount': sum([order.total_cost for order in orders]),
            'owner': f"{restaurant.owner.first_name} {restaurant.owner.last_name}",
        })

    return {
        'status': 200,
        'message': 'Dinify Restaurant Report generated successfully',
        'data': data
    }
