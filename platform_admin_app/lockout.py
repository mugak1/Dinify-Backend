"""
Durable, database-backed lockout for platform-staff accounts.

This — not the DRF throttles — is the guarantee. Throttle counters live in a
per-process ``LocMemCache`` (no ``CACHES`` is configured), so under mod_wsgi they
are per-worker and vanish on restart. ``PlatformStaffAuth.failed_attempts`` /
``locked_until`` survive both, so an attacker cannot reset the count by spreading
requests across workers or waiting for a deploy.

Counts BOTH factors: a wrong password and a wrong TOTP code advance the same
counter, because they are attempts on the same account.

CONCURRENCY. ``register_failure`` re-reads the row under ``select_for_update``
inside its own ``transaction.atomic()``. It used to increment an in-memory value and
``save()``, so parallel failures overwrote one another and the durable counter — the
only real guarantee — could be outrun by simply attacking in parallel. Taking the
lock here rather than relying on the caller makes it exact for BOTH call shapes: the
verify/elevate paths already hold this row's lock in the surrounding transaction, so
the nested block is a savepoint and re-locking is free, while ``login/`` calls it
with no transaction open and gets a real one.

PROGRESSIVE BACKOFF. A flat window plus a low threshold was a denial-of-service on a
platform with ONE administrator: anyone who learned the username could keep the
account locked indefinitely with a handful of wrong passwords. The threshold is now
higher and the window grows per failure beyond it, so a casual nuisance costs the
administrator a minute rather than a quarter of an hour, while sustained guessing
still escalates hard. The real escape is the break-glass path in ``endpoints/auth.py``
— a correct password plus a one-shot recovery code clears the lock, and an attacker
who does not hold a recovery code cannot trigger it.

``failed_attempts`` is deliberately CUMULATIVE: an expired window does not zero it,
so the next failure re-locks at the next step up rather than restarting at the
bottom. Only a successful verification (``reset``), the break-glass path, or
``manage.py unlock_platform_admin`` clears it.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

_THRESHOLD_DEFAULT = 10
_BACKOFF_BASE_DEFAULT = timedelta(minutes=1)
_BACKOFF_CAP_DEFAULT = timedelta(minutes=60)

# Ceiling on the doubling exponent. The cap below makes anything past a handful of
# steps identical anyway; this only stops a large counter from building an absurd
# intermediate timedelta before ``min()`` discards it.
_MAX_SHIFT = 20


def threshold():
    return getattr(settings, 'ADMIN_LOCKOUT_THRESHOLD', _THRESHOLD_DEFAULT)


def backoff_base():
    return getattr(settings, 'ADMIN_LOCKOUT_BACKOFF_BASE', _BACKOFF_BASE_DEFAULT)


def backoff_cap():
    return getattr(settings, 'ADMIN_LOCKOUT_BACKOFF_CAP', _BACKOFF_CAP_DEFAULT)


def backoff_for(failed_attempts):
    """
    How long a lock lasts at ``failed_attempts`` cumulative failures.

    Doubles per failure past the threshold, capped: at a threshold of 10 with a
    1-minute base and a 60-minute cap, the 10th failure locks for 1 minute, the 11th
    for 2, the 12th for 4 … and the 16th and beyond for 60.
    """
    steps = max(0, int(failed_attempts) - threshold())
    return min(backoff_base() * (2 ** min(steps, _MAX_SHIFT)), backoff_cap())


def is_locked(auth):
    """True while the account is inside an active lockout window."""
    if auth is None or auth.locked_until is None:
        return False
    return timezone.now() < auth.locked_until


def register_failure(auth):
    """
    Count one failed attempt; lock the account if that reaches the threshold.

    Returns True when this failure left the account locked, so the caller can emit
    the distinct lockout audit entry instead of an ordinary failure one. With
    progressive backoff that is true of every failure at or past the threshold, not
    only the first — each one extends the window.

    Exact under concurrency: the row is re-read under ``select_for_update``, so N
    parallel failures produce a count of N. ``auth`` is refreshed in place afterwards
    so the caller's later ``is_locked`` / response logic sees the committed values.
    """
    if auth is None:
        return False

    # Import here: platform_admin_app.models imports nothing from this module, but
    # keeping the dependency lazy leaves lockout importable from settings-adjacent
    # code without dragging the app registry in.
    from platform_admin_app.models import PlatformStaffAuth

    with transaction.atomic():
        row = PlatformStaffAuth.objects.select_for_update().get(pk=auth.pk)
        row.failed_attempts = (row.failed_attempts or 0) + 1
        triggered = row.failed_attempts >= threshold()
        if triggered:
            row.locked_until = timezone.now() + backoff_for(row.failed_attempts)
        row.save(update_fields=['failed_attempts', 'locked_until'])

    auth.failed_attempts = row.failed_attempts
    auth.locked_until = row.locked_until
    return triggered


def reset(auth):
    """Clear the counter and any lock — called on a fully successful login."""
    if auth is None:
        return
    if auth.failed_attempts or auth.locked_until:
        auth.failed_attempts = 0
        auth.locked_until = None
        auth.save(update_fields=['failed_attempts', 'locked_until'])
