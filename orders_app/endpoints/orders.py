"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.core.exceptions import ValidationError
from orders_app.models import Order
from orders_app.controllers.manage_order import update_order_status
from dinify_backend.configss.string_definitions import OrderStatus_Pending, MODULE_TABLES
from orders_app.controllers.con_orders import ConOrder
from users_app.controllers.permissions_check import can_user_access_module


class OrdersEndpoint(APIView):
    """
    The endpoint for handling orders
    """
    permission_classes = [AllowAny]

    def put(self, request, action):
        # Only `submit` (the anonymous diner placing their already-initiated
        # order) survives. `prepare`, `cancel` and `update-item` were retired:
        # they were orphaned (no caller) and resolved the order from a
        # body-supplied id with NO restaurant / ownership / module scope, so any
        # authenticated user could transition another restaurant's order. Live
        # order fulfilment lives in the kitchen module (api/v1/kitchen/), which
        # gates every write.
        if action == 'submit':
            data = request.data

            user = request.user
            # DRF gives unauthenticated requests an AnonymousUser (not None);
            # normalise it to None so the anonymous diner can submit and
            # attribution stays null.
            if user is None or user.is_anonymous:
                user = None

            # Anonymous, client-supplied order id: guard the lookup so a
            # missing / malformed / nonexistent id returns a clean 4xx instead
            # of a 500. Don't call .get(id=None).
            order_id = data.get('order')
            if not order_id:
                return Response(
                    {'status': 400, 'message': 'Invalid order id'},
                    status=400,
                )
            try:
                order = Order.objects.get(id=order_id)
            except ValidationError:
                return Response(
                    {'status': 400, 'message': 'Invalid order id'},
                    status=400,
                )
            except Order.DoesNotExist:
                return Response(
                    {'status': 404, 'message': 'Order not found'},
                    status=404,
                )
            response = update_order_status(
                order=order,
                new_status=OrderStatus_Pending,
                user=user,
            )
            return Response(response, status=response.get('status', 200))

        # Retired actions (prepare / cancel / update-item) and any unknown
        # action: 404 rather than falling through to a 500.
        return Response({'status': 404, 'message': 'Not found'}, status=404)


class V2OrdersEndpoint(APIView):
    """
    The V2 endpoint for handling orders
    """
    permission_classes = [AllowAny]

    def post(self, request, action):
        if action == 'initiate':
            data = request.data
            source = data.get('source')
            user = request.user
            try:
                user = request.user.pk
            except Exception:
                user = None

            customer = None
            created_by = None
            restaurant_id = data.get('restaurant')

            if source == 'admin':
                if user is None:
                    response = {
                        'status': 401,
                        'message': 'Please log in'
                    }
                    return Response(response, status=401)
                # Authorize the caller against the target restaurant BEFORE
                # trusting them as staff. Without this, any authenticated
                # principal (a self-registered diner) could set created_by and
                # thereby skip every availability gate in initiate_order
                # (accepting_orders, qr_mode, is_available_for_scan) at any
                # restaurant. 404 (not 403) mirrors the reports/finance
                # non-disclosure gates — a non-member must not learn whether the
                # restaurant exists. can_user_access_module already returns True
                # for dinify admins and fails closed on a missing/empty id.
                if not can_user_access_module(
                    request.user, restaurant_id, MODULE_TABLES,
                ):
                    return Response({'status': 404, 'message': 'Not found'}, status=404)
                created_by = request.user
            else:
                if user is not None:
                    user = str(user)
                    customer = request.user

            table_id = data.get('table')
            items = data.get('items')
            # idempotency key supplied by the diner app (Phase 3); absent today
            client_order_id = data.get('client_order_id')
            if restaurant_id is None or table_id is None:
                response = {
                    'status': 400,
                    'message': 'Please provide the restaurant and table ID'
                }
                return Response(response, status=400)
            response = ConOrder.initiate_order(
                restaurant_id=restaurant_id,
                table_id=table_id,
                items=items,
                customer=customer,
                created_by=created_by,
                client_order_id=client_order_id,
            )
            return Response(response, status=response.get('status', 200))

        # `add-items` (POST) is retired (orphaned, unscoped); any other action
        # 404s rather than falling through to a 500.
        return Response({'status': 404, 'message': 'Not found'}, status=404)

    def delete(self, request, action):
        # `add-items` deletion is retired (orphaned, unscoped). No v2 DELETE
        # actions remain — return 404 (not 405) for the retired route.
        return Response({'status': 404, 'message': 'Not found'}, status=404)

    def get(self, request, action):
        # All v2 GET order actions are retired. `details` — the orphaned,
        # unauthenticated full-order read (closes C1) — is gone. A future diner
        # "view my order" must use the scoped journey path (OrderJourneyEndpoint
        # + SerializerPublicOrderDetails), not a revived AllowAny full read.
        return Response({'status': 404, 'message': 'Not found'}, status=404)
