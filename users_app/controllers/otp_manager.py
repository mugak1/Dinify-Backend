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
from rest_framework_simplejwt.tokens import RefreshToken
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from notifications_app.controllers.messenger import Messenger
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError

logger = logging.getLogger(__name__)

# Lock an OTP challenge after this many failed verification attempts. A module
# constant (not a per-row column) so it can't be tampered with per challenge.
OTP_MAX_ATTEMPTS = 5


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
    ) -> True:
        # Canonicalise the msisdn at OTP creation so the stored UserOtp.msisdn,
        # the dedup filter and the SMS target are all canonical, and verify
        # (which also canonicalises) compares canonical-to-canonical. Defensive:
        # never let a bad msisdn break OTP creation.
        if msisdn is not None:
            try:
                msisdn = normalise_msisdn(msisdn)
            except MsisdnError:
                logger.warning("make_otp: could not canonicalise msisdn; using raw value")
        otp = secrets.randbelow(9000) + 1000
        otp_str = str(otp)
        if config('ENV') in ['dev']:
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

        def _send_otp_notifications():
            try:
                YoIntegration().send_sms(to=msisdn, message=otp_message)
            except Exception as error:
                logger.error("OTP SMS send error: %s", error)
            try:
                if config('ENV') in ['dev', 'test']:
                    recipients = [user.email] if user and user.email else []
                    if recipients:
                        otp_email_message = f"{otp_message} OTP is valid for 5 minutes."
                        Messenger().send_email(
                            to=recipients, cc=[], subject='Dinify OTP',
                            message=otp_email_message
                        )
            except Exception as error:
                logger.error("OTP email send error: %s", error)

        threading.Thread(target=_send_otp_notifications, daemon=True).start()
        return True

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
            token = RefreshToken.for_user(otp_user)
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
            'message': 'Failed to send OTP'
        }
