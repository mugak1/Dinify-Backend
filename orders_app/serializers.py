from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Unavailable,
    OrderItemStatus_Served,
    OrderStatus_Cancelled
)


class SerializerPutOrderItem(ModelSerializer):
    """
    Internal-only serializer for creating an order item during order build.

    order / item / parent_item and the audit FKs are SERVER-DERIVED: the
    order-build service (con_orders) passes the already-resolved, tenant-scoped
    objects via ``save()`` kwargs — they are never accepted from request input
    (read_only). A caller therefore cannot construct an OrderItem that links a
    foreign menu item to an order through this serializer.
    """
    class Meta:
        model = OrderItem
        fields = (
            # server-derived relations (set via save() by the order-build service)
            'id', 'order', 'item', 'parent_item',
            # business values written by the order-build service
            'available', 'option', 'option_choice', 'option_cost',
            'quantity', 'unit_price', 'discounted_price', 'discounted',
            'unit_cost_of_options', 'options', 'selected_modifiers',
            'item_name_snapshot', 'modifiers_snapshot', 'allergen_tags_snapshot',
            'total_cost', 'discounted_cost', 'savings', 'cost_of_options',
            'actual_cost', 'status',
            # audit / lifecycle (output-only)
            'last_updated_by', 'created_by', 'deleted_by', 'deleted',
            'time_deleted', 'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'order', 'item', 'parent_item', 'last_updated_by',
            'created_by', 'deleted_by', 'deleted', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )


class SerializerListOrderItem(ModelSerializer):
    item = SerializerMethodField()
    extra_items = SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'item', 'available',
            'quantity', 'unit_price',
            'discounted_price', 'savings',
            'options', 'cost_of_options',
            'actual_cost', 'status',
            'deleted', 'deletion_reason',
            'time_last_updated', 'extra_items'
        )

    def get_item(self, item):
        return {
            'id': item.item.pk,
            'name': item.item.name,
            'is_special': item.item.is_special,
        }

    def get_extra_items(self, item):
        extras = []
        extra_items = OrderItem.objects.filter(parent_item=item)
        for extra in extra_items:
            extras.append({
                'id': extra.pk,
                'name': extra.item.name,
                'quantity': extra.quantity,
                'unit_price': extra.unit_price,
                'discounted_price': extra.discounted_price,
                'savings': extra.savings,
                'actual_cost': extra.actual_cost,
                'status': extra.status,
                'deleted': extra.deleted,
                'deletion_reason': extra.deletion_reason,
                'time_last_updated': extra.time_last_updated
            })
        return extras


class SerializerListGetOrder(ModelSerializer):
    items = SerializerMethodField()
    table_details = SerializerMethodField()
    count_items_served = SerializerMethodField()
    count_items_considered = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'table', 'customer',
            'total_cost', 'discounted_cost', 'savings',
            'actual_cost', 'prepayment_required',
            'payment_status', 'order_status',
            'items', 'order_number', 'time_created', 'table_details',
            'count_items_served', 'count_items_considered',
            'total_paid', 'balance_payable', 'payment_status',
            'time_last_updated'
        )

    def get_items(self, order):
        items = OrderItem.objects.filter(order=order)
        return SerializerListOrderItem(items, many=True).data

    def get_table_details(self, order):
        return {
            'table_number': order.table.number,
            'table_room_name': order.table.room_name
        }

    def get_count_items_served(self, order):
        return OrderItem.objects.values('id').filter(
            order=order,
            status=OrderItemStatus_Served
        ).count()

    def get_count_items_considered(self, order):
        return OrderItem.objects.values('id').filter(
            order=order,
            deleted=False,
        ).exclude(status__in=[OrderItemStatus_Unavailable, OrderStatus_Cancelled]).count()


class SerializerPublicOrderDetails(ModelSerializer):
    items = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'table',
            'total_cost', 'discounted_cost', 'savings',
            'actual_cost', 'prepayment_required',
            'payment_status', 'order_status',
            'items', 'order_number',
            'total_paid', 'balance_payable', 'payment_status',
            'time_last_updated'
        )

    def get_items(self, order):
        items = OrderItem.objects.filter(order=order)
        return SerializerListOrderItem(items, many=True).data
