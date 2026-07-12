"""
Kitchen view endpoints (api/v1/kitchen/).

Module-gated through the central permission resolver: access honours the
owner-configured Roles & Access grid for the `kitchen` module
(can_user_access_module / MODULE_KITCHEN), so revoking/granting kitchen on a role
takes server-side effect. The in-progress goodwill-cancel escalation defers to
the manage-level gate (can_manage_restaurant), which is intentionally NOT
module-granular. All views are authenticated via the global SimpleJWT default —
there is no AllowAny here. Kitchen writes the fulfilment axis (fulfilment_status,
priority, served_at and the fulfilment timestamps) and, on the serve/recall
completion transition and on cancel, order_status; payment_status stays
finance-owned.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q, Prefetch
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response

from orders_app.models import Order, OrderItem
from orders_app.serializers_kitchen import (
    ActiveKitchenOrderSerializer,
    KitchenMenuItemSerializer,
)
from restaurants_app.models import MenuItem
from users_app.controllers.permissions_check import (
    can_user_access_module,
    can_manage_restaurant,
)
from dinify_backend.configss.string_definitions import (
    MODULE_KITCHEN,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Served,
    OrderStatus_Pending,
    CANCELLATION_REASONS,
)

logger = logging.getLogger(__name__)

# Rolling bound on the Completed feed: a served ticket is visible (and hence
# recallable) for this long after served_at. Keeps the feed finite — 24h is a
# sane "this service" window with no timezone math; adjustable.
COMPLETED_WINDOW = timedelta(hours=24)

# Server-authoritative forward transitions (one step each).
FORWARD_TRANSITIONS = {
    'new': 'preparing',
    'preparing': 'ready',
    'ready': 'served',
}
FULFILMENT_STATUSES = {'new', 'preparing', 'ready', 'served'}


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
            {'status': 200, 'message': 'Active kitchen orders retrieved', 'data': data},
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
            {'status': 200, 'message': 'Completed kitchen orders retrieved', 'data': data},
            status=200,
        )


class KitchenOrderFulfilmentStatusView(APIView):
    """PUT the server-authoritative fulfilment status of an order."""

    def put(self, request, pk):
        order = _get_order_or_none(pk)
        if order is None:
            return Response({'status': 404, 'message': 'Order not found'}, status=404)
        if not can_user_access_module(request.user, order.restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission for this kitchen'},
                status=403,
            )

        target = request.data.get('fulfilment_status')
        if target not in FULFILMENT_STATUSES:
            return Response(
                {'status': 400, 'message': 'Invalid fulfilment_status'},
                status=400,
            )

        current = order.fulfilment_status
        now = timezone.now()

        # The completion transition couples the finance-owned order_status to
        # the fulfilment axis: serving completes the sale (-> 'served');
        # recalling reverts it (-> 'pending'). Tracked here so order_status is
        # added to update_fields ONLY on the branch where it actually changed.
        order_status_changed = False

        if FORWARD_TRANSITIONS.get(current) == target:
            # forward one step; entering served stamps served_at and advances
            # order_status — but never resurrect a cancelled order into a sale.
            if target == 'served':
                order.served_at = now
                if order.order_status != OrderStatus_Cancelled:
                    order.order_status = OrderStatus_Served
                    order_status_changed = True
        elif current == 'served' and target == 'ready':
            # recall from the Completed feed — no age gate (the feed's own
            # COMPLETED_WINDOW already bounds what's visible/recallable). Undo
            # ONLY the coupling we set (order_status == 'served'); never touch a
            # cancelled/other state.
            order.served_at = None
            if order.order_status == OrderStatus_Served:
                order.order_status = OrderStatus_Pending
                order_status_changed = True
        elif current == 'ready' and target == 'preparing':
            # recall back to preparing is allowed whenever ready (served_at null)
            pass
        else:
            return Response(
                {'status': 400, 'message': f'Illegal transition {current} -> {target}'},
                status=400,
            )

        order.fulfilment_status = target
        order.fulfilment_status_updated_at = now
        order.fulfilment_status_updated_by = request.user
        # The fulfilment axis is written on every transition; the completion
        # transition also writes order_status (serve -> 'served', recall ->
        # 'pending'), appended to update_fields only on the branch where it
        # changed so non-completion transitions leave finance-owned order_status
        # untouched. payment_status is never written here. time_last_updated is
        # listed so its auto_now fires on this partial save.
        update_fields = [
            'fulfilment_status',
            'served_at',
            'fulfilment_status_updated_at',
            'fulfilment_status_updated_by',
            'time_last_updated',
        ]
        if order_status_changed:
            update_fields.append('order_status')
        order.save(update_fields=update_fields)

        return Response(
            {
                'status': 200,
                'message': 'Fulfilment status updated',
                'data': {
                    'id': str(order.id),
                    'fulfilment_status': order.fulfilment_status,
                    'served_at': order.served_at,
                },
            },
            status=200,
        )


class KitchenOrderPriorityView(APIView):
    """PUT (set or toggle) the priority flag of an order."""

    def put(self, request, pk):
        order = _get_order_or_none(pk)
        if order is None:
            return Response({'status': 404, 'message': 'Order not found'}, status=404)
        if not can_user_access_module(request.user, order.restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission for this kitchen'},
                status=403,
            )

        if 'priority' in request.data:
            order.priority = bool(request.data.get('priority'))
        else:
            order.priority = not order.priority
        order.save(update_fields=['priority', 'time_last_updated'])

        return Response(
            {
                'status': 200,
                'message': 'Priority updated',
                'data': {'id': str(order.id), 'priority': order.priority},
            },
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

        if 'in_stock' in request.data:
            item.in_stock = bool(request.data.get('in_stock'))
        else:
            item.in_stock = not item.in_stock
        item.save(update_fields=['in_stock', 'time_last_updated'])

        return Response(
            {
                'status': 200,
                'message': 'Stock updated',
                'data': {'id': str(item.id), 'in_stock': item.in_stock},
            },
            status=200,
        )


class KitchenOrderCancelView(APIView):
    """
    PUT to cancel/void an order — the ONE kitchen write that sets order_status.

    State-aware authorisation:
      - 'new'                   : free void (base kitchen permission only)
      - 'preparing' / 'ready'   : manager/owner-only (the goodwill gate)
      - 'served'                : not directly cancellable (recall it first)

    Setting order_status='cancelled' frees the table and drops the ticket from
    the board (both the occupancy gate and the active-set query exclude
    cancelled orders). Payments are parked — no refund mechanics. The write is
    deliberately limited to order_status + the cancellation provenance fields;
    payment_status and the fulfilment axis are never touched.
    """

    def put(self, request, pk):
        order = _get_order_or_none(pk)
        if order is None:
            return Response({'status': 404, 'message': 'Order not found'}, status=404)
        if not can_user_access_module(request.user, order.restaurant_id, MODULE_KITCHEN):
            return Response(
                {'status': 403, 'message': 'You do not have permission for this kitchen'},
                status=403,
            )

        if order.order_status == OrderStatus_Cancelled:
            return Response(
                {'status': 400, 'message': 'Order is already cancelled'},
                status=400,
            )
        if order.fulfilment_status == 'served':
            return Response(
                {'status': 400, 'message': 'Cannot cancel a served order; recall it first'},
                status=400,
            )
        if order.fulfilment_status in ('preparing', 'ready') and not \
                can_manage_restaurant(request.user, order.restaurant_id):
            return Response(
                {
                    'status': 403,
                    'message': 'Only a manager can cancel an order once preparation has started',
                },
                status=403,
            )

        reason = request.data.get('cancellation_reason')
        if reason not in CANCELLATION_REASONS:
            return Response(
                {'status': 400, 'message': 'A valid cancellation_reason is required'},
                status=400,
            )

        now = timezone.now()
        order.order_status = OrderStatus_Cancelled
        order.cancelled_at = now
        order.cancelled_by = request.user
        order.cancellation_reason = reason
        # Deliberately limited to order_status + cancellation provenance, so
        # payment_status and the fulfilment axis are never clobbered.
        # time_last_updated is listed so its auto_now fires on this partial save.
        order.save(update_fields=[
            'order_status',
            'cancelled_at',
            'cancelled_by',
            'cancellation_reason',
            'time_last_updated',
        ])

        return Response(
            {
                'status': 200,
                'message': 'Order cancelled',
                'data': {
                    'id': str(order.id),
                    'order_status': order.order_status,
                    'cancelled_at': order.cancelled_at,
                    'cancellation_reason': order.cancellation_reason,
                },
            },
            status=200,
        )
