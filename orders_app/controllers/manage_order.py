"""
handle the submission of an order
"""
from datetime import datetime
from typing import Union
from users_app.models import User
from orders_app.models import Order
from dinify_backend.configss.messages import (
    OK_ORDER_UPDATED, ERR_ORDER_UPDATED
)
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated,
    OrderStatus_Pending,
    OrderItemStatus_Preparing, OrderItemStatus_Served,
    OrderStatus_Cancelled,
    OrderStatus_Served
)


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
        if user is None:
            order.last_updated_by = user
        order.time_last_updated = datetime.now()
        order.save()
        return {
            'status': 200,
            'message': OK_ORDER_UPDATED
        }
    except Exception as error:
        print(f"ErrorUpdateOrderStatus: {error}")
        return {
            'status': 400,
            'message': ERR_ORDER_UPDATED
        }
