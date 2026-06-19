from rest_framework import serializers

from orders_app.models import Order


class SerializerOrderListingReport(serializers.ModelSerializer):
    """
    One row per sale order in the sales-listing report.

    Output-only. The queryset passed in MUST be annotated with ``item_count``
    (Count of the order's OrderItems) and ``payment_mode`` (the order's latest
    successful order-payment transaction's mode, or NULL) — see
    ``generate_restaurant_sales_listing`` — so the whole listing serialises in a
    single query with no per-row N+1.

    Money fields are emitted as JSON numbers (``coerce_to_string=False``) to
    stay consistent with the sales-summary dict, which renders raw Decimals as
    numbers via DRF's JSON encoder.
    """
    # order_number is an IntegerField(null=True) on the model; the contract
    # wants it as a string. CharField coerces int -> str on output and passes
    # null through.
    order_number = serializers.CharField(allow_null=True, read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    gross = serializers.DecimalField(
        source='total_cost', max_digits=50, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    discount = serializers.DecimalField(
        source='savings', max_digits=50, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    revenue = serializers.DecimalField(
        source='actual_cost', max_digits=50, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    # Real payment mode from the annotation (no hardcoded 'MoMo'); NULL when the
    # order has no successful order-payment transaction.
    payment_mode = serializers.CharField(allow_null=True, read_only=True)

    class Meta:
        model = Order
        # payment_status is emitted raw (e.g. 'paid'), matching the raw
        # payment_mode; time_created is rendered ISO 8601 by DRF's DateTimeField.
        fields = (
            'order_number', 'item_count', 'gross', 'discount',
            'revenue', 'payment_mode', 'payment_status', 'time_created',
        )
