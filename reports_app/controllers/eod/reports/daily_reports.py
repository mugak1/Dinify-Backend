import pandas
from datetime import date
from dinify_backend.mongo_db import MONGO_DB
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    OrderStatus_Refunded,
    OrderStatus_Cancelled,
    PaymentStatus_Paid,
    PaymentStatus_Pending
)


def make_orders_report(orders: list) -> dict:
    summary = {}
    # load the orders into a pandas DataFrame
    df_orders = pandas.DataFrame(orders)

    if not df_orders.empty:
        # to get the number of sales, check for orders where statues_at_eod.order_status is not None
        daily_sales = df_orders[df_orders['statuses_at_eod'].apply(
            lambda x: x.get('order_status') is not None
        )]
        count_daily_sales = daily_sales.shape[0]
        summary['count_daily_sales'] = count_daily_sales
        prospective_sales_amount = daily_sales['total_cost'].sum()
        summary['total_sales_amount'] = prospective_sales_amount if not pandas.isna(prospective_sales_amount) else 0.0
    else:
        summary['count_daily_sales'] = 0
        summary['total_sales_amount'] = 0.0

    # report on order statuses
    order_statuses = [
        OrderStatus_Initiated,
        OrderStatus_Pending,
        OrderStatus_Preparing,
        OrderStatus_Served,
        OrderStatus_Refunded,
        OrderStatus_Cancelled,
    ]

    for status in order_statuses:
        if df_orders.empty:
            summary[f'stats_by_orderstatus_count_{status.lower()}'] = 0
            summary[f'stats_by_orderstatus_amount_{status.lower()}'] = 0.0
            summary[f'stats_by_orderstatus_percentage_{status.lower()}'] = 0.0
        else:
            status_orders = df_orders['statuses_at_eod'].apply(
                lambda x: x.get('order_status') == status
            )
            status_orders = df_orders[status_orders]
            if status_orders.empty:
                count = status_orders.shape[0]
                amount = status_orders['total_cost'].sum() if not status_orders.empty else 0.0
                percentage = (count / count_daily_sales) * 100
                summary[f'stats_by_orderstatus_count_{status.lower()}'] = count
                summary[f'stats_by_orderstatus_amount_{status.lower()}'] = amount
                summary[f'stats_by_orderstatus_percentage_{status.lower()}'] = round(percentage, 2)
            else:
                summary[f'stats_by_orderstatus_count_{status.lower()}'] = 0
                summary[f'stats_by_orderstatus_amount_{status.lower()}'] = 0.0
                summary[f'stats_by_orderstatus_percentage_{status.lower()}'] = 0.0

    # report on payment statuses
    payment_statuses = [PaymentStatus_Paid, PaymentStatus_Pending]
    for status in payment_statuses:
        if df_orders.empty:
            summary[f'stats_by_paymentstatus_count_{status.lower()}'] = 0
            summary[f'stats_by_paymentstatus_amount_{status.lower()}'] = 0.0
            summary[f'stats_by_paymentstatus_percentage_{status.lower()}'] = 0.0
        else:
            # filter for orders where statuses_at_eod.payment_status is equal to the status
            status_orders = df_orders['statuses_at_eod'].apply(
                lambda x: x.get('payment_status') == status
            )
            status_orders = df_orders[status_orders]
            if status_orders.empty is False:
                count = status_orders.shape[0]
                amount = status_orders['total_cost'].sum() if not status_orders.empty else 0.0
                percentage = (count / count_daily_sales) * 100 if count_daily_sales > 0 else 0.0
                summary[f'stats_by_paymentstatus_count_{status.lower()}'] = count
                summary[f'stats_by_paymentstatus_amount_{status.lower()}'] = amount
                summary[f'stats_by_paymentstatus_percentage_{status.lower()}'] = round(percentage, 2)
            else:
                summary[f'stats_by_paymentstatus_count_{status.lower()}'] = 0
                summary[f'stats_by_paymentstatus_amount_{status.lower()}'] = 0.0
                summary[f'stats_by_paymentstatus_percentage_{status.lower()}'] = 0.0
    # maintain 2dps for all values
    for key in summary:
        value = float(summary[key])
        summary[key] = round(value, 2)

    return summary


def make_order_items_report(orders: list) -> dict:
    summary = {}
    order_ids = [order['id'] for order in orders]
    order_items = MONGO_DB['archive_order_items'].find(
        {'order': {'$in': order_ids}}
    )
    order_items = list(order_items)
    df_order_items = pandas.DataFrame(order_items)

    # find the most ordered item for the day
    if not df_order_items.empty:
        most_ordered_item = df_order_items['item'].value_counts().idxmax()
        summary['most_ordered_item'] = most_ordered_item

        least_ordered_item = df_order_items['item'].value_counts().idxmin()
        summary['least_ordered_item'] = least_ordered_item
    else:
        summary['most_ordered_item'] = None
        summary['least_ordered_item'] = None

    # add extra items
    if df_order_items.empty:
        summary['distinct_items_ordered'] = 0
        summary['average_items_per_order'] = 0.0
        summary['min_qty_items_ordered'] = 0
        summary['max_qty_items_ordered'] = 0
        summary['total_items_ordered'] = 0
    else:
        # count of distinct items ordered
        distinct_items = df_order_items['item'].nunique()
        summary['distinct_items_ordered'] = distinct_items

        # count of average items per order
        average_items_per_order = df_order_items.groupby('order').size().mean()
        summary['average_items_per_order'] = round(average_items_per_order, 2)

        # get the total number of items ordered
        total_items_ordered = df_order_items['quantity'].sum()
        summary['total_qty_items_ordered'] = total_items_ordered

        #  get the average quantity of items ordered
        average_qty_items_ordered = df_order_items['quantity'].mean()
        summary['average_qty_items_ordered'] = round(average_qty_items_ordered, 2)

        # get the minimum quantity of items ordered
        min_qty_items_ordered = df_order_items['quantity'].min()
        summary['min_qty_items_ordered'] = min_qty_items_ordered

        # get the maximum quantity of items ordered
        max_qty_items_ordered = df_order_items['quantity'].max()
        summary['max_qty_items_ordered'] = max_qty_items_ordered

    # maintain 2dps for all values
    for key in summary:
        try:
            value = float(summary[key])
            summary[key] = round(value, 2)
        except (ValueError, TypeError):
            summary[key] = summary[key]
    return summary


def generate_restaurant_daily_report(restaurant_id: int, eod_date: date) -> None:
    eod_date = '2025-05-31'
    orders = MONGO_DB['archive_orders'].find(
        {'restaurant': restaurant_id, 'eod_record_date': str(eod_date)}
    )
    orders = list(orders)
    report = {
        'restaurant_id': restaurant_id,
        'eod_date': str(eod_date)
    }
    # to the report, add the result from make_orders_report
    # orders_report = make_orders_report(orders)
    order_items_report = make_order_items_report(orders)
    # report.update(orders_report)
    report.update(order_items_report)
    print(f"\n\n{restaurant_id}\n{report}\n\n")
