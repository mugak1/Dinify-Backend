import logging

from restaurants_app.models import Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
)
from orders_app.models import Order

logger = logging.getLogger(__name__)


def any_present_ongoing_order(table: Table) -> dict:
    """
    determines if a table has an ongoing order

    A table is occupied iff it has an order that is not deleted, not
    cancelled, and whose fulfilment_status is not 'served'. Gating keys off
    the kitchen-owned fulfilment axis (not order_status / payment_status), so
    a table frees up once the kitchen serves its order. Returns the most
    recent such order.
    """
    ongoing_order = (
        Order.objects
        .filter(table=table, deleted=False)
        .exclude(order_status=OrderStatus_Cancelled)
        .exclude(fulfilment_status='served')
        .order_by('-time_created')
        .values('id')
        .first()
    )
    if ongoing_order is not None:
        return {
            'present': True,
            'order_id': ongoing_order['id']
        }
    return {'present': False}
