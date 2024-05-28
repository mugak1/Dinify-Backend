from django.db.models import Count, Sum, Avg, F
from orders_app.models import Order, OrderItem
from dinify_backend.configss.string_definitions import (
    PaymentStatus_Paid, OrderStatus_Cancelled,
    OrderStatus_Refunded
)

def generate_restaurant_dashboard_details(restaurant_id: str):
    orders = Order.objects.filter(restaurant=restaurant_id)
    order_items = OrderItem.objects.filter(
        order__restaurant=restaurant_id
    )

    num_sales = orders.count()
    paid_orders = orders.filter(payment_status=PaymentStatus_Paid)
    num_paid_orders = paid_orders.count()
    perc_paid_orders = (num_paid_orders / num_sales) * 100 if num_sales else 0
    perc_paid_orders = round(perc_paid_orders, 1)

    cancelled_orders = orders.filter(order_status=OrderStatus_Cancelled)
    num_cancelled_orders = cancelled_orders.count()
    perc_cancelled_orders = (num_cancelled_orders / num_sales) * 100 if num_sales else 0
    perc_cancelled_orders = round(perc_cancelled_orders, 1)

    refunded_orders = orders.filter(order_status=OrderStatus_Refunded)
    num_refunded_orders = refunded_orders.count()
    perc_refunded_orders = (num_refunded_orders / num_sales) * 100 if num_sales else 0
    perc_refunded_orders = round(perc_refunded_orders, 1)

    sales_amount = paid_orders.aggregate(total_cost=Sum('total_cost'))['total_cost']

    new_diners = orders.values('customer').distinct().count()
    repeat_diners = orders.values('customer').annotate(order_count=Count('id')).filter(order_count__gt=1).count()
    most_active_diner = orders.values('customer__first_name').annotate(order_count=Count('id')).order_by('-order_count').first()

    most_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('-total_quantity').first()
    least_ordered_item = order_items.values('item__name').annotate(total_quantity=Sum('quantity')).order_by('total_quantity').first()

    most_liked_item = None
    least_liked_item = None

    peak_hour = orders.annotate(hour=F('time_created__hour')).values('hour').annotate(order_count=Count('id')).order_by('-order_count').first()

    stats = {
        "num_sales": num_sales,
        "paid_orders": {
            "number": num_paid_orders,
            "percentage": perc_paid_orders,
        },
        "cancelled_orders": {
            "number": num_cancelled_orders,
            "percentage": perc_cancelled_orders,
        },
        "refunded_orders": {
            "number": num_refunded_orders,
            "percentage": perc_refunded_orders,
        },
        "sales_amount": sales_amount,
        "new_diners": new_diners,
        "repeat_diners": repeat_diners,
        "most_ordered_item": most_ordered_item['item__name'] if most_ordered_item else '',
        "least_ordered_item": least_ordered_item['item__name'] if least_ordered_item else '',
        "most_liked_item": most_liked_item['item__name'] if most_liked_item else '',
        "least_liked_item": least_liked_item['item__name'] if least_liked_item else '',
        "most_active_diner": most_active_diner['customer__first_name'] if most_active_diner else '',
        "peak_hour": peak_hour['hour'] if peak_hour else '',
    }

    return {
        'status': 200,
        'message': 'Successfully retrieved the restaurant dashboard',
        'data': stats
    }

# Total number of sales
# Paid orders (number and percentage)
# Cancelled orders (number and percentage)
# Refunded orders (number and percentage)
# Gross sales amount
# New diners
# Repeat diners
# Most ordered item
# Least ordered item
# Most liked item i.e. based on the ratings
# Least liked item i.e. based on the ratings
# Most active diner
# Peak hour
