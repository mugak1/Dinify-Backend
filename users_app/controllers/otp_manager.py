from typing import Optional
import random
import hashlib
from datetime import datetime, timedelta
from users_app.models import User, UserOtp
from misc_app.controllers.notifications.notification import Notification
from rest_framework_simplejwt.tokens import RefreshToken


class OtpManager:
    def make_otp(self, user: User, purpose: Optional[str] = None) -> True:
        otp = random.randint(1000, 9999)
        otp_str = str(otp)
        otp_str = '1234'
        encrypted_otp = hashlib.sha256(otp_str.encode()).hexdigest()
        user_otp = UserOtp(
            user=user,
            otp_hash=encrypted_otp,
            purpose=purpose
        )
        user_otp.save()
        Notification(msg_data={
            'msg_type': 'otp',
            'first_name': user.first_name,
            'otp': otp_str,
        }).create_notification()
        return True

    def verify_otp(self, user_id, otp) -> bool:
        encrypted_otp = hashlib.sha256(otp.encode()).hexdigest()
        try:
            UserOtp.objects.get(
                user_id=user_id,
                otp_hash=encrypted_otp,
                expiry_time__gte=datetime.now()
            )
            return True
        except UserOtp.DoesNotExist:
            return False

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
            else:
                user = User.objects.get(phone_number=identifier)
        except Exception as error:
            print(f"OTP Resend Error: {error}")
            return {
                'status': 400,
                'message': 'User not found'
            }

        # if the purpose is login, check if there is a recent otp,
        # the otp should not be older than 5 minutes
        if purpose == 'login':
            ten_minutes_ago = datetime.now() - timedelta(minutes=5)
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

        if self.make_otp(user=user, purpose=purpose):
            return {
                'status': 200,
                'message': 'OTP sent successfully'
            }

        return {
            'status': 500,
            'message': 'Failed to send OTP'
        }
