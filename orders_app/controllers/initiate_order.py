import logging

from restaurants_app.models import Table
from orders_app.models import Order
from orders_app.controllers.occupancy import ongoing_order_q

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

    The predicate is ``orders_app.controllers.occupancy.ongoing_order_q`` — the ONE
    definition, shared with ``ConOrder.any_present_ongoing_order`` and the
    dashboard-v2 tables card.
    """
    ongoing_order = (
        Order.objects
        .filter(ongoing_order_q(), table=table)
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
