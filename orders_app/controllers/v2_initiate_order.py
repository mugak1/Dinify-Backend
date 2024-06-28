from datetime import datetime
from typing import Optional, Union
from django.db import transaction
from restaurants_app.models import Restaurant, MenuItem, Table
from django.core.exceptions import ObjectDoesNotExist
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing
)
from orders_app.serializers import SerializerPutOrder, SerializerPutOrderItem
from orders_app.models import Order


def determine_effective_unit_price(
    menu_item: MenuItem,
    option: Optional[int] = None
) -> dict:
    unit_price = menu_item.primary_price
    effective_unit_price = unit_price

    if option is not None:
        if option < 0:
            return {
                'status': 400,
                'message': f'Invalid item option selected for item, {menu_item.name}'
            }

    # determine the option considered
    item_options = menu_item.options.get('options', [])
    if option is not None:
        if option < len(item_options):
            effective_unit_price = item_options[option].get('cost')
            if effective_unit_price is None:
                return {
                    'status': 400,
                    'message': f'Selected option for item, {menu_item.name}, has no cost'
                }
            return {
                'status': 200,
                'price': effective_unit_price
            }
        else:
            return {
                'status': 400,
                'message': f'Invalid item option selected for item, {menu_item.name}'
            }

    # consideration for the discount
    if menu_item.running_discount:
        if menu_item.discounted_price is not None:
            effective_unit_price = menu_item.discounted_price

    if menu_item.consider_discount_object:
        run_discount = False

        discount = menu_item.discount_details
        recurring_days = discount.get('recurring_days', [])
        start_date = discount.get('start_date', '')
        end_date = discount.get('end_date', '')
        start_time = discount.get('start_time', '')
        end_time = discount.get('end_time', '')
        discount_percentage = discount.get('discount_percentage', 0.0)
        discount_amount = discount.get('discount_amount', 0.0)

        # check if the discount is applicable
        # check if the discount is recurring
        # check if the discount is within the time frame
        # check if the discount is within the date frame
        time_now = datetime.now()
        if len(recurring_days) > 0:
            today = time_now.date().isoweekday()
            if today in recurring_days:
                run_discount = True
        if start_date != '':
            # parse the start date to date
            start_date = datetime.strptime(start_date, '%Y-%m-%d')
            if time_now.date() <= start_date.date():
                run_discount = False
        if end_date != '':
            # parse the end date to date
            end_date = datetime.strptime(end_date, '%Y-%m-%d')
            if time_now.date() >= end_date.date():
                run_discount = False

        if run_discount:
            if start_time != '' and start_date != '':
                # parse the start time to time
                start_time = datetime.strptime(f'{start_time}:00', '%H:%M:%S')
                if time_now.time() <= start_time.time():
                    run_discount = False
            if end_time != '' and end_date != '':
                # parse the end time to time
                end_time = datetime.strptime(f'{end_time}:00', '%H:%M:%S')
                if time_now.time() >= end_time.time():
                    run_discount = False

        if run_discount:
            if discount_percentage > 0:
                effective_unit_price = unit_price - (unit_price * discount_percentage/100)
            if discount_amount > 0:
                effective_unit_price = unit_price - discount_amount

    return {
        'status': 200,
        'price': effective_unit_price
    }


def process_order_item(
    item: dict,
    order_id: str
) -> dict:
    # check for item availability
    # check for item discount
    # process item options
    # process item extras
    menu_item = MenuItem.objects.get(pk=item['item'])
    unit_price = menu_item.primary_price

    option = item.get('option', None)
    option_cost = None
    choice = item.get('choice', None)
    option_name = None
    if option is not None:
        option = menu_item.options.get('options')[option]
        if choice is not None:
            choice = option['choices'][choice]
        option_name = option['name']
        option_cost = option['cost']

    price_selection = determine_effective_unit_price(menu_item=menu_item)
    if price_selection.get('status') != 200:
        return price_selection

    effective_unit_price = price_selection.get('price')
    if option is not None:
        effective_unit_price = option_cost
    total_cost = unit_price * item['quantity']
    discounted_cost = effective_unit_price * item['quantity']
    savings = total_cost - discounted_cost
    actual_cost = discounted_cost

    item = {
        'order': order_id,
        'item': str(menu_item.id),
        'item_name': menu_item.name,
        'option': option_name,
        'choice': choice,
        'option_cost': option_cost,
        'quantity': item['quantity'],

        'unit_price': unit_price,
        'discounted_price': effective_unit_price,
        'actual_price': effective_unit_price,
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

    # save the item to the menu items
    item_record = SerializerPutOrderItem(data=item)
    if not item_record.is_valid():
        raise Exception(item_record.errors)
    item_record.save()


    return {
        'status': 200,
    }
