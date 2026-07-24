"""
Opaque server-side admin sessions (the admin plane exits SimpleJWT entirely).

A session is a high-entropy random token handed to the browser exactly once; only
its SHA-256 hash is stored (``AdminSession.token_hash``). Every request re-derives
the hash, looks the row up, and re-checks expiry server-side, so a session can be
revoked instantly and there is no stateless-token window. The raw token is never
persisted and never logged.

Lifetimes are settings constants (defined in ``settings_admin.py``) read here via
``getattr`` with matching defaults, so these helpers also behave correctly under the
base / test settings where the admin-only names are undefined.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from platform_admin_app.models import AdminSession

# Raw-token entropy: token_urlsafe(48) yields a 64-char url-safe string (~288 bits).
_TOKEN_BYTES = 48

# Defaults mirror settings_admin.py so the helpers work under any settings module.
_ABSOLUTE_LIFETIME_DEFAULT = timedelta(hours=8)
_IDLE_TIMEOUT_DEFAULT = timedelta(minutes=30)
_TOUCH_THROTTLE_DEFAULT = timedelta(minutes=5)


def _cfg(name, default):
    return getattr(settings, name, default)


def hash_token(raw_token):
    """SHA-256 hex of the raw token — the only form ever stored or compared."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_session(user, ip=None, user_agent=''):
    """
    Mint a new admin session for ``user`` and return ``(raw_token, session)``.

    The caller (PR-2b login) is handed the raw token exactly once to set the cookie;
    only its hash is stored. ``absolute_expiry`` is fixed at issuance.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = timezone.now()
    session = AdminSession.objects.create(
        user=user,
        token_hash=hash_token(raw_token),
        issued_at=now,
        absolute_expiry=now + _cfg(
            'ADMIN_SESSION_ABSOLUTE_LIFETIME', _ABSOLUTE_LIFETIME_DEFAULT,
        ),
        last_seen=now,
        issued_ip=ip,
        issued_user_agent=user_agent or '',
    )
    return raw_token, session


def resolve_session(raw_token):
    """
    Resolve a raw token to a live ``AdminSession``, or ``None``.

    Returns ``None`` for an empty / unknown token, a revoked session, one past its
    absolute expiry, or one idle beyond the idle timeout. Never raises for a bad
    token, and never returns or logs the raw token.
    """
    if not raw_token:
        return None
    try:
        session = AdminSession.objects.select_related('user').get(
            token_hash=hash_token(raw_token),
        )
    except AdminSession.DoesNotExist:
        return None

    now = timezone.now()
    if session.revoked_at is not None:
        return None
    if now >= session.absolute_expiry:
        return None
    if now - session.last_seen > _cfg('ADMIN_SESSION_IDLE_TIMEOUT', _IDLE_TIMEOUT_DEFAULT):
        return None
    return session


def touch(session):
    """
    Advance ``last_seen`` to now, throttled to at most one write per throttle window.

    Called after a successful resolve so an actively-used session keeps sliding its
    idle window forward without incurring a DB write on every request.
    """
    now = timezone.now()
    if now - session.last_seen >= _cfg('ADMIN_SESSION_TOUCH_THROTTLE', _TOUCH_THROTTLE_DEFAULT):
        session.last_seen = now
        session.save(update_fields=['last_seen'])


def elevate(session):
    """
    Stamp ``elevated_at`` after a fresh second-factor check on a live session.

    Step-up authentication: holding a valid session is not enough for the actions
    whose blast radius is largest (delegation minting, lifecycle transitions,
    mark-paid). Those re-verify TOTP, and this records when that last happened;
    ``platform_admin_app.permissions`` decides how recent is recent enough.
    """
    session.elevated_at = timezone.now()
    session.save(update_fields=['elevated_at'])
    return session


def revoke(session, reason=''):
    """Revoke a single session (idempotent — a second call is a no-op)."""
    if session.revoked_at is None:
        session.revoked_at = timezone.now()
        session.revoked_reason = reason or ''
        session.save(update_fields=['revoked_at', 'revoked_reason'])


def revoke_all_for_user(user, reason=''):
    """Revoke every still-live session for ``user``; returns the number revoked."""
    return AdminSession.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now(),
        revoked_reason=reason or '',
    )
