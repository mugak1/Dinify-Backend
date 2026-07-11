"""
Order-creation service.

Centralises the single internal ``_create_order(...)`` entrypoint that
``ConOrder.initiate_order`` routes through. Inside one transaction it handles,
in order:

  1. idempotency (client_order_id) — return the existing order without gating
     or creating,
  2. table-gating (only for genuinely new submissions),
  3. daily order-number allocation (race-safe counter),
  4. order-row creation with the kitchen fulfilment axis initialised,
  5. order-item creation + amount roll-up.

This replaces the race-prone ``create_order_number`` pre_save signal.
"""
import logging

from django.db import transaction, IntegrityError
from django.utils import timezone

from orders_app.models import Order, RestaurantDailyOrderCounter
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    PaymentStatus_Pending,
)

logger = logging.getLogger(__name__)

ORDER_SOURCE_DINER = "diner_self_service"


def allocate_daily_order_number(restaurant, order_date):
    """
    Allocate the next per-restaurant, per-day order number.

    The row lock (``select_for_update``) is the normal path; the unique
    constraint on (restaurant, order_date) is the final guard against the
    first-of-day race where two creators both try to INSERT the counter row.

    The ``try/except`` deliberately wraps the ``with atomic()`` block (rather
    than sitting inside it) so the failed savepoint is rolled back *before* the
    retry — catching the IntegrityError inside the block would leave the
    connection needing rollback and the retry would raise
    ``TransactionManagementError``.
    """
    for _ in range(2):
        try:
            with transaction.atomic():
                counter, _created = (
                    RestaurantDailyOrderCounter.objects
                    .select_for_update()
                    .get_or_create(
                        restaurant=restaurant,
                        order_date=order_date,
                        defaults={"next_number": 1},
                    )
                )
                number = counter.next_number
                counter.next_number += 1
                counter.save(update_fields=["next_number"])
                return number
        except IntegrityError:
            # first-of-day race: a concurrent creator won the INSERT; the row
            # now exists, so the next pass locks it cleanly.
            continue
    raise IntegrityError("Could not allocate a daily order number after retry")


def _create_order(*, restaurant, table, items,
                  customer=None, created_by=None,
                  order_source=ORDER_SOURCE_DINER, client_order_id=None):
    """
    Create an order (and its items) atomically.

    Returns ``{'status': 200, 'order': <Order>, 'idempotent': bool}`` on
    success, or ``{'status': 400, 'message': ..., 'data': {...}}`` when
    table-gating rejects a genuinely new submission.
    """
    # imported lazily to avoid a circular import (con_orders imports this module)
    from orders_app.controllers.con_orders import ConOrder

    with transaction.atomic():
        # 1. idempotency FIRST — before gating or creation
        if client_order_id:
            existing = Order.objects.filter(
                restaurant=restaurant,
                client_order_id=client_order_id,
            ).first()
            if existing is not None:
                return {'status': 200, 'order': existing, 'idempotent': True}

        # 1b. Lock the table row so concurrent same-table submissions serialize.
        #     Mirrors allocate_daily_order_number's select_for_update in this file:
        #     the second creator blocks here until the first commits, then its
        #     step-2 gate below sees the first order and returns the 400. Placed
        #     AFTER step 1 so idempotent replays return without taking the lock.
        #     Lazy import keeps this module import-cycle-free (as with ConOrder).
        from restaurants_app.models import Table
        try:
            table = Table.objects.select_for_update().get(pk=table.pk)
        except Table.DoesNotExist:
            return {'status': 400, 'message': 'Invalid table for this restaurant'}

        # 2. table-gating — only for genuinely new submissions
        ongoing = ConOrder.any_present_ongoing_order(table)
        if ongoing.get('present'):
            return {
                'status': 400,
                'message': 'The table has an ongoing order',
                'data': {'order_id': ongoing.get('order_id')},
            }

        # 3. daily numbering (local business date)
        order_date = timezone.localdate()
        order_number = allocate_daily_order_number(restaurant, order_date)

        # 4. create the order with the fulfilment axis initialised.
        #    Kitchen owns fulfilment_status; order_status/payment_status stay
        #    finance-owned and are only seeded here at creation.
        #
        #    The INSERT is wrapped in a savepoint (nested atomic) so a concurrent
        #    double-tap — two requests carrying the same client_order_id, both past
        #    the step-1 lookup before either committed — degrades to the same
        #    idempotent replay as the sequential case instead of a raw 500. The
        #    partial unique constraint uniq_order_restaurant_client_order_id lets
        #    exactly one INSERT win; the loser catches the IntegrityError below.
        #    As in allocate_daily_order_number, the try/except deliberately WRAPS
        #    the atomic block so the failed savepoint is rolled back before the
        #    re-query, leaving the outer transaction usable.
        try:
            with transaction.atomic():
                order = Order.objects.create(
                    restaurant=restaurant,
                    table=table,

                    total_cost=0,
                    discounted_cost=0,
                    savings=0,
                    actual_cost=0,
                    prepayment_required=table.prepayment_required,

                    order_status=OrderStatus_Initiated,
                    payment_status=PaymentStatus_Pending,

                    customer=customer,
                    created_by=created_by,

                    order_source=order_source,
                    client_order_id=client_order_id,
                    order_number=order_number,
                    order_date=order_date,
                    fulfilment_status='new',
                    fulfilment_status_updated_at=timezone.now(),
                )
        except IntegrityError:
            # concurrent double-tap: a racing request with the same
            # client_order_id committed between our step-1 lookup and this INSERT.
            # Return the winner as the idempotent result — the SAME shape step 1
            # returns.
            if client_order_id:
                existing = Order.objects.filter(
                    restaurant=restaurant,
                    client_order_id=client_order_id,
                ).first()
                if existing is not None:
                    return {'status': 200, 'order': existing, 'idempotent': True}
            # not the client_order_id constraint (no existing row) — re-raise so a
            # genuinely unexpected IntegrityError is never silently swallowed.
            raise

        # 5. items + amount roll-up
        for item in items:
            ConOrder.add_order_item(item=item, order_id=str(order.id))
        order = Order.objects.select_for_update().get(id=order.id)
        ConOrder.update_order_amounts(order=order)

    return {'status': 200, 'order': order, 'idempotent': False}
