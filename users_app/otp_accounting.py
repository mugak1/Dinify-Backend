"""
OTP accounting (D11 B2-C): the evidence ledger for one-time codes.

WHAT THIS IS. Two append-mostly tables and the only code that writes them:

* ``OtpIssuance`` — one row per challenge ``OtpManager.make_otp`` wrote, recorded in the
  SAME transaction as the challenge, committed BEFORE any sender runs, then moved once
  from ``pending`` to a terminal state when the send's outcome is known.
* ``OtpVerificationFailure`` — one row per wrong code that was actually compared.

WHAT THIS IS NOT. Nothing reads these tables to decide anything. There is no rate
limit, no shadow decision and no refusal here, and recording never changes what an OTP
flow answers: a ledger failure before dispatch sends nothing (the challenge rolls back
with it), and every failure after dispatch is logged under a fixed category and
otherwise ignored.

KEYS, NOT IDENTIFIERS. A subject is ``u:<User UUID>`` or a phone key; a destination is
only ever a phone key. A phone key is ``p1:`` + HMAC-SHA256 under the existing OTP
pepper over a domain-separation label and the CANONICAL number. The pepper is passed in
by the caller (the OTP manager owns it), so this module imports neither the OTP manager
nor anything on the admin plane. A number that cannot be canonicalised is never keyed
and never stored raw: the key is NULL and a fixed category is logged.

RETENTION. A row strictly older than ``RETENTION`` is ELIGIBLE for deletion, in any
state. Deletion happens in bounded, oldest-first batches: opportunistically after each
issuance is finalized, and through ``manage.py prune_otp_accounting``. Eligibility is
not a deadline — nothing here runs on a schedule, so a row is only removed when one of
those two runs.
"""
import datetime
import hashlib
import hmac
import logging
import uuid
from typing import Optional, Tuple

from django.db import (
    DatabaseError, IntegrityError, OperationalError, connection, transaction,
)
from django.db.models import DateTimeField, ExpressionWrapper, Value
from django.db.models.functions import Now

from misc_app.controllers.msisdn import MsisdnError, normalise_msisdn
from users_app.models import (
    OTP_FAILURE_ORIGIN_UNRECORDED,
    OTP_FAILURE_ORIGINS,
    OTP_ISSUANCE_ACCEPTED,
    OTP_ISSUANCE_NOT_DISPATCHED,
    OTP_ISSUANCE_PENDING,
    OTP_ISSUANCE_STATES,
    OTP_ISSUANCE_UNKNOWN,
    OTP_ORIGIN_LOGIN_RESEND,
    OTP_ORIGIN_OWNER_CLAIM_CHALLENGE,
    OTP_ORIGIN_PASSWORD_LOGIN,
    OTP_ORIGIN_RESEND_REQUEST,
    OTP_ORIGIN_RESET_INITIATION,
    OTP_ORIGIN_UNATTRIBUTED,
    OTP_ORIGINS,
    OtpIssuance,
    OtpVerificationFailure,
    User,
)

logger = logging.getLogger(__name__)

# ── vocabulary (the database enforces the same sets) ────────────────────────────
ORIGIN_PASSWORD_LOGIN = OTP_ORIGIN_PASSWORD_LOGIN
ORIGIN_RESET_INITIATION = OTP_ORIGIN_RESET_INITIATION
ORIGIN_OWNER_CLAIM_CHALLENGE = OTP_ORIGIN_OWNER_CLAIM_CHALLENGE
ORIGIN_LOGIN_RESEND = OTP_ORIGIN_LOGIN_RESEND
ORIGIN_RESEND_REQUEST = OTP_ORIGIN_RESEND_REQUEST
ORIGIN_UNATTRIBUTED = OTP_ORIGIN_UNATTRIBUTED
ORIGINS = OTP_ORIGINS
FAILURE_ORIGIN_UNRECORDED = OTP_FAILURE_ORIGIN_UNRECORDED
FAILURE_ORIGINS = OTP_FAILURE_ORIGINS

STATE_PENDING = OTP_ISSUANCE_PENDING
STATE_ACCEPTED = OTP_ISSUANCE_ACCEPTED
STATE_UNKNOWN = OTP_ISSUANCE_UNKNOWN
STATE_NOT_DISPATCHED = OTP_ISSUANCE_NOT_DISPATCHED
STATES = OTP_ISSUANCE_STATES
TERMINAL_STATES = frozenset(STATES) - {STATE_PENDING}

# The only verifier binding that marks a failure as a redemption attempt. The same
# string as `owner_claim_redemption.OWNER_CLAIM_OTP_PURPOSE` (a test pins that); it is
# restated rather than imported so this module stays independent of the admin plane.
BOUND_REDEMPTION_PURPOSE = 'owner-claim'

RETENTION = datetime.timedelta(days=7)
OPPORTUNISTIC_PRUNE_BATCH = 100
MAX_PRUNE_BATCH = 1000

_PHONE_KEY_PREFIX = 'p1:'
_PHONE_KEY_DOMAIN = b'dinify:otp-accounting:phone:v1\x00'


# ── keys ─────────────────────────────────────────────────────────────────────────

def phone_key(canonical: str, pepper: bytes) -> str:
    """
    The pseudonymous key for ONE canonical number. Refuses anything that is not
    already canonical rather than keying a variant of it; the error names no number.
    """
    try:
        if not isinstance(canonical, str) or normalise_msisdn(canonical) != canonical:
            raise ValueError('not a canonical phone number')
    except MsisdnError:
        raise ValueError('not a canonical phone number') from None
    digest = hmac.new(
        pepper, _PHONE_KEY_DOMAIN + canonical.encode(), hashlib.sha256,
    ).hexdigest()
    return _PHONE_KEY_PREFIX + digest


def _phone_key_or_none(raw, pepper: bytes) -> Optional[str]:
    """Canonicalise then key; an unusable value is ``None``, never a raw fallback."""
    if not raw:
        return None
    try:
        return phone_key(normalise_msisdn(raw), pepper)
    except (MsisdnError, ValueError, TypeError):
        return None


def user_subject_key(user_id) -> str:
    return f'u:{uuid.UUID(str(user_id))}'


def server_origin(origin: Optional[str]) -> str:
    """Only a value from the server's own vocabulary is recorded; anything else is not."""
    if origin in ORIGINS:
        return origin
    if origin is not None:
        logger.warning('otp_accounting: origin_unrecognised')
    return ORIGIN_UNATTRIBUTED


def issuance_keys(*, user, msisdn, pepper: bytes) -> Tuple[Optional[str], Optional[str]]:
    """
    ``(subject_key, destination_key)`` for a challenge about to be written.

    The destination is where the code is SENT: the explicit ``msisdn`` when the caller
    named one, otherwise the account's stored phone — exactly the value ``make_otp``
    hands the SMS sender.
    """
    destination_raw = msisdn if msisdn is not None else (
        user.phone_number if user is not None else None
    )
    destination = _phone_key_or_none(destination_raw, pepper)
    if destination is None:
        logger.info('otp_accounting: destination_unavailable')
    if user is not None:
        subject = user_subject_key(user.pk)
    else:
        subject = _phone_key_or_none(msisdn, pepper)
    return subject, destination


# ── issuance ─────────────────────────────────────────────────────────────────────

def hold_subject(user_id) -> None:
    """
    Take the user's row ``FOR KEY SHARE`` — FIRST in the issuance transaction, before
    the challenge being replaced is deleted.

    The challenge row's foreign key to ``users`` is deferred, so without this the
    transaction would only reach ``users`` at COMMIT, AFTER it had locked the old
    challenge by deleting it. An owner-claim redemption takes the same two rows in the
    opposite order (``users`` FOR UPDATE, then the challenge FOR UPDATE), and the two
    would deadlock. Taken first, the issuance waits for the redemption holding nothing.

    Django has no spelling for KEY SHARE, hence the one raw statement.
    """
    if connection.vendor != 'postgresql':
        return
    table = connection.ops.quote_name(User._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {table} WHERE "id" = %s FOR KEY SHARE', [user_id])


def record_issuance(*, otp_id, subject_key, destination_key, origin) -> None:
    """The ``pending`` row, written inside the challenge's own transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('an issuance is recorded with its challenge, never alone')
    OtpIssuance.objects.create(
        id=otp_id, subject_key=subject_key, destination_key=destination_key,
        origin=server_origin(origin), state=STATE_PENDING,
    )


def finalize_issuance(otp_id, state: str) -> int:
    """
    The one transition out of ``pending``. Conditional, so a second call (or a call for
    a row already cleaned up) changes nothing. One statement; no transaction of its own.
    """
    if state not in TERMINAL_STATES:
        raise ValueError('not a terminal issuance state')
    return OtpIssuance.objects.filter(pk=otp_id, state=STATE_PENDING).update(
        state=state, finalized_at=Now(),
    )


def conclude_issuance(otp_id, state: str) -> None:
    """
    Finalize, then prune one bounded batch. Called after the send, outside any
    transaction. NEVER RAISES: nothing here may change what ``make_otp`` returns, cause
    a resend, or put exception text in a log.
    """
    try:
        finalize_issuance(otp_id, state)
    except Exception as exc:  # noqa: BLE001 - evidence only; see docstring
        logger.warning(
            'otp_accounting: issuance_finalize_failed (category=%s)', failure_category(exc),
        )
    prune_opportunistically()


# ── verification failures ────────────────────────────────────────────────────────

def _failure_origin(issuance_origin: Optional[str]) -> str:
    return issuance_origin if issuance_origin is not None else FAILURE_ORIGIN_UNRECORDED


def _failure_identity(challenge, pepper: bytes):
    """
    From the LOCKED challenge: the issuance's subject when one was recorded, else the
    challenge's user, else its canonical phone. The origin is copied from the issuance,
    or ``unrecorded`` for a challenge written before the ledger existed.
    """
    recorded = (
        OtpIssuance.objects.filter(pk=challenge.pk)
        .values_list('subject_key', 'origin')
        .first()
    )
    subject, origin, issuance_id = None, None, None
    if recorded is not None:
        subject, origin = recorded
        issuance_id = challenge.pk
    if subject is None:
        if challenge.user_id is not None:
            subject = user_subject_key(challenge.user_id)
        else:
            subject = _phone_key_or_none(challenge.msisdn, pepper)
    return subject, _failure_origin(origin), issuance_id


def observe_verification_failure(challenge, *, bound_redemption: bool, pepper: bytes) -> None:
    """
    Record one compared wrong code, ISOLATED from the verification around it.

    Called AFTER the challenge's attempt counter has been saved, inside the verifier's
    transaction. The write runs in a savepoint, and its error is handled OUTSIDE that
    savepoint: an ordinary failure rolls back only the observation, is logged under a
    fixed category, and leaves the counter to commit with the invalid answer.

    A failure the savepoint cannot contain — the connection is gone, so the rollback
    itself failed and the transaction is marked for rollback — is RE-RAISED. Returning
    ``invalid`` then would claim a counter increment that can no longer commit.
    """
    try:
        with transaction.atomic():
            subject, origin, issuance_id = _failure_identity(challenge, pepper)
            OtpVerificationFailure.objects.create(
                subject_key=subject, origin=origin,
                bound_redemption=bool(bound_redemption), issuance_id=issuance_id,
            )
    except Exception as exc:  # noqa: BLE001 - re-raised below unless contained
        if transaction.get_rollback():
            raise
        logger.warning(
            'otp_accounting: verification_failure_unrecorded (category=%s)',
            failure_category(exc),
        )


# ── cleanup ──────────────────────────────────────────────────────────────────────

def prune(*, batch_size: int, now: Optional[datetime.datetime] = None) -> dict:
    """
    Delete at most ``batch_size`` eligible rows from EACH table, oldest first, and
    return how many went. Eligible means STRICTLY older than ``RETENTION`` in any state,
    ``pending`` included; a row exactly at the boundary is kept.

    ``now`` defaults to the database's own clock (the one that stamped the rows).
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not (
        1 <= batch_size <= MAX_PRUNE_BATCH
    ):
        raise ValueError('batch_size out of range')
    if now is None:
        cutoff = ExpressionWrapper(
            Now() - Value(RETENTION), output_field=DateTimeField(),
        )
    else:
        cutoff = now - RETENTION
    return {
        'otp_issuances': _prune_table(OtpIssuance, 'created_at', cutoff, batch_size),
        'otp_verification_failures': _prune_table(
            OtpVerificationFailure, 'failed_at', cutoff, batch_size,
        ),
    }


def _prune_table(model, field, cutoff, batch_size) -> int:
    oldest = (
        model.objects.filter(**{f'{field}__lt': cutoff})
        .order_by(field, 'pk')
        .values('pk')[:batch_size]
    )
    deleted, _ = model.objects.filter(
        pk__in=oldest, **{f'{field}__lt': cutoff},
    ).delete()
    return deleted


def prune_opportunistically() -> None:
    """One bounded batch per table. Never raises."""
    try:
        prune(batch_size=OPPORTUNISTIC_PRUNE_BATCH)
    except Exception as exc:  # noqa: BLE001 - evidence only
        logger.warning('otp_accounting: prune_failed (category=%s)', failure_category(exc))


def failure_category(exc: BaseException) -> str:
    """A closed category for a failure; never the exception's own text."""
    if isinstance(exc, IntegrityError):
        return 'integrity'
    if isinstance(exc, OperationalError):
        return 'operational'
    if isinstance(exc, DatabaseError):
        return 'database'
    return 'other'
