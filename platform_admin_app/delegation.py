"""
Delegation-grant lifecycle — minting and revoking scoped access into one restaurant.

A grant replaces ambient authority with something bounded and attributable: WHO
reached in, WHICH restaurant, HOW MUCH (scope), FOR HOW LONG, and WHY. The one-time
exchange code follows the app's single token convention — ``secrets.token_urlsafe``
handed back exactly once, only ``hash_token``'s SHA-256 stored (imported from
``sessions``, never redefined here).

This module is the ADMIN side only. Redeeming a code for a delegated session, and
enforcing ``scope`` on the customer plane, are PR-4b — nothing here touches the
customer API, and no exchange path exists yet.

Every mint and revocation is audited inside the same transaction as the row it
describes, per the no-audit-no-action contract in ``platform_admin_app.audit``.
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from platform_admin_app import audit
from platform_admin_app.audit_actions import (
    ADMIN_DELEGATION_MINTED,
    ADMIN_DELEGATION_REVOKED,
    ADMIN_DELEGATION_SUPERSEDED,
)
from platform_admin_app.models import (
    RESULT_SUCCESS,
    SCOPE_CHOICES,
    DelegationGrant,
)
from platform_admin_app.sessions import hash_token

# Same width as the admin session / login challenge tokens (~288 bits).
_TOKEN_BYTES = 48

# Minimum substance for a stated reason. Short enough not to be bureaucratic, long
# enough that 'test' or 'x' cannot pass for a justification.
MIN_REASON_LENGTH = 10

# Defaults mirror settings_admin.py byte-for-byte so these helpers also work under
# the base / test settings, where the ADMIN_* names are absent.
_CODE_TTL_DEFAULT = timedelta(minutes=3)
_SESSION_TTL_DEFAULT = 900
_SESSION_TTL_MAX_DEFAULT = 3600
_MAX_LIVE_GRANTS_DEFAULT = 5

VALID_SCOPES = {value for value, _label in SCOPE_CHOICES}


def code_ttl():
    return getattr(settings, 'ADMIN_DELEGATION_CODE_TTL', _CODE_TTL_DEFAULT)


def default_session_ttl():
    return getattr(
        settings, 'ADMIN_DELEGATION_SESSION_TTL_DEFAULT', _SESSION_TTL_DEFAULT,
    )


def max_session_ttl():
    return getattr(
        settings, 'ADMIN_DELEGATION_SESSION_TTL_MAX', _SESSION_TTL_MAX_DEFAULT,
    )


def max_live_grants():
    return getattr(
        settings, 'ADMIN_DELEGATION_MAX_LIVE_GRANTS', _MAX_LIVE_GRANTS_DEFAULT,
    )


class DelegationValidationError(Exception):
    """
    A refused mint, carrying field-keyed errors.

    These are authenticated admin requests, so precise per-field errors are correct
    — the deliberately generic messaging of the login path exists to avoid
    disclosing account existence to anonymous callers, which does not apply here.

    ``code`` is the short machine-readable reason that goes into the audit entry's
    ``error_code``, so a refusal is greppable without parsing prose.
    """

    def __init__(self, errors, code='invalid'):
        super().__init__(code)
        self.errors = errors
        self.code = code


def live_grants_for(administrator):
    """
    This administrator's currently-live grants (code redeemable OR session running).

    The SQL prefilter bounds the set cheaply; the exact ``redeemed_at +
    session_ttl_seconds`` arithmetic then runs in Python via the model properties,
    which keeps one definition of "live" rather than duplicating it in SQL.
    """
    now = timezone.now()
    candidates = DelegationGrant.objects.filter(
        administrator=administrator, revoked_at__isnull=True,
    ).filter(Q(code_expires_at__gt=now) | Q(redeemed_at__isnull=False))
    return [g for g in candidates if g.is_code_live or g.is_session_live]


def _validate(restaurant, scope, reason, session_ttl_seconds):
    """Collect every field problem at once, so one round-trip fixes them all."""
    errors = {}
    code = 'invalid'

    # The restaurant must exist AND be live. There is no soft-delete manager in this
    # codebase — Restaurant.objects includes deleted rows — so this is explicit.
    if restaurant is None:
        errors['restaurant_id'] = 'No such restaurant.'
        code = 'restaurant_not_found'
    elif restaurant.deleted:
        # Deliberately the same message as "not found": a deleted tenant is not a
        # thing an administrator may reach into, and the distinction is not useful.
        errors['restaurant_id'] = 'No such restaurant.'
        code = 'restaurant_deleted'

    if scope not in VALID_SCOPES:
        errors['scope'] = f'Must be one of: {", ".join(sorted(VALID_SCOPES))}.'
        code = 'invalid_scope'

    cleaned_reason = (reason or '').strip()
    if not cleaned_reason:
        errors['reason'] = 'A reason is required.'
        code = 'reason_required'
    elif len(cleaned_reason) < MIN_REASON_LENGTH:
        errors['reason'] = (
            f'Please state a reason of at least {MIN_REASON_LENGTH} characters.'
        )
        code = 'reason_too_short'

    if session_ttl_seconds is not None:
        try:
            ttl = int(session_ttl_seconds)
        except (TypeError, ValueError):
            errors['session_ttl_seconds'] = 'Must be a whole number of seconds.'
            code = 'invalid_ttl'
        else:
            if ttl <= 0 or ttl > max_session_ttl():
                errors['session_ttl_seconds'] = (
                    f'Must be between 1 and {max_session_ttl()} seconds.'
                )
                code = 'invalid_ttl'

    if errors:
        raise DelegationValidationError(errors, code=code)

    return cleaned_reason


def _supersede(administrator, restaurant, request=None):
    """
    Revoke this administrator's earlier unredeemed grants for the same restaurant.

    Without this, tapping "delegate" three times leaves three live codes for one
    tenant and only the last is remembered. A superseded grant is audited under its
    own action so it never reads as a manual revocation.
    """
    now = timezone.now()
    superseded = [
        grant for grant in DelegationGrant.objects.filter(
            administrator=administrator,
            restaurant=restaurant,
            revoked_at__isnull=True,
            redeemed_at__isnull=True,
            code_expires_at__gt=now,
        )
    ]
    for grant in superseded:
        grant.revoked_at = now
        grant.revoked_reason = 'superseded'
        grant.save(update_fields=['revoked_at', 'revoked_reason'])
        _audit(
            request,
            ADMIN_DELEGATION_SUPERSEDED,
            grant=grant,
            actor=administrator,
            reason='Replaced by a newer grant for the same restaurant.',
        )
    return superseded


def _audit(request, action, *, grant, actor, reason='', after_state=None,
           result=RESULT_SUCCESS):
    """One audit entry for a grant, with the delegation and restaurant ids attached."""
    kwargs = dict(
        action=action,
        result=result,
        actor=actor,
        resource_type='DelegationGrant',
        resource_id=str(grant.id),
        restaurant_id=grant.restaurant_id,
        delegation_id=grant.id,
        reason=reason,
        after_state=after_state,
    )
    if request is not None:
        return audit.record_from_request(request, action, **{
            key: value for key, value in kwargs.items() if key != 'action'
        })
    return audit.record(**kwargs)


def mint_grant(*, administrator, admin_session, restaurant, scope, reason,
               session_ttl_seconds=None, request=None):
    """
    Mint a delegation grant; returns ``(raw_code, grant)``.

    The raw exchange code is returned to the caller EXACTLY ONCE — only its hash is
    stored, so it cannot be recovered from the row afterwards. It must never be
    logged or passed into an audit field.

    Order matters: validate, then supersede (which frees a slot for the same
    restaurant), then apply the cap, then create. Everything runs in one
    transaction, so a failed audit write unwinds the grant it would have described.
    """
    cleaned_reason = _validate(restaurant, scope, reason, session_ttl_seconds)
    ttl = int(session_ttl_seconds) if session_ttl_seconds is not None else default_session_ttl()

    with transaction.atomic():
        _supersede(administrator, restaurant, request=request)

        if len(live_grants_for(administrator)) >= max_live_grants():
            raise DelegationValidationError(
                {
                    'restaurant_id': (
                        f'You already hold {max_live_grants()} live delegations. '
                        'Revoke one before minting another.'
                    ),
                },
                code='live_grant_cap',
            )

        raw_code = secrets.token_urlsafe(_TOKEN_BYTES)
        now = timezone.now()
        grant = DelegationGrant.objects.create(
            administrator=administrator,
            admin_session=admin_session,
            restaurant=restaurant,
            scope=scope,
            reason=cleaned_reason,
            exchange_code_hash=hash_token(raw_code),
            code_expires_at=now + code_ttl(),
            session_ttl_seconds=ttl,
            issued_at=now,
            issued_ip=getattr(request, 'client_ip', None) if request else None,
            issued_user_agent=(
                request.META.get('HTTP_USER_AGENT', '') if request is not None else ''
            ),
        )
        _audit(
            request,
            ADMIN_DELEGATION_MINTED,
            grant=grant,
            actor=administrator,
            reason=cleaned_reason,
            # Scope and TTL are the shape of the authority handed over — recorded so
            # the log answers "how much could they do?" without joining the grant.
            after_state={
                'scope': scope,
                'session_ttl_seconds': ttl,
                'code_expires_at': now + code_ttl(),
                'restaurant_name': restaurant.name,
            },
        )

    return raw_code, grant


def revoke_grant(grant, reason='', *, request=None, actor=None):
    """
    Revoke a grant. Idempotent — revoking an already-revoked grant is a no-op.

    Revoking a REDEEMED grant is meaningful and allowed: PR-4b treats revocation as
    immediately killing the delegated session, not merely blocking future redemption.
    That is why this is the one delegation action that requires no step-up elevation
    — stopping access must never depend on a second factor the admin cannot produce.
    """
    if grant.revoked_at is not None:
        return grant

    with transaction.atomic():
        grant.revoked_at = timezone.now()
        grant.revoked_reason = (reason or '').strip()[:255]
        grant.save(update_fields=['revoked_at', 'revoked_reason'])
        _audit(
            request,
            ADMIN_DELEGATION_REVOKED,
            grant=grant,
            actor=actor if actor is not None else grant.administrator,
            reason=grant.revoked_reason,
        )
    return grant
