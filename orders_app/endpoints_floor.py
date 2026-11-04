"""
The floor view (``api/v1/floor/``, Step 2, PR BE-D) — floor contract v1.

TWO READS AND TWO COMMANDS, every one gated on the ``tables`` module at the
restaurant named by ``?restaurant=`` (reads) or ``{"restaurant": ...}`` (commands),
and every authorization or scope failure answered with ONE non-disclosing
``404 {"status":404,"message":"Not found"}``: an unknown restaurant, one the caller
holds no ``tables`` access at, a table or order of another restaurant and an
unknown id are indistinguishable. A missing ``restaurant`` is a 400. Every response
is ``no-store``. None of the four is on the delegated ``ALLOWED_ROUTES``, so a
delegated session is refused before dispatch.

THE COMMAND VIEWS ARE THIN ADAPTERS over ``round_release.confirm`` / ``decline``,
which own the locks, the re-checks and the writes, exactly as the kitchen's views
are adapters over ``kitchen_transition.execute``.

THE READS take no lock, open no transaction and write nothing.

* OCCUPANCY IS ``occupancy.ongoing_order_q`` — never ``Table.status``, which is a
  separate, staff-set floor axis and is reported as it is stored. Each ongoing order
  is one round. An order with no placement (accepted before Step 1, or with the
  writer off) is a RELEASED round with ``visit: null`` and ``round_number: 1``.
* ``stale``, ``needs_staff``, ``occupied`` and ``waiting_for_staff`` (the number of
  HELD ROUNDS on the floor) are computed here, at one ``server_time``.
* Tables needing staff come first, then the rest; table-number order within each.
* THERE IS NO PAYMENT CLAIM: the only payment word is a visit's ``payment:
  "unknown"``, a constant read from no column (``round_release.visit_object``).
* COST, constant in the number of tables and rounds: after the module gate, the
  floor read is three statements (restaurant, tables, rounds); the drawer is three
  (table, rounds, lines) plus ONE catalogue statement per HELD round for its
  ``cannot_be_made`` check. Both pinned in ``tests_floor_reads``.
"""
import uuid as uuid_module

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from dinify_backend.configss.string_definitions import MODULE_TABLES
from misc_app.controllers.http import private_no_store
from orders_app.controllers.occupancy import ongoing_order_q
from orders_app.controllers.orders.serializers import historical_name
from orders_app.controllers.services import round_release
from orders_app.controllers.services.round_placement import placement_of
from orders_app.controllers.services.round_release import (
    FLOOR_CONTRACT,
    STATE_HELD,
    RoundNotFound,
    RoundRefusal,
    accepted_at_of,
    item_count_expression,
    iso,
    round_object,
    visit_object,
)
from orders_app.models import Order, OrderItem
from restaurants_app.models import Restaurant, Table
from users_app.controllers.permissions_check import can_user_access_module

NOT_FOUND = {'status': 404, 'message': 'Not found'}
RESTAURANT_REQUIRED = {'status': 400, 'message': 'restaurant is required'}


def _not_found():
    return Response(NOT_FOUND, status=404)


def _scope(user, raw):
    """``(restaurant_uuid, None)`` or ``(None, error_response)``.

    Missing or blank is a 400; malformed, unknown or not the caller's is the one
    404. The module gate is asked of a PARSED id, so a malformed value never reaches
    the resolver's queries.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, Response(RESTAURANT_REQUIRED, status=400)
    if not isinstance(raw, str):
        return None, _not_found()
    try:
        restaurant_id = uuid_module.UUID(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return None, _not_found()
    if not can_user_access_module(user, str(restaurant_id), MODULE_TABLES):
        return None, _not_found()
    return restaurant_id, None


def _live_rounds(queryset):
    """The ongoing orders in ``queryset``, ready for ``round_object`` with no
    further query: placement, visit and acceptance by LEFT JOIN, ``item_count`` by a
    scalar subquery."""
    return (
        queryset
        .filter(ongoing_order_q())
        .select_related('placement', 'placement__visit', 'acceptance')
        .annotate(item_count=item_count_expression())
        .order_by('time_created', 'id')
    )


def _round_sort_key(order):
    accepted_at, _ = accepted_at_of(order)
    return (accepted_at, order.time_created, str(order.pk))


def _table_label(table):
    """The label the kitchen ticket shows (``serializers_kitchen``), so the floor
    and the board name a table the same way."""
    return table.display_name or table.str_number or str(table.number)


def _table_object(table, orders, now, round_builder=None):
    """The contract's table object. ``orders`` are the table's ongoing orders."""
    orders = sorted(orders, key=_round_sort_key)
    build = round_builder or (lambda order: round_object(order, now=now))
    rounds = [build(order) for order in orders]
    # The table's visit: that of its first round that has one. Until Step 3 a table
    # normally holds one ongoing round, so this is simply that round's visit.
    visit = None
    for order in orders:
        placement = placement_of(order)
        if placement is not None:
            visit = placement.visit
            break
    return {
        'id': str(table.pk),
        'number': table.number,
        'display_name': _table_label(table),
        'area_id': str(table.dining_area_id) if table.dining_area_id else None,
        # The staff-set FLOOR axis, as stored. Not occupancy.
        'status': table.status,
        'occupied': bool(rounds),
        'needs_staff': any(r['state'] == STATE_HELD for r in rounds),
        'visit': visit_object(visit),
        'rounds': rounds,
    }


def _floor_order(tables):
    """Tables needing staff first, then the rest, each in table-number order."""
    return sorted(
        tables,
        key=lambda pair: (not pair[1]['needs_staff'], pair[0].number,
                          pair[0].str_number, str(pair[0].pk)),
    )


def _line(row, children):
    name, _provenance = historical_name(row)
    snapshot = row.modifiers_snapshot
    modifiers = [m if isinstance(m, str) else str(m)
                 for m in snapshot] if isinstance(snapshot, list) else []
    return {
        'name': name,
        'quantity': row.quantity,
        'extras': [
            {'name': historical_name(child)[0], 'quantity': child.quantity}
            for child in children
        ],
        'modifiers': modifiers,
        # No order line records a note (the item-note emit was removed), so this is
        # always null; the key is kept because the contract carries it.
        'note': None,
    }


def _items(rows):
    """The round's lines as the kitchen lists them: available main dishes, each
    with its available extras. Names and modifiers are the SNAPSHOTS saved at
    acceptance, never today's catalogue."""
    shown = [row for row in rows if row.available]
    children = {}
    for row in shown:
        if row.parent_item_id is not None:
            children.setdefault(row.parent_item_id, []).append(row)
    return [
        _line(row, children.get(row.pk, []))
        for row in shown if row.parent_item_id is None
    ]


class _FloorView(APIView):
    """Authenticated by the default customer stack; every response ``no-store``."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return private_no_store(response)


class FloorView(_FloorView):
    """GET ``api/v1/floor/?restaurant=<uuid>`` — every table and its live rounds."""

    def get(self, request):
        restaurant_id, error = _scope(request.user, request.GET.get('restaurant'))
        if error is not None:
            return error

        restaurant = (
            Restaurant.objects
            .filter(pk=restaurant_id, deleted=False)
            .values('id', 'accepting_orders', 'service_configuration__payment_timing')
            .first()
        )
        if restaurant is None:
            return _not_found()

        now = timezone.now()
        tables = list(
            Table.objects
            .filter(restaurant_id=restaurant_id, deleted=False)
            .order_by('number', 'str_number', 'id')
        )
        by_table = {}
        for order in _live_rounds(Order.objects.filter(restaurant_id=restaurant_id)):
            by_table.setdefault(order.table_id, []).append(order)

        built = [(table, _table_object(table, by_table.get(table.pk, []), now))
                 for table in tables]
        ordered = [obj for _table, obj in _floor_order(built)]

        data = {
            'contract': FLOOR_CONTRACT,
            'server_time': iso(now),
            'restaurant': {
                'id': str(restaurant['id']),
                'accepting_orders': bool(restaurant['accepting_orders']),
                # The configured value, or null when nothing is configured.
                # (`not_chosen` is a visit's word and is not emitted here.)
                'payment_timing': restaurant['service_configuration__payment_timing'],
            },
            'waiting_for_staff': sum(
                1 for table in ordered for r in table['rounds']
                if r['state'] == STATE_HELD),
            'tables': ordered,
        }
        return Response({'status': 200, 'message': 'Floor retrieved', 'data': data},
                        status=200)


class FloorTableView(_FloorView):
    """GET ``api/v1/floor/tables/<uuid>/?restaurant=<uuid>`` — the table drawer: the
    same table object, each round carrying ``items`` and ``cannot_be_made``."""

    def get(self, request, table_id):
        restaurant_id, error = _scope(request.user, request.GET.get('restaurant'))
        if error is not None:
            return error
        try:
            table = (
                Table.objects
                .filter(pk=table_id, restaurant_id=restaurant_id, deleted=False)
                .first()
            )
        except (ValidationError, ValueError, TypeError):
            table = None
        if table is None:
            return _not_found()

        now = timezone.now()
        orders = list(_live_rounds(
            Order.objects.filter(restaurant_id=restaurant_id, table_id=table.pk)))
        rows_by_order = {}
        if orders:
            for row in (OrderItem.objects
                        .filter(order__in=[o.pk for o in orders], deleted=False)
                        .order_by('time_created', 'id')):
                rows_by_order.setdefault(row.order_id, []).append(row)

        def detailed(order):
            base = round_object(order, now=now)
            rows = rows_by_order.get(order.pk, [])
            base['items'] = _items(rows)
            # The release makeability check Confirm would run, for a HELD round only.
            base['cannot_be_made'] = (
                round_release.cannot_be_made(order, rows, now)
                if base['state'] == STATE_HELD else None
            )
            return base

        data = _table_object(table, orders, now, round_builder=detailed)
        return Response({'status': 200, 'message': 'Table retrieved', 'data': data},
                        status=200)


class _RoundCommandView(_FloorView):
    """POST ``{"restaurant": "<uuid>"}``. HTTP in, HTTP out; no rule lives here."""

    #: ``round_release.confirm`` or ``round_release.decline``.
    command = None

    def post(self, request, order_id):
        body = request.data
        if not isinstance(body, dict):
            return Response(
                {'status': 400, 'message': 'The request body must be a JSON object.'},
                status=400)
        restaurant = body.get('restaurant')
        if restaurant is None or (isinstance(restaurant, str) and not restaurant.strip()):
            return Response(RESTAURANT_REQUIRED, status=400)
        if isinstance(restaurant, str):
            restaurant = restaurant.strip()
        try:
            result = type(self).command(order_id, request.user, restaurant)
        except RoundNotFound:
            return _not_found()
        except RoundRefusal as refusal:
            return Response(refusal.body(), status=refusal.status)
        return Response(
            {'status': 200, 'message': result['message'],
             'data': {'round': result['round'], 'changed': result['changed']}},
            status=200,
        )


class RoundConfirmView(_RoundCommandView):
    """POST ``api/v1/floor/rounds/<order>/confirm/`` — release a held round."""
    command = staticmethod(round_release.confirm)


class RoundDeclineView(_RoundCommandView):
    """POST ``api/v1/floor/rounds/<order>/decline/`` — decline a held round. The
    body carries no reason: the server records ``declined_by_staff``."""
    command = staticmethod(round_release.decline)
