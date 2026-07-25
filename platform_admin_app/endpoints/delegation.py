"""
Delegation endpoints — mint, list and revoke scoped access into one restaurant.

ELEVATION. Minting hands a credential into someone else's tenant, so it is
step-up-gated: the session must have cleared a second factor within the elevation
window. Revocation deliberately is NOT — stopping access must never depend on a
second factor the administrator may be unable to produce in the moment. Listing is
a plain authenticated read.

AUDITING A PERMISSION DENIAL. DRF rejects a failed permission inside
``check_permissions``, *before* the handler runs — so a stale-elevation mint would
otherwise return 403 with no audit entry, breaking ``AdminAPIView``'s
"exactly one entry per unsafe request, including denials". ``permission_denied`` is
overridden below to record the refusal before DRF raises.

No exchange path exists here. Redeeming a code for a delegated session, and
enforcing ``scope`` on the customer plane, are PR-4b.
"""
from django.core.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from platform_admin_app import delegation
from platform_admin_app.audit_actions import (
    ADMIN_DELEGATION_MINT_DENIED,
    ADMIN_DELEGATION_REVOKED,
)
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_SUCCESS,
    DelegationGrant,
)
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView

# Newest-first history is capped so `?all=true` can never become an unbounded scan.
HISTORY_LIMIT = 100


def _serialize(grant):
    """
    A grant as the portal sees it.

    Hand-built rather than a ``ModelSerializer`` so ``exchange_code_hash`` cannot be
    exposed by omission — the field simply has no path to a response.
    """
    return {
        'id': str(grant.id),
        'restaurant_id': str(grant.restaurant_id),
        'restaurant_name': grant.restaurant.name,
        'administrator': grant.administrator.username,
        'scope': grant.scope,
        'reason': grant.reason,
        'session_ttl_seconds': grant.session_ttl_seconds,
        'issued_at': grant.issued_at.isoformat(),
        'code_expires_at': grant.code_expires_at.isoformat(),
        'redeemed_at': grant.redeemed_at.isoformat() if grant.redeemed_at else None,
        'revoked_at': grant.revoked_at.isoformat() if grant.revoked_at else None,
        'revoked_reason': grant.revoked_reason,
        'is_code_live': grant.is_code_live,
        'is_session_live': grant.is_session_live,
    }


class AdminDelegationsView(AdminAPIView):
    """``GET`` the live grants; ``POST`` to mint a new one (elevation required)."""

    def get_permissions(self):
        # Per-method, because minting and listing sit at the same path but carry very
        # different risk. Mirrors the house per-action `get_throttles()` idiom.
        # NOTE: DRF does not merge permission_classes across the MRO, so
        # IsAuthenticated is restated — dropping it would assert freshness without
        # identity.
        if self.request.method == 'POST':
            return [IsAuthenticated(), IsRecentlyElevated()]
        return [IsAuthenticated()]

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused mint before DRF turns it into a 403.

        Only when the request authenticated successfully: an unauthenticated call is
        a 401 about identity, not a delegation decision, and must not manufacture a
        ``mint_denied`` entry for a caller we cannot name.
        """
        if request.method == 'POST' and getattr(request, 'successful_authenticator', None):
            self.audit(
                request,
                ADMIN_DELEGATION_MINT_DENIED,
                result=RESULT_DENIED,
                resource_type='DelegationGrant',
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    def get(self, request):
        show_all = str(request.GET.get('all', '')).lower() in ('1', 'true', 'yes')
        queryset = (
            DelegationGrant.objects
            .select_related('restaurant', 'administrator')
            .order_by('-issued_at')
        )
        if show_all:
            grants = list(queryset[:HISTORY_LIMIT])
        else:
            # "Live" spans both clocks — a code still redeemable, or a delegated
            # session still running. Evaluated via the model properties so there is
            # one definition of live rather than a second one in SQL.
            grants = [
                grant for grant in queryset[:HISTORY_LIMIT]
                if grant.is_code_live or grant.is_session_live
            ]
        return Response(
            {
                'status': 200,
                'message': 'ok',
                'data': [_serialize(grant) for grant in grants],
            },
            status=200,
        )

    def post(self, request):
        restaurant = _resolve_restaurant(request.data.get('restaurant_id'))
        try:
            raw_code, grant = delegation.mint_grant(
                administrator=request.user,
                admin_session=request.auth,
                restaurant=restaurant,
                scope=request.data.get('scope'),
                reason=request.data.get('reason'),
                session_ttl_seconds=request.data.get('session_ttl_seconds'),
                request=request,
            )
        except delegation.DelegationValidationError as exc:
            self.audit(
                request,
                ADMIN_DELEGATION_MINT_DENIED,
                result=RESULT_DENIED,
                resource_type='DelegationGrant',
                restaurant_id=restaurant.id if restaurant is not None else None,
                reason=str(request.data.get('reason') or '')[:500],
                error_code=exc.code,
            )
            return Response(
                {
                    'status': 400,
                    'message': 'The delegation could not be created.',
                    'errors': exc.errors,
                },
                status=400,
            )

        # The ONLY time the raw exchange code is ever readable. It is not stored and
        # cannot be recovered from the grant afterwards.
        return Response(
            {
                'status': 201,
                'message': 'Delegation created.',
                'data': {
                    **_serialize(grant),
                    'exchange_code': raw_code,
                },
            },
            status=201,
        )


class AdminDelegationRevokeView(AdminAPIView):
    """``POST`` to revoke a grant. Idempotent, and deliberately not elevation-gated."""

    def post(self, request, grant_id):
        grant = (
            DelegationGrant.objects
            .select_related('restaurant', 'administrator')
            .filter(id=grant_id)
            .first()
        )
        if grant is None:
            self.audit(
                request,
                ADMIN_DELEGATION_REVOKED,
                result=RESULT_DENIED,
                resource_type='DelegationGrant',
                resource_id=str(grant_id),
                error_code='grant_not_found',
            )
            return Response(
                {'status': 404, 'message': 'Not found.'}, status=404,
            )

        already_revoked = grant.revoked_at is not None
        delegation.revoke_grant(
            grant,
            request.data.get('reason', ''),
            request=request,
            actor=request.user,
        )
        if already_revoked:
            # The service no-ops on an already-revoked grant, so it wrote nothing.
            # Record the attempt here instead — exactly one entry either way, and a
            # repeated revoke stays visible without pretending state changed.
            self.audit(
                request,
                ADMIN_DELEGATION_REVOKED,
                result=RESULT_SUCCESS,
                resource_type='DelegationGrant',
                resource_id=str(grant.id),
                restaurant_id=grant.restaurant_id,
                delegation_id=grant.id,
                error_code='already_revoked',
            )
        return Response(
            {
                'status': 200,
                'message': 'Delegation revoked.',
                'data': _serialize(grant),
            },
            status=200,
        )


def _resolve_restaurant(restaurant_id):
    """
    Resolve a restaurant id to a row, or ``None``.

    Returns ``None`` for a missing, malformed or unknown id alike — the service then
    reports one 'No such restaurant.' for all three, so a malformed UUID never 500s
    and an unknown id never enumerates. Liveness (``deleted``) is the service's call,
    since there is no soft-delete manager on ``Restaurant``.
    """
    if not restaurant_id:
        return None
    from restaurants_app.models import Restaurant  # lazy: avoid an app import cycle

    try:
        return Restaurant.objects.filter(id=restaurant_id).first()
    except (ValueError, TypeError, ValidationError):
        return None
