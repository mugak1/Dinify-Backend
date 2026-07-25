"""
Delegated-session lifecycle — redeeming a grant, resolving it, ending it.

This is the CUSTOMER-plane half of delegation. PR-4a's ``DelegationGrant`` records
that an administrator was authorised to reach into one restaurant; this module
turns that authorisation into a credential that actually travels, and re-decides on
every single request whether it is still good.

Three rules the rest of this feature rests on:

1. **The restaurant comes from the stored grant, never from the request.** A
   ``DelegationContext`` is built entirely from database rows. There is no code
   path in which a header, parameter or body value can influence which tenant a
   delegated session reaches.
2. **Liveness is recomputed per request, never cached.** ``resolve_session`` reads
   the session AND its grant AND the administrator in one query and re-checks all
   of them. Revoking on the admin plane therefore takes effect on the very next
   customer request, with no window and nothing to invalidate.
3. **Single use, atomically.** Redemption locks the grant row, so two concurrent
   exchanges of the same code produce exactly one session.

Token handling follows the house convention exactly: ``secrets.token_urlsafe(48)``,
returned once, with only ``hash_token``'s SHA-256 stored — and ``hash_token`` is
IMPORTED from ``sessions``, never redefined (a third copy is the anti-pattern).
"""
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import audit, delegation
from platform_admin_app.audit_actions import (
    ADMIN_DELEGATION_SESSION_ENDED,
    ADMIN_DELEGATION_SESSION_STARTED,
    ADMIN_DELEGATION_SESSION_START_DENIED,
)
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_SUCCESS,
    DelegatedSession,
    DelegationGrant,
)
from platform_admin_app.services import has_active_membership
from platform_admin_app.sessions import hash_token

# Same width as the admin session / login challenge / exchange code (~288 bits).
_TOKEN_BYTES = 48


class DelegatedSessionError(Exception):
    """
    A delegated credential that cannot be honoured.

    ``code`` is the short machine-readable reason recorded as the audit entry's
    ``error_code``. ``message`` is deliberately GENERIC at the boundary: an
    exchange caller is not yet authenticated, so distinguishing "no such code" from
    "already redeemed" would tell an anonymous prober which guesses were closer.
    """

    def __init__(self, code, message='This delegation link is no longer valid.'):
        super().__init__(code)
        self.code = code
        self.message = message


class DelegationContext:
    """
    The resolved authority of one delegated request — built from stored rows only.

    Deliberately a plain object, not a model: it is attached in memory to the
    request and to ``request.user`` as ``active_delegation``, and must have no way
    of being persisted or of being constructed from request data.
    """

    __slots__ = ('session', 'grant', 'administrator', 'restaurant')

    def __init__(self, session, grant, administrator, restaurant):
        self.session = session
        self.grant = grant
        self.administrator = administrator
        self.restaurant = restaurant

    @property
    def restaurant_id(self):
        """The ONE restaurant this session reaches, as a string, from the grant."""
        return str(self.grant.restaurant_id)

    @property
    def scope(self):
        """
        The scope this session may actually exercise RIGHT NOW.

        Normally the grant's own scope, but the restaurant's lifecycle state can
        cap it: an ``offboarded`` tenant admits ``view`` only, however the grant
        was minted (``lifecycle_policy``'s delegated-access row). Applied here, on
        the property every caller already reads — the middleware's route check, the
        permission resolver's scope grid and the session payload — so there is no
        path that sees the uncapped value and no extra query (the restaurant is
        already select_related on every resolve).

        Only ever narrows. ``effective_delegated_scope`` cannot upgrade a scope.
        """
        from restaurants_app.controllers.lifecycle_policy import (
            effective_delegated_scope,
        )

        return effective_delegated_scope(self.grant.scope, self.restaurant.status)

    @property
    def granted_scope(self):
        """The scope the grant was MINTED with, before any lifecycle ceiling."""
        return self.grant.scope

    @property
    def delegation_id(self):
        return self.grant.id

    @property
    def expires_at(self):
        return self.session.expires_at

    def acting_as(self):
        """
        The acting-as payload the portal renders its banner from.

        Names the administrator and the authority, never a token. Attribution is the
        point of this feature: a delegated session must be visibly an administrator
        acting under a delegation, not the owner.
        """
        administrator = self.administrator
        display = (
            f'{administrator.first_name or ""} {administrator.last_name or ""}'.strip()
            or administrator.email
            or str(administrator.id)
        )
        return {
            'delegation_id': str(self.grant.id),
            # The EFFECTIVE scope, so the banner states what this session can
            # actually do rather than what it was minted with — those differ at an
            # offboarded restaurant, and the narrower answer is the honest one.
            'scope': self.scope,
            'granted_scope': self.granted_scope,
            'reason': self.grant.reason,
            'expires_at': self.session.expires_at.isoformat(),
            'restaurant': {
                'id': str(self.grant.restaurant_id),
                'name': self.restaurant.name,
            },
            'administrator': {'name': display},
        }


def _administrator_still_eligible(user):
    """
    The same fail-closed identity test ``AdminSessionAuthentication`` applies.

    Re-checked on every delegated request, not just at exchange: deactivating an
    administrator, or resolving a dual-role account by giving it a restaurant
    membership, must kill any delegated session they are holding immediately.
    """
    if user is None or not user.is_active:
        return False
    if user.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
        return False
    return not has_active_membership(user)


def exchange_code(raw_code, *, ip=None, user_agent='', request=None):
    """
    Redeem a one-time exchange code; returns ``(raw_token, context)``.

    Single-use and race-safe: the grant row is locked for the whole transaction, so
    a concurrent second exchange of the same code blocks, then finds ``redeemed_at``
    already set and is refused. The raw session token is returned to the caller
    EXACTLY ONCE — only its hash is stored.

    Raises ``DelegatedSessionError`` for a missing, malformed, unknown, expired,
    already-redeemed or revoked code — all with one generic message.
    """
    if not raw_code or not isinstance(raw_code, str):
        _audit_denial(request, code='code_missing')
        raise DelegatedSessionError('code_missing')

    # ONE transaction, ONE lock. A refusal is CARRIED OUT of the block rather than
    # raised inside it: raising would roll the block back and take the denial's own
    # audit row with it, leaving a refused redemption invisible in the log. The
    # success path keeps its audit INSIDE the block, so a failed audit write unwinds
    # the session it would have described (PR-3's no-audit-no-action contract).
    denial = None
    redeemed = None

    with transaction.atomic():
        grant = (
            DelegationGrant.objects
            .select_for_update()
            .select_related('administrator', 'restaurant')
            .filter(exchange_code_hash=hash_token(raw_code))
            .first()
        )
        if grant is None:
            denial = ('code_unknown', None)
        elif not grant.is_code_live:
            # Covers redeemed, revoked and expired alike — one generic refusal, with
            # the distinction kept in the audit entry rather than the response. A
            # concurrent second exchange lands here: it blocks on the lock above,
            # then sees redeemed_at already set.
            denial = ((
                'code_revoked' if grant.revoked_at is not None
                else 'code_already_redeemed' if grant.redeemed_at is not None
                else 'code_expired'
            ), grant)
        elif not _administrator_still_eligible(grant.administrator):
            denial = ('administrator_ineligible', grant)
        elif grant.restaurant.deleted:
            denial = ('restaurant_unavailable', grant)
        else:
            now = timezone.now()
            grant.redeemed_at = now
            grant.save(update_fields=['redeemed_at'])

            raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
            session = DelegatedSession.objects.create(
                grant=grant,
                token_hash=hash_token(raw_token),
                issued_at=now,
                # The SAME arithmetic as DelegationGrant.is_session_live, so the
                # admin plane's listing and this credential cannot disagree.
                expires_at=now + timedelta(seconds=grant.session_ttl_seconds),
                issued_ip=ip,
                issued_user_agent=user_agent or '',
            )
            _audit(
                request,
                ADMIN_DELEGATION_SESSION_STARTED,
                grant=grant,
                actor=grant.administrator,
                reason=grant.reason,
                after_state={
                    'scope': grant.scope,
                    'expires_at': session.expires_at,
                    'restaurant_name': grant.restaurant.name,
                },
            )
            redeemed = (raw_token, DelegationContext(
                session=session,
                grant=grant,
                administrator=grant.administrator,
                restaurant=grant.restaurant,
            ))

    if denial is not None:
        code, denied_grant = denial
        _audit_denial(request, code=code, grant=denied_grant)
        raise DelegatedSessionError(code)

    return redeemed


def resolve_session(raw_token):
    """
    Resolve a raw session token to a live ``DelegationContext``, or ``None``.

    ONE query, NO cache, every request. Returns ``None`` for an empty or unknown
    token, an ended or expired session, a revoked grant, an administrator who is no
    longer eligible, or a restaurant that has since been deleted. Never raises for a
    bad token and never logs the raw token.
    """
    if not raw_token or not isinstance(raw_token, str):
        return None
    session = (
        DelegatedSession.objects
        .select_related('grant', 'grant__administrator', 'grant__restaurant')
        .filter(token_hash=hash_token(raw_token))
        .first()
    )
    if session is None:
        return None

    now = timezone.now()
    if session.ended_at is not None or now >= session.expires_at:
        return None

    grant = session.grant
    # Revocation wins immediately — this is the whole point of re-reading per
    # request rather than trusting the token's own expiry.
    if grant.revoked_at is not None or grant.redeemed_at is None:
        return None
    if not _administrator_still_eligible(grant.administrator):
        return None
    if grant.restaurant.deleted:
        return None

    return DelegationContext(
        session=session,
        grant=grant,
        administrator=grant.administrator,
        restaurant=grant.restaurant,
    )


def end_session(context, *, request=None, reason='ended_by_administrator'):
    """
    End a delegated session at the administrator's own request. Idempotent.

    Also revokes the underlying grant (``revoked_reason='session_ended'``). Ending
    is meant to mean "I have left this tenant", and a grant whose session has ended
    has no remaining legitimate use — leaving it live would let the same authority
    be picked up again. Reusing ``delegation.revoke_grant`` keeps one definition of
    revocation and writes its own audit entry alongside this one.
    """
    session = context.session
    if session.ended_at is None:
        with transaction.atomic():
            session.ended_at = timezone.now()
            session.ended_reason = (reason or '')[:64]
            session.save(update_fields=['ended_at', 'ended_reason'])
            _audit(
                request,
                ADMIN_DELEGATION_SESSION_ENDED,
                grant=context.grant,
                actor=context.administrator,
                reason=session.ended_reason,
            )
            delegation.revoke_grant(
                context.grant,
                'session_ended',
                request=request,
                actor=context.administrator,
            )
    return session


def _audit_denial(request, *, code, grant=None):
    """Record a refused redemption. Never carries the code that was presented."""
    kwargs = dict(
        result=RESULT_DENIED,
        resource_type='DelegatedSession',
        error_code=code,
    )
    if grant is not None:
        kwargs.update(
            actor=grant.administrator,
            resource_id=str(grant.id),
            restaurant_id=grant.restaurant_id,
            delegation_id=grant.id,
        )
    if request is not None:
        audit.record_from_request(
            request, ADMIN_DELEGATION_SESSION_START_DENIED, **kwargs,
        )
    else:
        audit.record(action=ADMIN_DELEGATION_SESSION_START_DENIED, **kwargs)


def _audit(request, action, *, grant, actor, reason='', after_state=None,
           result=RESULT_SUCCESS):
    """One entry for a delegated-session event, attributed to the administrator."""
    kwargs = dict(
        result=result,
        actor=actor,
        resource_type='DelegatedSession',
        resource_id=str(grant.id),
        restaurant_id=grant.restaurant_id,
        delegation_id=grant.id,
        reason=reason,
        after_state=after_state,
    )
    if request is not None:
        return audit.record_from_request(request, action, **kwargs)
    return audit.record(action=action, **kwargs)
