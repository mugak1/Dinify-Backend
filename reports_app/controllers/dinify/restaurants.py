# This report shows the list of restaurants using Dinify. The report listing shows:
# Restaurant name
# Cumulative number of orders
# Cumulative Diners
# Cumulative revenue earned from the restaurant
# Contact Person
# Cumulative order amount
# Current Restaurant Actual Balance
# Current Restaurant Available Balance

# Clicking on the restaurant should open the detailed reports specific to that restaurant, similar to the Sales and Transactions reports.

# This report allows for filters based on attributes such as the name and registration date.
from typing import Optional
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
            print(f"Error when getting restaurant account: {error}")
            pass

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
