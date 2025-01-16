from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem


class SerializerOrderListingReport(ModelSerializer):
    no_items = SerializerMethodField()
    payment_mode = SerializerMethodField()
    last_updated_by = SerializerMethodField()
    payment_status = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'order_number', 'no_items',
            'total_cost', 'discounted_cost',
            'payment_mode', 'payment_status',
            'time_created', 'last_updated_by'
        )

    def get_no_items(self, order):
        items = OrderItem.objects.values('id').filter(order=order)
        return items.count()

    def get_payment_mode(self, order):
        return 'MoMo'

    def get_last_updated_by(self, order):
        if order.last_updated_by is not None:
            return f"{order.last_updated_by.first_name} {order.last_updated_by.last_name}"
        return ''

    def get_payment_status(self, order):
        return order.payment_status.replace('_', ' ').title()
