"""
D11 enforcement: durable abuse limits on OTP issuance and the public msisdn lookup.

The per-process DRF throttles live in LocMemCache, so each mod_wsgi process counts on
its own and a restart forgets. These limits live in PostgreSQL and hold across every
worker. The throttles stay, as defence in depth. The design note is
``docs/engineering/d11-enforcement.md``.

THE LIMITS (owner's starting values, each a setting):

* L1  issuance per subject (``u:<User>``, or ``p1:<phone>`` with no account), per class
* L2  issuance per destination phone, across subjects and purposes, per class
* L3  issuance per client IP, unverified class only
* L4  global issuance: refuses the unverified class only; its two alerts count every
      issuance
* L5  ``GET users/msisdn-lookup/`` per client IP
* L6  verify attempts per challenge: ``otp_manager.OTP_MAX_ATTEMPTS``, unchanged

THE TWO CLASSES. An issuance is VERIFIED when the requester proved something only the
account holder has before the code was issued, and UNVERIFIED when anyone could have
asked. The class is a function of the issuance's server-owned ``origin``, the value
``make_otp`` records in the ledger, and of nothing in the request (``issuance_class``).
L1 and L2 count only the rows of the issuance's own class, against the same numbers, so
anonymous requests cannot spend a verified budget and verified ones cannot spend an
anonymous one. L3 and L4 never refuse a verified issuance: it has passed a credential
check, and L1 and L2 still bound it per account.

L1, L2 and L4 count rows of ``otp_issuances``, the B2-C ledger, which this module only
READS. L3 and L5 count hits in ``account_abuse_counters``, which this module alone
writes.

WHERE L1 TO L4 ARE DECIDED. Inside ``make_otp``'s issuance transaction, after the
user's KEY SHARE and the bucket lock (E-R3), before the replacement DELETE:
``decide_issuance`` takes more transaction-scoped advisory locks, in this order (subject
``OTPS``, destination ``OTPD``, and for an unverified issuance client ``OTPC``), then
runs ONE statement for the ledger counts and, when L3 applies, ONE for the client's
hits, and decides in Python. Every refusal of one class on one route therefore runs the
same statements, whichever limit fired.

A REFUSAL writes nothing: ``make_otp`` rolls its transaction back and raises
``OtpIssuanceLimited``, whose body is the same for every reason.
"""
import dataclasses
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import threading
import time
from typing import FrozenSet, Optional

from decouple import config
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import connections, transaction
from django.db.models import Count, DateTimeField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Now
from django.utils import timezone
from rest_framework.exceptions import APIException

from notifications_app.controllers.messenger import Messenger
from platform_admin_app.middleware import client_ip_from_request
from users_app.models import (
    ABUSE_SCOPE_MSISDN_LOOKUP,
    ABUSE_SCOPE_OTP_ISSUE,
    OTP_ORIGIN_LOGIN_RESEND,
    OTP_ORIGIN_OWNER_CLAIM_CHALLENGE,
    OTP_ORIGIN_PASSWORD_LOGIN,
    OTP_SPEND_ALERT_CAP,
    OTP_SPEND_ALERT_WARNING,
    AbuseCounter,
    OtpIssuance,
    OtpSpendAlert,
)

logger = logging.getLogger(__name__)

# ── the refusal ──────────────────────────────────────────────────────────────────

# ONE body for every limit, on every route that answers a refusal with a 429. It names
# no limit and carries no Retry-After: either would say which window fired.
LIMIT_REFUSAL_MESSAGE = 'Too many attempts. Please wait and try again.'

L1, L2, L3, L4, L5 = 'L1', 'L2', 'L3', 'L4', 'L5'


def limit_refusal() -> dict:
    """The refusal envelope, a fresh dict each time (callers return it as a body)."""
    return {'status': 429, 'message': LIMIT_REFUSAL_MESSAGE}


class OtpIssuanceLimited(APIException):
    """
    ``make_otp`` refused to issue because a limit is reached. Nothing was written.

    An ``APIException`` so that a DRF view that does not catch it still answers the
    right 429 with the right body (the owner-claim challenge does exactly that). The
    reasons are kept for the server log and never reach a response.
    """
    status_code = 429
    default_code = 'otp_issuance_limited'

    def __init__(self, reasons: FrozenSet[str] = frozenset()):
        # Not ``super().__init__``: DRF would turn every value into an ErrorDetail
        # STRING, and ``status`` would render as "429".
        self.detail = limit_refusal()
        self.reasons = frozenset(reasons)

    def __str__(self):
        return LIMIT_REFUSAL_MESSAGE


class Refused(Exception):
    """Raised INSIDE the issuance transaction so that it rolls back."""


# ── the issuance class ───────────────────────────────────────────────────────────

CLASS_VERIFIED = 'verified'
CLASS_UNVERIFIED = 'unverified'

# The origins whose code is issued only after the requester proved something the account
# holder has. AN ALLOWLIST: every other origin is unverified, ``unattributed`` (no origin,
# or one outside the vocabulary) and any origin the vocabulary gains later included, so a
# new caller is limited as an anonymous one until somebody decides otherwise.
VERIFIED_ORIGINS = frozenset({
    OTP_ORIGIN_PASSWORD_LOGIN,          # issued only after the password is accepted
    OTP_ORIGIN_LOGIN_RESEND,            # E-R4: bound to that password step's proof
    OTP_ORIGIN_OWNER_CLAIM_CHALLENGE,   # needs a valid claim token, about 288 bits
})


def issuance_class(origin) -> str:
    """
    ``verified`` or ``unverified`` for an issuance's server-owned origin. Total: any value
    that is not exactly a verified origin, a non-string included, is unverified.
    """
    if isinstance(origin, str) and origin in VERIFIED_ORIGINS:
        return CLASS_VERIFIED
    return CLASS_UNVERIFIED


def _class_rows(verified: bool) -> Q:
    """
    The ledger rows of one class. ``origin`` is NOT NULL and constrained to the
    vocabulary, so the two filters partition the ledger with nothing left over.
    """
    verified_rows = Q(origin__in=sorted(VERIFIED_ORIGINS))
    return verified_rows if verified else ~verified_rows


# ── configuration ────────────────────────────────────────────────────────────────

MIN_WINDOW_SECONDS = 60
# The ledger keeps rows for 7 days (otp_accounting.RETENTION). A longer window would
# count rows that may already be pruned, so it would under-count.
MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
ALERT_PERIOD = datetime.timedelta(hours=24)


@dataclasses.dataclass(frozen=True)
class Window:
    count: int
    seconds: int


@dataclasses.dataclass(frozen=True)
class Limits:
    subject_short: Window
    subject_long: Window
    destination_short: Window
    destination_long: Window
    client: Window
    global_: Window
    lookup: Window
    alert_percent: int
    alert_email: str

    @property
    def warning_at(self) -> int:
        """The rolling 24-hour count at which the warning alert fires (240 of 300)."""
        return math.ceil(self.global_.count * self.alert_percent / 100)

    @property
    def longest_ledger_window(self) -> int:
        return max(self.subject_short.seconds, self.subject_long.seconds,
                   self.destination_short.seconds, self.destination_long.seconds,
                   self.global_.seconds)


# (setting name, default). The defaults repeat settings.py so that a settings module
# that predates this change still gets the owner's values.
_WINDOWS = {
    'subject_short': ('OTP_LIMIT_SUBJECT_SHORT', 3, 900),
    'subject_long': ('OTP_LIMIT_SUBJECT_LONG', 10, 86400),
    'destination_short': ('OTP_LIMIT_DESTINATION_SHORT', 3, 900),
    'destination_long': ('OTP_LIMIT_DESTINATION_LONG', 10, 86400),
    'client': ('OTP_LIMIT_CLIENT', 20, 3600),
    'global_': ('OTP_LIMIT_GLOBAL', 300, 86400),
    'lookup': ('MSISDN_LOOKUP_LIMIT_CLIENT', 30, 3600),
}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read():
    """``(values, errors)``. An error names a setting, never a value."""
    errors = []
    windows = {}
    for field, (prefix, default_count, default_seconds) in _WINDOWS.items():
        count = getattr(settings, f'{prefix}_COUNT', default_count)
        seconds = getattr(settings, f'{prefix}_SECONDS', default_seconds)
        if not _is_int(count) or count < 1:
            errors.append(f'{prefix}_COUNT must be an integer of at least 1.')
        if not _is_int(seconds) or not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
            errors.append(
                f'{prefix}_SECONDS must be an integer from {MIN_WINDOW_SECONDS} '
                f'to {MAX_WINDOW_SECONDS}.')
        windows[field] = Window(count, seconds)
    percent = getattr(settings, 'OTP_SPEND_ALERT_PERCENT', 80)
    if not _is_int(percent) or not 1 <= percent <= 100:
        errors.append('OTP_SPEND_ALERT_PERCENT must be an integer from 1 to 100.')
    email = getattr(settings, 'OTP_SPEND_ALERT_EMAIL', '')
    if not isinstance(email, str):
        errors.append('OTP_SPEND_ALERT_EMAIL must be a string.')
        email = ''
    email = email.strip()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            errors.append('OTP_SPEND_ALERT_EMAIL must be empty or one e-mail address.')
    return Limits(alert_percent=percent, alert_email=email, **windows), errors


def configuration_errors() -> list:
    """What the system check reports. Empty when the settings are usable."""
    return _read()[1]


def current() -> Limits:
    """
    The limits in force, read from settings on every call (so a test can override
    them). FAILS CLOSED: an unusable value raises, and the route answers 500 rather than
    issue unlimited codes. The system check stops such a value at deploy time.
    """
    limits, errors = _read()
    if errors:
        raise ImproperlyConfigured('D11 abuse limits are misconfigured: ' + ' '.join(errors))
    return limits


# ── the client ───────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Client:
    """
    The network client a request came from, as the trusted position reports it.

    A caller that is NOT acting for a network client passes no ``Client`` at all, and
    L3 does not apply to it. Nor does L3 apply to a VERIFIED issuance, whatever its
    client: ``decide_issuance`` drops the key, so it takes no client lock, reads no
    counter and writes no hit. A request with no usable address still gets a
    ``Client``; its key is the one shared ``unknown`` key, which fails closed together.
    """
    address: Optional[str]

    @classmethod
    def from_request(cls, request) -> 'Client':
        return cls(address=client_ip_from_request(request))


_CLIENT_KEY_PREFIX = 'ip1:'
# VERSIONED: changing it, or the pepper, starts every client counter again.
_CLIENT_KEY_DOMAIN = b'dinify:abuse-limits:client:v1\x00'
_UNKNOWN_ADDRESS = 'unknown'


def client_network(address) -> str:
    """
    What a client is counted as. IPv4: the address. IPv6: its /64, which one subscriber
    usually holds whole, so the full address would let one host step through 2^64 keys.
    IPv4-mapped IPv6: the IPv4 address. Anything unparseable: ``unknown``.
    """
    if not isinstance(address, str):
        return _UNKNOWN_ADDRESS
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return _UNKNOWN_ADDRESS
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        prefix = (int(ip) >> 64) << 64
        return str(ipaddress.IPv6Network((prefix, 64)))
    return str(ip)


def client_key(client: Client, pepper: bytes) -> str:
    """``ip1:`` + HMAC-SHA256 under the OTP pepper. The raw address is never stored."""
    material = client_network(client.address).encode('ascii')
    digest = hmac.new(pepper, _CLIENT_KEY_DOMAIN + material, hashlib.sha256).hexdigest()
    return _CLIENT_KEY_PREFIX + digest


# ── locks ────────────────────────────────────────────────────────────────────────

# PostgreSQL's two-integer advisory form, like the bucket lock ``OTPI`` (0x4F545049).
# Each class has its own first key, so its keys never meet another class's.
SUBJECT_LOCK_CLASS = 0x4F545053      # "OTPS"
DESTINATION_LOCK_CLASS = 0x4F545044  # "OTPD"
CLIENT_LOCK_CLASS = 0x4F545043       # "OTPC"
# VERSIONED, and changing it is a rollout event: two workers deriving different keys
# for one subject do not wait for each other.
_LOCK_KEY_DOMAIN = b'dinify.abuse-limit-lock.v1\x00'


def lock_key(*parts) -> int:
    """A signed 32-bit key for one subject, destination or (scope, client)."""
    material = json.dumps(list(parts), separators=(',', ':'))
    digest = hashlib.sha256(_LOCK_KEY_DOMAIN + material.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big', signed=True)


def _hold(lock_class: int, key: int) -> None:
    """A transaction-scoped advisory lock. Refused outside a transaction."""
    conn = transaction.get_connection()
    if not conn.in_atomic_block:
        raise RuntimeError('an abuse-limit lock is taken inside a transaction, never alone')
    if conn.vendor != 'postgresql':
        return
    with conn.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [lock_class, key])


# ── counting ─────────────────────────────────────────────────────────────────────

# A counter window is cut into this many aligned buckets (5 minutes for an hour).
BUCKETS_PER_WINDOW = 12


def _now():
    """The host clock the counter buckets use. A seam for tests."""
    return timezone.now()


@dataclasses.dataclass(frozen=True)
class _Bucket:
    start: datetime.datetime
    coverage_after: datetime.datetime
    expires_at: datetime.datetime


def _bucket(window: Window, now) -> _Bucket:
    """
    The aligned bucket ``now`` falls in, and what to sum.

    Summing every bucket that STARTED after ``now - window - g`` covers every hit in the
    last ``window``, so admissions never exceed the limit in any rolling window. The
    cost: a hit is forgotten between ``window`` and ``window + g`` after it happened.
    """
    granularity = max(1, window.seconds // BUCKETS_PER_WINDOW)
    epoch = int(now.timestamp())
    start = datetime.datetime.fromtimestamp(epoch - epoch % granularity, tz=datetime.timezone.utc)
    span = datetime.timedelta(seconds=window.seconds + granularity)
    return _Bucket(start=start, coverage_after=now - span, expires_at=start + span)


def _client_hits(scope: str, key: str, bucket: _Bucket) -> int:
    total = AbuseCounter.objects.filter(
        scope=scope, client_key=key, bucket_start__gt=bucket.coverage_after,
    ).aggregate(total=Sum('hits'))['total']
    return total or 0


def _count_hit(scope: str, key: str, bucket: _Bucket) -> None:
    """One more hit. Called under the client's lock, so no two callers race here."""
    updated = AbuseCounter.objects.filter(
        scope=scope, client_key=key, bucket_start=bucket.start,
    ).update(hits=F('hits') + 1)
    if not updated:
        AbuseCounter.objects.create(
            scope=scope, client_key=key, bucket_start=bucket.start, hits=1,
            expires_at=bucket.expires_at,
        )


def _recent(seconds: int) -> Q:
    """``created_at`` within the last ``seconds``, on the database clock."""
    cutoff = ExpressionWrapper(
        Now() - Value(datetime.timedelta(seconds=seconds)), output_field=DateTimeField(),
    )
    return Q(created_at__gt=cutoff)


def _keyed(field: str, key: Optional[str], seconds: int) -> Q:
    if key is None:
        return Q(pk__isnull=True)    # never true: an unkeyed subject is not counted
    return Q(**{field: key}) & _recent(seconds)


def _ledger_counts(subject_key, destination_key, verified: bool, limits: Limits) -> dict:
    """
    ONE statement for every ledger count, the same statement whichever limit will fire.

    The four keyed counts are of the issuance's OWN class only. ``issued`` is every
    issuance, whatever its class: it is what the alerts report. ``issued_unverified`` is
    what L4 compares with the cap.

    It reads every issuance in the longest window through the ``created_at`` index. L4
    bounds the unverified rows (the cap, plus its overshoot); L1 bounds the verified rows
    to 10 a day per account that receives a verified code.
    """
    own = _class_rows(verified)
    return OtpIssuance.objects.filter(_recent(limits.longest_ledger_window)).aggregate(
        subject_short=Count('pk', filter=own & _keyed(
            'subject_key', subject_key, limits.subject_short.seconds)),
        subject_long=Count('pk', filter=own & _keyed(
            'subject_key', subject_key, limits.subject_long.seconds)),
        destination_short=Count('pk', filter=own & _keyed(
            'destination_key', destination_key, limits.destination_short.seconds)),
        destination_long=Count('pk', filter=own & _keyed(
            'destination_key', destination_key, limits.destination_long.seconds)),
        issued=Count('pk', filter=_recent(limits.global_.seconds)),
        issued_unverified=Count('pk', filter=_class_rows(False) & _recent(
            limits.global_.seconds)),
    )


# ── the issuance decision (L1-L4) ────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class IssuanceDecision:
    refused: FrozenSet[str]
    # ``issuance_class`` of the origin this decision was made for.
    issuance_class: str
    # Every issuance BEFORE this one in the L4 window, whatever its class: the alerts.
    issued: int
    # The unverified issuances before this one in the L4 window: what L4 compares.
    issued_unverified: int
    limits: Limits
    # ``None`` when L3 does not apply: no client, or a verified issuance.
    client_key: Optional[str]
    bucket: Optional[_Bucket]

    @property
    def admitted(self) -> bool:
        return not self.refused

    @property
    def verified(self) -> bool:
        return self.issuance_class == CLASS_VERIFIED


def decide_issuance(*, subject_key, destination_key, client_key, origin) -> IssuanceDecision:
    """
    Decide L1-L4 for one challenge about to be written. Called INSIDE the issuance
    transaction, after the user's KEY SHARE and the bucket lock, before the DELETE.

    ``origin`` is the server-owned origin ``make_otp`` records for this challenge, and it
    alone decides the class. It is REQUIRED: a caller that forgot it would otherwise be
    decided under a default class.

    Takes the subject, destination and client locks in that fixed order (each only when
    its key exists), so concurrent issuers for one subject, destination or client take
    turns and those three counts are exact. The subject and destination locks are the
    same for both classes, so each class's counts are exact while the other class
    issues too. A verified issuance takes no client lock (L3 does not apply to it). L4
    takes no lock: see the design note for its bounded overshoot.
    """
    limits = current()
    issuance_class_ = issuance_class(origin)
    verified = issuance_class_ == CLASS_VERIFIED
    if verified:
        client_key = None
    if subject_key is not None:
        _hold(SUBJECT_LOCK_CLASS, lock_key('subject', subject_key))
    if destination_key is not None:
        _hold(DESTINATION_LOCK_CLASS, lock_key('destination', destination_key))
    if client_key is not None:
        _hold(CLIENT_LOCK_CLASS, lock_key(ABUSE_SCOPE_OTP_ISSUE, client_key))

    counts = _ledger_counts(subject_key, destination_key, verified, limits)
    bucket, hits = None, 0
    if client_key is not None:
        bucket = _bucket(limits.client, _now())
        hits = _client_hits(ABUSE_SCOPE_OTP_ISSUE, client_key, bucket)

    refused = set()
    if subject_key is not None and (
        counts['subject_short'] >= limits.subject_short.count
        or counts['subject_long'] >= limits.subject_long.count
    ):
        refused.add(L1)
    if destination_key is not None and (
        counts['destination_short'] >= limits.destination_short.count
        or counts['destination_long'] >= limits.destination_long.count
    ):
        refused.add(L2)
    if client_key is not None and hits >= limits.client.count:
        refused.add(L3)
    if not verified and counts['issued_unverified'] >= limits.global_.count:
        refused.add(L4)
    return IssuanceDecision(
        refused=frozenset(refused), issuance_class=issuance_class_,
        issued=counts['issued'], issued_unverified=counts['issued_unverified'],
        limits=limits, client_key=client_key, bucket=bucket,
    )


def refuse_inside(decision: IssuanceDecision) -> None:
    """Raise inside the transaction when the decision refuses, so it rolls back."""
    if not decision.admitted:
        raise Refused()


def record_admission(decision: IssuanceDecision) -> None:
    """The client's hit, written in the issuance transaction, after the challenge."""
    if decision.client_key is not None:
        _count_hit(ABUSE_SCOPE_OTP_ISSUE, decision.client_key, decision.bucket)


def after_admission(decision: IssuanceDecision) -> None:
    """
    After COMMIT. Starts the warning alert when this issuance brought the count of EVERY
    issuance, verified ones included, to the threshold: the alert is about spend.
    """
    issued = decision.issued + 1
    unverified = decision.issued_unverified + (0 if decision.verified else 1)
    if issued >= decision.limits.warning_at:
        _start_alert(OTP_SPEND_ALERT_WARNING, issued, unverified, decision.limits)


def refusal(decision: IssuanceDecision) -> OtpIssuanceLimited:
    """
    After ROLLBACK: log the refusal, start the cap alert when L4 refused (which only an
    unverified issuance can be), and return the exception for ``make_otp`` to raise. The
    log names the class and the limits; the response never does.
    """
    logger.info('otp_limits: issuance refused (class=%s limits=%s)',
                decision.issuance_class, ','.join(sorted(decision.refused)))
    if L4 in decision.refused:
        _start_alert(OTP_SPEND_ALERT_CAP, decision.issued, decision.issued_unverified,
                     decision.limits)
    return OtpIssuanceLimited(decision.refused)


# ── the msisdn lookup (L5) ───────────────────────────────────────────────────────

def admit_lookup(client: Client) -> bool:
    """
    L5: whether one more ``msisdn-lookup`` from this client is allowed, counting it if
    so. Its own short transaction, under the client's lock for the lookup scope. A
    refusal writes nothing.
    """
    from users_app.controllers.otp_manager import _otp_pepper  # the OTP pepper's owner
    limits = current()
    key = client_key(client, _otp_pepper())
    with transaction.atomic(durable=True):
        _hold(CLIENT_LOCK_CLASS, lock_key(ABUSE_SCOPE_MSISDN_LOOKUP, key))
        bucket = _bucket(limits.lookup, _now())
        admitted = _client_hits(ABUSE_SCOPE_MSISDN_LOOKUP, key, bucket) < limits.lookup.count
        if admitted:
            _count_hit(ABUSE_SCOPE_MSISDN_LOOKUP, key, bucket)
    if admitted:
        prune_expired()
    else:
        logger.info('otp_limits: lookup refused (limits=%s)', L5)
    return admitted


# ── cleanup ──────────────────────────────────────────────────────────────────────

PRUNE_BATCH = 100


def prune_expired(batch: int = PRUNE_BATCH) -> None:
    """Delete at most ``batch`` dead counter rows, oldest first. Never raises."""
    try:
        now = _now()
        dead = AbuseCounter.objects.filter(expires_at__lt=now).order_by(
            'expires_at', 'pk').values('pk')[:batch]
        AbuseCounter.objects.filter(pk__in=dead, expires_at__lt=now).delete()
    except Exception as exc:  # noqa: BLE001 - housekeeping only
        logger.warning('otp_limits: prune_failed (category=%s)', type(exc).__name__)


# ── alerts (L4) ──────────────────────────────────────────────────────────────────

# A process starts at most one alert thread per alert per this many seconds, so a flood
# of refusals cannot start a flood of threads. The cross-worker "once per 24 hours" is
# the claim below, not this.
_ALERT_LOCAL_INTERVAL = 60.0
_ALERT_GATE = {}
_ALERT_GATE_LOCK = threading.Lock()

ALERT_LOG_MARKER = 'OTP_SPEND_ALERT'
ALERT_EMAIL_FAILED_MARKER = 'OTP_SPEND_ALERT_EMAIL_FAILED'
_ALERT_SUBJECTS = {
    OTP_SPEND_ALERT_WARNING: 'Dinify: one-time codes have reached {percent}% of the daily cap',
    OTP_SPEND_ALERT_CAP:
        'Dinify: the daily cap on one-time codes for unverified requests is reached',
}


def _start_alert(alert: str, issued: int, unverified: int, limits: Limits) -> None:
    """
    Hand the alert to a daemon thread, off the request path. ``issued`` is every
    issuance in the L4 window, whatever its class; ``unverified`` is the part the cap
    applies to.
    """
    started = time.monotonic()
    with _ALERT_GATE_LOCK:
        last = _ALERT_GATE.get(alert)
        if last is not None and started - last < _ALERT_LOCAL_INTERVAL:
            return
        _ALERT_GATE[alert] = started
    spawner = threading.current_thread()

    def run():
        try:
            send_alert(alert, issued, unverified, limits)
        except Exception as exc:  # noqa: BLE001 - an alert never breaks issuance
            logger.error('%s threshold=%s (category=%s)',
                         ALERT_EMAIL_FAILED_MARKER, alert, type(exc).__name__)
        finally:
            # A thread of its own opened its own database connection; close it. Run
            # inline (tests), it is the caller's connection and is left alone.
            if threading.current_thread() is not spawner:
                connections.close_all()

    _spawn(run)


def _spawn(run) -> None:
    """Start ``run`` in a daemon thread. A seam: tests run it inline."""
    threading.Thread(target=run, daemon=True).start()


def claim_alert(alert: str) -> bool:
    """
    Whether this caller may send ``alert`` now: true when it has never been sent, or was
    last sent at least 24 hours ago. The row is locked while deciding, so across every
    worker at most one caller wins per 24 hours.
    """
    now = _now()
    with transaction.atomic():
        row, created = OtpSpendAlert.objects.select_for_update().get_or_create(
            alert=alert, defaults={'last_sent_at': now},
        )
        if created:
            return True
        if row.last_sent_at > now - ALERT_PERIOD:
            return False
        OtpSpendAlert.objects.filter(pk=alert).update(last_sent_at=now)
        return True


def send_alert(alert: str, issued: int, unverified: int, limits: Limits) -> None:
    """Claim, log one bounded line, and e-mail. Runs in the alert thread."""
    if not claim_alert(alert):
        return
    email = limits.alert_email
    # The line is #374's, byte for byte: ``issued`` is every issuance, whatever its
    # class. How many of them the cap applies to is in the e-mail.
    logger.warning(
        '%s threshold=%s issued=%d cap=%d email=%s',
        ALERT_LOG_MARKER, alert, issued, limits.global_.count,
        'sending' if email else 'not_configured',
    )
    if not email:
        return
    subject = _ALERT_SUBJECTS[alert].format(percent=limits.alert_percent)
    message = alert_message(alert, issued, unverified, limits)
    if not Messenger().send_email(to=[email], cc=[], subject=subject, message=message):
        logger.error('%s threshold=%s', ALERT_EMAIL_FAILED_MARKER, alert)


def alert_message(alert: str, issued: int, unverified: int, limits: Limits) -> str:
    """The e-mail body. Counts and settings only: no phone, account or address."""
    hours = limits.global_.seconds // 3600
    lines = [
        f'Environment: {config("ENV", default="unknown")}',
        f'In the last {hours} hours Dinify issued {issued} one-time codes. {unverified} of '
        'them answered requests that proved nothing about the requester (password reset, '
        'registration and other resends). The cap applies to those requests only and is '
        f'{limits.global_.count} (OTP_LIMIT_GLOBAL_COUNT).',
    ]
    if alert == OTP_SPEND_ALERT_CAP:
        lines.append(
            'Those requests are now refused until the oldest of their codes age out of the '
            'window: HTTP 429, except a password reset request, which is acknowledged and '
            'sends nothing. Password logins, login resends and owner-claim challenges are '
            'not refused by the cap. Raise OTP_LIMIT_GLOBAL_COUNT on the host to relax it.')
    else:
        lines.append(
            f'This is the warning at {limits.alert_percent}% of the cap '
            '(OTP_SPEND_ALERT_PERCENT). It counts every code issued, whatever the request, '
            'because it is about what codes cost.')
    lines.append('This alert is sent at most once every 24 hours.')
    return '<br>'.join(lines)
