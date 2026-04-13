from restaurants_app.models import Table, MenuSection, UpsellConfig
from restaurants_app.serializers import (
    SerializerPublicGetTableDetails, SerializerGetFullMenu, UpsellConfigSerializer
)
from dinify_backend.configss.messages import OK_SCANNED_TABLE, OK_RETRIEVED_FULL_MENU
from orders_app.models import Order
from orders_app.serializers import SerializerPublicOrderDetails
from finance_app.models import DinifyTransaction


def handle_table_scan(table_id: str) -> dict:
    table = Table.objects.get(id=table_id)
    table_data = SerializerPublicGetTableDetails(
        table, many=False
    ).data
    # check if the table is reserved
    if table.reserved:
        return {
            'status': 400,
            'message': 'This table is reserved. Please contact the restaurant staff for assistance.', # noqa
        }
    return {
        'status': 200,
        'message': OK_SCANNED_TABLE,
        'data': table_data
    }


def handle_show_menu(restaurant_id: str, ignore_approval: str) -> dict:
    filters = {
        'restaurant': restaurant_id,
        'approved': True,
        'enabled': True,
        'available': True,
        'deleted': False
    }

    if ignore_approval in ['true', True]:
    # if ignore_approval is None:
        filters.pop('approved')
        filters.pop('enabled')

    sections = MenuSection.objects.filter(**filters)

    menu_data = SerializerGetFullMenu(
        sections,
        many=True,
        context={'ignore_approval': ignore_approval}
    ).data

    # Bundle upsell config (when enabled) so the diner basket can render
    # the "You might also like" carousel without an extra round-trip.
    upsell_data = None
    try:
        upsell_config = UpsellConfig.objects.get(restaurant_id=restaurant_id)
        if upsell_config.enabled:
            upsell_data = UpsellConfigSerializer(upsell_config).data
    except UpsellConfig.DoesNotExist:
        pass

    return {
        'status': 200,
        'message': OK_RETRIEVED_FULL_MENU,
        'data': menu_data,
        'upsell': upsell_data
    }


def handle_show_order_details(order_id: str) -> dict:
    if order_id is None:
        response = {
            'status': 400,
            'message': 'Please provide the order id'
        }
        return response

    order = Order.objects.get(id=order_id)
    response = {
        'status': 200,
        'message': 'Successfully retrieved the order details',
        'data':  SerializerPublicOrderDetails(order, many=False).data
    }
    return response


def handle_show_transaction_details(transaction_id: str) -> dict:
    if transaction_id is None:
        response = {
            'status': 400,
            'message': 'Please provide the transaction reference'
        }
        return response

    transaction_record = DinifyTransaction.objects.values(
        'id', 'order', 'transaction_amount', 'transaction_status',
        'order__review', 'order__rating'
    ).get(id=transaction_id)

    response = {
        'status': 200,
        'message': 'Successfully retrieved the transaction details',
        'data': transaction_record
    }
    return response
