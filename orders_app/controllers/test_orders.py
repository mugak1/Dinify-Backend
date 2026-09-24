"""
Test orders — the queryable facts about them, and THE ONE RULE for when they count.

An ``Order`` is marked ``is_test`` at creation for either of two reasons: its
restaurant is a TEST restaurant (``Restaurant.is_test``), or its restaurant was not yet
trading (``lifecycle_policy.orders_are_commercial``) — a pre-go-live rehearsal. The
rehearsal is a hard blocker on the Phase-1 go-live checklist: an owner must prove the
whole path — table QR, menu, ordering, kitchen, serve — before the restaurant is
allowed to open.

THE FLAG IS A LABEL, AND AT A TEST RESTAURANT IT LIMITS NOTHING. A test restaurant
exists so somebody can check that everything a live restaurant does actually works,
so its orders — every one of them flagged ``is_test`` — are counted in its reports and
dashboards, can be reviewed, and are matched to customers exactly like a live
restaurant's. Anything less means the one kind of restaurant built for trying the
product out is the one where parts of it cannot be tried.

WHAT IS LEFT OUT is a PRACTICE ORDER: a test order at a restaurant that is NOT a test
restaurant. In practice that is a rehearsal a real restaurant ran before it went
live, or an order from a restaurant's time as a test restaurant before it was
switched to real. Those stay out of that real restaurant's figures, its reviews and
customer matching, so its real numbers are never mixed with practice ones.

``counted_orders_q`` and ``is_practice_order`` are that rule, stated ONCE. Every
consumer asks through them — the reports (``reports_app.controllers.common.
sale_filters`` and the dashboards and transactions report that build their own
querysets), review submission, and ``determine-customers`` — so no consumer can
drift back into treating "flagged test" as "switched off". A bare ``is_test=False``
filter anywhere else is the defect this module exists to prevent.

KNOWN EDGE, stated rather than hidden: the REVIEW READS aggregate on
``Review.restaurant`` and never consult the order's flag. Reviews left on a test
restaurant's orders therefore stay visible if that restaurant is later switched to
real, while its test ORDERS drop out of its figures. Reviews on practice orders at a
real restaurant cannot arise, because submission refuses them.

The rest of this module exists so the go-live checklist can ask its question through a
named function rather than growing an ad-hoc queryset. It is a FACT, not an endpoint:
nothing here is routed, and nothing here writes.
"""
from django.db.models import Q

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


def counted_orders_q(prefix=''):
    """
    The orders a restaurant's figures, reviews and customer matching act on.

    Every order EXCEPT a practice order — a test order at a restaurant that is not a
    test restaurant. At a test restaurant nothing is excluded.

    ``prefix`` is the lookup path from the queried model to the order: ``''`` for an
    ``Order`` queryset, ``'order__'`` for a model that carries an ``order`` FK. Built
    in the POSITIVE form deliberately: an ``exclude()`` across a nullable ``order`` FK
    is join-shaped and can silently drop rows that have no order at all (the
    transactions report's subscription rows), which a positive ``Q`` cannot.

    It joins the order's restaurant inside the SAME statement, so it adds no query to
    any caller — several of which have their query counts pinned.
    """
    return (
        Q(**{f'{prefix}is_test': False})
        | Q(**{f'{prefix}restaurant__is_test': True})
    )


def is_practice_order(order) -> bool:
    """
    The same rule for one order already in hand.

    Reads ``order.restaurant`` — select it with the order (``select_related``) where
    the caller cares about the query count.
    """
    return bool(order.is_test) and not bool(order.restaurant.is_test)


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
