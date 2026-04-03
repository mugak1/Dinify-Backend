import logging
from decimal import Decimal
from operator import le
import pandas

logger = logging.getLogger(__name__)
from datetime import date, datetime
from dinify_backend.mongo_db import MONGO_DB
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    OrderStatus_Refunded,
    OrderStatus_Cancelled,
    PaymentStatus_Paid,
    PaymentStatus_Pending,

    TransactionType_OrderPayment,
    TransactionType_OrderRefund,
    TransactionType_OrderCharge,
    TransactionType_Disbursement,
    TransactionType_Subscription,
    TransactionType_Tip,

    TransactionStatus_Success,
    TransactionStatus_Failed,
    TransactionStatus_Pending,
    TransactionStatus_Initiated
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
        summary[key] = Decimal(str(summary[key])).quantize(Decimal('0.01'))

    return summary


def make_order_items_report(orders: list) -> dict:
    summary = {}
    order_ids = [order['id'] for order in orders]
    try:
        order_items = MONGO_DB['archive_order_items'].find(
            {'order': {'$in': order_ids}}
        )
        order_items = list(order_items)
    except Exception as e:
        logger.error("Failed to query archive_order_items: %s", e)
        order_items = []
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
            summary[key] = Decimal(str(summary[key])).quantize(Decimal('0.01'))
        except Exception:
            pass
    return summary


def report_on_customer_behaviour(orders: list) -> dict:
    summary = {}
    # load the orders into a pandas DataFrame
    df_orders = pandas.DataFrame(orders)

    if df_orders.empty:
        return {
            'distinct_customers': 0,
            'count_returning_customers': 0,
            'percentage_returning_customers': 0,
            'amount_paid_by_returning_customers': 0,
            'count_new_customers': 0,
            'percentage_new_customers': 0,
            'amount_paid_by_new_customers': 0,
            'top_customer_by_amount': None,
            'top_customer_by_no_orders': None
        }

    #  get the distinct customers
    distinct_customers = df_orders['customer'].nunique()
    summary['distinct_customers'] = distinct_customers

    # to get the returning customers, orders where anl_days_since_last_customer_order_at_restaurant is greater than 0 and not None
    returning_customers = df_orders['anl_days_since_last_customer_order_at_restaurant'].apply(
        lambda x: x is not None and x > 0
    )
    returning_customers = df_orders[returning_customers]
    count_returning_customers = returning_customers['customer'].nunique()
    percentage_returning_customers = (count_returning_customers / distinct_customers) * 100

    # amount from returning customers
    paid_orders_by_returning_customers = returning_customers['statuses_at_eod'].apply(
        lambda x: x.get('payment_status') == PaymentStatus_Paid and x.get('order_status') != OrderStatus_Cancelled
    )
    paid_orders_by_returning_customers = returning_customers[paid_orders_by_returning_customers]
    amount_paid_by_returning_customers = paid_orders_by_returning_customers['total_cost'].sum()

    # to get the new customers, orders where anl_days_since_last_customer_order_at_restaurant is None or 0
    new_customers = df_orders['anl_days_since_last_customer_order_at_restaurant'].apply(
        lambda x: x is None or x == 0
    )
    new_customers = df_orders[new_customers]
    count_new_customers = new_customers['customer'].nunique()
    percentage_new_customers = (count_new_customers / distinct_customers) * 100
    paid_orders_by_new_customers = new_customers['statuses_at_eod'].apply(
        lambda x: x.get('payment_status') == PaymentStatus_Paid and x.get('order_status') != OrderStatus_Cancelled
    )
    paid_orders_by_new_customers = new_customers[paid_orders_by_new_customers]
    amount_paid_by_new_customers = paid_orders_by_new_customers['total_cost'].sum()

    # top customer by amount
    top_customer_by_amount = df_orders.groupby('customer')['total_cost'].sum().idxmax()
    top_customer_by_no_orders = df_orders.groupby('customer').size().idxmax()

    summary = {
        'distinct_customers': distinct_customers,
        'count_returning_customers': count_returning_customers,
        'percentage_returning_customers': round(percentage_returning_customers, 2),
        'amount_paid_by_returning_customers': Decimal(str(amount_paid_by_returning_customers)).quantize(Decimal('0.01')),
        'count_new_customers': count_new_customers,
        'percentage_new_customers': round(percentage_new_customers, 2),
        'amount_paid_by_new_customers': Decimal(str(amount_paid_by_new_customers)).quantize(Decimal('0.01')),
        'top_customer_by_amount': top_customer_by_amount,
        'top_customer_by_no_orders': top_customer_by_no_orders
    }

    return summary


def report_on_transactions(restaurant_id: str, eod_date: date) -> dict:
    summary = {}
    # get all the accounts associated with the restaurant
    try:
        accounts = MONGO_DB['archive_accounts'].find(
            filter={'restaurant': restaurant_id},
            projection={'id': 1, '_id': 0}
        )
        account_ids = [account['id'] for account in accounts]
    except Exception as e:
        logger.error("Failed to query archive_accounts for restaurant %s: %s", restaurant_id, e)
        return summary

    # get all the transactions associated with the accounts for the restaurant
    try:
        transactions = MONGO_DB['archive_transactions'].find({
            '$or': [
                {'account': {'$in': account_ids}},
                {'restaurant': restaurant_id}
            ],
            'eod_record_date': str(eod_date)
        })
    except Exception as e:
        logger.error("Failed to query archive_transactions for restaurant %s: %s", restaurant_id, e)
        return summary

    transactions = list(transactions)
    df_transactions = pandas.DataFrame(transactions)

    if df_transactions.empty:
        summary['no_transactions'] = 0

    no_transactions = df_transactions.shape[0]

    # report on each transaction type
    transaction_types = [
        TransactionType_OrderPayment,
        TransactionType_OrderRefund,
        TransactionType_OrderCharge,
        TransactionType_Disbursement,
        TransactionType_Subscription,
        TransactionType_Tip
    ]

    transaction_statuses = [
        TransactionStatus_Success,
        TransactionStatus_Failed,
        TransactionStatus_Pending,
        TransactionStatus_Initiated
    ]

    for transaction_type in transaction_types:
        if df_transactions.empty:
            summary[f'stats_by_transactiontype_count_{transaction_type.lower()}'] = 0
            summary[f'stats_by_transactiontype_amount_{transaction_type.lower()}'] = 0.0
            summary[f'stats_by_transactiontype_percentage_{transaction_type.lower()}'] = 0.0
        else:
            type_transactions = df_transactions[df_transactions['transaction_type'] == transaction_type]
            logger.debug("Transaction type shape: %s", type_transactions.shape)
            count = type_transactions.shape[0]
            amount = type_transactions['transaction_amount'].sum() if not type_transactions.empty else 0.0
            percentage = (count / no_transactions) * 100

            summary[f'stats_by_transactiontype_count_{transaction_type.lower()}'] = count
            summary[f'stats_by_transactiontype_amount_{transaction_type.lower()}'] = amount
            summary[f'stats_by_transactiontype_percentage_{transaction_type.lower()}'] = percentage

        # summarize by transaction status for each transaction type
        for transaction_status in transaction_statuses:
            if df_transactions.empty:
                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_count_{transaction_status.lower()}'] = 0
                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_amount_{transaction_status.lower()}'] = 0.0
                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_percentage_{transaction_status.lower()}'] = 0.0
            else:
                status_transactions = type_transactions[type_transactions['transaction_status'] == transaction_status]
                status_count = status_transactions.shape[0]
                status_amount = status_transactions['transaction_amount'].sum() if not status_transactions.empty else 0.0
                status_percentage = (status_count / count) * 100 if count > 0 else 0.0

                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_count_{transaction_status.lower()}'] = status_count
                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_amount_{transaction_status.lower()}'] = status_amount
                summary[f'stats_by_transactiontype_status_{transaction_type.lower()}_percentage_{transaction_status.lower()}'] = status_percentage

    for transaction_status in transaction_statuses:
        if df_transactions.empty:
            summary[f'stats_by_transactionstatus_count_{transaction_status.lower()}'] = 0
            summary[f'stats_by_transactionstatus_amount_{transaction_status.lower()}'] = 0.0
            summary[f'stats_by_transactionstatus_percentage_{transaction_status.lower()}'] = 0.0
        else:
            status_transactions = df_transactions[df_transactions['transaction_status'] == transaction_status]
            count = status_transactions.shape[0]
            amount = status_transactions['transaction_amount'].sum() if not status_transactions.empty else 0.0
            percentage = (count / no_transactions) * 100

            summary[f'stats_by_transactionstatus_count_{transaction_status.lower()}'] = count
            summary[f'stats_by_transactionstatus_amount_{transaction_status.lower()}'] = amount
            summary[f'stats_by_transactionstatus_percentage_{transaction_status.lower()}'] = percentage

    for key in summary:
        try:
            summary[key] = Decimal(str(summary[key])).quantize(Decimal('0.01'))
        except Exception:
            pass
    return summary


def generate_restaurant_daily_report(restaurant_id: int, eod_date: date) -> None:
    eod_date = '2025-05-31'
    try:
        orders = MONGO_DB['archive_orders'].find({
            'restaurant': restaurant_id,
            'eod_record_date': str(eod_date)
        })
        orders = list(orders)
    except Exception as e:
        logger.error("Failed to query archive_orders for restaurant %s daily report: %s", restaurant_id, e)
        return
    report = {
        'restaurant_id': restaurant_id,
        'eod_date': str(eod_date)
    }
    # to the report, add the result from make_orders_report
    # orders_report = make_orders_report(orders)
    # report.update(orders_report)

    # order_items_report = make_order_items_report(orders)
    # report.update(order_items_report)

    # customer_behaviour = report_on_customer_behaviour(orders)
    # report.update(customer_behaviour)

    transactions_report = report_on_transactions(restaurant_id, eod_date)
    report.update(transactions_report)

    logger.debug("Daily report: %s", report)

    report['eod_date'] = eod_date
    report['report_type'] = 'daily'
    str_eod_date = str(eod_date)
    # change the eod_date to a datetime object where the time is 23:59:59
    eod_date = f"{eod_date}T23:59:59"
    eod_date = datetime.fromisoformat(eod_date)
    report['eod_time'] = eod_date
    # save the report to MongoDB
    # MONGO_DB['analysis_restaurant_reports'].find_one_and_update(
    #     {
    #         'restaurant_id': restaurant_id,
    #         'eod_date': str_eod_date
    #     },
    #     {'$set': report},
    #     upsert=True
    # )
