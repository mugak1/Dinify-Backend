from restaurants_app.models import Table, MenuSection
from restaurants_app.serializers import SerializerPublicGetTableDetails, SerializerGetFullMenu
from dinify_backend.configss.messages import OK_SCANNED_TABLE, OK_RETRIEVED_FULL_MENU
from orders_app.models import Order
from orders_app.serializers import SerializerPublicOrderDetails


def handle_table_scan(table_id: str) -> dict:
    table = Table.objects.get(id=table_id)
    table_data = SerializerPublicGetTableDetails(
        table, many=False
    ).data
    return {
        'status': 200,
        'message': OK_SCANNED_TABLE,
        'data': table_data
    }


def handle_show_menu(restaurant_id: str) -> dict:
    sections = MenuSection.objects.filter(
        restaurant=restaurant_id
    )
    menu_data = SerializerGetFullMenu(
        sections, many=True
    ).data

    return {
        'status': 200,
        'message': OK_RETRIEVED_FULL_MENU,
        'data': menu_data
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