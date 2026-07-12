import uuid

from django.core.exceptions import ValidationError

from restaurants_app.models import Table, MenuSection, UpsellConfig, Restaurant
from restaurants_app.serializers import (
    SerializerPublicGetTableDetails, SerializerGetFullMenu, UpsellConfigSerializer
)
from dinify_backend.configss.messages import (
    OK_SCANNED_TABLE, OK_RETRIEVED_FULL_MENU,
    ERR_TABLE_REFERENCE_REQUIRED, ERR_TABLE_REFERENCE_INVALID,
    ERR_TABLE_UNAVAILABLE,
)
from orders_app.models import Order
from orders_app.serializers import SerializerPublicOrderDetails
from finance_app.models import DinifyTransaction


def handle_table_scan(table_id: str) -> dict:
    # Public AllowAny endpoint: diners aren't authenticated, so the protection
    # here is input validation + table-state gating, not authorization.
    raw = '' if table_id is None else str(table_id).strip()
    if not raw:
        return {'status': 400, 'message': ERR_TABLE_REFERENCE_REQUIRED}
    try:
        resolved_id = uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return {'status': 400, 'message': ERR_TABLE_REFERENCE_INVALID}

    # select_related collapses the restaurant/dining_area FK lookups the
    # serializer would otherwise issue lazily, per row.
    table = (
        Table.objects
        .select_related('restaurant', 'dining_area')
        .filter(id=resolved_id)
        .first()
    )
    # An unknown id or a removed/disabled/inactive/out-of-service table must
    # not resolve into an orderable session. 404 (not 403) keeps the diner
    # app's shared 403->logout interceptor out of it and does not confirm the
    # existence of an unknown id.
    if table is None or not table.is_available_for_scan():
        return {'status': 404, 'message': ERR_TABLE_UNAVAILABLE}

    # check if the table is reserved
    if table.reserved:
        return {
            'status': 400,
            'message': 'This table is reserved. Please contact the restaurant staff for assistance.', # noqa
        }

    table_data = SerializerPublicGetTableDetails(
        table, many=False
    ).data
    return {
        'status': 200,
        'message': OK_SCANNED_TABLE,
        'data': table_data
    }


def handle_show_menu(restaurant_id: str, ignore_approval: str) -> dict:
    from restaurants_app.controllers.utils.schedule_utils import (
        is_section_currently_active,
    )

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
    # Schedule is stored as JSON; can't filter at queryset level cleanly.
    # Section count is bounded so Python-side filter is fine.
    sections = [s for s in sections if is_section_currently_active(s)]

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

    # Surface the operator's chosen sort mode so the diner frontend can apply
    # the matching sort. Items themselves stay in listing_position order; the
    # backend does not re-sort. Defaults to 'manual' if the restaurant is absent.
    item_sort_mode = (
        Restaurant.objects
        .filter(id=restaurant_id)
        .values_list('menu_item_sort_mode', flat=True)
        .first()
    ) or 'manual'

    return {
        'status': 200,
        'message': OK_RETRIEVED_FULL_MENU,
        'data': menu_data,
        'upsell': upsell_data,
        'item_sort_mode': item_sort_mode
    }


def handle_show_order_details(order_id: str) -> dict:
    if order_id is None:
        response = {
            'status': 400,
            'message': 'Please provide the order id'
        }
        return response

    # Public AllowAny path with a client-supplied id: a malformed (non-UUID) or
    # nonexistent id must return a clean 4xx, not a 500.
    try:
        order = Order.objects.get(id=order_id)
    except ValidationError:
        return {'status': 400, 'message': 'Invalid order id'}
    except Order.DoesNotExist:
        return {'status': 404, 'message': 'Order not found'}

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

    # Public AllowAny path with a client-supplied id: a malformed (non-UUID) or
    # nonexistent id must return a clean 4xx, not a 500.
    try:
        transaction_record = DinifyTransaction.objects.values(
            'id', 'order', 'transaction_amount', 'transaction_status'
        ).get(id=transaction_id)
    except ValidationError:
        return {'status': 400, 'message': 'Invalid transaction reference'}
    except DinifyTransaction.DoesNotExist:
        return {'status': 404, 'message': 'Transaction not found'}

    response = {
        'status': 200,
        'message': 'Successfully retrieved the transaction details',
        'data': transaction_record
    }
    return response
