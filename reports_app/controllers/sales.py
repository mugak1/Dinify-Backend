from django.db.models import Count, Sum, Avg, Max, Min
from psycopg import Transaction
from orders_app.models import Order
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment
)

# Number of sales
# Gross sales amount
# Number of sales by payment channel e.g. mobile money, visa, cash
# Gross sales amount by payment channel
# Average order amount
# Maximum order amount
# Minimum order amount
# Total discounts offered


def generate_restaurant_sales_summary(restaurant_id: str) -> dict:
    orders = Order.objects.filter(restaurant=restaurant_id)
    transactions = DinifyTransaction.objects.filter(
        transaction_type=TransactionType_OrderPayment,
        order__restaurant=restaurant_id
    )

    num_sales = orders.count()
    sales_amount = orders.aggregate(total_cost=Sum('total_cost'))['total_cost']

    sales_by_payment_channel = transactions.values('payment_mode').annotate(num_sales=Count('id')).order_by('payment_mode') # noqa
    amount_by_payment_channel = transactions.values('payment_mode').annotate(total_amount=Sum('transaction_amount')).order_by('payment_mode') # noqa

    avg_order_amount = orders.aggregate(avg_amount=Avg('total_cost'))['avg_amount']
    max_order_amount = orders.aggregate(max_amount=Max('total_cost'))['max_amount']
    min_order_amount = orders.aggregate(max_amount=Min('total_cost'))['max_amount']
    total_discounts = orders.aggregate(total_discount=Sum('discounted_cost'))['total_discount']

    stats = {
        "number_of_sales": num_sales,
        "gross_sales_amount": sales_amount,
        "sales_by_payment_channel": {item['payment_mode']: item['num_sales'] for item in sales_by_payment_channel}, # noqa
        "sales_amount_by_payment_channel": {item['payment_mode']: item['total_amount'] for item in amount_by_payment_channel}, # noqa
        "average_order_amount": avg_order_amount,
        "maximum_order_amount": max_order_amount,
        "minimum_order_amount": min_order_amount,
        "total_discounts_offered": total_discounts,
    }

    return {
        'status': 200,
        'message': 'Successfully retrieved the sales summary',
        'data': stats
    }
