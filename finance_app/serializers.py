from rest_framework import serializers
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from finance_app.models import DinifyTransaction


class SerializerGetRestaurantTransactionListing(ModelSerializer):
    """One row per restaurant transaction in the transactions-listing report.

    Output-only, RAW enums — transaction_type / transaction_status /
    transaction_platform / payment_mode are emitted as stored (no Title-casing,
    no 'momo'->'MoMo'); the frontend owns display formatting. Direction (money
    in vs out) is derivable from transaction_type, so a single ``amount`` is
    emitted, not amount_in / amount_out. The queryset MUST be
    ``select_related('order')`` (see ``generate_restaurant_transaction_listing``)
    so ``order_number`` costs no per-row query.
    """
    order_number = SerializerMethodField()
    # Money as a JSON number (coerce_to_string=False), matching the sales-listing
    # serializer; a single neutral amount replaces the custodial amount_in/out.
    amount = serializers.DecimalField(
        source='transaction_amount', max_digits=50, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )

    class Meta:
        model = DinifyTransaction
        fields = (
            'id', 'transaction_type', 'transaction_status', 'order_number',
            'amount', 'payment_mode', 'transaction_platform', 'time_created',
        )

    def get_order_number(self, record):
        # order is NULL for subscription transactions; the contract wants the
        # number as a string when present.
        if record.order is None:
            return None
        number = record.order.order_number
        return str(number) if number is not None else None
