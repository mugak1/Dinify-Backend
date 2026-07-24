"""
Durable, database-backed lockout for platform-staff accounts.

This — not the DRF throttles — is the guarantee. Throttle counters live in a
per-process ``LocMemCache`` (no ``CACHES`` is configured), so under mod_wsgi they
are per-worker and vanish on restart. ``PlatformStaffAuth.failed_attempts`` /
``locked_until`` survive both, so an attacker cannot reset the count by spreading
requests across workers or waiting for a deploy.

Counts BOTH factors: a wrong password and a wrong TOTP code advance the same
counter, because they are attempts on the same account.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

_THRESHOLD_DEFAULT = 5
_DURATION_DEFAULT = timedelta(minutes=15)


def threshold():
    return getattr(settings, 'ADMIN_LOCKOUT_THRESHOLD', _THRESHOLD_DEFAULT)


def duration():
    return getattr(settings, 'ADMIN_LOCKOUT_DURATION', _DURATION_DEFAULT)


def is_locked(auth):
    """True while the account is inside an active lockout window."""
    if auth is None or auth.locked_until is None:
        return False
    return timezone.now() < auth.locked_until


def register_failure(auth):
    """
    Count one failed attempt; lock the account if that crosses the threshold.

    Returns True when THIS failure triggered the lockout, so the caller can emit the
    distinct lockout audit entry instead of an ordinary failure one.
    """
    if auth is None:
        return False

    auth.failed_attempts = (auth.failed_attempts or 0) + 1
    triggered = auth.failed_attempts >= threshold()
    if triggered:
        auth.locked_until = timezone.now() + duration()
    auth.save(update_fields=['failed_attempts', 'locked_until'])
    return triggered


def reset(auth):
    """Clear the counter and any lock — called on a fully successful login."""
    if auth is None:
        return
    if auth.failed_attempts or auth.locked_until:
        auth.failed_attempts = 0
        auth.locked_until = None
        auth.save(update_fields=['failed_attempts', 'locked_until'])
