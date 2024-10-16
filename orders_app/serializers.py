from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem
from dinify_backend.configss.string_definitions import OrderItemStatus_Served


class SerializerPutOrder(ModelSerializer):
    """
    serializer for adding and updating an order
    """
    class Meta:
        model = Order
        fields = '__all__'


class SerializerPutOrderItem(ModelSerializer):
    """
    serializer for adding and updating an order item
    """
    class Meta:
        model = OrderItem
        fields = '__all__'


class SerializerListOrderItem(ModelSerializer):
    item = SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'item', 'available',
            'quantity', 'unit_price',
            'discounted_price', 'savings',
            'options', 'cost_of_options',
            'actual_cost', 'status', 'deleted'
        )

    def get_item(self, item):
        return {
            'id': item.item.pk,
            'name': item.item.name
        }


class SerializerListGetOrder(ModelSerializer):
    items = SerializerMethodField()
    table_details = SerializerMethodField()
    count_items_served = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'table', 'customer',
            'total_cost', 'discounted_cost', 'savings',
            'actual_cost', 'prepayment_required',
            'payment_status', 'order_status',
            'items', 'order_number', 'time_created', 'table_details',
            'order_remarks', 'count_items_served',
            'total_paid', 'balance_payable', 'payment_status'
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
            'order_remarks',
            'total_paid', 'balance_payable', 'payment_status'
        )

    def get_items(self, order):
        items = OrderItem.objects.filter(order=order)
        return SerializerListOrderItem(items, many=True).data
