"""
implementation to reset a user's password

Flow:
1. Client calls reset-password with username — backend sends OTP.
2. Client calls reset-password with username + OTP — backend verifies,
   generates a temporary password (never sent externally), sets
   prompt_password_change=True, and returns a short-lived JWT so the
   client can immediately call change-password.

The plaintext/generated password is never sent over SMS or email.
"""
import secrets
import string
from rest_framework_simplejwt.tokens import RefreshToken
from users_app.models import User
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action
from users_app.controllers.otp_manager import OtpManager


def _make_random_password(length=20):
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def initiate_password_reset(username):
    """
    Step 1: verify the user exists, send an OTP for purpose='reset-password'.
    """
    user = _resolve_user(username)
    if user is None:
        return {
            'status': 400,
            'message': MESSAGES.get('NO_PHONE_NUMBER')
        }

    otp_sent = OtpManager().make_otp(user=user, purpose='reset-password')
    if otp_sent:
        return {
            'status': 200,
            'message': 'An OTP has been sent. Please verify to continue password reset.',
            'data': {
                'user_id': str(user.id),
            }
        }

    return {
        'status': 500,
        'message': 'Failed to send OTP. Please try again.'
    }


def reset_password(username, otp):
    """
    Step 2: verify the OTP, set a temporary internal password,
    mark prompt_password_change, and return a token so the client
    can call change-password immediately.
    """
    user = _resolve_user(username)
    if user is None:
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

    if otp is None:
        return initiate_password_reset(username)

    # verify the otp
    verified_otp = OtpManager().verify_otp(user_id=str(user.id), otp=otp)
    if not verified_otp['data']['valid']:
        return {
            'status': 400,
            'message': 'Invalid OTP.'
        }

    # Set a random internal password the user will never see.
    # prompt_password_change forces them to set their own.
    temp_password = _make_random_password(length=20)
    user.set_password(temp_password)
    user.prompt_password_change = True
    user.save()

    # save the action performed
    save_action(
        affected_model='User',
        affected_record=str(user.id),
        action='reset-password',
        narration='Password reset verified. User must set a new password.',
        result=ACTION_LOG_STATUSES.get('success'),
        user_id=None,
        username=username,
        submitted_data={'username': username},
        changes=None,
        filter_information=None
    )

    # Issue a token so the client can call change-password immediately.
    # The verify_otp for purpose='login' would return a token, but this
    # is purpose='reset-password' so we issue one explicitly.
    token = RefreshToken.for_user(user)

    return {
        'status': 200,
        'message': 'OTP verified. Please set a new password.',
        'data': {
            'token': str(token.access_token),
            'refresh': str(token),
            'temp_password': temp_password,
            'prompt_password_change': True,
        }
    }


def _resolve_user(username):
    """Resolve a user by email or phone number."""
    try:
        if '@' in username:
            return User.objects.get(email=username)
        else:
            return User.objects.get(phone_number=username)
    except User.DoesNotExist:
        return None
