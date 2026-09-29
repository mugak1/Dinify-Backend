"""
endpoints to handle order
"""
import uuid

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
from orders_app.controllers.services.order_authority import StaffAuthority
from users_app.controllers.permissions_check import can_user_access_module
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.http import NoStoreResponseMixin
from restaurants_app.controllers.diner_capability import (
    require_table_session, resolve_table_session, session_token_from_request,
    DinerCapabilityError,
)
from restaurants_app.controllers import diner_capability
from dinify_backend.request_context import ORDER_COMMAND, note_outcome
from orders_app.controllers import manage_order
from orders_app.controllers.services import (
    order_eligibility, order_intent, quote_closure, quote_policy,
)
from orders_app.controllers.services.acceptance_result import (
    ACCEPTANCE_ACCEPTED, OUTCOME_ALREADY_ACCEPTED, OUTCOME_NEWLY_ACCEPTED,
)


# --- D15 R2: the bounded order-command trace ----------------------------------------
#
# Three commands — `initiate`, `submit` and `retire-quote` — are MARKED before DRF
# authenticates them, and each existing return point below attaches only what that
# point has ALREADY established: a fixed outcome word, a fixed reason code, the channel,
# and the order and intent key once they are authorized and validated. The middleware
# (dinify_backend.request_context) writes one `dinify.outcome` line per marked request
# with the status actually sent. Nothing here queries, re-orders a check or changes a
# response; an attempted identifier the caller is not entitled to is never recorded.

# The endpoint's own refusal codes — one per early return below.
_TRACE_ORDER_REQUIRED = 'order_required'
_TRACE_CAPABILITY_DENIED = 'capability_denied'
_TRACE_CAPABILITY_INVALID = 'capability_invalid'
_TRACE_NOT_FOUND = 'not_found'
_TRACE_SESSION_REQUIRED = 'session_required'
_TRACE_LOGIN_REQUIRED = 'login_required'
_TRACE_INVALID_REQUEST = 'invalid_request'
_TRACE_SCOPE_MISMATCH = 'scope_mismatch'

#: EXPLICIT, never discovered at runtime: a controller `reason` reaches the trace only
#: if it is one of these server constants, so a free-text reason added later stays
#: unknown instead of being echoed. `tests_order_trace` fails when a controller gains a
#: `REASON_*` this list does not name. (purchase_integrity's `purchase_needs_review` is
#: the same value as quote_closure's REASON_PURCHASE_CHANGED, taken from there because
#: that module is already in this endpoint's import graph.)
_TRACED_REASONS = frozenset({
    manage_order.REASON_LEGACY_PRICING,
    manage_order.REASON_QUOTE_REQUIRED,
    manage_order.REASON_QUOTE_STALE,
    manage_order.REASON_QUOTE_INCOMPLETE,
    manage_order.REASON_NOTHING_TO_PREPARE,
    manage_order.REASON_ALREADY_ACCEPTED,
    order_eligibility.REASON_RESTAURANT_PAUSED,
    order_eligibility.REASON_RESTAURANT_UNAVAILABLE,
    order_eligibility.REASON_TABLE_ORDERING_UNAVAILABLE,
    order_eligibility.REASON_TABLE_UNAVAILABLE,
    order_intent.REASON_INTENT_MISMATCH,
    order_intent.REASON_INTENT_UNUSABLE,
    order_intent.REASON_INTENT_BINDING_UNAVAILABLE,
    quote_closure.REASON_EXPIRED,
    quote_closure.REASON_PURCHASE_CHANGED,
    quote_closure.REASON_ALREADY_ACCEPTED,
    quote_closure.REASON_EVIDENCE_UNAVAILABLE,
    quote_closure.REASON_QUOTE_REF_MISMATCH,
    quote_closure.REASON_QUOTE_CLOSED,
    quote_policy.REASON_QUOTE_EXPIRED,
    quote_policy.REASON_QUOTE_UNVERIFIABLE,
    _TRACE_ORDER_REQUIRED,
    _TRACE_CAPABILITY_DENIED,
    _TRACE_CAPABILITY_INVALID,
    _TRACE_NOT_FOUND,
    _TRACE_SESSION_REQUIRED,
    _TRACE_LOGIN_REQUIRED,
    _TRACE_INVALID_REQUEST,
    _TRACE_SCOPE_MISMATCH,
})

#: retire-quote's own success words, copied into the trace unchanged.
_RETIRE_OUTCOMES = {
    quote_closure.OUTCOME_STILL_VALID: 'quote_still_valid',
    quote_closure.OUTCOME_CLOSED: 'quote_closed',
    quote_closure.OUTCOME_ALREADY_CLOSED: 'quote_already_closed',
}


def _trace(request, **fields):
    note_outcome(request, ORDER_COMMAND, **fields)


def _trace_refused(request, reason):
    if reason in _TRACED_REASONS:
        _trace(request, outcome='refused', reason=reason)


def _trace_capability_refusal(request, exc):
    _trace_refused(request, _TRACE_CAPABILITY_DENIED
                   if isinstance(exc, diner_capability.DinerCapabilityDenied)
                   else _TRACE_CAPABILITY_INVALID)


def _acceptance_outcome(response, order):
    """`accepted` / `already_accepted` ONLY when the answer states it consistently: an
    explicit boolean `idempotent`, AND the correlated acceptance answer naming THIS
    order as accepted with the matching attempt outcome. Anything missing, malformed or
    inconsistent is no success claim. Reads those fields and nothing else — never the
    quote reference or the rest of the projection."""
    if order is None:
        return None
    idempotent = response.get('idempotent')
    if idempotent is not True and idempotent is not False:
        return None
    checkout = response.get('checkout')
    if not isinstance(checkout, dict) or checkout.get('order_id') != str(order.pk):
        return None
    acceptance = checkout.get('acceptance')
    if not isinstance(acceptance, dict) or acceptance.get('state') != ACCEPTANCE_ACCEPTED:
        return None
    expected = OUTCOME_ALREADY_ACCEPTED if idempotent else OUTCOME_NEWLY_ACCEPTED
    if acceptance.get('outcome') != expected:
        return None
    return 'already_accepted' if idempotent else 'accepted'


def _returned_order(response):
    data = response.get('data')
    details = data.get('order_details') if isinstance(data, dict) else None
    returned = details.get('id') if isinstance(details, dict) else None
    if not isinstance(returned, str):
        return None
    try:
        return str(uuid.UUID(returned))
    except ValueError:
        return None


def _trace_result(request, command, response, order=None):
    """Classify a controller's answer from what the answer itself states. A generic
    400 with no known reason — the shape a swallowed exception produces — is left
    `unclassified`, never called a refusal."""
    try:
        if not isinstance(response, dict):
            return
        if response.get('status', 200) != 200:
            reason = response.get('reason')
            if isinstance(reason, str) and reason in _TRACED_REASONS:
                _trace(request, outcome='refused', reason=reason)
            return
        if command == 'retire_quote':
            word = response.get('outcome')
            if isinstance(word, str) and word in _RETIRE_OUTCOMES:
                reason = response.get('reason')
                _trace(request, outcome=_RETIRE_OUTCOMES[word],
                       reason=(reason if isinstance(reason, str)
                               and reason in _TRACED_REASONS else None))
        elif command == 'submit':
            outcome = _acceptance_outcome(response, order)
            if outcome is not None:
                _trace(request, outcome=outcome)
        elif command == 'initiate':
            # An authorized order came back. NOT that it is new, a draft or accepted:
            # the controller drops its replay flag, and a replay returns whatever the
            # order has become since.
            returned = _returned_order(response)
            if returned is not None:
                _trace(request, outcome='order_returned', order=returned)
    except Exception:
        return


class _TracedCommandsMixin:
    """Mark a traced command before DRF authentication, then initialise normally."""

    _TRACED_COMMANDS = {}

    def initial(self, request, *args, **kwargs):
        # BEFORE authentication, so a refusal DRF makes itself (401, 415, a
        # malformed body) is traced too. A request rejected by a MIDDLEWARE never
        # gets here; it still carries its request ID.
        command = None
        try:
            command = self._TRACED_COMMANDS.get((request.method, kwargs.get('action')))
        except Exception:
            pass
        if command is not None:
            _trace(request, action=command)
        super().initial(request, *args, **kwargs)


class OrdersEndpoint(_TracedCommandsMixin, NoStoreResponseMixin, APIView):
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
    _TRACED_COMMANDS = {('PUT', 'submit'): 'submit', ('PUT', 'retire-quote'): 'retire_quote'}

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
                _trace_refused(request, _TRACE_ORDER_REQUIRED)
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
                    _trace_capability_refusal(request, exc)
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
                    _trace_refused(request, _TRACE_NOT_FOUND)
                    return Response(
                        {'status': 404, 'message': 'Order not found'}, status=404,
                    )
                _trace(request, channel='diner', order=order.pk,
                       intent=order.client_order_id)
                user = None  # anonymous diner — attribution stays null
                # CARRY WHAT THIS SESSION ASSERTED to the protected boundary
                # (D06). The checks above ran in autocommit; the transition then
                # waits for three locks, and a QR regeneration inside that wait
                # revokes this session. The transition re-checks the generation
                # on the row it locks and answers with the same opaque 404 this
                # branch does. It is context, not authority — the authority
                # decision is the one just made here.
                capability = diner_capability.capability_from_table(table)
                # No staff module gate was used, so there is none to re-assert.
                authority = None
            else:
                # No diner session: fall back to an authorised staff caller.
                try:
                    decode_jwt_token(request)
                except Exception:
                    _trace_refused(request, _TRACE_SESSION_REQUIRED)
                    return Response(
                        {'status': 400, 'message': 'A diner table session is required.'},
                        status=400,
                    )
                try:
                    order = Order.objects.get(id=order_id)
                except (Order.DoesNotExist, ValidationError, ValueError):
                    _trace_refused(request, _TRACE_NOT_FOUND)
                    return Response(
                        {'status': 404, 'message': 'Order not found'}, status=404,
                    )
                if not can_user_access_module(
                    request.user, str(order.restaurant_id), MODULE_TABLES,
                ):
                    # The order found above is NOT this caller's: nothing about it
                    # is recorded.
                    _trace_refused(request, _TRACE_NOT_FOUND)
                    return Response({'status': 404, 'message': 'Not found'}, status=404)
                _trace(request, channel='staff', order=order.pk,
                       intent=order.client_order_id)
                user = request.user
                # NO CAPABILITY CHANNEL WAS USED, so there is no QR generation to
                # re-verify. That is NOT the same as having nothing to re-ask,
                # which is what this comment used to claim (D06 completion, G1b):
                # "A staff caller's authority is the module gate above, which is
                # not revoked by a QR regeneration." True, and beside the point.
                # The module gate immediately above ran in AUTOCOMMIT; the
                # transition then WAITS for the admission advisory lock, the
                # table row and the order row. A membership deactivated, a role
                # removed or the restaurant leaving the portal-access states
                # inside that wait revokes exactly the authority this request is
                # still acting on, and nothing downstream noticed.
                #
                # So the MINIMAL RECORD of what was authorized travels to the
                # protected boundary, which asks the SAME resolver the SAME
                # question about the state as it is then. Three facts and NO
                # CREDENTIAL — a credential must not travel past the point that
                # verifies it — and `restaurant_id` is the SERVER-RESOLVED one,
                # read off the order rather than the body. The principal is
                # carried as the OBJECT and not an id: a delegation is an
                # in-memory attribute the middleware set on it, so re-fetching
                # the row would silently ask a different question and refuse
                # somebody this endpoint correctly admitted.
                capability = None
                authority = StaffAuthority(
                    user=request.user,
                    restaurant_id=str(order.restaurant_id),
                    module=MODULE_TABLES,
                )

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
                    authority=authority,
                )
            else:
                response = update_order_status(
                    order=order,
                    new_status=OrderStatus_Pending,
                    user=user,
                    quote_ref=data.get('quote_ref'),
                    capability=capability,
                    authority=authority,
                )
            _trace_result(request, 'retire_quote' if action == 'retire-quote' else 'submit',
                          response, order)
            return Response(response, status=response.get('status', 200))

        # Retired actions (prepare / cancel / update-item) and any unknown
        # action: 404 rather than falling through to a 500.
        return Response({'status': 404, 'message': 'Not found'}, status=404)


class V2OrdersEndpoint(_TracedCommandsMixin, NoStoreResponseMixin, APIView):
    """
    The V2 endpoint for handling orders
    """
    permission_classes = [AllowAny]
    _TRACED_COMMANDS = {('POST', 'initiate'): 'initiate'}

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
                _trace_refused(request, _TRACE_INVALID_REQUEST)
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
            # A1 — one of these is set by the branch that authorized the request;
            # the other stays `None`, which the boundary reads as "that channel
            # was not used" and re-checks nothing for.
            capability = None
            authority = None

            if source == 'admin':
                if user is None:
                    _trace_refused(request, _TRACE_LOGIN_REQUIRED)
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
                    _trace_refused(request, _TRACE_NOT_FOUND)
                    return Response({'status': 404, 'message': 'Not found'}, status=404)
                created_by = request.user
                # A1 — THE MINIMAL RECORD OF WHAT WAS JUST AUTHORIZED, carried
                # to the boundary that writes. The gate immediately above ran in
                # AUTOCOMMIT; creation then waits for the admission advisory
                # lock and the table row, and a membership deactivated, a role
                # removed or the restaurant leaving the portal-access states
                # inside that wait revokes exactly this authority. The same
                # record the acceptance boundary has carried since G1b: three
                # facts, no credential, and the principal as the OBJECT so a
                # delegation's in-memory context is not silently lost.
                #
                # `_create_order` cross-checks `restaurant_id` against the
                # `Restaurant` row it loads before re-asking the gate, which is
                # what makes the target server-derived rather than merely
                # authorized-at-the-door.
                authority = StaffAuthority(
                    user=request.user,
                    restaurant_id=restaurant_id,
                    module=MODULE_TABLES,
                )
                table_id = data.get('table')
                if restaurant_id is None or table_id is None:
                    _trace_refused(request, _TRACE_INVALID_REQUEST)
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
                    _trace_capability_refusal(request, exc)
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
                    _trace_refused(request, _TRACE_SCOPE_MISMATCH)
                    return Response(
                        {'status': 400,
                         'message': 'restaurant does not match your table session'},
                        status=400,
                    )
                if body_table is not None and str(body_table) != table_id:
                    _trace_refused(request, _TRACE_SCOPE_MISMATCH)
                    return Response(
                        {'status': 400,
                         'message': 'table does not match your table session'},
                        status=400,
                    )
                # A1 — AND THE DINER'S HALF OF THE SAME THING. `require_table_session`
                # has just verified the signature, the expiry, the generation and
                # the table's scannability, in autocommit; a QR regeneration
                # committing while creation waits on its locks revokes this
                # session, and nothing downstream knew what generation was
                # presented. Built ONLY from the table the session resolved to —
                # building one from anything else would manufacture an
                # authorization fact.
                capability = diner_capability.capability_from_table(table)

            # FULL INPUT VALIDATION, after authority is resolved. Placed here
            # so no catalogue-shaped feedback ever precedes authorization; the
            # rule itself is pure and reads nothing, so the ordering costs
            # nothing. The endpoint validates to give the caller useful
            # feedback — it is NOT what makes the order safe: `initiate_order`
            # and `_create_order` each run the same rule themselves.
            _trace(request, channel='staff' if authority is not None else 'diner')
            validated = validate_order_request(data)
            if validated.get('status') != 200:
                _trace_refused(request, _TRACE_INVALID_REQUEST)
                return Response(validated, status=400)
            _trace(request, intent=validated['client_order_id'])

            response = ConOrder.initiate_order(
                restaurant_id=restaurant_id,
                table_id=table_id,
                items=validated['items'],
                customer=customer,
                created_by=created_by,
                client_order_id=validated['client_order_id'],
                capability=capability,
                authority=authority,
            )
            _trace_result(request, 'initiate', response)
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
