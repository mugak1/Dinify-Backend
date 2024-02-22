from restaurants_app.models import Restaurant, MenuItem, Table
from django.core.exceptions import ObjectDoesNotExist
from dinify_backend.configs import MESSAGES


def any_present_ongoing_order(table):
    """
    determines if a table has an ongoing order
    """


def initiate_order(data):
    """
    initiates an order that a customer has placed
    """

    # check if the table has any other ongoing order

    # check if the restaurant is not blocked
    try:
        restaurant = Restaurant.objects.get(pk=data['restaurant'])
        if restaurant.status in ['blocked']:
            return {
                'status': 400,
                'message': MESSAGES.get('BLOCKED_RESTAURANT')
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

    # check if all items are available
    order_items = []
    unavailable_items = []
    available_items = []

    order_total = 0
    order_discount = 0
    order_cost_payable = 0

    for item in data['items']:
        try:
            menu_item = MenuItem.objects.get(pk=item['item'])

            unit_price = menu_item.primary_price
            effective_unit_price = unit_price
            if menu_item.running_discount:
                effective_unit_price = menu_item.discounted_price
            savings = (unit_price - effective_unit_price) * item['quantity']
            item = {
                'id': menu_item.id,
                'item': menu_item.name,
                'quantity': item['quantity'],
                'unit_price': unit_price,
                'discounted': menu_item.running_discount,
                'discounted_price': menu_item.discounted_price,
                'total_price': effective_unit_price * item['quantity'],
                'savings': savings,
            }

            if not menu_item.available:
                item['quantity'] = 0
                item['total_price'] = 0
                item['discounted_price'] = 0
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

    # make the order total from the available items
    order_total = sum([item['total_price'] for item in available_items])
    # get the discount
    order_discount = sum([item['savings'] for item in available_items])
    # the cost payable
    order_cost_payable = sum([item['total_price'] for item in available_items])

    # TODO save the order with status as initiated

    return {
        'status': 200,
        'message': MESSAGES.get('ORDER_INITIATED'),
        'data': {
            'order_details': {
                'id': 'dummy_order_id',
                'table': '',
                'no_items': len(order_items),
                'total': order_total,
                'discount': order_discount,
                'cost_payable': order_cost_payable,
                'order_status': 'initiated',
                'payment_status': order_payment_status,
            },
            'unavailable_items': unavailable_items,
            'available_items': available_items
        }
    }
