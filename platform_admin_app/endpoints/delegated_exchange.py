"""
The delegated session's own three endpoints, on the CUSTOMER plane.

These are the only delegation routes outside the admin control plane. They live in
``platform_admin_app`` rather than in a customer app so that every line of
delegation logic stays in one place and the customer-plane diff is a single
``include()``.

``exchange/`` is the handoff: it turns the one-time code an administrator was given
when the grant was minted into a session credential. It is necessarily
unauthenticated — the caller holds the code and nothing else — so it runs with
``authentication_classes = []``. That is deliberate and load-bearing: an ambient
customer JWT, or a stray admin cookie, must have no influence whatsoever on who a
redeemed code belongs to. The administrator is read from the STORED grant.

``session/`` and ``end/`` are the delegated session talking about itself. They are
reached only through the delegated credential, and both are on the middleware's
allowlist for BOTH scopes — being able to leave a tenant must never depend on
holding a write scope.

Responses are hand-built dicts. There is no ``ModelSerializer`` anywhere near
``DelegationGrant`` or ``DelegatedSession``, so ``exchange_code_hash`` and
``token_hash`` have no path to a response even by omission.
"""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from platform_admin_app import delegated_sessions
from platform_admin_app.delegated_middleware import (
    code_from_request,
    delegation_context,
    stamp_audit_context,
    stamp_delegated,
)


class DelegationExchangeThrottle(AnonRateThrottle):
    """
    Per-IP cap on redemption attempts.

    Defence in depth only — guessing a 288-bit code is not a real threat, and DRF's
    counters are per-process without a shared cache (see
    ``platform_admin_app.throttles``). What it buys is that a flood of redemption
    attempts costs an attacker something before it reaches a locking DB read.
    """

    scope = 'delegation_exchange'


class _DelegatedView(APIView):
    """
    Base for the two credential-bearing views.

    The middleware already stamps these responses (both routes are on its
    allowlist); doing it here as well makes the no-store / acting-as property a
    property of the VIEW, so it holds regardless of middleware ordering — and both
    calls are idempotent.
    """

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return stamp_delegated(response)


class DelegationExchangeView(APIView):
    """``POST`` a one-time code in ``X-Delegation-Code`` → a delegated session."""

    # No authenticator at all. The code is the credential; nothing else about the
    # caller may influence the outcome.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [DelegationExchangeThrottle]

    def post(self, request):
        # The middleware only stamps requests carrying a SESSION; a redemption
        # carries a code, so the audit context is attached here instead — otherwise
        # the session_started row would have no request id and no source IP.
        stamp_audit_context(request)
        # Header-only, like the diner capability channel. A credential in a query
        # string or body ends up in access logs, Referer headers and history.
        raw_code = code_from_request(request)
        try:
            raw_token, context = delegated_sessions.exchange_code(
                raw_code,
                ip=getattr(request, 'client_ip', None),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                request=request,
            )
        except delegated_sessions.DelegatedSessionError as exc:
            # One generic message for missing / unknown / expired / used / revoked:
            # the caller is unauthenticated, so telling them which guess was closer
            # would be an oracle. The audit entry carries the real reason.
            return stamp_delegated(Response(
                {'status': 400, 'message': exc.message}, status=400,
            ))

        # The ONLY time the raw session token is readable. It is not stored and
        # cannot be recovered from the row afterwards.
        return stamp_delegated(Response(
            {
                'status': 201,
                'message': 'Delegated session started.',
                'data': {
                    'session_token': raw_token,
                    'acting_as': context.acting_as(),
                },
            },
            status=201,
        ))


class DelegationSessionView(_DelegatedView):
    """``GET`` the acting-as context of the current delegated session."""

    def get(self, request):
        context = delegation_context(request)
        if context is None:  # pragma: no cover - the middleware refuses first
            return Response({'status': 401, 'message': 'Not a delegated session.'}, status=401)
        return Response(
            {'status': 200, 'message': 'ok', 'data': context.acting_as()}, status=200,
        )


class DelegationEndView(_DelegatedView):
    """``POST`` to leave the tenant. Idempotent, and never scope-gated."""

    def post(self, request):
        context = delegation_context(request)
        if context is None:  # pragma: no cover - the middleware refuses first
            return Response({'status': 401, 'message': 'Not a delegated session.'}, status=401)
        delegated_sessions.end_session(context, request=request)
        return Response(
            {'status': 200, 'message': 'Delegated session ended.'}, status=200,
        )
