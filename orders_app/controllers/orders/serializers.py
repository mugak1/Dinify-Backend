from orders_app.models import Order, OrderItem
from rest_framework.serializers import ModelSerializer, SerializerMethodField


def serialize_order_details(order: Order) -> dict:
    all_order_items = OrderItem.objects.filter(order=order)
    non_extra_items = all_order_items.filter(parent_item__isnull=True)
    extra_items = all_order_items.filter(parent_item__isnull=False)
    parent_items = all_order_items.filter(
        parent_item__isnull=True,
        available=True
    )
    unavailable_parent_items = all_order_items.filter(
        parent_item__isnull=True,
        available=False
    )
    available_extras_selected = all_order_items.filter(
        parent_item__isnull=False,
        available=True
    )
    unavailable_extras_selected = all_order_items.filter(
        parent_item__isnull=False,
        available=False
    )

    order = {
        'id': str(order.pk),
        'time_created': order.time_created,
        'order_number': order.order_number,

        'restaurant': str(order.restaurant.pk),
        'table': str(order.table.pk),
        'table_number': str(order.table.number),

        'total_cost': order.total_cost,
        'discounted_cost': order.discounted_cost,
        'savings': order.savings,
        'actual_cost': order.actual_cost,
        'prepayment_required': order.prepayment_required,

        'no_items': len(parent_items),
        'no_unavailable_items': len(unavailable_parent_items),
        'no_available_items': len(parent_items),
        'no_available_extras': len(available_extras_selected),
        'no_unavailable_extras': len(unavailable_extras_selected),
        'order_status': order.order_status,
        'payment_status': order.payment_status,
    }

    order_items = [serialize_order_item_details(item=item) for item in non_extra_items]
    available_items = [serialize_order_item_details(item=item) for item in parent_items]
    unavailable_items = [serialize_order_item_details(item=item) for item in unavailable_parent_items]  # noqa
    extras = [serialize_order_item_details(item=item) for item in extra_items]
    available_extras = [serialize_order_item_details(item=item) for item in available_extras_selected]  # noqa
    unavailable_extras = [serialize_order_item_details(item=item) for item in unavailable_extras_selected]  # noqa

    return {
        'order': order,
        'order_items': order_items,
        'available_items': available_items,
        'unavailable_items': unavailable_items,
        'extras': extras,
        'available_extras': available_extras,
        'unavailable_extras': unavailable_extras
    }


def serialize_order_item_details(item: OrderItem) -> dict:
    extras = OrderItem.objects.filter(
        parent_item=item
    )
    extras_list = [
        {
            'item_name': item.item.name,
            'quantity': item.quantity,
            'actual_cost': item.actual_cost,
            'available': item.available,
            'status': item.status
        }
        for item in extras
    ]
    return {
        'item': str(item.item.id),
        'item_name': item.item.name,
        'quantity': item.quantity,

        'unit_price': item.unit_price,
        'discounted_price': item.discounted_price,
        'discounted': item.discounted,

        'total_cost': item.total_cost,
        'discounted_cost': item.discounted_cost,
        'savings': item.savings,
        'actual_cost': item.actual_cost,

        'available': item.available,
        'status': item.status,

        'is_extra': False if item.parent_item is None else True,
        'no_extras': extras.count(),
        'extras': extras_list
    }
