"""
Serializers for the kitchen view (api/v1/kitchen/).

Produces exactly the Phase 1 kitchen contract from the Order fulfilment axis
and the per-item snapshots — never the report/archive serializers.
"""
from rest_framework import serializers

from orders_app.models import Order


class ActiveKitchenOrderSerializer(serializers.ModelSerializer):
    """
    One active kitchen ticket.

    `items` are available main items only (parent_item is null, available),
    each with its available extras nested underneath. The view attaches the
    pre-filtered, prefetched item set as `active_items` so partitioning happens
    in Python with no per-order query.
    """
    table_label = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='time_created')
    items = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'order_number',
            'table_label',
            'order_source',
            'fulfilment_status',
            'priority',
            'created_at',
            'served_at',
            'items',
        ]

    def get_table_label(self, obj):
        table = obj.table
        return table.display_name or table.str_number or str(table.number)

    @staticmethod
    def _line(row):
        return {
            'item_name_snapshot': row.item_name_snapshot,
            'quantity': row.quantity,
            'modifiers': row.modifiers_snapshot,
            'allergen_tags': row.allergen_tags_snapshot,
        }

    def get_items(self, obj):
        # Prefer the prefetched, pre-filtered (deleted=False, available=True) set
        # attached by the view; fall back to a query if it isn't present.
        item_rows = getattr(obj, 'active_items', None)
        if item_rows is None:
            item_rows = list(obj.order.filter(deleted=False, available=True))

        # Partition mains vs extras in Python — no per-item query.
        mains = []
        children_by_parent = {}
        for row in item_rows:
            if row.parent_item_id is None:
                mains.append(row)
            else:
                children_by_parent.setdefault(row.parent_item_id, []).append(row)

        result = []
        for main in mains:
            line = self._line(main)
            line['item_note'] = None  # no per-item note captured yet
            line['extras'] = [
                self._line(child) for child in children_by_parent.get(main.id, [])
            ]
            result.append(line)
        return result
