from django.core.exceptions import ValidationError
from django.utils import timezone

from restaurants_app.models import MenuSection, MenuItem, UpsellConfig
from restaurants_app.serializers import (
    SerializerPublicGetTableDetails, SerializerGetFullMenu, UpsellConfigSerializer
)
from restaurants_app.controllers.menu_publication import (
    resolve_public_restaurant, section_operationally_visible,
    build_safe_extras_map,
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
    # Resolve the restaurant ONCE, failing closed (missing → 400; malformed /
    # unknown / soft-deleted / pending / rejected / inactive / blocked → one
    # generic non-disclosing 404). Reused below for the upsell config and sort
    # mode — no separate unscoped lookups per response component.
    restaurant, error = resolve_public_restaurant(restaurant_id)
    if error is not None:
        return error

    # ONE evaluation time governs the whole response — section filtering,
    # is_currently_active, item visibility and upsell eligibility all read it — so
    # the response can never include a section by one clock reading and then
    # serialize it inactive by another sampled milliseconds later. Captured in the
    # LOCAL timezone (EAT) because is_section_currently_active reads the day/hour of
    # the supplied `now` directly (it only converts to settings.TIME_ZONE when it
    # samples the clock itself). `timezone` is a module-level name so tests can pin it.
    now = timezone.localtime()

    # Structurally-published sections (query), then the canonical operational
    # (available + schedule) gate at the captured time (Python — schedule is JSON,
    # section count is bounded).
    sections = [
        section
        for section in MenuSection.objects.filter(
            restaurant=restaurant, approved=True, enabled=True, deleted=False,
        )
        if section_operationally_visible(section, now)
    ]

    # Batch-resolve every safe nested extra referenced across these sections' items
    # ONCE, so the item serializer never does a per-extra global lookup.
    referenced_items = list(
        MenuItem.objects
        .filter(section__in=sections, deleted=False)
        .only('id', 'extras_applicable')
    )
    extras_map = build_safe_extras_map(referenced_items, restaurant.id)

    # The trusted, internal publication context — NOT caller-controllable. Its
    # presence is the ONLY thing that switches the shared serializers into strict
    # public mode; its absence (management / unit tests) leaves them untouched.
    menu_policy = {
        'now': now,
        'restaurant_id': str(restaurant.id),
        'extras_map': extras_map,
    }

    menu_data = SerializerGetFullMenu(
        sections, many=True, context={'menu_policy': menu_policy},
    ).data

    # Bundle upsell config (when enabled). The public carousel inherits the SAME
    # publication policy, so an item hidden from the main menu (by its section,
    # group, schedule or its own state) cannot re-enter via upsell.
    upsell_data = None
    try:
        upsell_config = UpsellConfig.objects.get(restaurant=restaurant)
        if upsell_config.enabled:
            upsell_data = UpsellConfigSerializer(
                upsell_config,
                context={'public_only': True, 'menu_policy': menu_policy},
            ).data
    except UpsellConfig.DoesNotExist:
        pass

    # Operator sort mode from the already-resolved restaurant (no extra query).
    # Items themselves stay in listing_position order; the backend does not re-sort.
    item_sort_mode = restaurant.menu_item_sort_mode or 'manual'

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
