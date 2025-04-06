"""
implementation to reset a user's password
"""
from decouple import config
from users_app.models import User
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action
from misc_app.controllers.notifications.notification import Notification
from users_app.controllers.otp_manager import OtpManager


def reset_password(username, otp):
    """
    reset a user's password
    """
    # check if the user exists
    user = None
    try:
        if '@' in username:
            user = User.objects.get(email=username)
        else:
            user = User.objects.get(phone_number=username)
    except User.DoesNotExist:
        save_action(
            affected_model='User',
            affected_record=None,
            action='reset-password',
            narration=MESSAGES.get('NO_PHONE_NUMBER'),
            result=ACTION_LOG_STATUSES.get('failed'),
            user_id=None,
            username=username,
            submitted_data={'username': username},
            changes=None,
            filter_information=None
        )
        return {
            'status': 400,
            'message': MESSAGES.get('NO_PHONE_NUMBER')
        }

    if user is None:
        return {
            'status': 400,
            'message': 'No user found.'
        }

    # verify the otp
    verified_otp = OtpManager().verify_otp(user_id=str(user.id), otp=otp)
    if not verified_otp['data']['valid']:
        return {
            'status': 400,
            'message': 'Invalid OTP.'
        }

    password = User.objects.make_random_password()
    if config('ENV') in ['dev']:
        password = '1234'  # for testing purposes since emails are not sent out
    user.set_password(password)
    user.prompt_password_change = True
    user.save()

    # save the action performed
    save_action(
        affected_model='User',
        affected_record=str(user.id),
        action='reset-password',
        narration=MESSAGES.get('OK_PASSWORD_RESET'),
        result=ACTION_LOG_STATUSES.get('success'),
        user_id=None,
        username=username,
        submitted_data={'username': username},
        changes=None,
        filter_information=None
    )

    Notification(msg_data={
        'msg_type': 'forgot-password',
        'first_name': user.first_name,
        'password': password,
        'user_id': str(user.id)
    }).create_notification()

    return {
        'status': 200,
        'message': MESSAGES.get('OK_PASSWORD_RESET'),
    }
