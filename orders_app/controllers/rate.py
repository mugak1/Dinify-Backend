from typing import Optional
from orders_app.models import Order, OrderItem
from users_app.models import User


def rate_and_review(
    order: Optional[str] = None,
    order_item: Optional[str] = None,
    rating: Optional[int] = None,
    review: Optional[str] = None
) -> dict:
    if order is not None:
        order = Order.objects.get(id=order)
        if order.rating is not None or order.review is not None:
            return {
                'status': 400,
                'message': 'This order has already been reviewed.'
            }
        order.rating = rating
        order.review = review
        order.save()
        return {
            'status': 200,
            'message': 'Your review has been submitted successfully.'
        }
    if order_item is not None:
        order_item = OrderItem.objects.get(id=order_item)
        if order_item.rating is not None or order_item.review is not None:
            return {
                'status': 400,
                'message': 'This item has already been reviewed.'
            }
        order_item.rating = rating
        order_item.review = review
        order_item.save()
        return {
            'status': 200,
            'message': 'Your review has been submitted successfully.'
        }


def block_review(
    user: User,
    order: Optional[str] = None,
    order_item: Optional[str] = None,
    block_reason: Optional[str] = None,
) -> dict:
    if order is not None:
        order = Order.objects.get(id=order)
        order.block_review = True
        if block_reason is not None:
            order.block_review_reason = block_reason
        order.review_blocked_by = user
        order.save()
        return {
            'status': 200,
            'message': 'Reviewing has been blocked for this order.'
        }
    if order_item is not None:
        order_item = OrderItem.objects.get(id=order_item)
        order_item.block_review = True
        if block_reason is not None:
            order_item.block_review_reason = block_reason
        order_item.review_blocked_by = user

        order_item.save()
        return {
            'status': 200,
            'message': 'Reviewing has been blocked for this item.'
        }
