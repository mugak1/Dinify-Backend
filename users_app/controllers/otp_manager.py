from typing import Optional
import base64
import logging
import secrets
import uuid
import hmac
import hashlib
import json
import threading
from decouple import config
from datetime import timedelta
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone
from users_app.models import User, UserOtp
from misc_app.controllers.notifications.notification import Notification
from notifications_app.controllers.messenger import Messenger
from notifications_app.controllers.sms import send_sms
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError
from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from users_app import customer_access, otp_accounting

logger = logging.getLogger(__name__)


# ━━ ONE ISSUER PER REPLACEMENT BUCKET AT A TIME (D11 E-R3) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# A replacement bucket is ``(user, msisdn, purpose)``: the rows ``make_otp`` deletes
# before it writes a new challenge. The DELETE locks only the rows it can SEE, and under
# READ COMMITTED it cannot see a row another issuer has written and not yet committed.
# So two issuers of one bucket used to each delete the older row, each insert, and leave
# TWO live challenges, both of which a login or a reset would accept.
#
# Every issuer of a bucket now takes a transaction-scoped advisory lock on that bucket
# before its DELETE. The second issuer waits until the first COMMITS, and its DELETE then
# reads a fresh snapshot that includes the first issuer's row, so it replaces that row.
#
# THE NAMESPACE. ``0x4F545049`` ("OTPI") is the first key of PostgreSQL's two-integer
# advisory form, which keeps a key space of its own (``objsubid = 2`` in ``pg_locks``),
# apart from the single-bigint form the restaurant admission lock uses. Nothing else in
# this repository takes a two-integer advisory lock.
ISSUANCE_BUCKET_LOCK_CLASS = 0x4F545049

# VERSIONED, and changing it is a rollout event: two workers that derive different keys
# for one bucket do not wait for each other, which is exactly the race this closes.
_ISSUANCE_BUCKET_DOMAIN = b'dinify.otp-issuance-bucket.v1\x00'


def issuance_bucket_key(user, msisdn, purpose) -> int:
    """
    The second advisory key for one replacement bucket: a signed 32-bit integer.

    The input is the bucket as the replacement DELETE filters it, so equal keys can only
    come from one bucket or a hash collision. The user is its primary key. The msisdn
    (already canonicalised by ``make_otp`` where it can be) and the purpose go through
    the coercion the DELETE's lookups apply, ``CharField.get_prep_value``, so a caller's
    non-string value names the same bucket here as it does there. A JSON array keeps the
    three values apart: ``null`` (no user, no destination, no purpose) is a different
    value from ``""`` or ``"null"``, and no separator can be forged inside a string.

    Secret-free, like the admission key: it is a coordination address, not a
    credential. A collision between two buckets only makes their issuers take turns.
    """
    msisdn = UserOtp._meta.get_field('msisdn').get_prep_value(msisdn)
    purpose = UserOtp._meta.get_field('purpose').get_prep_value(purpose)
    material = json.dumps(
        [None if user is None else str(user.pk), msisdn, purpose],
        separators=(',', ':'),
    )
    digest = hashlib.sha256(_ISSUANCE_BUCKET_DOMAIN + material.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big', signed=True)


def _hold_bucket(user, msisdn, purpose) -> None:
    """
    Take the bucket's advisory lock for the rest of the issuance transaction.

    Called on the connection the transaction is open on (``transaction.get_connection()``,
    the same default alias as the ``atomic`` block in ``make_otp``), AFTER the user's
    KEY SHARE and BEFORE the replacement DELETE. A transaction-scoped lock is released by
    the COMMIT or ROLLBACK that ends the issuance, so nothing is held when the code is
    sent and nothing can leak onto a reused connection.

    REFUSED outside a transaction: there an ``_xact_`` lock is released by the statement
    that takes it and protects nothing. Not PostgreSQL: a no-op, so the unit suite still
    runs on another backend, which therefore proves nothing about concurrency.
    """
    conn = transaction.get_connection()
    if not conn.in_atomic_block:
        raise RuntimeError(
            'the issuance bucket lock is taken inside the issuance transaction, never alone'
        )
    if conn.vendor != 'postgresql':
        return
    with conn.cursor() as cursor:
        cursor.execute(
            'SELECT pg_advisory_xact_lock(%s, %s)',
            [ISSUANCE_BUCKET_LOCK_CLASS, issuance_bucket_key(user, msisdn, purpose)],
        )


# Lock an OTP challenge after this many failed verification attempts. A module
# constant (not a per-row column) so it can't be tampered with per challenge.
OTP_MAX_ATTEMPTS = 5

# The OTP purposes that are part of GENERIC CUSTOMER AUTHENTICATION — the two flows
# that can end in a customer session or a customer password. An identity that is not
# customer-access-established is refused these, and ONLY these.
#
# NARROW ON PURPOSE (Step 2D.1). A blanket "this user gets no OTPs at all" would
# foreclose the very flow the pending state exists for: the future owner-invitation
# redemption may well want a factor of its own, under its own purpose, and it must be
# able to reach a pending identity — that is the one identity it is for. So the policy
# names the two customer-auth purposes rather than the user.
CUSTOMER_AUTH_OTP_PURPOSES = frozenset({'login', 'reset-password'})

# The ONLY purposes the generic ``resend-otp`` route issues (D11 E-R2), matched EXACTLY:
# no case-folding, trimming or other coercion. ``None`` is the purpose an omitted or
# JSON-null value already meant, and stays accepted. ``owner-claim`` is deliberately
# absent: its challenge is issued only by the dedicated owner-claim route, which binds
# it to the invitation and the CURRENT canonical phone, and a generic resend under that
# purpose replaced that challenge outright.
#
# A TUPLE, NOT A SET, ON PURPOSE. Membership in a set hashes the candidate, and a JSON
# array or object arrives as an unhashable list or dict and would RAISE; a tuple compares
# by equality, so every JSON value (string, number, boolean, null, array, object) gets
# an answer.
GENERIC_RESEND_PURPOSES = ('login', 'reset-password', 'register', None)
INVALID_RESEND_PURPOSE = {'status': 400, 'message': 'Invalid purpose'}

# ━━ A LOGIN RESEND NAMES THE PASSWORD LOGIN IT CONTINUES (D11 E-R4) ━━━━━━━━━━━━━━━━━━━
#
# A login resend is honoured only for the client that completed the password step. That
# client is handed ``login_resend_proof`` in the login response: an HMAC, under the OTP
# pepper and its own domain, of the user's id and the id of the ANCHOR challenge that
# login wrote (``(user, NULL, 'login')``). A resend must present it, and is honoured only
# while that exact anchor still exists and is less than LOGIN_RESEND_WINDOW old.
#
# Nothing is stored: the proof is derived from two ids and the server key, so there is no
# migration and no row to clean up. It dies on its own when the anchor is replaced (a newer
# password login for the same account), when the anchor ages out of the window, and when
# the pepper changes. It is never derived from anything a second party can learn: the user
# id and the phone number select an account; they prove nothing.
_LOGIN_RESEND_PROOF_DOMAIN = b'dinify.login-resend-proof.v1\x00'
LOGIN_RESEND_PROOF_PREFIX = 'v1.'
LOGIN_RESEND_PROOF_MAX_LENGTH = 128
LOGIN_RESEND_WINDOW = timedelta(minutes=5)
# The proof is DISCLOSED only to a client that says it can carry one: the login request
# names `login_resend_protocol` with exactly this string. That is a compatibility signal,
# not authentication. It authorizes no resend and selects no fallback: a client that
# omits it still logs in and gets the login code, and its resend is refused like any
# other resend without a proof.
LOGIN_RESEND_PROTOCOL = 'v1'
# One refusal for every login resend that is not honoured: unknown or malformed account,
# no phone, no anchor in the window, a missing, malformed or wrong proof. The wording is
# the one the anchor refusal has always used, so a deployed client shows the same toast.
LOGIN_RESEND_REFUSAL = {
    'status': 400,
    'message': 'Please provide your username and password again to get a login OTP',
}


def wants_login_resend_proof(capability) -> bool:
    """Whether a login request opted into the resend proof. Only the exact string."""
    return type(capability) is str and capability == LOGIN_RESEND_PROTOCOL


def login_resend_proof(user_pk, anchor_pk) -> str:
    """The proof a password login hands its client. Both ids are UUIDs."""
    mac = hmac.new(
        _otp_pepper(),
        _LOGIN_RESEND_PROOF_DOMAIN
        + uuid.UUID(str(user_pk)).bytes
        + uuid.UUID(str(anchor_pk)).bytes,
        hashlib.sha256,
    ).digest()
    return LOGIN_RESEND_PROOF_PREFIX + base64.urlsafe_b64encode(mac).rstrip(b'=').decode('ascii')


def _otp_pepper() -> bytes:
    """
    Server-side HMAC key for OTP hashing.

    Uses a dedicated ``OTP_HMAC_PEPPER`` when configured (stronger secret
    separation); otherwise derives one deterministically from ``SECRET_KEY`` so
    prod and CI work with zero config changes. Always returns ``bytes`` (the
    configured value is a ``str`` and must be encoded before use as an HMAC key).
    """
    configured = config('OTP_HMAC_PEPPER', default=None)
    if configured:
        return configured.encode()
    return hmac.new(
        settings.SECRET_KEY.encode(), b'otp-pepper', hashlib.sha256
    ).hexdigest().encode()


class OtpManager:
    # The pk of the challenge the last `make_otp` call on THIS instance committed, or
    # None when that call committed nothing. Cleared at the start of every call and set
    # only from the row its own transaction wrote, so a password login binds its resend
    # proof to the exact anchor it created, never to whichever row is newest (D11 E-R4).
    issued_otp_id = None

    def make_otp(
        self,
        user: Optional[User] = None,
        msisdn: Optional[str] = None,
        purpose: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> bool:
        """
        Write one challenge and deliver its code. Returns whether delivery was
        established (see each environment branch below).

        ``origin`` is the SERVER-OWNED reason the code was requested, recorded in the
        OTP accounting ledger (D11 B2-C). Each production caller names its own; any
        other value, or none, is recorded as ``unattributed``. It changes nothing about
        what is sent or returned.

        ━━ THE ISSUANCE TRANSACTION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        The replacement delete, the new challenge and its ``pending`` ledger row are
        written in ONE durable transaction that COMMITS BEFORE ANY SENDER IS ENTERED,
        so no lock and no transaction is held across SMS or e-mail I/O. For a
        user-backed challenge it takes the user's row ``FOR KEY SHARE`` first; see
        ``otp_accounting.hold_subject`` for the deadlock that ordering prevents. Every
        challenge, user-backed or not, then takes the advisory lock on its replacement
        bucket (``_hold_bucket``, D11 E-R3), so a second issuer of the same bucket waits
        for this one to commit and then replaces its row. The order is: KEY SHARE, bucket
        lock, replacement DELETE, challenge INSERT, ledger INSERT, COMMIT, then delivery.
        That serializes the issuers that take the lock. It does not order requests by
        arrival or by SMS delivery.

        ``durable=True`` REFUSES to run inside a caller's transaction, before any write
        or send: an enclosing transaction would hold the new challenge's locks across
        delivery. If the ledger cannot be written, the whole transaction rolls back —
        the challenge it would have replaced survives — nothing is sent, and the answer
        is ``False``, the value every caller already handles as a delivery failure.
        """
        self.issued_otp_id = None
        # Canonicalise the msisdn at OTP creation so the stored UserOtp.msisdn,
        # the dedup filter and the SMS target are all canonical, and verify
        # (which also canonicalises) compares canonical-to-canonical. Defensive:
        # never let a bad msisdn break OTP creation.
        if msisdn is not None:
            try:
                msisdn = normalise_msisdn(msisdn)
            except MsisdnError:
                logger.warning("make_otp: could not canonicalise msisdn; using raw value")
        # ISSUANCE GATE (Step 2D.1). Refuse to mint a generic customer-auth OTP for an
        # identity that may not hold a customer session. The security boundary is at
        # the SINKS below (verify_otp's mint, and reset_password's resolver); this is
        # the half that stops a spendable-looking credential existing at all, exactly
        # as `services.revoke_pending_customer_otps` does for promotion — and it stops
        # the platform telling an owner "OTP sent" for a flow it will then refuse.
        #
        # Returns False, which every caller already handles as a delivery failure: the
        # two callers that matter never reach this line for a pending identity (login
        # gates first, `initiate_password_reset`'s resolver gates first), so in
        # practice only `resend_otp` sees it, and its 500 is the same answer a real
        # SMS failure produces. Purposes outside CUSTOMER_AUTH_OTP_PURPOSES are
        # untouched.
        if (
            purpose in CUSTOMER_AUTH_OTP_PURPOSES
            and user is not None
            and customer_access.is_refused(user)
        ):
            logger.info(
                'make_otp: refused (customer access not established, purpose=%s)',
                purpose,
            )
            return False

        env = config('ENV')
        otp = secrets.randbelow(9000) + 1000
        otp_str = str(otp)
        if env in ['dev']:
            otp_str = '1234'

        # Salted HMAC-SHA256 (keyed by the server pepper): the stored hash can
        # neither be reversed nor precomputed from a DB read alone. make_otp and
        # verify_otp hash identically (same pepper + per-row salt).
        pepper = _otp_pepper()
        salt = secrets.token_hex(16)
        otp_hash = hmac.new(
            pepper, (salt + otp_str).encode(), hashlib.sha256
        ).hexdigest()

        # Stable identity for this challenge (also the per-identifier throttle key).
        if user is not None:
            identifier = f"user:{user.id}"
        elif msisdn is not None:
            identifier = f"msisdn:{msisdn}"
        else:
            identifier = ''

        origin = otp_accounting.server_origin(origin)
        subject_key, destination_key = otp_accounting.issuance_keys(
            user=user, msisdn=msisdn, pepper=pepper,
        )

        user_otp = UserOtp(
            user=user,
            msisdn=msisdn,
            otp_hash=otp_hash,
            purpose=purpose,
            salt=salt,
            identifier=identifier,
            attempts=0,
            consumed_at=None,
        )
        try:
            with transaction.atomic(durable=True):
                if user is not None:
                    otp_accounting.hold_subject(user.pk)
                # One issuer per bucket at a time (D11 E-R3). Taken before the DELETE, so
                # a second issuer's DELETE runs after this transaction commits and sees
                # the row it wrote.
                _hold_bucket(user, msisdn, purpose)
                # Replace only this PURPOSE's earlier challenge to the same identity and
                # destination (D11 E-R2). A challenge issued for another purpose belongs
                # to another flow, and deleting it interrupted that flow: an anonymous
                # reset request deleted a login in progress (both store msisdn NULL), and
                # an account-resolving resend deleted a live owner-claim challenge (both
                # store the phone), whose correct code was then charged to the
                # invitation's claim budget. `purpose=None` matches `purpose IS NULL`, so
                # a null-purpose request still replaces only an earlier null-purpose one.
                UserOtp.objects.filter(user=user, msisdn=msisdn, purpose=purpose).delete()
                user_otp.save()
                otp_accounting.record_issuance(
                    otp_id=user_otp.pk, subject_key=subject_key,
                    destination_key=destination_key, origin=origin,
                )
        except DatabaseError as exc:
            logger.error(
                'make_otp: challenge not recorded; nothing was sent (category=%s)',
                otp_accounting.failure_category(exc),
            )
            return False

        self.issued_otp_id = user_otp.pk

        # Until a branch below establishes otherwise, the outcome is not known — which
        # is also what is recorded if a sender raises.
        dispatch_state = otp_accounting.STATE_UNKNOWN
        try:
            delivered, dispatch_state = self._dispatch(user, msisdn, otp_str, env)
        finally:
            otp_accounting.conclude_issuance(user_otp.pk, dispatch_state)
        return delivered

    @staticmethod
    def _dispatch(user, msisdn, otp_str, env):
        """
        Deliver a committed challenge's code. Returns ``(delivered, ledger_state)``;
        ``delivered`` is exactly what ``make_otp`` has always returned for the branch.
        Runs with no transaction open.
        """
        otp_message = f"Your Dinify OTP is {otp_str}."
        if msisdn is None:
            msisdn = user.phone_number

        def _send_email() -> bool:
            recipients = [user.email] if user and user.email else []
            if not recipients:
                return False
            otp_email_message = f"{otp_message} OTP is valid for 5 minutes."
            return bool(Messenger().send_email(
                to=recipients, cc=[], subject='Dinify OTP',
                message=otp_email_message
            ))

        if env == 'dev':
            # dev: UNCHANGED contract — fire-and-forget thread, immediate True,
            # no delivery latency. The hardcoded '1234' flow (above) plus this
            # short-circuit are a deliberate pre-launch state; the SMS sender's
            # own ENV gate makes the threaded send a no-op here anyway.
            def _send_otp_notifications():
                # The catches log a FIXED category and never the exception: an
                # adapter's exception text can carry the destination, the gateway URL
                # and query (with credentials) or the message itself (D11 B1).
                try:
                    send_sms(message=otp_message, msisdn=msisdn)
                except Exception:
                    logger.error("OTP SMS dispatch raised an exception")
                try:
                    _send_email()
                except Exception:
                    logger.error("OTP email dispatch raised an exception")

            threading.Thread(target=_send_otp_notifications, daemon=True).start()
            # The SMS sender's own ENV gate makes the SMS a no-op here, so what may
            # still go out is e-mail — asynchronously, with no outcome ever reported.
            # With nobody to e-mail, nothing is dispatched at all.
            has_email = bool(user and user.email)
            return True, (
                otp_accounting.STATE_UNKNOWN if has_email
                else otp_accounting.STATE_NOT_DISPATCHED
            )

        # test/prod: the caller needs the TRUTH, so the SMS goes out
        # synchronously with a tight cap (3s — never the default 10s) and the
        # return value reflects what the gateway actually said. This is the
        # sanctioned "return value needed" exception to the threaded-SMS rule.
        sms_ok = send_sms(message=otp_message, msisdn=msisdn, timeout=3)

        if env == 'test':
            if sms_ok:
                # Email stays a secondary, fire-and-forget channel in test.
                def _send_email_async():
                    try:
                        _send_email()
                    except Exception:
                        logger.error("OTP email dispatch raised an exception")

                threading.Thread(target=_send_email_async, daemon=True).start()
                return True, otp_accounting.STATE_ACCEPTED
            # SMS failed: email is a REAL delivery channel in test — send it
            # synchronously and report ITS truth, so a user who received the
            # email can still log in.
            try:
                emailed = _send_email()
            except Exception:
                logger.error("OTP email dispatch raised an exception")
                return False, otp_accounting.STATE_UNKNOWN
            return emailed, (
                otp_accounting.STATE_ACCEPTED if emailed else otp_accounting.STATE_UNKNOWN
            )

        # prod is SMS-only by design — no email channel here.
        return sms_ok, (
            otp_accounting.STATE_ACCEPTED if sms_ok else otp_accounting.STATE_UNKNOWN
        )

    def verify_otp(
        self,
        otp: str,
        user_id: Optional[str] = None,
        msisdn: Optional[str] = None,
        email: Optional[str] = None,
        expected_purpose: Optional[str] = None,
        expected_msisdn: Optional[str] = None,
    ) -> dict:
        """
        Verify a submitted OTP for one identity. Returns the shared result dict.

        ``user_id`` / ``msisdn`` / ``email`` select WHOSE challenge to look at. The two
        ``expected_*`` arguments are different in kind: they NARROW the locked query, so
        a row that does not match them is not merely rejected — it is never selected, and
        a challenge issued for something else is left completely untouched.

        ━━ WHY EXPECTED PURPOSE EXISTS (Step 2F.2) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        Without it this method picks the most recent live challenge for the identity and
        reads ``purpose`` OFF THE ROW, so "the code was correct" does not mean "the code
        was issued for what I am about to authorise". For owner-claim redemption, which
        converts a bearer credential into restaurant authority, a valid login or
        password-reset code would satisfy it, and under ``ENV=dev`` every code is
        ``1234``, so the numbers would match by construction.

        It was not harmless for login either (D11 E-R2). Unbound, the ``verify-otp`` route
        took the newest live row of ANY purpose: a newer reset, claim or null-purpose row
        hid the login code, a wrong guess was charged to that row, and a correct reset or
        claim code was CONSUMED there, where it mints nothing, so its own flow then failed.
        That route now binds ``expected_purpose='login'``.

        ━━ AND WHY EXPECTED DESTINATION EXISTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        An OTP proves control of the number it was DELIVERED TO. If the identity's phone
        changes after a challenge is issued, the old code proves control of the old
        number and nothing about the current one. ``expected_msisdn`` binds the row to
        the destination the caller believes is current, so a code sent to a superseded
        number cannot be spent.

        It is matched EXACTLY and is deliberately NOT canonicalised here, unlike the
        ``msisdn`` identity selector above. The caller passes a value it has already
        proved canonical; normalising would let ``+256…`` satisfy a ``256…`` expectation,
        which is precisely the widening the binding exists to prevent. A mismatch simply
        selects no row and answers ``invalid``, which is fail-closed.

        ━━ BACKWARD COMPATIBILITY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        Both default to ``None`` and add NOTHING to the query when omitted, so the
        callers that pass neither (``self_register`` and ``create_employee``) behave
        exactly as before. That is pinned by tests rather than assumed — this is an
        authentication primitive, and a silent change to what it selects would be felt in
        shipped flows. ``reset_password`` binds ``expected_purpose='reset-password'`` and
        no destination (D11 E-R1); the ``verify-otp`` endpoint binds
        ``expected_purpose='login'`` and no destination (D11 E-R2). Neither caller's
        binding changes the default for anybody else.
        """
        # Canonicalise the msisdn so lookups match canonically-stored OTPs.
        # Defensive: on a bad msisdn, leave it raw — the lookup simply finds
        # nothing and returns "invalid OTP" rather than raising.
        if msisdn is not None:
            try:
                msisdn = normalise_msisdn(msisdn)
            except MsisdnError:
                pass

        invalid = {
            'status': 200,
            'message': 'Invalid OTP',
            'data': {
                'valid': False,
            }
        }

        # Never crash on a missing code — treat as invalid.
        if otp is None:
            return invalid
        otp = str(otp)

        # Resolve the identity to a single-table filter. If none is derivable
        # (all identifiers None) return invalid rather than crashing.
        if user_id is not None:
            identity = {'user_id': user_id}
        elif msisdn is not None:
            identity = {'msisdn': msisdn}
        elif email is not None:
            # Resolve email -> user_id so the locked query stays single-table
            # (avoids SELECT ... FOR UPDATE across a join).
            resolved_id = (
                User.objects.filter(email=email)
                .values_list('id', flat=True)
                .first()
            )
            if resolved_id is None:
                return invalid
            identity = {'user_id': resolved_id}
        else:
            return invalid

        time_now = timezone.now()

        with transaction.atomic():
            # Fetch the SINGLE active challenge for this identity (NOT by the
            # submitted hash — the hash now depends on the per-row salt) and
            # lock the row so concurrent verifies serialise on the counter.
            # The optional bindings NARROW this query rather than being checked after
            # it. That matters: a purpose or destination mismatch must leave the other
            # challenge entirely alone — unselected, unconsumed and with its own attempt
            # counter untouched — instead of burning somebody else's factor.
            bindings = {}
            if expected_purpose is not None:
                bindings['purpose'] = expected_purpose
            if expected_msisdn is not None:
                bindings['msisdn'] = expected_msisdn

            challenge = (
                UserOtp.objects.select_for_update()
                .filter(
                    **identity,
                    **bindings,
                    consumed_at__isnull=True,
                    expiry_time__gte=time_now,
                )
                .order_by('-time_created')
                .first()
            )
            if challenge is None:
                return invalid

            # Too many wrong guesses: locked. Consume it so it can't be retried.
            if challenge.attempts >= OTP_MAX_ATTEMPTS:
                challenge.consumed_at = time_now
                # update_fields excludes expiry_time so the set_expiry_time
                # pre_save signal can't re-extend the window on this write.
                challenge.save(update_fields=['consumed_at'])
                return invalid

            candidate = hmac.new(
                _otp_pepper(), (challenge.salt + otp).encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(challenge.otp_hash, candidate):
                # Wrong guess — count it (the row is locked, so this is race-free).
                challenge.attempts += 1
                challenge.save(update_fields=['attempts'])
                # Evidence only (D11 B2-C), in its own savepoint AFTER the counter:
                # its failure never costs the counter or changes this answer.
                otp_accounting.observe_verification_failure(
                    challenge,
                    bound_redemption=(
                        expected_purpose == otp_accounting.BOUND_REDEMPTION_PURPOSE
                    ),
                    pepper=_otp_pepper(),
                )
                return invalid

            # Correct: mark single-use so a verified code can never be replayed.
            challenge.consumed_at = time_now
            challenge.save(update_fields=['consumed_at'])
            purpose = challenge.purpose
            otp_user = challenge.user

        # if the otp purpose is for login, make a token and return it
        if purpose == 'login':
            # Platform staff authenticate on the admin origin only. Refused HERE,
            # immediately above the mint, for the same reason login.py:112 sits above
            # its own RefreshToken.for_user(): this is the LAST customer-token mint
            # that did not read account_type. A platform-staff account cannot
            # ORIGINATE a login OTP (make_otp(purpose='login') is only reached from
            # login.py:176, below that refusal) — but promotion does not invalidate an
            # OTP already in flight, so an owner who logged in moments before being
            # promoted could still spend the pending code here. promote_to_platform_staff
            # now purges those rows; this is the sink half, and it also covers rows left
            # by any future promotion path.
            #
            # Returns the shared `invalid` dict, so the refusal is byte-identical to a
            # wrong code. That is not politeness: verify-otp is AllowAny with a
            # client-supplied user id, so a distinct shape or status would be an
            # account-type oracle — and three callers index ['data']['valid'] unguarded.
            if getattr(otp_user, 'account_type', None) == ACCOUNT_TYPE_PLATFORM_STAFF:
                logger.info(
                    'verify_otp: refused (platform staff on customer origin)')
                return invalid

            # The Step-2D.1 half of the same argument, and an INDEPENDENT token sink:
            # a pending identity cannot ORIGINATE a login OTP (make_otp refuses the
            # purpose above, and login gates before ever asking), but a code already
            # in flight when the state was written would otherwise still be spendable
            # here. Same shape as the refusal above it — the shared `invalid` dict —
            # because verify-otp is AllowAny with a client-supplied user id, so a
            # distinct response would be an account-state oracle, and three callers
            # index ['data']['valid'] unguarded.
            if customer_access.is_refused(otp_user):
                logger.info(
                    'verify_otp: refused (customer access not established)')
                return invalid

            # The single sanctioned customer mint.
            token = customer_access.issue_customer_tokens(otp_user)
            return {
                'status': 200,
                'message': 'Valid OTP',
                'data': {
                    'valid': True,
                    'token': str(token.access_token),
                    'refresh': str(token)
                }
            }

        return {
            'status': 200,
            'message': 'Valid OTP',
            'data': {
                'valid': True,
            }
        }

    def resend_otp(
        self,
        identification: Optional[str] = None,
        identifier: Optional[str] = None,
        purpose: Optional[str] = None,
        login_resend_proof: Optional[str] = None,
    ) -> dict:
        # The purpose is a fact about the REQUEST, so it is decided FIRST — before any
        # account is resolved, and before the presence check below — and the refusal is
        # identical whatever the identifier names. Nothing is looked up, issued, deleted,
        # recorded or sent (D11 E-R2). Every other value used to be accepted: an unknown
        # word occupied a row of its own, and `owner-claim` replaced the live claim
        # challenge, which purpose-scoped replacement alone cannot prevent.
        if purpose not in GENERIC_RESEND_PURPOSES:
            return dict(INVALID_RESEND_PURPOSE)
        user = None
        msisdn = None
        if identification is None or identifier is None:
            return {
                'status': 400,
                'message': 'Please provide both identification and identifier'
            }
        # A LOGIN resend is decided before any other account lookup, so every refusal is
        # one answer whatever the identifier names (D11 E-R4).
        if purpose == 'login':
            return self._resend_login(identification, identifier, login_resend_proof)
        try:
            if identification == 'id':
                user = User.objects.get(pk=identifier)
            elif identification == 'phone':
                user = User.objects.get(phone_number=identifier)
            elif identification == 'email':
                user = User.objects.get(email=identifier)
            elif identification == 'msisdn':
                # No user context: issue/resend an msisdn-keyed challenge.
                # Canonicalise so it matches the stored/compared form; a bad
                # number is a clean 400 rather than a later None-deref.
                try:
                    msisdn = normalise_msisdn(identifier)
                except MsisdnError:
                    return {
                        'status': 400,
                        'message': 'Invalid phone number'
                    }
                # Reuse an existing account for this number when there is one.
                user = User.objects.filter(phone_number=msisdn).first()
        except Exception as error:
            logger.error("OTP Resend Error: %s", error)
            return {
                'status': 400,
                'message': 'User not found'
            }

        # Issue the OTP against whichever identity we resolved.
        # The origin says WHY a code was requested: a resend admitted under a login
        # anchor, or any other resend request.
        if user is not None:
            made = self.make_otp(
                user=user, purpose=purpose, msisdn=user.phone_number,
                origin=otp_accounting.ORIGIN_RESEND_REQUEST,
            )
        elif msisdn is not None:
            # Never a login resend: those return from _resend_login above.
            made = self.make_otp(
                msisdn=msisdn, purpose=purpose,
                origin=otp_accounting.ORIGIN_RESEND_REQUEST,
            )
        else:
            return {
                'status': 400,
                'message': 'User not found'
            }

        if made:
            return {
                'status': 200,
                'message': 'OTP sent successfully'
            }

        return {
            'status': 500,
            'message': "We couldn't send your verification code. Please try again."
        }

    def _resend_login(self, identification, identifier, proof) -> dict:
        """
        Re-send a login code, but only to the client that completed the password step.

        The account is selected by its id, the only shape a supported client sends, and
        the request must carry the ``login_resend_proof`` that password login returned.
        The proof is checked against every anchor ``(user, NULL, 'login')`` written in
        the last LOGIN_RESEND_WINDOW; one matches only if this client's own login wrote
        it and no newer login has replaced it. As before, the anchor's consumption and
        attempt count are not considered, and a resend never renews the window: the
        resend's own row stores the phone, so it is never an anchor.

        Every refusal is the one ``LOGIN_RESEND_REFUSAL``, written before anything is
        looked up beyond the anchors, so it issues, deletes, records and sends nothing.

        What this does NOT promise, stated rather than implied:

        * The check is a plain read at admission. A login that replaces the anchor, or
          the window closing, after that read does not stop a resend already admitted:
          it is issued, to the same account phone.
        * A retry with the same proof is a NEW issuance, not a repeat delivery: it
          replaces the earlier resend's row, whose code then stops working.
        * While a resend row is live, the anchor's own code is shadowed at verification
          (the newest live login row is selected); it can become selectable again once
          the resend row is spent, expired or locked.
        * The same body and status for every refusal is not the same timing, and says
          nothing about throttling (a 429) or authentication outcomes.
        """
        if identification != 'id' or not isinstance(proof, str):
            return dict(LOGIN_RESEND_REFUSAL)
        if (
            len(proof) > LOGIN_RESEND_PROOF_MAX_LENGTH
            or not proof.startswith(LOGIN_RESEND_PROOF_PREFIX)
            or not proof.isascii()
        ):
            return dict(LOGIN_RESEND_REFUSAL)
        try:
            user_pk = uuid.UUID(str(identifier))
        except (ValueError, TypeError, AttributeError):
            return dict(LOGIN_RESEND_REFUSAL)
        anchors = UserOtp.objects.filter(
            user_id=user_pk,
            purpose='login',
            msisdn__isnull=True,
            time_created__gte=timezone.now() - LOGIN_RESEND_WINDOW,
        ).values_list('pk', flat=True)
        if not any(
            hmac.compare_digest(login_resend_proof(user_pk, anchor).encode('ascii'),
                                proof.encode('ascii'))
            for anchor in anchors
        ):
            return dict(LOGIN_RESEND_REFUSAL)
        user = User.objects.filter(pk=user_pk).first()
        # An account with NO phone is refused: its resend row would store `msisdn IS
        # NULL`, replace the anchor and renew the window, and there is nowhere to send.
        if user is None or not user.phone_number:
            return dict(LOGIN_RESEND_REFUSAL)
        made = self.make_otp(
            user=user, purpose='login', msisdn=user.phone_number,
            origin=otp_accounting.ORIGIN_LOGIN_RESEND,
        )
        if made:
            return {
                'status': 200,
                'message': 'OTP sent successfully'
            }
        return {
            'status': 500,
            'message': "We couldn't send your verification code. Please try again."
        }
