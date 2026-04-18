import logging

from restaurants_app.models import Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    PaymentStatus_Pending
)
from orders_app.models import Order

logger = logging.getLogger(__name__)


def any_present_ongoing_order(table: Table) -> dict:
    """
    determines if a table has an ongoing order
    """
    present_orders = Order.objects.values('id', 'eod_record_date').filter(
        table=table,
        order_status__in=[
            OrderStatus_Initiated,
            OrderStatus_Pending,
            OrderStatus_Preparing
        ]
    ).order_by(
        '-time_created'
    )
    if present_orders.count() > 0:
        order = present_orders.first()
        return {
            'present': True,
            'order_id': order['id']
        }

    served_unpaid_orders = Order.objects.values('id', 'eod_record_date').filter(
        table=table,
        order_status=OrderStatus_Served,
        payment_status=PaymentStatus_Pending
    ).order_by(
        '-time_created'
    )
    if served_unpaid_orders.count() > 0:
        order = served_unpaid_orders.first()
        return {
            'present': True,
            'order_id': order['id']
        }
    return {'present': False}
