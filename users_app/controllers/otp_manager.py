from typing import Optional
import random
import hashlib
from datetime import datetime
from users_app.models import User, UserOtp
from misc_app.controllers.notifications.notification import Notification


# @dataclass
class OtpManager:
    def make_otp(self, user: User) -> True:
        otp = random.randint(1000, 9999)
        otp_str = str(otp)
        otp_str = '1234'
        encrypted_otp = hashlib.sha256(otp_str.encode()).hexdigest()
        user_otp = UserOtp(user=user, otp_hash=encrypted_otp)
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
        identifier: Optional[str] = None
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

        if self.make_otp(user):
            return {
                'status': 200,
                'message': 'OTP sent successfully'
            }

        return {
            'status': 500,
            'message': 'Failed to send OTP'
        }
