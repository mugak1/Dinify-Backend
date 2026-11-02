import logging

from restaurants_app.models import Table

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

    THIS IS NOT A SECOND COPY. It delegates to ``ConOrder.any_present_ongoing_order``,
    whose predicate is ``orders_app.controllers.occupancy.ongoing_order_q``, the ONE
    definition of "is the table busy?". It used to restate the query; one function
    answering the question is what keeps the table cards (``restaurants_app.
    controllers.tables``), the order gates and the Step 1 visit state agreeing.

    Imported lazily: ``restaurants_app.controllers.tables`` imports this module at
    load time, and ``con_orders`` pulls in a large part of both apps.
    """
    from orders_app.controllers.con_orders import ConOrder
    return ConOrder.any_present_ongoing_order(table)
