from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import KitchenTicket, KitchenTicketItem


class KitchenTicketItemSerializer(ModelSerializer):
    class Meta:
        model = KitchenTicketItem
        fields = (
            'id', 'order_item', 'name', 'quantity', 'notes', 'station'
        )


class KitchenTicketSerializer(ModelSerializer):
    ticket_items = SerializerMethodField()

    class Meta:
        model = KitchenTicket
        fields = (
            'id', 'order', 'restaurant', 'table', 'ticket_number',
            'status', 'station', 'items_count', 'target_prep_minutes',
            'placed_at', 'prep_started_at', 'ready_at',
            'fulfilled_at', 'cancelled_at', 'ticket_items'
        )

    def get_ticket_items(self, obj):
        items = KitchenTicketItem.objects.filter(ticket=obj)
        return KitchenTicketItemSerializer(items, many=True).data


class KitchenTicketListSerializer(ModelSerializer):
    class Meta:
        model = KitchenTicket
        fields = (
            'id', 'order', 'ticket_number', 'status', 'station',
            'items_count', 'target_prep_minutes', 'placed_at'
        )
