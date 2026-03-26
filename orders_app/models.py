from decimal import Decimal

from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.db import models
from django.db import transaction
from datetime import datetime
from users_app.models import User, BaseModel
from restaurants_app.models import Restaurant, MenuItem, Table
from dinify_backend.configss.string_definitions import (
    PaymentStatus_Pending, OrderStatus_Initiated,
    OrderItemStatus_Initiated
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

    class Meta:
        db_table = 'orders'
        ordering = ['-time_created']


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


@receiver(pre_save, sender=Order)
def create_order_number(sender, instance, **kwargs):
    if instance.order_number is None:
        with transaction.atomic():
            # get the count of today's order for the restaurant
            date_today = datetime.now().date()
            count = Order.objects.select_for_update().filter(
                restaurant=instance.restaurant,
                time_created__date=date_today
            ).count()
            instance.order_number = count+1
