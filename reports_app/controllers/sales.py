from misc_app.controllers.clean_dates import clean_dates
from django.db.models import Count, Sum, Avg, Max, Min
from orders_app.models import Order
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment
)
from reports_app.serializers import SerializerOrderListingReport

# Number of sales
# Gross sales amount
# Number of sales by payment channel e.g. mobile money, visa, cash
# Gross sales amount by payment channel
# Average order amount
# Maximum order amount
# Minimum order amount
# Total discounts offered


def generate_restaurant_sales_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str,
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
    transactions = DinifyTransaction.objects.filter(
        transaction_type=TransactionType_OrderPayment,
        order__restaurant=restaurant_id,
        order__time_created__gte=date_from,
        order__time_created__lte=date_to,
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


def generate_restaurant_sales_listing(
    restaurant_id: str,
    date_from: str,
    date_to: str
) -> dict:
    # convert the date_from and date_to to actual dates
    # if the date range is greater than 31 days, then reject
    # get the listing of orders
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

    orders = Order.objects.filter(
        restaurant=restaurant_id,
        time_created__gte=date_from,
        time_created__lte=date_to
    )

    records = SerializerOrderListingReport(orders, many=True)
    return {
        'status': 200,
        'message': 'Successfully retrieved the sales listings',
        'data': records.data
    }
