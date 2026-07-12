"""
handle the submission of an order
"""
import logging

from datetime import datetime
from typing import Union
from django.db import transaction
from users_app.models import User
from orders_app.models import Order, OrderItem
from dinify_backend.configss.messages import (
    OK_ORDER_UPDATED, ERR_ORDER_UPDATED,
    ERR_UPDATING_ITEM_STATUS_UNSUPPORTED_STATUS,
    OK_UPDATED_ITEM_STATUS, ERR_ORDER_ITEM_NOT_AVAILABLE
)
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated, OrderItemStatus_Unavailable,
    OrderStatus_Pending,
    OrderItemStatus_Preparing, OrderItemStatus_Served,
    OrderStatus_Cancelled, OrderStatus_Preparing,
    OrderStatus_Served
)

logger = logging.getLogger(__name__)


def update_order_status(
    order: Order,
    new_status: str,
    user: Union[User, None]
) -> dict:
    """
    update an order
    """
    try:
        # SUBMIT (initiated -> pending) is the transition that turns a draft
        # into a real order and CLAIMS the table, so it must be race-safe.
        # It is handled by its own transactional helper; every other status
        # change below is left exactly as it was (no new locking/transaction).
        if new_status == OrderStatus_Pending:
            return _submit_order(order, user)
        # an unauthenticated diner arrives as AnonymousUser (not None); never
        # assign it to a User FK — normalise to None so attribution stays null.
        if user is not None and user.is_anonymous:
            user = None
        order.order_status = new_status
        logger.debug("The submitted user is %s", user)
        if user is not None:
            order.last_updated_by = user
        if new_status == OrderStatus_Preparing:
            order.waiter = user
            # set all oder items
            order_items = OrderItem.objects.filter(order=order)
            for item in order_items:
                item.status = OrderItemStatus_Preparing
                item.last_updated_by = user
                item.save()
        order.time_last_updated = datetime.now()
        order.save()
        return {
            'status': 200,
            'message': OK_ORDER_UPDATED
        }
    except Exception:
        # log the full traceback so this failure class is diagnosable; keep the
        # user-facing message generic.
        logger.exception("ErrorUpdateOrderStatus")
        return {
            'status': 400,
            'message': ERR_ORDER_UPDATED
        }


def _submit_order(order: Order, user: Union[User, None]) -> dict:
    """
    Submit a draft order (order_status 'initiated' -> 'pending').

    This is the transition that turns a draft into a real order and CLAIMS the
    table, so it is transactional and race-safe:
      * lock the order's table row FIRST (matching _create_order's table->order
        lock order),
      * RE-READ the order under that lock — the instance handed in was fetched
        outside the transaction and may be stale,
      * re-check the "must still be a draft" rule and table occupancy on the
        FRESH row before flipping.
    Two diners submitting for the same table therefore serialize on the table
    lock: the first claims it, the second gets a clean 400.
    """
    # Local imports keep this off the module import graph and dodge the
    # con_orders <-> create_order import cycle.
    from restaurants_app.models import Table
    from orders_app.controllers.con_orders import ConOrder

    # an unauthenticated diner arrives as AnonymousUser (not None); never
    # assign it to a User FK — normalise to None so attribution stays null.
    if user is not None and user.is_anonymous:
        user = None

    with transaction.atomic():
        if order.table_id is not None:
            # Table-first lock, then re-read the order under the same lock.
            locked_table = (
                Table.objects.select_for_update().get(pk=order.table_id)
            )
            order = Order.objects.select_for_update().get(pk=order.pk)

            # Status check on the FRESH row: a concurrent double-submit that
            # already flipped this order loses here with the existing 400.
            if order.order_status != OrderItemStatus_Initiated:
                return {
                    'status': 400,
                    'message': 'This order cannot be submitted.'
                }

            # Re-check occupancy under the lock. After the drafts-are-invisible
            # change this order (still 'initiated') is excluded from the
            # predicate anyway; the not-this-order guard is defense-in-depth.
            ongoing = ConOrder.any_present_ongoing_order(locked_table)
            if ongoing.get('present') and ongoing.get('order_id') != order.id:
                return {
                    'status': 400,
                    'message': 'The table has an ongoing order'
                }
        else:
            # No table to claim (defensive — Order.table is non-nullable today,
            # so this branch is currently unreachable). Re-read and flip.
            order = Order.objects.select_for_update().get(pk=order.pk)
            if order.order_status != OrderItemStatus_Initiated:
                return {
                    'status': 400,
                    'message': 'This order cannot be submitted.'
                }

        order.order_status = OrderStatus_Pending
        if user is not None:
            order.last_updated_by = user
        order.time_last_updated = datetime.now()
        order.save()

    return {
        'status': 200,
        'message': OK_ORDER_UPDATED
    }


def update_item_status(
    item_id: str,
    new_status: str,
    user: User
) -> dict:
    """
    update the status of an order item
    """
    if new_status not in [OrderItemStatus_Preparing, OrderItemStatus_Served]:
        return {
            'status': 400,
            'message': ERR_UPDATING_ITEM_STATUS_UNSUPPORTED_STATUS
        }

    with transaction.atomic():
        item = OrderItem.objects.select_for_update().get(id=item_id)

        if not item.available:
            return {
                'status': 200,
                'message': ERR_ORDER_ITEM_NOT_AVAILABLE
            }

        time_now = datetime.now()

        item.status = new_status
        item.last_updated_by = user
        item.time_last_updated = time_now
        item.save()

        # check if to set the order status to served
        if new_status in [OrderItemStatus_Preparing, OrderItemStatus_Served]:
            order = Order.objects.select_for_update().get(id=item.order.pk)
            available_order_items = OrderItem.objects.filter(
                order=order,
                available=True
            ).exclude(status=OrderItemStatus_Unavailable)

            updated_items = available_order_items.filter(status=new_status)

            if available_order_items.count() == updated_items.count():
                order.order_status = new_status  # OrderStatus_Served
                order.last_updated_by = user
                order.time_last_updated = time_now
                order.save()

        return {
            'status': 200,
            'message': OK_UPDATED_ITEM_STATUS
        }
