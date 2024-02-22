from django.db import models
from users_app.models import User, BaseModel
from restaurants_app.models import Restaurant, MenuItem, Table


# Create your models here.
class Order(BaseModel):
    """
    the orders that have been placed
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='restaurant')
    table = models.ForeignKey(Table, on_delete=models.CASCADE, related_name='table')
    customer = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='user')
    customer_match_attempted = models.BooleanField(default=False)

    total_cost = models.FloatField()  # the total cost of the order using primary prices
    discounted_cost = models.FloatField()  # the total cost of the order using discounted prices
    savings = models.FloatField()  # the total savings from the order i.e. discounted cost  - total cost
    actual_cost = models.FloatField()  # the actual cost that is payable by the customer
    prepayment_required = models.BooleanField(default=False)

    payment_status = models.CharField(max_length=50, default='pending')
    order_status = models.CharField(max_length=50, default='initiated')
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='last_updated_by')  # noqa

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

    quantity = models.IntegerField()
    unit_price = models.FloatField()
    discounted_price = models.FloatField()
    discounted = models.BooleanField(default=False)

    total_cost = models.FloatField()
    discounted_cost = models.FloatField()
    savings = models.FloatField()
    actual_cost = models.FloatField()

    status = models.CharField(max_length=50, default='initiated')
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='order_item_last_updated_by')  # noqa

    class Meta:
        db_table = 'order_items'
        ordering = ['item__name']
