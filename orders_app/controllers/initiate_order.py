from django.db import transaction
from restaurants_app.models import Restaurant, MenuItem, Table
from django.core.exceptions import ObjectDoesNotExist
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    PaymentStatus_Pending
)
from orders_app.serializers import SerializerPutOrder, SerializerPutOrderItem
from orders_app.models import Order


def any_present_ongoing_order(table: Table) -> dict:
    """
    determines if a table has an ongoing order
    """
    # TODO check if this is an ongoing order
    present_orders = Order.objects.values('id', 'eod_record_date').filter(
        table=table,
        order_status__in=[
            OrderStatus_Initiated,
            OrderStatus_Pending,
            OrderStatus_Preparing
        ]
    ).order_by(
        '-time_created'
    )
    if present_orders.count() > 0:
        order = present_orders.first()
        if not order['eod_record_date'] is None:
            return {
                'present': True,
                'order_id': order['id']
            }

    served_unpaid_orders = Order.objects.values('id', 'eod_record_date').filter(
        table=table,
        order_status=OrderStatus_Served,
        payment_status=PaymentStatus_Pending
    ).order_by(
        '-time_created'
    )
    if served_unpaid_orders.count() > 0:
        order = served_unpaid_orders.first()
        if not order['eod_record_date'] is None:
            return {
                'present': True,
                'order_id': order['id']
            }
    return {'present': False}


def initiate_order(data):
    """
    initiates an order that a customer has placed
    """
    # check if the restaurant is not blocked
    try:
        restaurant = Restaurant.objects.get(pk=data['restaurant'])
        if restaurant.status in ['blocked']:
            return {
                'status': 400,
                'message': MESSAGES.get('BLOCKED_RESTAURANT')
            }
        if restaurant.eod_restaurant_status in [1, 2]:
            return {
                'status': 400,
                'message': MESSAGES.get('EOD_IN_PROGRESS')
            }
    except ObjectDoesNotExist:
        return {
            'status': 400,
            'message': MESSAGES.get('RESTAURANT_NOT_FOUND')
        }
    except Exception as error:
        print(f"InitiateOrder-Error: {error}")
        return {
            'status': 400,
            'message': MESSAGES.get('GENERAL_ERROR')
        }

    # if the order items < 1, return error
    if len(data['items']) < 1:
        return {
            'status': 400,
            'message': MESSAGES.get('NO_ORDER_ITEMS')
        }

    order_payment_status = 'pending'
    # check if the mode of payment supported by the table
    if data.get('table') is not None:
        table = Table.objects.get(pk=data['table'])
        if table.prepayment_required:
            # TODO process the payment process
            pass

    # check if the table has any other ongoing order
    ongoing_orders = any_present_ongoing_order(table)
    if ongoing_orders.get('present'):
        return {
            'status': 400,
            'message': 'The table has an ongoing order',
            'data': {
                'order_id': ongoing_orders.get('order_id'),
            }
        }

    # check if all items are available
    order_items = []
    unavailable_items = []
    available_items = []

    total_cost = 0
    discounted_cost = 0
    savings = 0
    actual_cost = 0

    for item in data['items']:
        try:
            menu_item = MenuItem.objects.get(pk=item['item'])

            unit_price = menu_item.primary_price
            effective_unit_price = unit_price
            if menu_item.running_discount:
                if menu_item.discounted_price is not None:
                    effective_unit_price = menu_item.discounted_price

            total_cost = unit_price * item['quantity']
            discounted_cost = effective_unit_price * item['quantity']
            savings = total_cost - discounted_cost
            actual_cost = discounted_cost

            item = {
                'item': str(menu_item.id),
                'item_name': menu_item.name,
                'quantity': item['quantity'],

                'unit_price': unit_price,
                'discounted_price': effective_unit_price,
                'discounted': menu_item.running_discount,

                'total_cost': total_cost,
                'discounted_cost': discounted_cost,
                'savings': savings,
                'actual_cost': actual_cost,

                'available': menu_item.available,
                'status': 'initiated'
            }

            if not menu_item.available:
                item['quantity'] = 0
                item['total_cost'] = 0
                item['discounted_cost'] = 0
                item['savings'] = 0
                item['actual_cost'] = 0
                item['available'] = False
                item['status'] = 'unavailable'
                unavailable_items.append(item)
            else:
                available_items.append(item)

            order_items.append(item)
        except Exception as error:
            print(f"InitiateOrder-CheckItemAvailability-Error: {error}")
            return {
                'status': 400,
                'message': MESSAGES.get('GENERAL_ERROR')
            }

    total_cost = sum([item['total_cost'] for item in available_items])
    discounted_cost = sum([item['discounted_cost'] for item in available_items])
    savings = sum([item['savings'] for item in available_items])
    actual_cost = sum([item['actual_cost'] for item in available_items])

    # TODO save the order with status as initiated
    with transaction.atomic():
        # save the order
        order_data = {
            'restaurant': data['restaurant'],
            'table': data['table'],  # replace with the order details
            'order_remarks': data.get('order_remarks'),

            'total_cost': total_cost,
            'discounted_cost': discounted_cost,
            'savings': savings,
            'actual_cost': actual_cost,
            'prepayment_required': table.prepayment_required,

            'order_status': 'initiated',
            'payment_status': order_payment_status,

            'customer': data.get('customer'),
            'created_by': data.get('created_by')
        }
        order_record = SerializerPutOrder(data=order_data)
        if not order_record.is_valid():
            error_message = ""
            print(order_record.errors)
            for _, value in order_record.errors.items():
                error_message += f"{', '.join(value)}\n"
            return {
                'status': 400,
                'message': error_message
            }

        order_record.save()
        order_id = str(order_record.data['id'])
        # save the order items
        for item in order_items:
            item['order'] = order_id
            # save the order item
            item_record = SerializerPutOrderItem(data=item)
            if not item_record.is_valid():
                raise Exception(item_record.errors)
            item_record.save()

    return {
        'status': 200,
        'message': MESSAGES.get('ORDER_INITIATED'),
        'data': {
            'order_details': {
                'id': order_id,
                'time_created': order_record.data['time_created'],
                'order_number': order_record.data['order_number'],

                'restaurant': data['restaurant'],
                'table': data['table'],
                'table_number': table.number,

                'total_cost': total_cost,
                'discounted_cost': discounted_cost,
                'savings': savings,
                'actual_cost': actual_cost,
                'prepayment_required': table.prepayment_required,

                'no_items': len(order_items),
                'no_unavailable_items': len(unavailable_items),
                'no_available_items': len(available_items),

                'order_status': 'initiated',
                'payment_status': order_payment_status,
            },
            'order_items': order_items,
            'unavailable_items': unavailable_items,
            'available_items': available_items
        }
    }
