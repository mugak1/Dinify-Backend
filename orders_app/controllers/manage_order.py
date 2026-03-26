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
        # if the status is submitted
        # check if the order requires prepayment before updating

        # if the new status is to submit,
        # check that the current status is initiated
        if new_status == OrderStatus_Pending:
            if order.order_status != OrderItemStatus_Initiated:
                return {
                    'status': 400,
                    'message': 'This order cannot be submitted.'
                }
        order.order_status = new_status
        logger.debug("The submitted user is %s", user)
        if user is not None:
            # order.last_updated_by = User.objects.get(pk=user)
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
    except Exception as error:
        logger.error("ErrorUpdateOrderStatus: %s", error)
        return {
            'status': 400,
            'message': ERR_ORDER_UPDATED
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
