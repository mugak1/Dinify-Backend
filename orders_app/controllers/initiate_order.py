import logging

from restaurants_app.models import Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
)
from orders_app.models import Order

logger = logging.getLogger(__name__)


def any_present_ongoing_order(table: Table) -> dict:
    """
    determines if a table has an ongoing order

    A table is occupied iff it has a SUBMITTED order — one that is not a draft
    (order_status != 'initiated'), not deleted, not cancelled, and whose
    fulfilment_status is not 'served'. An 'initiated' order is an unconfirmed
    draft that does NOT occupy the table (it claims the table only at submit).
    Occupancy otherwise keys off the kitchen-owned fulfilment axis (not
    payment_status), so a table frees up once the kitchen serves its order.
    Returns the most recent such order.
    """
    ongoing_order = (
        Order.objects
        .filter(table=table, deleted=False)
        .exclude(order_status=OrderStatus_Initiated)
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
