"""
Pre-go-live rehearsal orders — the queryable facts about them.

An ``Order`` is marked ``is_test`` at creation when its restaurant was not yet trading
(``lifecycle_policy.orders_are_commercial``). The rehearsal is a hard blocker on the
Phase-1 go-live checklist: an owner must prove the whole path — table QR, menu,
ordering, kitchen, serve — before the restaurant is allowed to open.

This module exists so that checklist can ask its question through a named function
rather than growing an ad-hoc queryset. It is a FACT, not an endpoint: nothing here is
routed, and nothing here writes.
"""
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    OrderStatus_Paid,
    OrderStatus_Served,
)

# What "completed" means for a rehearsal. The kitchen serving the ticket is the last
# step the owner controls end to end — diner payment is not wired, so requiring `paid`
# would make the checklist unsatisfiable. `paid` is accepted too so the definition
# does not become wrong the moment payment lands.
COMPLETED_TEST_ORDER_STATUSES = (OrderStatus_Served, OrderStatus_Paid)


def completed_test_orders(restaurant_id):
    """The completed rehearsal orders for ``restaurant_id``, as a lazy queryset."""
    return Order.objects.filter(
        restaurant=restaurant_id,
        is_test=True,
        deleted=False,
        order_status__in=COMPLETED_TEST_ORDER_STATUSES,
    )


def has_completed_test_order(restaurant) -> bool:
    """
    Whether this restaurant has completed at least one end-to-end rehearsal order.

    Accepts a ``Restaurant`` or a bare id, so the Phase-1 readiness checklist can call
    it with whatever it is holding.
    """
    restaurant_id = getattr(restaurant, 'pk', restaurant)
    if restaurant_id is None:
        return False
    return completed_test_orders(restaurant_id).exists()
