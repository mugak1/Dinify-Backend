from typing import Optional
from orders_app.models import Order, OrderItem


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