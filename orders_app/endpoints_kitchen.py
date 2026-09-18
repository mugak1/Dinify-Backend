"""
Kitchen view endpoints (api/v1/kitchen/).

THE WRITE VIEWS ARE THIN ADAPTERS (D05). Every order command — advance, serve,
correct, recall, cancel, priority — parses its body, delegates to
``orders_app.controllers.services.kitchen_transition.execute``, and renders the
envelope. No business rule lives here: the transition service owns the lock
order, the re-read, the authoritative permission decision, eligibility, the
revision precondition, the recall window and occupancy. Two adapters over one
boundary cannot drift into two opinions, which is what the split rules used to
do.

Module-gated through the central permission resolver: access honours the
owner-configured Roles & Access grid for the `kitchen` module
(can_user_access_module / MODULE_KITCHEN), so revoking/granting kitchen on a role
takes server-side effect. The in-progress goodwill-cancel escalation defers to
the manage-level gate (can_manage_restaurant), which is intentionally NOT
module-granular; the service re-evaluates BOTH against state re-read under the
lock. All views are authenticated via the global SimpleJWT default — there is no
AllowAny here. Kitchen writes the fulfilment axis (fulfilment_status, priority,
served_at, the fulfilment timestamps and the D05 revision) and, on the
serve/recall completion transition and on cancel, order_status; payment_status
stays finance-owned.

The READ views are unchanged in shape and gate, and now publish the two fields a
client needs to command safely (``order_status``, ``fulfilment_revision``) plus
``kitchen_protocol`` — a narrow capability declaration, separate from D04's
``checkout_protocol`` because they are different contracts.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Prefetch
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response

from orders_app.models import Order, OrderItem
from orders_app.controllers.services.kitchen_transition import (
    KITCHEN_PROTOCOL,
    KitchenRefusal,
    OrderNotFound,
    execute,
    parse_cancel_command,
    parse_fulfilment_command,
    parse_priority_command,
    read_state,
)
from orders_app.serializers_kitchen import (
    ActiveKitchenOrderSerializer,
    KitchenMenuItemSerializer,
)
from restaurants_app.models import MenuItem
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import (
    MODULE_KITCHEN,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
)

logger = logging.getLogger(__name__)

# Rolling bound on the Completed feed: a served ticket is visible (and hence
# recallable) for this long after served_at. Keeps the feed finite — 24h is a
# sane "this service" window with no timezone math; adjustable.
COMPLETED_WINDOW = timedelta(hours=24)

def _get_order_or_none(pk):
    # Guard against malformed UUIDs so bad input is a 404, never a 500.
    try:
        return Order.objects.filter(id=pk, deleted=False).first()
    except (ValidationError, ValueError, TypeError):
        return None


def _get_menu_item_or_none(pk):
    # Same UUID guard as _get_order_or_none; select_related('section') so the
    # restaurant_id permission check below doesn't trigger a second query.
    try:
        return MenuItem.objects.select_related('section').filter(
            id=pk, deleted=False,
        ).first()
    except (ValidationError, ValueError, TypeError):
        return None


class ActiveKitchenOrdersView(APIView):
    """GET the active kitchen orders for a restaurant."""

    def get(self, request):
        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400,
            )
        if not can_user_access_module(request.user, restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission to view this kitchen'},
                status=403,
            )

        # Served tickets leave the board immediately — they live in the
        # Completed feed (CompletedKitchenOrdersView) until COMPLETED_WINDOW
        # lapses. The board only ever shows SUBMITTED tickets that are
        # new / preparing / ready — an 'initiated' order is an unconfirmed
        # draft and never reaches the kitchen until it is submitted.
        qs = (
            Order.objects
            .filter(
                ~Q(fulfilment_status='served'),
                deleted=False,
                restaurant=restaurant_id,
            )
            .exclude(order_status=OrderStatus_Cancelled)
            .exclude(order_status=OrderStatus_Initiated)
            .select_related('table')
            .prefetch_related(
                Prefetch(
                    'order',
                    queryset=OrderItem.objects.filter(deleted=False, available=True),
                    to_attr='active_items',
                )
            )
        )

        data = ActiveKitchenOrderSerializer(qs, many=True).data
        return Response(
            {'status': 200, 'message': 'Active kitchen orders retrieved',
             'kitchen_protocol': KITCHEN_PROTOCOL, 'data': data},
            status=200,
        )


class CompletedKitchenOrdersView(APIView):
    """
    GET the Completed feed — served tickets from the last COMPLETED_WINDOW,
    newest-completed first. Mirrors ActiveKitchenOrdersView (same gate, same
    serializer, same envelope) so the frontend renders identical cards; recall
    (served -> ready) is initiated from here.
    """

    def get(self, request):
        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400,
            )
        if not can_user_access_module(request.user, restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission to view this kitchen'},
                status=403,
            )

        qs = (
            Order.objects
            .filter(
                fulfilment_status='served',
                deleted=False,
                restaurant=restaurant_id,
                served_at__gte=timezone.now() - COMPLETED_WINDOW,
            )
            .exclude(order_status=OrderStatus_Cancelled)
            .select_related('table')
            .prefetch_related(
                Prefetch(
                    'order',
                    queryset=OrderItem.objects.filter(deleted=False, available=True),
                    to_attr='active_items',
                )
            )
            .order_by('-served_at')
        )

        data = ActiveKitchenOrderSerializer(qs, many=True).data
        return Response(
            {'status': 200, 'message': 'Completed kitchen orders retrieved',
             'kitchen_protocol': KITCHEN_PROTOCOL, 'data': data},
            status=200,
        )


class _KitchenCommandView(APIView):
    """Shared adapter for the three order-command routes.

    HTTP in, HTTP out. It parses (through the service's own validators), calls
    ``execute``, and renders. The only thing it decides is how a refusal becomes
    a status code — every rule that could differ between routes lives in the one
    boundary, so the routes cannot disagree about drafts, cancellation,
    preconditions or permissions.
    """

    #: Subclasses set this to the matching ``parse_*_command``.
    parse = None

    def put(self, request, pk):
        try:
            command = type(self).parse(request.data)
            result = execute(pk, request.user, command)
        except OrderNotFound:
            # ONE non-disclosing answer for unknown, soft-deleted and
            # out-of-scope alike — this route must never become an oracle for
            # order ids at a restaurant the caller cannot see.
            return Response({'status': 404, 'message': 'Order not found'},
                            status=404)
        except KitchenRefusal as refusal:
            body = {
                'status': refusal.status,
                'message': refusal.message,
                'reason': refusal.reason,
            }
            # Present only where the caller has already cleared the module gate
            # for THIS order's restaurant, so a conflict can tell an operator
            # what the ticket actually is without disclosing anything they were
            # not already entitled to read.
            if refusal.state is not None:
                body['data'] = refusal.state
            return Response(body, status=refusal.status)

        return Response(
            {
                'status': 200,
                'message': ('Ticket updated'
                            if result['outcome'] == 'applied'
                            else 'No change'),
                'outcome': result['outcome'],
                'data': result['state'],
            },
            status=200,
        )


class KitchenOrderFulfilmentStatusView(_KitchenCommandView):
    """PUT one fulfilment command: ``{"action": ..., "if_revision": n}``.

    The ACTION is required and there is no ``fulfilment_status`` fallback: a
    target does not identify a command. ``preparing`` is reachable both forwards
    (from ``new``) and backwards (from ``ready``), so a delayed forward request
    used to execute as a recall and silently undo another device.
    """
    parse = staticmethod(parse_fulfilment_command)


class KitchenOrderPriorityView(_KitchenCommandView):
    """PUT ``{"priority": true|false, "if_revision": n}``.

    A strict JSON boolean, always stated. The omitted-value toggle is gone: it
    made a retried request undo itself, which is the opposite of the property a
    retryable flag needs.
    """
    parse = staticmethod(parse_priority_command)


class KitchenOrderCancelView(_KitchenCommandView):
    """PUT ``{"cancellation_reason": ..., "if_revision": n}``.

    State-aware authorisation, decided by the service from state re-read under
    the lock: a free void while ``new``, manager/owner past that, never on a
    served ticket (recall it first, if the recall is itself currently legal).

    Cancelling frees the table and drops the ticket from the board. Payments are
    parked — there are no refund mechanics here, and a cancellation is not a
    statement that any money moved.
    """
    parse = staticmethod(parse_cancel_command)


class KitchenOrderStateView(APIView):
    """GET ``kitchen/orders/<pk>/state/`` — what ONE order is right now.

    A thin adapter over ``kitchen_transition.read_state``, exactly as the three
    command routes are thin adapters over ``execute``. It exists because the two
    feeds cannot answer for an order that has left them, which is precisely what
    a cancellation — the command whose uncertain outcome matters most — produces.

    It is a READ: no lock, no transaction, no write, no repair, and no claim that
    any earlier command caused what it reports. It answers for a cancelled,
    served, terminal or draft order, because those are the orders it is for.

    It renders the SAME projection and the SAME ``kitchen_protocol`` declaration
    the command routes do, so a client has one shape to reconcile against however
    it obtained it.
    """

    def get(self, request, pk):
        try:
            state = read_state(pk, request.user)
        except OrderNotFound:
            return Response({'status': 404, 'message': 'Order not found'},
                            status=404)
        except KitchenRefusal as refusal:
            return Response(
                {'status': refusal.status, 'message': refusal.message,
                 'reason': refusal.reason},
                status=refusal.status,
            )
        return Response(
            {'status': 200, 'message': 'Order state',
             'kitchen_protocol': KITCHEN_PROTOCOL, 'data': state},
            status=200,
        )


class KitchenMenuItemsView(APIView):
    """
    GET the restaurant's on-menu items with their in_stock state — the read
    behind the kitchen sold-out panel. Mirrors ActiveKitchenOrdersView: required
    ?restaurant=<id>, the kitchen-module gate, the {status, message, data}
    envelope.
    """

    def get(self, request):
        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400,
            )
        if not can_user_access_module(request.user, restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission to view this kitchen'},
                status=403,
            )

        # available=True only — these are the items currently ON the menu, the
        # ones a kitchen can 86. Hidden (available=False) items are already off
        # it. section__deleted=False defends against orphaned items. The default
        # model ordering (section, listing_position, name) applies.
        qs = (
            MenuItem.objects
            .filter(
                section__restaurant=restaurant_id,
                section__deleted=False,
                available=True,
                deleted=False,
            )
            .select_related('section')
        )

        data = KitchenMenuItemSerializer(qs, many=True).data
        return Response(
            {'status': 200, 'message': 'Kitchen menu items retrieved', 'data': data},
            status=200,
        )


class KitchenMenuItemStockView(APIView):
    """
    86 a menu item — toggle MenuItem.in_stock (sold-out / back-in-stock).

    Writes the SAME in_stock column the menu module's toggle writes, so the
    kitchen and the menu module stay in sync by construction. Least-privilege:
    the kitchen writes in_stock and nothing else.
    """

    def put(self, request, pk):
        item = _get_menu_item_or_none(pk)
        if item is None:
            return Response({'status': 404, 'message': 'Menu item not found'}, status=404)
        if not can_user_access_module(request.user, item.section.restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission for this kitchen'},
                status=403,
            )

        # ONE transaction for the write and its audit. This is one of the two tenant
        # writes a delegated administrator can reach, and the audit used to be written
        # by the middleware after this view had already returned — too late to unwind
        # anything. Recording it here means a failed audit rolls the toggle back.
        # A no-op for ordinary staff: the helper returns False and writes nothing.
        from platform_admin_app.delegated_audit import audit_delegated_write
        from restaurants_app.controllers.catalogue_admission import (
            lock_catalogue_for_write,
        )

        with transaction.atomic():
            # THE ADMISSION BARRIER, FIRST (D06 G1a). `in_stock` is read at
            # acceptance — a line that is sold out sends the whole order back for
            # review rather than being silently zeroed — so an 86 committing
            # between an acceptance's catalogue read and its transition put a
            # dish the kitchen had just run out of onto the kitchen's own board.
            lock_catalogue_for_write(item.section.restaurant_id)

            # RE-READ UNDER THE LOCK, because the ABSENT-value branch below
            # derives the new value from the old one. `item` was resolved in
            # autocommit, so two boards toggling at once would both read the
            # same state and the second would undo the first without either
            # being told. The explicit branch does not need it and is harmless
            # either way; asking once keeps the two on one path.
            item = MenuItem.objects.select_for_update().get(pk=item.pk)

            if 'in_stock' in request.data:
                item.in_stock = bool(request.data.get('in_stock'))
            else:
                item.in_stock = not item.in_stock

            item.save(update_fields=['in_stock', 'time_last_updated'])
            audit_delegated_write(request, after_state={
                'resource': 'MenuItem',
                'menu_item_id': str(item.id),
                'in_stock': item.in_stock,
            })

        return Response(
            {
                'status': 200,
                'message': 'Stock updated',
                'data': {'id': str(item.id), 'in_stock': item.in_stock},
            },
            status=200,
        )

