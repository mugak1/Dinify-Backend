"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.core.exceptions import ValidationError
from orders_app.models import Order
from orders_app.controllers.manage_order import (
    retire_quote_for_review, update_order_status,
)
from dinify_backend.configss.string_definitions import OrderStatus_Pending, MODULE_TABLES
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.services.order_input import validate_order_request
from users_app.controllers.permissions_check import can_user_access_module
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.http import NoStoreResponseMixin
from restaurants_app.controllers.diner_capability import (
    require_table_session, resolve_table_session, session_token_from_request,
    DinerCapabilityError,
)
from restaurants_app.controllers import diner_capability


class OrdersEndpoint(NoStoreResponseMixin, APIView):
    """
    The endpoint for handling orders
    """
    permission_classes = [AllowAny]

    # ACTIONS THAT ACT ON ONE SAVED DRAFT AND SHARE ONE AUTHORITY MODEL. Both
    # resolve the order the same way and both carry the same capability context;
    # what they do with the draft is where they differ, and that difference lives
    # in the controller rather than in two copies of the authority code.
    #
    # `retire-quote` is deliberately a SEPARATE action rather than a flag on
    # `submit`: placing an order and establishing that it can no longer be placed
    # are opposite decisions with opposite consequences, and which one a request
    # made should be readable from the path rather than from a body. The same
    # reasoning the admin plane applies to reissue vs cancel.
    #
    # SHARING ONE AUTHORITY MODEL INCLUDES THE LIVENESS GATE, and that is the
    # part a reader is most likely to want to carve out. `resolve_table_session`
    # re-checks `Table.is_available_for_scan()` live, so a diner holding a quote
    # at a table that has since been soft-deleted, disabled, deactivated or taken
    # out of service is refused HERE, with the channel's opaque 404, and
    # `retire_quote_for_review` is never entered. That is deliberate: a revoked
    # session must not drive a durable write, and D06's own eligibility rule
    # makes table liveness bind every provenance for exactly the same reason.
    # The diner loses nothing they could otherwise have — no replacement quote
    # can be minted at that table either — and the client treats the 404 as an
    # unanswered round trip, so it retries rather than submitting. The full
    # reasoning is on `retire_quote_for_review`; do not add a second, laxer
    # capability resolution for this one action.
    _DRAFT_ACTIONS = ('submit', 'retire-quote')

    def put(self, request, action):
        # Only `submit` (the anonymous diner placing their already-initiated
        # order) and `retire-quote` (D06 — asking whether that draft's saved
        # quote can still be honoured, and retiring it if it cannot) are live.
        # `prepare`, `cancel` and `update-item` were retired: they were orphaned
        # (no caller) and resolved the order from a body-supplied id with NO
        # restaurant / ownership / module scope, so any authenticated user could
        # transition another restaurant's order. Live order fulfilment lives in
        # the kitchen module (api/v1/kitchen/), which gates every write.
        if action in self._DRAFT_ACTIONS:
            data = request.data

            order_id = data.get('order')
            if not order_id:
                return Response(
                    {'status': 400, 'message': 'Invalid order id'},
                    status=400,
                )

            # Authority is the table SESSION bound to this order's table — order-UUID
            # knowledge alone is no longer enough to drive initiated->pending (BOLA
            # fix). A staff JWT with the tables module at the order's restaurant is
            # the separate authorised path (e.g. a manager submitting an
            # admin-initiated order). Both unknown-order and wrong-scope collapse to
            # one non-disclosing 404.
            session_token = session_token_from_request(request)
            if session_token:
                try:
                    table = resolve_table_session(session_token)
                except DinerCapabilityError as exc:
                    return Response(
                        {'status': exc.status, 'message': exc.message},
                        status=exc.status,
                    )
                try:
                    order = Order.objects.get(
                        id=order_id,
                        restaurant_id=table.restaurant_id,
                        table_id=table.id,
                    )
                except (Order.DoesNotExist, ValidationError, ValueError):
                    return Response(
                        {'status': 404, 'message': 'Order not found'}, status=404,
                    )
                user = None  # anonymous diner — attribution stays null
                # CARRY WHAT THIS SESSION ASSERTED to the protected boundary
                # (D06). The checks above ran in autocommit; the transition then
                # waits for three locks, and a QR regeneration inside that wait
                # revokes this session. The transition re-checks the generation
                # on the row it locks and answers with the same opaque 404 this
                # branch does. It is context, not authority — the authority
                # decision is the one just made here.
                capability = diner_capability.capability_from_table(table)
            else:
                # No diner session: fall back to an authorised staff caller.
                try:
                    decode_jwt_token(request)
                except Exception:
                    return Response(
                        {'status': 400, 'message': 'A diner table session is required.'},
                        status=400,
                    )
                try:
                    order = Order.objects.get(id=order_id)
                except (Order.DoesNotExist, ValidationError, ValueError):
                    return Response(
                        {'status': 404, 'message': 'Order not found'}, status=404,
                    )
                if not can_user_access_module(
                    request.user, str(order.restaurant_id), MODULE_TABLES,
                ):
                    return Response({'status': 404, 'message': 'Not found'}, status=404)
                user = request.user
                # No capability channel was used, so there is nothing to
                # re-verify. A staff caller's authority is the module gate above,
                # which is not revoked by a QR regeneration.
                capability = None

            # THE QUOTE ACKNOWLEDGEMENT (D02/P9). The submission must name the
            # exact server-priced draft it is accepting; the transition validates
            # it under the order lock. It is read here and passed through — it is
            # NOT authority (the diner-session / staff-module checks above are,
            # and they are unchanged) and there is no staff bypass: a trusted
            # caller is not a reason to accept an amount nobody reviewed.
            #
            # An older client that sends no `quote_ref` is refused with a usable
            # message and a stable reason code. That is deliberate compatibility
            # handling, not seamless backward compatibility: auto-submitting a
            # freshly calculated amount such a client never displayed is exactly
            # what this contract exists to stop.
            #
            # `retire-quote` requires it for the same reason and a sharper one:
            # a closure retires ONE named reference, so a request that cannot
            # name the quote it means must not be allowed to retire whatever
            # happens to be current.
            if action == 'retire-quote':
                response = retire_quote_for_review(
                    order,
                    supplied_quote_ref=data.get('quote_ref'),
                    capability=capability,
                )
            else:
                response = update_order_status(
                    order=order,
                    new_status=OrderStatus_Pending,
                    user=user,
                    quote_ref=data.get('quote_ref'),
                    capability=capability,
                )
            return Response(response, status=response.get('status', 200))

        # Retired actions (prepare / cancel / update-item) and any unknown
        # action: 404 rather than falling through to a 500.
        return Response({'status': 404, 'message': 'Not found'}, status=404)


class V2OrdersEndpoint(NoStoreResponseMixin, APIView):
    """
    The V2 endpoint for handling orders
    """
    permission_classes = [AllowAny]

    def post(self, request, action):
        if action == 'initiate':
            # OUTER SHAPE FIRST (D01). `request.data` is whatever the client
            # sent: a JSON array or a bare string arrives as a list/str, and
            # every `.get()` below raised AttributeError -> 500. This one pure
            # guard is allowed to precede authority resolution precisely because
            # it reads no catalogue and discloses nothing — it only establishes
            # that there is a mapping to read at all.
            data = request.data
            if not isinstance(data, dict):
                return Response(
                    {'status': 400,
                     'message': 'The order request is not valid. Please try again.'},
                    status=400,
                )
            source = data.get('source')
            try:
                user = request.user.pk
            except Exception:
                user = None

            customer = None
            created_by = None

            if source == 'admin':
                if user is None:
                    return Response(
                        {'status': 401, 'message': 'Please log in'}, status=401,
                    )
                restaurant_id = data.get('restaurant')
                # Authorize the caller against the target restaurant BEFORE
                # trusting them as staff. Without this, any authenticated
                # principal (a self-registered diner) could set created_by and
                # thereby skip every availability gate in initiate_order
                # (accepting_orders, qr_mode, is_available_for_scan) at any
                # restaurant. 404 (not 403) mirrors the reports/finance
                # non-disclosure gates. can_user_access_module fails closed on a
                # missing/empty id.
                if not can_user_access_module(
                    request.user, restaurant_id, MODULE_TABLES,
                ):
                    return Response({'status': 404, 'message': 'Not found'}, status=404)
                created_by = request.user
                table_id = data.get('table')
                if restaurant_id is None or table_id is None:
                    return Response(
                        {'status': 400,
                         'message': 'Please provide the restaurant and table ID'},
                        status=400,
                    )
            else:
                # Anonymous diner: authority is the opaque table SESSION, not the
                # body-supplied restaurant/table ids. Derive them from the session.
                if user is not None:
                    customer = request.user
                try:
                    table = require_table_session(request)
                except DinerCapabilityError as exc:
                    return Response(
                        {'status': exc.status, 'message': exc.message},
                        status=exc.status,
                    )
                restaurant_id = str(table.restaurant_id)
                table_id = str(table.id)
                # Transitional: the body may STILL carry restaurant/table, but they
                # may only MATCH the session — never override it. Reject a mismatch.
                body_restaurant = data.get('restaurant')
                body_table = data.get('table')
                if body_restaurant is not None and str(body_restaurant) != restaurant_id:
                    return Response(
                        {'status': 400,
                         'message': 'restaurant does not match your table session'},
                        status=400,
                    )
                if body_table is not None and str(body_table) != table_id:
                    return Response(
                        {'status': 400,
                         'message': 'table does not match your table session'},
                        status=400,
                    )

            # FULL INPUT VALIDATION, after authority is resolved. Placed here
            # so no catalogue-shaped feedback ever precedes authorization; the
            # rule itself is pure and reads nothing, so the ordering costs
            # nothing. The endpoint validates to give the caller useful
            # feedback — it is NOT what makes the order safe: `initiate_order`
            # and `_create_order` each run the same rule themselves.
            validated = validate_order_request(data)
            if validated.get('status') != 200:
                return Response(validated, status=400)

            response = ConOrder.initiate_order(
                restaurant_id=restaurant_id,
                table_id=table_id,
                items=validated['items'],
                customer=customer,
                created_by=created_by,
                client_order_id=validated['client_order_id'],
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
