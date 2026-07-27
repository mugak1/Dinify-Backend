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
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from platform_admin_app.models import AdminLoginChallenge
from platform_admin_app.sessions import hash_token

_TOKEN_BYTES = 48
_CHALLENGE_TTL_DEFAULT = timedelta(minutes=5)
_MAX_ATTEMPTS_DEFAULT = 5


def _ttl():
    return getattr(settings, 'ADMIN_CHALLENGE_TTL', _CHALLENGE_TTL_DEFAULT)


def max_attempts():
    """
    How many second-factor guesses one challenge tolerates before it dies.

    Its OWN setting. This used to read ``ADMIN_LOCKOUT_THRESHOLD``, which silently
    coupled the per-challenge budget to the account lockout policy — raising the
    threshold to soften the lockout denial-of-service would have widened this at the
    same time, which is the opposite of what that change wanted.
    """
    return getattr(settings, 'ADMIN_CHALLENGE_MAX_ATTEMPTS', _MAX_ATTEMPTS_DEFAULT)


@transaction.atomic
def create_challenge(user, *, recovery_only=False):
    """
    Mint a challenge for ``user``; returns ``(raw_token, challenge)``.

    Any still-live challenge for the same user is consumed first, so a fresh
    password submission always invalidates the previous half-finished attempt
    rather than leaving two valid paths open.

    ``recovery_only=True`` marks it as the break-glass path out of a lockout: only a
    one-shot recovery code may spend it. ``login/`` sets this when the account is
    locked but the password was correct.

    CONCURRENCY. The consume and the insert used to be two autocommitted statements
    with no lock, so two simultaneous correct-password logins could interleave
    ``UPDATE → UPDATE → INSERT → INSERT`` and leave the user holding TWO live
    challenges — quietly breaking the promise the paragraph above makes. They are
    now one transaction serialised on the ``User`` row, and a partial unique index
    (``one_live_admin_challenge_per_user``, migration ``0008``) enforces the same
    invariant in the database, where it cannot be forgotten by a future caller.

    LOCK ORDER: ``User`` → ``AdminLoginChallenge`` → ``PlatformStaffAuth``. Locking
    ``PlatformStaffAuth`` here instead would INVERT the order ``verify/`` takes:
    this function goes on to write-lock challenge rows through its ``UPDATE``, so
    holding the auth row first would give ``PlatformStaffAuth → AdminLoginChallenge``
    against verification's ``AdminLoginChallenge → PlatformStaffAuth`` — a real
    deadlock cycle. ``User`` is safe to take first because the admin-auth
    transactions all acquire it in ONE consistent position:
    ``resolve_challenge(for_update=True)`` passes ``of=('self',)``, so its
    ``select_related`` does not lock the joined ``User`` row as a side effect.

    This used to read "precisely because nothing else holds it", which was FALSE and
    is worth recording rather than quietly deleting. ``delegated_sessions.exchange_code``
    held a ``User`` row lock — and a ``Restaurant`` one — because it combined
    ``select_for_update()`` with a multi-table ``select_related()`` and no ``of=``,
    which on PostgreSQL locks every row in the join. Its lock ORDER was never wrong
    (it takes all three rows in a single statement, so it can neither self-deadlock
    nor interleave), but the breadth was, and it closed a cycle against the lifecycle
    transition service. PR-E scoped that lock too, so this module is once again the
    only place that takes ``User`` EXCLUSIVELY — note the qualifier: every
    ``AdminAuditLog`` insert takes ``FOR KEY SHARE`` on its actor's row, so "nothing
    else locks it" was never going to be true as stated. That is the lesson worth
    keeping: "nothing else does X" is a claim about the whole codebase, and this one
    went stale without anyone touching this file.
    """
    # Serialise concurrent minting for this user. `.first()` rather than `.get()`:
    # the row is guaranteed to exist (the caller resolved `user` from it), and this
    # function's job is to lock, not to re-validate the account.
    get_user_model().objects.select_for_update().filter(pk=user.pk).first()

    now = timezone.now()
    # Re-read AFTER the lock: a concurrent login may have inserted and committed
    # while this transaction waited, and that row is the one that must be consumed.
    AdminLoginChallenge.objects.filter(
        user=user, consumed_at__isnull=True,
    ).update(consumed_at=now)

    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    challenge = AdminLoginChallenge.objects.create(
        user=user,
        token_hash=hash_token(raw_token),
        created_at=now,
        expires_at=now + _ttl(),
        recovery_only=recovery_only,
    )
    return raw_token, challenge


def resolve_challenge(raw_token, *, for_update=False):
    """
    Resolve a raw challenge token to a live challenge, or ``None``.

    ``None`` for an empty/unknown token, one already consumed, one past expiry, or
    one that has burned through its attempt budget.

    ``for_update=True`` takes a row lock on the challenge, and MUST be called inside
    a transaction. The verification endpoint uses it so the liveness checks below are
    made against a row no concurrent request can consume underneath them — without
    it, two requests resolved the same challenge and both minted a session.
    ``of=('self',)`` keeps the lock on the challenge row alone: the ``select_related``
    would otherwise lock the joined ``User`` row as a side effect.
    """
    if not raw_token:
        return None

    queryset = AdminLoginChallenge.objects.select_related('user')
    if for_update:
        queryset = queryset.select_for_update(of=('self',))

    try:
        challenge = queryset.get(token_hash=hash_token(raw_token))
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
    """
    Count a failed second-factor guess against the challenge.

    A single ``F()`` update rather than an in-memory increment plus ``save()``, so
    parallel guesses cannot overwrite one another's count. The in-memory object is
    brought back into step for the caller.
    """
    AdminLoginChallenge.objects.filter(pk=challenge.pk).update(
        attempts=F('attempts') + 1,
    )
    challenge.refresh_from_db(fields=['attempts'])
    return challenge


def consume(challenge):
    """
    Spend the challenge, but only if it is still unspent. Returns True iff we won.

    Conditional on purpose. The unconditional ``save()`` this replaces let two
    concurrent verifications both "consume" the same challenge and both mint a
    session. The caller runs this inside the session transaction and treats False as
    a lost race, discarding everything it did — including the second factor it just
    consumed.
    """
    updated = AdminLoginChallenge.objects.filter(
        pk=challenge.pk, consumed_at__isnull=True,
    ).update(consumed_at=timezone.now())
    return bool(updated)
