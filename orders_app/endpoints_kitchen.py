"""
Kitchen view endpoints (api/v1/kitchen/).

Role-checked (owner / manager / kitchen, or Dinify admin) and authenticated via
the global SimpleJWT default — there is no AllowAny here. Kitchen writes ONLY
the fulfilment axis (fulfilment_status, priority, served_at and the fulfilment
timestamps); order_status / payment_status stay finance-owned.
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q, Prefetch
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response

from orders_app.models import Order, OrderItem
from orders_app.serializers_kitchen import ActiveKitchenOrderSerializer
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    get_user_restaurant_roles,
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    RESTAURANT_KITCHEN,
    OrderStatus_Cancelled,
)

logger = logging.getLogger(__name__)

# Window during which a just-served ticket stays visible and can be recalled
# (served -> ready).
RECALL_WINDOW = timedelta(minutes=10)

# Server-authoritative forward transitions (one step each).
FORWARD_TRANSITIONS = {
    'new': 'preparing',
    'preparing': 'ready',
    'ready': 'served',
}
FULFILMENT_STATUSES = {'new', 'preparing', 'ready', 'served'}


def user_can_access_kitchen(user, restaurant_id) -> bool:
    """
    Owner / manager / kitchen of the restaurant, or a Dinify admin. Mirrors
    ConMenuItemSortMode._user_can_manage_restaurant with RESTAURANT_KITCHEN added.
    """
    if is_dinify_admin(user):
        return True
    roles = get_user_restaurant_roles(
        user_id=str(user.id),
        restaurant_id=str(restaurant_id),
    )
    return any(
        role in (RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_KITCHEN)
        for role in roles
    )


def _get_order_or_none(pk):
    # Guard against malformed UUIDs so bad input is a 404, never a 500.
    try:
        return Order.objects.filter(id=pk, deleted=False).first()
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
        if not user_can_access_kitchen(request.user, restaurant_id):
            return Response(
                {'status': 403, 'message': 'You do not have permission to view this kitchen'},
                status=403,
            )

        now = timezone.now()
        active_window_q = (
            ~Q(fulfilment_status="served")
            | Q(served_at__gte=now - RECALL_WINDOW)
        )
        qs = (
            Order.objects
            .filter(active_window_q, deleted=False, restaurant=restaurant_id)
            .exclude(order_status=OrderStatus_Cancelled)
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


class KitchenOrderFulfilmentStatusView(APIView):
    """PUT the server-authoritative fulfilment status of an order."""

    def put(self, request, pk):
        order = _get_order_or_none(pk)
        if order is None:
            return Response({'status': 404, 'message': 'Order not found'}, status=404)
        if not user_can_access_kitchen(request.user, order.restaurant_id):
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

        if FORWARD_TRANSITIONS.get(current) == target:
            # forward one step; entering served stamps served_at
            if target == 'served':
                order.served_at = now
        elif current == 'served' and target == 'ready':
            # recall is only allowed within the recall window of served_at
            if order.served_at is None or (now - order.served_at) > RECALL_WINDOW:
                return Response(
                    {'status': 400, 'message': 'Recall window has expired'},
                    status=400,
                )
            order.served_at = None
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
        # Limit the write to the fulfilment axis so finance-owned order_status /
        # payment_status are never clobbered. time_last_updated is listed so its
        # auto_now fires on this partial save.
        order.save(update_fields=[
            'fulfilment_status',
            'served_at',
            'fulfilment_status_updated_at',
            'fulfilment_status_updated_by',
            'time_last_updated',
        ])

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
        if not user_can_access_kitchen(request.user, order.restaurant_id):
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
