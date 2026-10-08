from typing import Optional
import logging
import secrets
import hmac
import hashlib
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
        ``otp_accounting.hold_subject`` for the deadlock that ordering prevents.

        ``durable=True`` REFUSES to run inside a caller's transaction, before any write
        or send: an enclosing transaction would hold the new challenge's locks across
        delivery. If the ledger cannot be written, the whole transaction rolls back —
        the challenge it would have replaced survives — nothing is sent, and the answer
        is ``False``, the value every caller already handles as a delivery failure.
        """
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
        purpose: Optional[str] = None
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

        # A LOGIN resend is only honoured within five minutes of a PASSWORD proof
        # (D11 B1). The proof is the challenge `login` itself created: it calls
        # `make_otp(user=..., purpose='login')` with no msisdn, so that row — and only
        # that row — stores `msisdn IS NULL`. A resend stores the destination phone, so
        # its own row can never satisfy this check. Before B1 any recent login-purpose
        # row qualified, which let each resend renew the window it was admitted under:
        # one genuine login kept resends (and fresh verification budgets) available for
        # ever to anyone holding the user id.
        #
        # The anchor's time is `time_created`, which nothing rewrites, so a resend never
        # refreshes it; a new genuine login replaces it (make_otp's
        # `(user, None, 'login')` replacement) and so restores eligibility. Deliberately
        # NOT considered: whether the anchor was consumed or how many attempts it holds,
        # and nothing about how a later verify selects its challenge — those are
        # unchanged.
        #
        # An account with NO phone is refused outright: its resend row would also store
        # `msisdn IS NULL`, replace the anchor through that same `(user, None, 'login')`
        # key and so renew it — the exact loop this closes — and there is nowhere to
        # send an SMS anyway.
        if purpose == 'login':
            five_minutes_ago = timezone.now() - timedelta(minutes=5)
            anchored = user is not None and bool(user.phone_number) and UserOtp.objects.filter(
                user_id=user.id,
                purpose='login',
                msisdn__isnull=True,
                time_created__gte=five_minutes_ago,
            ).exists()
            if not anchored:
                return {
                    'status': 400,
                    'message': 'Please provide your username and password again to get a login OTP'
                }

        # Issue the OTP against whichever identity we resolved.
        # The origin says WHY a code was requested: a resend admitted under a login
        # anchor, or any other resend request.
        if user is not None:
            made = self.make_otp(
                user=user, purpose=purpose, msisdn=user.phone_number,
                origin=(
                    otp_accounting.ORIGIN_LOGIN_RESEND if purpose == 'login'
                    else otp_accounting.ORIGIN_RESEND_REQUEST
                ),
            )
        elif msisdn is not None:
            # Never a login resend: that is refused above without a user.
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
