from decimal import Decimal

from django.db import models
from users_app.models import User, BaseModel
from restaurants_app.models import Restaurant, MenuItem, Table
from dinify_backend.configss.string_definitions import (
    PaymentStatus_Pending, OrderStatus_Initiated,
    OrderItemStatus_Initiated,
)


# Create your models here.
class Order(BaseModel):
    """
    the orders that have been placed
    """
    waiter = models.ForeignKey(
        User,
        null=True,
        on_delete=models.SET_NULL,
        related_name='waiter'
    )
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='restaurant')
    table = models.ForeignKey(Table, on_delete=models.CASCADE, related_name='table')
    order_number = models.IntegerField(null=True)
    order_remarks = models.TextField(null=True, blank=True)

    customer_phone = models.CharField(max_length=50, null=True, blank=True)
    customer_email = models.EmailField(max_length=50, null=True, blank=True)
    customer = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='user')
    customer_match_attempted = models.BooleanField(default=False)

    total_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the total cost of the order using primary prices
    discounted_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the total cost of the order using discounted prices
    savings = models.DecimalField(max_digits=50, decimal_places=2)  # the total savings from the order i.e. discounted cost  - total cost  # noqa
    actual_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the actual cost that is payable by the customer
    prepayment_required = models.BooleanField(default=False)

    total_paid = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    balance_payable = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)

    payment_status = models.CharField(max_length=50, default=PaymentStatus_Pending, db_index=True)
    order_status = models.CharField(max_length=50, default=OrderStatus_Initiated, db_index=True)
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='last_updated_by')  # noqa

    rating = models.IntegerField(null=True, blank=True)
    review = models.TextField(null=True, blank=True)
    block_review = models.BooleanField(default=False)
    block_review_reason = models.TextField(null=True, blank=True)
    review_blocked_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='order_review_blocked_by')  # noqa

    # === order provenance + idempotency (Phase 2) ===
    order_source = models.CharField(
        max_length=32,
        choices=[
            ("diner_self_service", "Diner self-service"),
            ("server_assisted", "Server assisted"),
        ],
        default="diner_self_service",
        db_index=True,
    )
    # idempotency key supplied by the diner app (Phase 3); absent today
    client_order_id = models.UUIDField(null=True, blank=True, db_index=True)

    # === kitchen-owned fulfilment axis (Phase 2) ===
    # Kitchen writes ONLY these fields, never order_status / payment_status.
    fulfilment_status = models.CharField(
        max_length=20,
        choices=[
            ("new", "New"),
            ("preparing", "Preparing"),
            ("ready", "Ready"),
            ("served", "Served"),
        ],
        default="new",
        db_index=True,
    )
    fulfilment_status_updated_at = models.DateTimeField(null=True, blank=True)
    fulfilment_status_updated_by = models.ForeignKey(
        "users_app.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_fulfilment_updated",
    )
    served_at = models.DateTimeField(null=True, blank=True)
    priority = models.BooleanField(default=False, db_index=True)
    # local business date the order belongs to; authoritative for daily numbering
    order_date = models.DateField(null=True, db_index=True)

    class Meta:
        db_table = 'orders'
        ordering = ['-time_created']
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "client_order_id"],
                condition=models.Q(client_order_id__isnull=False),
                name="uniq_order_restaurant_client_order_id",
            ),
            models.UniqueConstraint(
                fields=["restaurant", "order_date", "order_number"],
                condition=models.Q(order_number__isnull=False),
                name="uniq_order_restaurant_date_number",
            ),
        ]


class OrderItem(BaseModel):
    """
    the order items
    """
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order')
    item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='item')
    available = models.BooleanField(default=True)

    # tracking options and choices
    option = models.CharField(max_length=50, null=True)
    option_choice = models.CharField(max_length=50, null=True)
    option_cost = models.DecimalField(max_digits=50, decimal_places=2, null=True)

    # for extras
    parent_item = models.ForeignKey(
        'self',
        null=True,
        on_delete=models.SET_NULL,
        related_name='parent_order_item'
    )  # noqa

    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=50, decimal_places=2)
    discounted_price = models.DecimalField(max_digits=50, decimal_places=2)
    discounted = models.BooleanField(default=False)
    unit_cost_of_options = models.DecimalField(max_digits=50, decimal_places=2, null=True)

    options = models.JSONField(default=list)

    selected_modifiers = models.JSONField(default=dict, null=True, blank=True)
    # Stores the diner's grouped modifier selections:
    # { "group_id": ["choice_id", ...], ... }

    # === kitchen snapshots (Phase 2): resolved at creation, immutable ===
    # item_name_snapshot preserves the name even if the menu item is renamed.
    item_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    # modifiers_snapshot holds resolved human-readable labels e.g. ["Size: Large"]
    modifiers_snapshot = models.JSONField(default=list, blank=True)
    # allergen_tags_snapshot holds [{name, icon, colour}] from item.tags (allergen)
    allergen_tags_snapshot = models.JSONField(default=list, blank=True)

    total_cost = models.DecimalField(max_digits=50, decimal_places=2)
    discounted_cost = models.DecimalField(max_digits=50, decimal_places=2)
    savings = models.DecimalField(max_digits=50, decimal_places=2)
    cost_of_options = models.DecimalField(max_digits=50, decimal_places=2, default=Decimal('0'))
    actual_cost = models.DecimalField(max_digits=50, decimal_places=2)

    rating = models.IntegerField(null=True, blank=True)
    review = models.TextField(null=True, blank=True)
    block_review = models.BooleanField(default=False)
    block_review_reason = models.TextField(null=True, blank=True)
    review_blocked_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='order_item_review_blocked_by')  # noqa

    status = models.CharField(max_length=50, default=OrderItemStatus_Initiated)
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='order_item_last_updated_by')  # noqa

    class Meta:
        db_table = 'order_items'
        ordering = ['-time_created', 'item__name']


class RestaurantDailyOrderCounter(BaseModel):
    """
    Per-restaurant, per-day monotonic source of order_number.

    Replaces the race-prone count()+1 pre_save signal: allocation takes a
    row lock (select_for_update) on the (restaurant, order_date) row, and the
    unique constraint is the final guard against the first-of-day race.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    order_date = models.DateField()
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'restaurant_daily_order_counters'
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "order_date"],
                name="uniq_daily_counter",
            ),
        ]
