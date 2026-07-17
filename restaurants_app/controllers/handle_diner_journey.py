from django.core.exceptions import ValidationError

from restaurants_app.models import MenuSection, UpsellConfig, Restaurant
from restaurants_app.serializers import (
    SerializerPublicGetTableDetails, SerializerGetFullMenu, UpsellConfigSerializer
)
from restaurants_app.controllers.diner_capability import (
    resolve_qr_credential, issue_table_session, require_table_session,
    credential_from_request, DinerCapabilityError,
)
from dinify_backend.configss.messages import (
    OK_SCANNED_TABLE, OK_RETRIEVED_FULL_MENU, ERR_TABLE_REFERENCE_REQUIRED,
)
from orders_app.models import Order
from orders_app.serializers import SerializerPublicOrderDetails
from finance_app.models import DinifyTransaction


def handle_table_scan(request) -> dict:
    # Anonymous QR entry point. The ONLY authority is the opaque, signed QR
    # CREDENTIAL presented in the X-Diner-Credential header — never a raw table
    # UUID (a leaked or guessed id must not mint a session). A successful scan
    # mints a short-lived diner table SESSION that every downstream anonymous op
    # requires.
    credential = credential_from_request(request)
    if not credential:
        return {'status': 400, 'message': ERR_TABLE_REFERENCE_REQUIRED}
    try:
        table = resolve_qr_credential(credential)
    except DinerCapabilityError as exc:
        return {'status': exc.status, 'message': exc.message}

    # A reserved table blocks the diner (is_available_for_scan does not cover it).
    if table.reserved:
        return {
            'status': 400,
            'message': 'This table is reserved. Please contact the restaurant staff for assistance.', # noqa
        }

    table_data = SerializerPublicGetTableDetails(table, many=False).data
    # The short-lived capability the diner presents on every subsequent op.
    table_data['session_token'] = issue_table_session(table)
    return {
        'status': 200,
        'message': OK_SCANNED_TABLE,
        'data': table_data
    }


def handle_show_menu(restaurant_id: str) -> dict:
    from restaurants_app.controllers.utils.schedule_utils import (
        is_section_currently_active,
    )

    # Diner publication contract: a section only reaches a diner when it is
    # approved, enabled, available and not soft-deleted. These predicates are
    # unconditional — there is deliberately no caller-controlled bypass.
    filters = {
        'restaurant': restaurant_id,
        'approved': True,
        'enabled': True,
        'available': True,
        'deleted': False
    }

    sections = MenuSection.objects.filter(**filters)
    # Schedule is stored as JSON; can't filter at queryset level cleanly.
    # Section count is bounded so Python-side filter is fine.
    sections = [s for s in sections if is_section_currently_active(s)]

    menu_data = SerializerGetFullMenu(
        sections,
        many=True
    ).data

    # Bundle upsell config (when enabled) so the diner basket can render
    # the "You might also like" carousel without an extra round-trip.
    upsell_data = None
    try:
        upsell_config = UpsellConfig.objects.get(restaurant_id=restaurant_id)
        if upsell_config.enabled:
            # public_only prunes carousel entries whose menu item is no longer
            # published (unapproved / disabled / soft-deleted) so an unpublished
            # item cannot re-enter the anonymous diner payload via upsell.
            upsell_data = UpsellConfigSerializer(
                upsell_config, context={'public_only': True}
            ).data
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


def handle_show_order_details(request) -> dict:
    # Bind the read to the diner SESSION and an order on that session's table —
    # order-UUID knowledge alone is no longer authority (the BOLA fix).
    try:
        table = require_table_session(request)
    except DinerCapabilityError as exc:
        return {'status': exc.status, 'message': exc.message}

    order_id = request.GET.get('order')
    if order_id is None:
        return {'status': 400, 'message': 'Please provide the order id'}

    # Scope the lookup to the session's restaurant+table. A foreign / unknown /
    # malformed id all collapse to ONE non-disclosing 404.
    try:
        order = Order.objects.get(
            id=order_id,
            restaurant_id=table.restaurant_id,
            table_id=table.id,
        )
    except (Order.DoesNotExist, ValidationError, ValueError):
        return {'status': 404, 'message': 'Order not found'}

    return {
        'status': 200,
        'message': 'Successfully retrieved the order details',
        'data': SerializerPublicOrderDetails(order, many=False).data,
    }


def handle_show_transaction_details(request) -> dict:
    # Payment-detail lookup shares the one anonymous capability boundary: require
    # the session, and the transaction's order must belong to the session's table.
    try:
        table = require_table_session(request)
    except DinerCapabilityError as exc:
        return {'status': exc.status, 'message': exc.message}

    transaction_id = request.GET.get('transaction')
    if transaction_id is None:
        return {'status': 400, 'message': 'Please provide the transaction reference'}

    try:
        record = DinifyTransaction.objects.select_related('order').get(id=transaction_id)
    except (DinifyTransaction.DoesNotExist, ValidationError, ValueError):
        return {'status': 404, 'message': 'Transaction not found'}

    # A null-order transaction is not diner-scoped; a transaction whose order is on
    # another table/restaurant is not this diner's. Both → non-disclosing 404.
    order = record.order
    if (
        order is None
        or order.restaurant_id != table.restaurant_id
        or order.table_id != table.id
    ):
        return {'status': 404, 'message': 'Transaction not found'}

    return {
        'status': 200,
        'message': 'Successfully retrieved the transaction details',
        'data': {
            'id': str(record.id),
            'order': str(record.order_id),
            'transaction_amount': record.transaction_amount,
            'transaction_status': record.transaction_status,
        },
    }
