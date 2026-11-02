"""
Table occupancy — THE ONE DEFINITION of "this order is ongoing at its table".

A table is occupied iff it has an order that is:

- not soft-deleted (``deleted=False``);
- SUBMITTED — not ``initiated``. An ``initiated`` order is an unconfirmed draft that
  does not occupy its table; the table is claimed only at ``submit`` (PR #210);
- not ``cancelled``; and
- not yet SERVED (``fulfilment_status != 'served'``).

Occupancy keys off the kitchen-owned FULFILMENT axis and never off
``payment_status`` (PR #186). Diner payment is not wired, so nothing in production
moves ``payment_status`` off ``pending``: a predicate that waited for payment would
keep every order that was ever placed occupying its table forever.

WHO ASKS THROUGH IT. ``ConOrder.any_present_ongoing_order`` and its standalone twin
``orders_app.controllers.initiate_order.any_present_ongoing_order`` (the per-table
question the order-create, submit and recall gates and the table cards ask), and the
``dashboard-v2`` tables card (``reports_app.controllers.restaurant.dashboard.
_build_tables``), which counts the tables that answer yes. One definition is what
makes the card agree with the gates and the cards; it used to restate its own, keyed
on ``payment_status``, and disagreed with both.

DELIBERATELY NOT FILTERED ON ``is_test``. This is LIVE FLOOR STATE, not history: a
test or rehearsal order really does occupy its table, so ``counted_orders_q`` — the
rule for what a restaurant's FIGURES count — does not apply here.

``refunded`` IS NOT EXCLUDED, and that is recorded rather than decided here: nothing
in production writes ``order_status='refunded'`` (the Dinify-initiated refund path
was removed in the custodial teardown), so the difference is unreachable today.
Excluding it would change what the order-create, submit and recall occupancy gates
decide, which is a decision of its own.
"""
from django.db.models import Q

from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
)

# The fulfilment state that frees a table.
FULFILMENT_SERVED = 'served'

# Order statuses that never occupy a table, whatever their fulfilment state.
NON_OCCUPYING_ORDER_STATUSES = (OrderStatus_Initiated, OrderStatus_Cancelled)


def ongoing_order_q():
    """A ``Q`` over ``Order`` matching exactly the orders that occupy their table.

    Combine it with ``table=`` for the per-table question, or with ``restaurant=``
    and count distinct ``table`` values for the floor-wide one. Returns a fresh
    ``Q`` on every call.
    """
    return (
        Q(deleted=False)
        & ~Q(order_status__in=NON_OCCUPYING_ORDER_STATUSES)
        & ~Q(fulfilment_status=FULFILMENT_SERVED)
    )
