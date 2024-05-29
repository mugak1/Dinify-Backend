from datetime import timedelta
from misc_app.controllers.clean_dates import clean_dates
from misc_app.controllers.report_support_functions import (
    make_graph_series_data,
    make_month_range,
    make_quarter_range,
    make_annual_range
)
from django.db.models import Count, Sum, Avg, Max, Min
from orders_app.models import Order
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    OrderStatus_Served,
    TransactionStatus_Success
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

    orders = None
    transactions = None

    if date_from == date_to:
        orders = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__date=date_to,
            order_status__in=[OrderStatus_Served]
        )
        transactions = DinifyTransaction.objects.filter(
            transaction_type=TransactionType_OrderPayment,
            order__restaurant=restaurant_id,
            order__time_created__date=date_to,
            order__order_status__in=[OrderStatus_Served],
            transaction_status=TransactionStatus_Success
        )
    else:
        orders = Order.objects.filter(
            restaurant=restaurant_id,
            time_created__gte=date_from,
            time_created__lte=date_to,
            order_status__in=[OrderStatus_Served]
        )
        transactions = DinifyTransaction.objects.filter(
            transaction_type=TransactionType_OrderPayment,
            order__restaurant=restaurant_id,
            order__time_created__gte=date_from,
            order__time_created__lte=date_to,
            order__order_status__in=[OrderStatus_Served],
            transaction_status=TransactionStatus_Success
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
        "gross_sales_amount": sales_amount if sales_amount is not None else 0,
        "sales_by_payment_channel": {item['payment_mode']: item['num_sales'] for item in sales_by_payment_channel}, # noqa
        "sales_amount_by_payment_channel": {item['payment_mode']: item['total_amount'] for item in amount_by_payment_channel}, # noqa
        "average_order_amount": avg_order_amount if avg_order_amount is not None else 0,
        "maximum_order_amount": max_order_amount if max_order_amount is not None else 0,
        "minimum_order_amount": min_order_amount if min_order_amount is not None else 0,
        "total_discounts_offered": total_discounts if total_discounts is not None else 0,
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


def generate_restaurant_sales_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_category: str,
    trend_result: str
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates

    date_from = dates.get('date_from')
    date_to = dates.get('date_to')

    if trend_category == 'daily':
        if (date_to - date_from).days > 31:
            return {
                'status': 400,
                'message': 'Date range should not be greater than 31 days.'
            }
        return get_daily_trends(
            restaurant_id=restaurant_id,
            date_from=date_from,
            date_to=date_to,
            trend_result=trend_result
        )
    if trend_category == 'monthly':
        if (date_to - date_from).days > 731:
            return {
                'status': 400,
                'message': 'Date range should not be greater than 2 years.'
            }
        return get_monthly_trends(
            restaurant_id=restaurant_id,
            date_from=date_from,
            date_to=date_to,
            trend_result=trend_result
        )
    if trend_category == 'quarterly':
        if (date_to - date_from).days > 731:
            return {
                'status': 400,
                'message': 'Date range should not be greater than 2 years.'
            }
        return get_quarterly_trends(
            restaurant_id=restaurant_id,
            date_from=date_from,
            date_to=date_to,
            trend_result=trend_result
        )

    if trend_category == 'annual':
        print((date_to - date_from).days)
        if (date_to - date_from).days > 1850:
            return {
                'status': 400,
                'message': 'Date range should not be greater than 5 years.'
            }
        return get_annual_trends(
            restaurant_id=restaurant_id,
            date_from=date_from,
            date_to=date_to,
            trend_result=trend_result
        )


def get_daily_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_result: str
) -> dict:

    x_categories = []
    days = []
    trend_table = []
    trend_graph = []

    day0 = date_from
    while day0 <= date_to:
        days.append(day0)
        x_categories.append(str(day0))
        day0 += timedelta(days=1)

    def get_tabular_trend_data(days: list):
        for day in days:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=day,
                date_to=day
            ).get('data')
            summary['date'] = str(day)
            trend_table.append(summary)

    def get_graph_trend_data(days: list):
        for day in days:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=day,
                date_to=day
            ).get('data')
            summary['date'] = str(day)
            for key, value in summary.get('sales_by_payment_channel').items():
                summary[f'NoSales_{key.title()}'] = value
            for key, value in summary.get('sales_amount_by_payment_channel').items():
                summary[f'SalesAmount_{key.title()}'] = value
            del summary['sales_by_payment_channel']
            del summary['sales_amount_by_payment_channel']
            trend_graph.append(summary)

    if trend_result == 'table':
        get_tabular_trend_data(days)
        return {
            'status': 200,
            'message': 'Successfully retrieved the daily trend data in tabular format.',
            'data': trend_table
        }
    if trend_result == 'graph':
        get_graph_trend_data(days)
        data = make_graph_series_data(
            x_title='Days',
            y_values=trend_graph,
            x_detail='date'
        )
        return {
            'status': 200,
            'message': 'Successfully retrieved the daily trend data in graph series.',
            'data': data
        }


def get_monthly_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_result: str
) -> dict:
    trend_table = []
    trend_graph = []
    month_range = make_month_range(
        start=date_from,
        end=date_to
    )

    def get_tabular_trend_data():
        for month in month_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=month['sd'],
                date_to=month['ed']
            ).get('data')
            name = month['month_name'][:3]
            year = str(month['year'])[2:]
            summary['month'] = f"{name}-{year}"
            trend_table.append(summary)

    def get_graph_trend_data():
        for month in month_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=month['sd'],
                date_to=month['ed']
            ).get('data')
            name = month['month_name'][:3]
            year = str(month['year'])[2:]
            summary['month'] = f"{name}-{year}"
            for key, value in summary.get('sales_by_payment_channel').items():
                summary[f'NoSales_{key.title()}'] = value
            for key, value in summary.get('sales_amount_by_payment_channel').items():
                summary[f'SalesAmount_{key.title()}'] = value
            del summary['sales_by_payment_channel']
            del summary['sales_amount_by_payment_channel']
            trend_graph.append(summary)

    if trend_result == 'table':
        get_tabular_trend_data()
        return {
            'status': 200,
            'message': 'Successfully retrieved the monthly trend data in tabular format.',
            'data': trend_table
        }
    if trend_result == 'graph':
        get_graph_trend_data()
        data = make_graph_series_data(
            x_title='Months',
            y_values=trend_graph,
            x_detail='month'
        )
        return {
            'status': 200,
            'message': 'Successfully retrieved the monthly trend data in graph series.',
            'data': data
        }


def get_quarterly_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_result: str
) -> dict:
    trend_table = []
    trend_graph = []
    quarter_range = make_quarter_range(
        start=date_from.year,
        end=date_to.year
    )

    def get_tabular_trend_data():
        for quarter in quarter_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=quarter['start'],
                date_to=quarter['end']
            ).get('data')
            summary['quarter'] = quarter['quarter']
            trend_table.append(summary)

    def get_graph_trend_data():
        for quarter in quarter_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=quarter['start'],
                date_to=quarter['end']
            ).get('data')
            summary['quarter'] = quarter['quarter']

            for key, value in summary.get('sales_by_payment_channel').items():
                summary[f'NoSales_{key.title()}'] = value
            for key, value in summary.get('sales_amount_by_payment_channel').items():
                summary[f'SalesAmount_{key.title()}'] = value
            del summary['sales_by_payment_channel']
            del summary['sales_amount_by_payment_channel']
            trend_graph.append(summary)

    if trend_result == 'table':
        get_tabular_trend_data()
        return {
            'status': 200,
            'message': 'Successfully retrieved the quarterly trend data in tabular format.',
            'data': trend_table
        }

    if trend_result == 'graph':
        get_graph_trend_data()
        data = make_graph_series_data(
            x_title='Quarters',
            y_values=trend_graph,
            x_detail='quarter'
        )
        return {
            'status': 200,
            'message': 'Successfully retrieved the quarterly trend data in graph series.',
            'data': data
        }


def get_annual_trends(
    restaurant_id: str,
    date_from: str,
    date_to: str,
    trend_result: str
) -> dict:
    trend_table = []
    trend_graph = []
    annual_range = make_annual_range(
        start=date_from.year,
        end=date_to.year
    )

    def get_tabular_trend_data():
        for year in annual_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=year['start'],
                date_to=year['end']
            ).get('data')
            summary['year'] = year['year']
            trend_table.append(summary)

    def get_graph_trend_data():
        for year in annual_range:
            summary = generate_restaurant_sales_summary(
                restaurant_id=restaurant_id,
                date_from=year['start'],
                date_to=year['end']
            ).get('data')
            summary['year'] = year['year']

            for key, value in summary.get('sales_by_payment_channel').items():
                summary[f'NoSales_{key.title()}'] = value
            for key, value in summary.get('sales_amount_by_payment_channel').items():
                summary[f'SalesAmount_{key.title()}'] = value
            del summary['sales_by_payment_channel']
            del summary['sales_amount_by_payment_channel']
            trend_graph.append(summary)

    if trend_result == 'table':
        get_tabular_trend_data()
        return {
            'status': 200,
            'message': 'Successfully retrieved the annual trend data in tabular format.',
            'data': trend_table
        }

    if trend_result == 'graph':
        get_graph_trend_data()
        data = make_graph_series_data(
            x_title='Years',
            y_values=trend_graph,
            x_detail='year'
        )
        return {
            'status': 200,
            'message': 'Successfully retrieved the annual trend data in graph series.',
            'data': data
        }
