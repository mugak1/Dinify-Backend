"""
implementation to handle user login i.e. authentication
"""
from typing import Optional
from django.utils import timezone
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from users_app.models import User
from users_app.serializers import SerGetUserProfile
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action
from users_app.controllers.otp_manager import OtpManager
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    is_dinify_superuser,
    get_any_restaurant_roles
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_FINANCE,
    RESTAURANT_MANAGER
)


def login(
    username: str,
    password: str,
    source: Optional[str] = 'restaurant'
) -> dict:
    """
    handle the login of a user
    """
    username = username.strip()
    password = password.strip()

    # in case one has supplied the email,
    # then get the corresponding username
    # to use for authentication
    consider_email = User.objects.filter(email=username.lower()).exists()
    if consider_email:
        username = User.objects.get(email=username).username

    # authenticate the user
    username = username.strip()
    auth_user = authenticate(
        username=username,
        password=password
    )

    if auth_user is None:
        # Single query to check why auth failed (replaces exists() + get())
        try:
            existing_user = User.objects.get(username=username)
        except User.DoesNotExist:
            save_action(
                affected_model='User',
                affected_record=None,
                action='login',
                narration=MESSAGES.get('NO_USERNAME'),
                result=ACTION_LOG_STATUSES.get('failed'),
                user_id=None,
                username=username,
                submitted_data={},
                changes=None,
                filter_information=None
            )
            return {
                'status': 401,
                'message': MESSAGES.get('NO_USERNAME')
            }

        if not existing_user.is_active:
            save_action(
                affected_model='User',
                affected_record=None,
                action='login',
                narration=MESSAGES.get('ACCOUNT_NOT_ACTIVE'),
                result=ACTION_LOG_STATUSES.get('failed'),
                user_id=None,
                username=username,
                submitted_data={},
                changes=None,
                filter_information=None
            )
            return {
                'status': 401,
                'message': MESSAGES.get('ACCOUNT_NOT_ACTIVE')
            }

        return {
            'status': 401,
            'message': MESSAGES.get('WRONG_PASSWORD')
        }

    # when the login is successful
    # Use update() to avoid triggering the post_save archive_user signal —
    # archiving the full user to MongoDB on every login is unnecessary overhead.
    User.objects.filter(username=username).update(last_login=timezone.now())
    auth_user = User.objects.get(username=username)
    token = RefreshToken.for_user(auth_user)

    # save action
    save_action(
        affected_model='User',
        affected_record=str(auth_user.id),
        action='login',
        narration=MESSAGES.get('OK_LOGIN'),
        result=ACTION_LOG_STATUSES.get('success'),
        user_id=None,
        username=username,
        submitted_data={},
        changes=None,
        filter_information=None
    )

    # TODO check if the user has roles that require otp login
    require_otp = False
    if is_dinify_admin(user=auth_user) or is_dinify_superuser(user=auth_user):
        require_otp = True
    restaurant_roles = get_any_restaurant_roles(user=auth_user)

    for restaurant_role in restaurant_roles:
        if any(role in [
            RESTAURANT_OWNER,
            RESTAURANT_FINANCE,
            RESTAURANT_MANAGER
        ] for role in restaurant_role['roles']):
            require_otp = True
            break

    # require_otp = True
    if require_otp and source != 'diner':
        data = {
            'require_otp': True,
            'prompt_password_change': auth_user.prompt_password_change,
            'user_id': str(auth_user.id),
            'profile': SerGetUserProfile(auth_user, context={'restaurant_roles': restaurant_roles}).data
        }

        # Always require OTP for privileged roles — never leak tokens
        # before OTP is verified.  When prompt_password_change is True
        # the frontend should complete OTP first, then call
        # change-password with the token returned by verify-otp.
        otp = OtpManager().make_otp(user=auth_user, purpose='login')

        if otp:
            return {
                'status': 200,
                'message': 'Please enter the OTP',
                'data': data
            }

    return {
        'status': 200,
        'message': MESSAGES.get('OK_LOGIN'),
        'data': {
            'require_otp': False,
            'prompt_password_change': auth_user.prompt_password_change,
            'token': str(token.access_token),
            'refresh': str(token),
            'profile': SerGetUserProfile(auth_user, context={'restaurant_roles': restaurant_roles}).data
        }
    }
