from django.db import transaction
from django.db.models import Sum
from datetime import datetime
from typing import Optional, Union
from users_app.models import User
from dinify_backend.configss.messages import MESSAGES
from django.core.exceptions import ObjectDoesNotExist
from restaurants_app.models import Restaurant, MenuItem, Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    PaymentStatus_Pending,
    TransactionStatus_Success
)
from orders_app.models import Order, OrderItem
from orders_app.serializers import SerializerPutOrder, SerializerPutOrderItem
from finance_app.models import DinifyTransaction
from orders_app.controllers.orders.serializers import serialize_order_details


class ConOrder:
    @staticmethod
    def check_options_requirements(order_items: list) -> dict:
        for item in order_items:
            menu_item = MenuItem.objects.get(pk=item['item'])
            item_options = menu_item.options
            if item_options:
                min_selections = item_options.get('min_selections', 0)
                max_selections = item_options.get('max_selections', 0)
                selected_options = item.get('options', [])

                if len(selected_options) < min_selections:
                    return {
                        'status': 400,
                        'message': f"Item {menu_item.name} requires at least {min_selections} option selections."
                    }
                if len(selected_options) > max_selections:
                    return {
                        'status': 400,
                        'message': f"Item {menu_item.name} allows a maximum of {max_selections} option selections."
                    }
        return {'status': 200}

    @staticmethod
    def any_present_ongoing_order(table: Table) -> dict:
        # TODO check if this is an ongoing order
        present_orders = Order.objects.values('id', 'eod_record_date').filter(
            table=table,
            order_status__in=[
                OrderStatus_Initiated,
                OrderStatus_Pending,
                OrderStatus_Preparing
            ]
        ).order_by('-time_created')
        if present_orders.count() > 0:
            order = present_orders.first()
            # if order['eod_record_date'] is None:
            return {
                'present': True,
                'order_id': order['id']
            }

        served_unpaid_orders = Order.objects.values('id', 'eod_record_date').filter(
            table=table,
            order_status=OrderStatus_Served,
            payment_status=PaymentStatus_Pending
        ).order_by('-time_created')

        if served_unpaid_orders.count() > 0:
            order = served_unpaid_orders.first()
            # if order['eod_record_date'] is None:
            return {
                'present': True,
                'order_id': order['id']
            }
        return {'present': False}

    @staticmethod
    def determine_existing_order_item(item:dict, order_id: str) -> bool:
        menu_item = MenuItem.objects.get(pk=item['item'])
        existing_item = OrderItem.objects.filter(
            order__id=order_id,
            item=menu_item,
            deleted=False
        )

        if existing_item.exists():
            extras = item.get('extras')
            existing_item_extras = OrderItem.objects.filter(parent_item=existing_item[0])

            if existing_item_extras.count() == 0:
                if extras is None:
                    return True
                if len(extras) == 0:
                    return True

            if len(extras) == existing_item_extras.count():
                for existing_item in existing_item_extras:
                    if str(existing_item.item.pk) not in extras:
                        return False
                return True

            options = item.get('options')
        return False

    @staticmethod
    def determine_effective_unit_price(menu_item: MenuItem, options: dict = None) -> dict:
        unit_price = menu_item.primary_price
        effective_unit_price = unit_price

        if options is not None:
            for key, value in options.items():
                if not isinstance(key, int):
                    try:
                        key = int(key)
                    except ValueError:
                        return {
                            'status': 400,
                            'message': f'Invalid options selected for item, {menu_item.name}'
                        }
                if key < 0 or any(v < 0 for v in value):
                    return {
                        'status': 400,
                        'message': f'Invalid options selected for item, {menu_item.name}'
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
                    effective_unit_price = unit_price - (unit_price * discount_percentage / 100)
                if discount_amount > 0:
                    effective_unit_price = unit_price - discount_amount

        # add the cost of the options
        cost_of_options = 0
        if options is not None:
            item_options = menu_item.options.get('options', [])
            for key, value in options.items():
                if not isinstance(key, int):
                    try:
                        key = int(key)
                    except ValueError:
                        return {
                            'status': 400,
                            'message': f'Invalid options selected for item, {menu_item.name}'
                        }

                option_item = item_options[key]
                option_price = option_item.get('cost', 0)
                cost_of_options += option_price
                # effective_unit_price += option_price

        effective_unit_price += cost_of_options
        return {
            'status': 200,
            'price': effective_unit_price,
            'cost_of_options': cost_of_options
        }

    @staticmethod
    def process_item_extras(item: dict, order_id: str, order_item_id: str ) -> dict:
        extras = item.get('extras', None)

        if extras is None:
            return

        for extra in extras:
            extra_item = MenuItem.objects.get(pk=extra)
            unit_price = extra_item.primary_price
            quantity = 1  # extra['quantity']

            price_selection = ConOrder.determine_effective_unit_price(menu_item=extra_item)
            if price_selection.get('status') != 200:
                return price_selection

            effective_unit_price = price_selection.get('price')
            total_cost = unit_price * quantity
            discounted_cost = effective_unit_price * quantity
            savings = total_cost - discounted_cost
            actual_cost = discounted_cost

            extra = {
                'order': order_id,
                'parent_item': order_item_id,
                'item': str(extra_item.id),
                'item_name': extra_item.name,
                'quantity': quantity,

                'unit_price': unit_price,
                'discounted_price': effective_unit_price,
                'actual_price': effective_unit_price,
                'discounted': extra_item.running_discount,
                'unit_cost_of_options': 0,

                'total_cost': total_cost,
                'discounted_cost': discounted_cost,
                'savings': savings,
                'actual_cost': actual_cost,
                'cost_of_options': 0,

                'available': extra_item.available,
                'status': 'initiated'
            }

            if not extra_item.available:
                extra['quantity'] = 0
                extra['total_cost'] = 0
                extra['discounted_cost'] = 0
                extra['savings'] = 0
                extra['actual_cost'] = 0
                extra['available'] = False
                extra['status'] = 'unavailable'

            # save the item to the menu items
            extra_record = SerializerPutOrderItem(data=extra)
            if not extra_record.is_valid():
                raise Exception(extra_record.errors)
            extra_record.save()

    @staticmethod
    def update_item_quantity(item: dict, order_id: str) -> dict:
        menu_item = MenuItem.objects.get(pk=item['item'])

        existing_item = OrderItem.objects.get(
            order__id=order_id,
            item=menu_item,
            deleted=False
        )
        new_quantity = existing_item.quantity + item['quantity']
        new_total_cost = existing_item.unit_price * new_quantity
        new_cost_of_options = existing_item.cost_of_options * new_quantity
        new_discounted_cost = existing_item.discounted_price * new_quantity
        new_savings = new_total_cost - new_discounted_cost

        existing_item.quantity = new_quantity
        existing_item.total_cost = new_total_cost
        existing_item.discounted_cost = new_discounted_cost
        existing_item.cost_of_options = new_cost_of_options
        existing_item.savings = new_savings

        existing_item.save()

        return {
            'status': 200,
            'message': 'Order item quantity has been updated successfully.'
        }

    @staticmethod
    def add_order_item(item: dict, order_id: str):
        menu_item = MenuItem.objects.get(pk=item['item'])
        unit_price = menu_item.primary_price

        # check if the item already exists in the order so that we just update the quantity
        existing_item = ConOrder.determine_existing_order_item(item=item, order_id=order_id)
        if existing_item:
            ...
            # return update_item_quantity(item=item, order_id=order_id)

        # handling multiple options
        selected_options = []
        item_options = item.get('options')
        if item_options is not None:
            if isinstance(item_options, dict):
                for key, value in item_options.items():
                    index = None
                    try:
                        index = int(key)
                    except ValueError:
                        pass
                    if isinstance(index, int):
                        option_detail = menu_item.options.get('options')[index]
                        choices = option_detail.get('choices')
                        option_name = option_detail['name']
                        if choices is not None:
                            names = ''
                            if len(choices) > 0:
                                names += ', '.join([choices[v] for v in value if v < len(choices)])
                                # option_name = choices[value]
                            option_name = names
                        option_cost = option_detail['cost']
                        selected_options.append({
                            'name': option_name,
                            'cost': option_cost,
                        })

        price_selection = ConOrder.determine_effective_unit_price(menu_item=menu_item, options=item_options)
        if price_selection.get('status') != 200:
            return price_selection

        effective_unit_price = price_selection.get('price')
        unit_cost_of_options = price_selection.get('cost_of_options')

        total_cost = unit_price * item['quantity']
        discounted_cost = effective_unit_price * item['quantity']
        savings = total_cost - discounted_cost
        actual_cost = discounted_cost
        cost_of_options = unit_cost_of_options * item['quantity']

        item_data = {
            'order': order_id,
            'item': str(menu_item.id),
            'item_name': menu_item.name,
            # 'option': option_name,
            # 'choice': choice,
            # 'option_cost': option_cost,
            'quantity': item['quantity'],

            'options': selected_options,

            'unit_price': unit_price,
            'discounted_price': effective_unit_price,
            'actual_price': effective_unit_price,
            'discounted': menu_item.running_discount,
            'unit_cost_of_options': unit_cost_of_options,

            'total_cost': total_cost,
            'discounted_cost': discounted_cost,
            'savings': savings,
            'actual_cost': actual_cost,
            'cost_of_options': cost_of_options,

            'available': menu_item.available,
            'status': 'initiated'
        }

        if not menu_item.available:
            item_data['quantity'] = 0
            item_data['total_cost'] = 0
            item_data['discounted_cost'] = 0
            item_data['savings'] = 0
            item_data['actual_cost'] = 0
            item_data['available'] = False
            item_data['status'] = 'unavailable'

        # save the item to the menu items
        item_record = SerializerPutOrderItem(data=item_data)
        if not item_record.is_valid():
            raise Exception(item_record.errors)
        item_record.save()

        # process the item extras
        ConOrder.process_item_extras(
            item=item,
            order_id=order_id,
            order_item_id=str(item_record.data['id'])
        )

    @staticmethod
    def update_order_amounts(order: Order) -> dict:
        order_items = OrderItem.objects.select_for_update().filter(
            deleted=False,
            order=order
        )
        total_cost = sum([item.total_cost for item in order_items])
        discounted_cost = sum([item.discounted_cost for item in order_items])
        savings = total_cost - discounted_cost
        actual_cost = discounted_cost

        # get the total payments done on the order
        order_payments = DinifyTransaction.objects.filter(
            order=order,
            transaction_status=TransactionStatus_Success
        )
        total_paid = order_payments.aggregate(
            Sum('transaction_amount')
        )['transaction_amount__sum'] or 0

        balance_payable = actual_cost - total_paid

        order.total_cost = total_cost
        order.discounted_cost = discounted_cost
        order.savings = savings
        order.actual_cost = actual_cost
        order.total_paid = total_paid
        order.balance_payable = balance_payable
        order.save()

    @staticmethod
    def initiate_order(
        restaurant_id: str,
        table_id: str,
        items: list,
        order_remarks: Optional[str] = None,
        customer: Union[User, None] = None,
        created_by: Union[User, None] = None
    ):
        # check that the restaurant is not blocked
        try:
            restaurant = Restaurant.objects.get(pk=restaurant_id)
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

        # check that order items are provided
        if items is None:
            return {
                'status': 400,
                'message': MESSAGES.get('NO_ORDER_ITEMS')
            }

        if len(items) < 1:
            return {
                'status': 400,
                'message': MESSAGES.get('NO_ORDER_ITEMS')
            }

        # for each order item, check if the options are applicable
        options_check = ConOrder.check_options_requirements(items)
        if options_check.get('status') != 200:
            return options_check

        # check that the table does not have any other ongoing order
        table = Table.objects.get(pk=table_id)
        ongoing_orders = ConOrder.any_present_ongoing_order(table)
        if ongoing_orders.get('present'):
            return {
                'status': 400,
                'message': 'The table has an ongoing order',
                'data': {
                    'order_id': ongoing_orders.get('order_id'),
                }
            }

        # initiate the order object
        customer = customer.pk if customer else None
        order_data = {
            'restaurant': restaurant_id,
            'table': table_id,
            'order_remarks': order_remarks,

            'total_cost': 0,
            'discounted_cost': 0,
            'savings': 0,
            'actual_cost': 0,
            'prepayment_required': table.prepayment_required,

            'order_status': 'initiated',
            'payment_status': 'pending',

            'customer': customer,
            'created_by': created_by
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

        with transaction.atomic():
            order_record.save()
            order_id = order_record.data['id']

            for item in items:
                ConOrder.add_order_item(item=item, order_id=order_id)
            order_rec = Order.objects.select_for_update().get(id=order_id)
            ConOrder.update_order_amounts(order=order_rec)

        order_rec.refresh_from_db()

        order_details = serialize_order_details(order=order_rec)
        return {
            'status': 200,
            'message': MESSAGES.get('ORDER_INITIATED'),
            'data': {
                'order_details': order_details.get('order'),
                'order_items': order_details.get('order_items'),
                'available_items': order_details.get('available_items'),
                'unavailable_items': order_details.get('unavailable_items'),
                'extras': order_details.get('extras'),
                'available_extras': order_details.get('available_extras'),
                'unavailable_extras': order_details.get('unavailable_extras')
            }
        }