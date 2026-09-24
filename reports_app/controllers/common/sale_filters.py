"""
Canonical definition of "what is a sale" and "what is revenue" for
Order-based restaurant reports.

This module is the single source of truth that the Sales / Transactions /
Diners report rebuild (PR4-6) consumes. No controller should ever re-derive
revenue from ``total_cost`` (pre-discount gross) or ``discounted_cost``
(post-discount total) again — import :data:`REVENUE_FIELD` /
:func:`revenue_sum` (and :data:`DISCOUNT_FIELD` / :func:`discount_sum`) from
here instead.

Scope: ``Order``-based reports only. The ``DinifyTransaction`` path has
different status semantics and is intentionally out of scope here — though
:data:`SALE_STATUSES` is deliberately reusable on that join too (see below).
"""

from django.db.models import Sum

from orders_app.models import Order
from orders_app.controllers.test_orders import counted_orders_q
from dinify_backend.configss.string_definitions import (
    OrderStatus_Served,
    OrderStatus_Paid,
)


# A sale is a *completed service*. Served and paid orders both count.
# ``initiated`` / ``pending`` / ``preparing`` are in-flight (and reversible),
# so they are excluded; ``cancelled`` / ``refunded`` are not sales either.
# Reusable both as ``order_status__in=`` on an ``Order`` queryset and as
# ``order__order_status__in=`` on a ``DinifyTransaction`` join.
SALE_STATUSES = [OrderStatus_Served, OrderStatus_Paid]


# Canonical revenue / discount basis — defined ONCE here and imported
# everywhere so no report re-derives it inconsistently:
#   revenue  = Sum('actual_cost')  -> the amount actually payable by the
#              customer (net of discounts). NOT ``total_cost`` (gross) and NOT
#              ``discounted_cost`` (the post-discount order total).
#   discount = Sum('savings')      -> the discount magnitude. NOT
#              ``discounted_cost``.
REVENUE_FIELD = 'actual_cost'
DISCOUNT_FIELD = 'savings'


def revenue_sum():
    """Canonical revenue aggregate expression.

    Use as ``qs.aggregate(revenue=revenue_sum())`` or inside a grouped
    ``.annotate(revenue=revenue_sum())``.
    """
    return Sum(REVENUE_FIELD)


def discount_sum():
    """Canonical discount aggregate expression (the discount magnitude)."""
    return Sum(DISCOUNT_FIELD)


def sale_orders(restaurant_id, date_from, date_to):
    """Return a sale-filtered, restaurant-scoped, date-ranged ``Order`` queryset.

    The queryset is lazy (no query runs until it is evaluated) and filters on:
      * ``restaurant`` == ``restaurant_id``
      * ``order_status__in`` == :data:`SALE_STATUSES`
      * not a PRACTICE order (``orders_app.controllers.test_orders.counted_orders_q``)
        — a test order at a real restaurant, such as a rehearsal before it went
        live, is not a sale. At a TEST restaurant nothing is excluded: its orders
        are flagged test and still count, exactly like a live restaurant's
      * ``time_created`` within the inclusive local-day range
        ``[date_from, date_to]``

    THIS IS THE CHOKEPOINT for practice-order exclusion. Sales listing/trends/hourly,
    diners summary/listing and menu summary all derive their base queryset from here,
    so they inherit the rule and must not re-add it — least of all as a bare
    ``is_test=False``, which would switch a test restaurant's reports off. The
    dashboards and the transactions report build their own querysets and apply the
    same ``counted_orders_q`` — see ``restaurant/dashboard.py`` and
    ``restaurant/transactions.py``.

    Date filtering aligns to the *local* day (EAT). Under ``USE_TZ = True`` the
    ``__date`` lookup extracts the date in the active timezone, so a ``__date``
    range is the correct local-day filter — do NOT filter by UTC day with a raw
    ``time_created__gte/lte`` datetime range.

    ``date_from`` / ``date_to`` are inclusive and may be ``date`` objects or
    ``'YYYY-MM-DD'`` strings.
    """
    return Order.objects.filter(
        counted_orders_q(),
        restaurant=restaurant_id,
        order_status__in=SALE_STATUSES,
        time_created__date__gte=date_from,
        time_created__date__lte=date_to,
    )
