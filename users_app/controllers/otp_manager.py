from typing import Optional
import logging
import secrets
import hmac
import hashlib
import threading
from decouple import config
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from users_app.models import User, UserOtp
from misc_app.controllers.notifications.notification import Notification
from notifications_app.controllers.messenger import Messenger
from notifications_app.controllers.sms import send_sms
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError
from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from users_app import customer_access

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
    ) -> bool:
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
        salt = secrets.token_hex(16)
        otp_hash = hmac.new(
            _otp_pepper(), (salt + otp_str).encode(), hashlib.sha256
        ).hexdigest()

        # Stable identity for this challenge (also the per-identifier throttle key).
        if user is not None:
            identifier = f"user:{user.id}"
        elif msisdn is not None:
            identifier = f"msisdn:{msisdn}"
        else:
            identifier = ''

        # delete any old otps associated with the user
        UserOtp.objects.filter(user=user, msisdn=msisdn).delete()

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
        user_otp.save()

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
            # zero added latency. The hardcoded '1234' flow (above) plus this
            # short-circuit are a deliberate pre-launch state; the SMS sender's
            # own ENV gate makes the threaded send a no-op here anyway.
            def _send_otp_notifications():
                try:
                    send_sms(message=otp_message, msisdn=msisdn)
                except Exception as error:
                    logger.error("OTP SMS send error: %s", error)
                try:
                    _send_email()
                except Exception as error:
                    logger.error("OTP email send error: %s", error)

            threading.Thread(target=_send_otp_notifications, daemon=True).start()
            return True

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
                    except Exception as error:
                        logger.error("OTP email send error: %s", error)

                threading.Thread(target=_send_email_async, daemon=True).start()
                return True
            # SMS failed: email is a REAL delivery channel in test — send it
            # synchronously and report ITS truth, so a user who received the
            # email can still log in.
            try:
                return _send_email()
            except Exception as error:
                logger.error("OTP email send error: %s", error)
                return False

        # prod is SMS-only by design — no email channel here.
        return sms_ok

    def verify_otp(
        self,
        otp: str,
        user_id: Optional[str] = None,
        msisdn: Optional[str] = None,
        email: Optional[str] = None
    ) -> dict:
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
            challenge = (
                UserOtp.objects.select_for_update()
                .filter(
                    **identity,
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

        # if the purpose is login, check if there is a recent otp,
        # the otp should not be older than 5 minutes
        if purpose == 'login':
            five_minutes_ago = timezone.now() - timedelta(minutes=5)
            recent = UserOtp.objects.filter(
                purpose=purpose,
                time_created__gte=five_minutes_ago,
            )
            if user is not None:
                recent = recent.filter(user_id=user.id)
            elif msisdn is not None:
                recent = recent.filter(msisdn=msisdn)
            if recent.count() < 1:
                return {
                    'status': 400,
                    'message': 'Please provide your username and password again to get a login OTP'
                }

        # Issue the OTP against whichever identity we resolved.
        if user is not None:
            made = self.make_otp(user=user, purpose=purpose, msisdn=user.phone_number)
        elif msisdn is not None:
            made = self.make_otp(msisdn=msisdn, purpose=purpose)
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
