from typing import Optional
import logging
import random
import hashlib
import threading
from decouple import config
from datetime import timedelta
from django.utils import timezone
from users_app.models import User, UserOtp
from misc_app.controllers.notifications.notification import Notification
from rest_framework_simplejwt.tokens import RefreshToken
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from notifications_app.controllers.messenger import Messenger

logger = logging.getLogger(__name__)


class OtpManager:
    def make_otp(
        self,
        user: Optional[User] = None,
        msisdn: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> True:
        otp = random.randint(1000, 9999)
        otp_str = str(otp)
        if config('ENV') in ['dev']:
            otp_str = '1234'
        encrypted_otp = hashlib.sha256(otp_str.encode()).hexdigest()

        # delete any old otps associated with the user
        UserOtp.objects.filter(user=user, msisdn=msisdn).delete()

        user_otp = UserOtp(
            user=user,
            msisdn=msisdn,
            otp_hash=encrypted_otp,
            purpose=purpose
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
        encrypted_otp = hashlib.sha256(otp.encode()).hexdigest()
        time_now = timezone.now()

        if user_id is not None:
            otps = UserOtp.objects.filter(
                user_id=user_id,
                otp_hash=encrypted_otp,
                expiry_time__gte=time_now
            ).order_by('-time_created')
        elif msisdn is not None:
            otps = UserOtp.objects.filter(
                msisdn=msisdn,
                otp_hash=encrypted_otp,
                expiry_time__gte=time_now
            ).order_by('-time_created')
        elif email is not None:
            otps = UserOtp.objects.filter(
                user__email=email,
                otp_hash=encrypted_otp,
                expiry_time__gte=time_now
            ).order_by('-time_created')

        if otps.count() < 1:
            return {
                'status': 200,
                'message': 'Invalid OTP',
                'data': {
                    'valid': False,
                }
            }

        verified_otp = otps.first()

        # if the otp purpose is for login,
        # make a token and return it
        if verified_otp.purpose == 'login':
            token = RefreshToken.for_user(verified_otp.user)
            # delete the otp right after verification
            verified_otp.delete()
            return {
                'status': 200,
                'message': 'Valid OTP',
                'data': {
                    'valid': True,
                    'token': str(token.access_token),
                    'refresh': str(token)
                }
            }

        # delete the otp right after verification
        verified_otp.delete()
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
                pass
        except Exception as error:
            logger.error("OTP Resend Error: %s", error)
            return {
                'status': 400,
                'message': 'User not found'
            }

        # if the purpose is login, check if there is a recent otp,
        # the otp should not be older than 5 minutes
        if purpose == 'login':
            ten_minutes_ago = timezone.now() - timedelta(minutes=5)
            try:
                old_otps = UserOtp.objects.filter(
                    user_id=user.id,
                    purpose=purpose,
                    time_created__gte=ten_minutes_ago
                ).count()
                if old_otps < 1:
                    return {
                        'status': 400,
                        'message': 'Please provide your username and password again to get a login OTP'
                    }
            except UserOtp.DoesNotExist:
                return {
                    'status': 400,
                    'message': 'Please provide your username and password again to get a login OTP'
                }

        # if purpose == 'first-time-payment':
        if self.make_otp(
            user=user,
            purpose=purpose,
            msisdn=user.phone_number
        ):
            return {
                'status': 200,
                'message': 'OTP sent successfully'
            }

        return {
            'status': 500,
            'message': 'Failed to send OTP'
        }
