"""
implementation to handle user login i.e. authentication
"""
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


def login(username: str, password: str) -> dict:
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
        # check if the username exists
        if not User.objects.filter(username=username).exists():
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
        # check if the user account is not active
        if not User.objects.get(username=username).is_active:
            # save to indicate that the user account is not active
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
        # TODO save to the action log indicating that the user failed to login

        return {
            'status': 401,
            'message': MESSAGES.get('WRONG_PASSWORD')
        }

    # when the login is successful
    auth_user = User.objects.get(username=username)
    auth_user.last_login = timezone.now()
    auth_user.save()
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
    if require_otp:
        otp = OtpManager().make_otp(user=auth_user, purpose='login')
        if otp:
            return {
                'status': 200,
                'message': 'Please enter the OTP',
                'data': {
                    'profile': SerGetUserProfile(auth_user).data
                }
            }

    return {
        'status': 200,
        'message': MESSAGES.get('OK_LOGIN'),
        'data': {
            'token': str(token.access_token),
            'refresh': str(token),
            'profile': SerGetUserProfile(auth_user).data
        }
    }
