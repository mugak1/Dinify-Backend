import logging
from typing import Optional

logger = logging.getLogger(__name__)

from restaurants_app.models import Restaurant
from finance_app.models import DinifyAccount
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
        restaurant_account = None
        orders = Order.objects.filter(restaurant=restaurant)
        try:
            restaurant_account = DinifyAccount.objects.get(restaurant=restaurant)
        except Exception as error:
            logger.error("Error when getting restaurant account: %s", error)

        data.append({
            'id': str(restaurant.id),
            'name': restaurant.name,
            'cum_num_orders': orders.count(),
            'cum_num_diners': orders.values('customer').distinct().count(),
            'cum_order_amount': sum([order.total_cost for order in orders]),
            'owner': f"{restaurant.owner.first_name} {restaurant.owner.last_name}",
            'momo_actual_balance': restaurant_account.momo_actual_balance if restaurant_account is not None else 0, # noqa
            'momo_available_balance': restaurant_account.momo_available_balance if restaurant_account is not None else 0, # noqa
            'card_actual_balance': restaurant_account.card_actual_balance if restaurant_account is not None else 0, # noqa
            'card_available_balance': restaurant_account.card_available_balance if restaurant_account is not None else 0, # noqa
            'cash_actual_balance': restaurant_account.cash_actual_balance if restaurant_account is not None else 0, # noqa
            'cash_available_balance': restaurant_account.cash_available_balance if restaurant_account is not None else 0, # noqa
        })

    return {
        'status': 200,
        'message': 'Dinify Restaurant Report generated successfully',
        'data': data
    }
