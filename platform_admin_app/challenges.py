"""
Login-challenge lifecycle — the first-factor receipt in the two-step admin login.

Mirrors ``platform_admin_app.sessions`` deliberately: a high-entropy random token
handed to the browser once, only its SHA-256 hash stored, and every expiry check
made server-side on read. The difference is lifetime and meaning — a challenge says
"this request proved the password minutes ago", nothing more, and is consumed the
moment it buys a session.
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from platform_admin_app.models import AdminLoginChallenge
from platform_admin_app.sessions import hash_token

_TOKEN_BYTES = 48
_CHALLENGE_TTL_DEFAULT = timedelta(minutes=5)
_MAX_ATTEMPTS_DEFAULT = 5


def _ttl():
    return getattr(settings, 'ADMIN_CHALLENGE_TTL', _CHALLENGE_TTL_DEFAULT)


def max_attempts():
    return getattr(settings, 'ADMIN_LOCKOUT_THRESHOLD', _MAX_ATTEMPTS_DEFAULT)


def create_challenge(user):
    """
    Mint a challenge for ``user``; returns ``(raw_token, challenge)``.

    Any still-live challenge for the same user is consumed first, so a fresh
    password submission always invalidates the previous half-finished attempt
    rather than leaving two valid paths open.
    """
    now = timezone.now()
    AdminLoginChallenge.objects.filter(
        user=user, consumed_at__isnull=True,
    ).update(consumed_at=now)

    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    challenge = AdminLoginChallenge.objects.create(
        user=user,
        token_hash=hash_token(raw_token),
        created_at=now,
        expires_at=now + _ttl(),
    )
    return raw_token, challenge


def resolve_challenge(raw_token):
    """
    Resolve a raw challenge token to a live challenge, or ``None``.

    ``None`` for an empty/unknown token, one already consumed, one past expiry, or
    one that has burned through its attempt budget.
    """
    if not raw_token:
        return None
    try:
        challenge = AdminLoginChallenge.objects.select_related('user').get(
            token_hash=hash_token(raw_token),
        )
    except AdminLoginChallenge.DoesNotExist:
        return None

    if challenge.consumed_at is not None:
        return None
    if timezone.now() >= challenge.expires_at:
        return None
    if challenge.attempts >= max_attempts():
        return None
    return challenge


def record_attempt(challenge):
    """Count a failed second-factor guess against the challenge."""
    challenge.attempts += 1
    challenge.save(update_fields=['attempts'])
    return challenge


def consume(challenge):
    """Mark the challenge spent. Called in the same transaction as the session mint."""
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=['consumed_at'])
    return challenge
