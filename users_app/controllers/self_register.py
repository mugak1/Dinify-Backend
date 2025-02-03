"""
create a user on dinify
"""
from typing import Optional
from misc_app.controllers.check_required_information import check_required_information
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.required_information import REQUIRED_INFORMATION
from users_app.models import User
from users_app.controllers.otp_manager import OtpManager
from misc_app.controllers.notifications.notification import Notification


def self_register(
    data: dict,
    return_user_id: Optional[bool] = False,
    send_credentials: Optional[bool] = False,
    skip_otp: Optional[bool] = False,
) -> dict:
    """
    Handle user self registration
    - `data` is the registration data
    """
    # check that that the phone number is not repeated
    existing_phone = User.objects.filter(phone_number=data.get('phone_number'))
    if existing_phone.exists():
        return {
            'status': 400,
            'message': MESSAGES.get('PHONE_NUMBER_EXISTS'),
            'user_id': existing_phone.first().id,
            'user_profile': existing_phone.first()
        }

    # check that the email is not repeated
    if data.get('email') is not None:
        if User.objects.filter(email=data.get('email')).exists():
            return {
                'status': 400,
                'message': MESSAGES.get('EMAIL_EXISTS'),
            }

    # check if all the required information is present
    info_check = check_required_information(
        REQUIRED_INFORMATION.get('new_user'),
        data
    )
    if not info_check.get('status'):
        return {
            'status': 400,
            'message': info_check.get('message')
        }

    # verify otp
    # check if the otp has been provided
    if not skip_otp:
        otp = data.get('otp')
        if otp is None:
            return {
                'status': 400,
                'message': 'Please provide the OTP.'
            }

        # verify the otp
        verified_otp = OtpManager().verify_otp(
            msisdn=data.get('phone_number'),
            otp=otp
        )

        if not verified_otp['data']['valid']:
            return {
                'status': 400,
                'message': 'Invalid OTP.'
            }

    # create the user
    email = data.get('email')
    if email is not None:
        email = email.strip().lower()
    user = User.objects.create_user(
        first_name=data.get('first_name').strip().title(),
        last_name=data.get('last_name').strip().title(),
        email=email,
        phone_number=data.get('phone_number'),
        username=data.get('phone_number'),
        country=data.get('country').strip().upper(),
        password=data.get('password')
    )

    # TODO send welcome email
    Notification(msg_data={
        'msg_type': 'new-user',
        'first_name': data.get('first_name').strip().title(),
        'user_id': str(user.id)
    }).create_notification()

    # TODO send credentials email
    if send_credentials:
        Notification(msg_data={
            'msg_type': 'new-user-credentials',
            'first_name': data.get('first_name').strip().title(),
            'username': data.get('phone_number'),
            'password': data.get('password'),
            'user_id': str(user.id)
        }).create_notification()

    if return_user_id:
        return {
            'status': 200,
            'user_id': user.id,
            'user_profile': user
        }

    return {
        'status': 200,
        'message': MESSAGES.get('OK_SELF_REGISTER')
    }
