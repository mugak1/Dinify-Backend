"""
implementation to handle user login i.e. authentication
"""
import logging
import time
from typing import Optional
from django.utils import timezone
from django.contrib.auth import authenticate
from users_app.models import User
from users_app.serializers import SerGetUserProfile
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action
from users_app.controllers.email_lookup import get_user_by_email
from users_app.controllers.otp_manager import OtpManager
from users_app.controllers.permissions_check import get_any_restaurant_roles
from users_app import customer_access
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    RESTAURANT_OWNER,
    RESTAURANT_FINANCE,
    RESTAURANT_MANAGER
)

logger = logging.getLogger(__name__)


def login(
    username: str,
    password: str,
    source: Optional[str] = 'restaurant'
) -> dict:
    """
    handle the login of a user
    """
    t_start = time.monotonic()
    username = username.strip()
    password = password.strip()

    # in case one has supplied the email,
    # then get the corresponding username
    # to use for authentication
    consider_email = User.objects.filter(email=username.lower()).exists()
    if consider_email:
        # Not `User.objects.get(email=username)`: that asks for the address as typed
        # while the check above asked for it lower-cased, so `Diner@Example.com`
        # passed the check and then raised DoesNotExist, a 500 from the endpoint.
        username = get_user_by_email(username).username

    t_lookup = time.monotonic()
    logger.info("login [%s]: email lookup %.3fs", username, t_lookup - t_start)

    # authenticate the user
    username = username.strip()
    auth_user = authenticate(
        username=username,
        password=password
    )

    t_auth = time.monotonic()
    logger.info("login [%s]: authenticate %.3fs", username, t_auth - t_lookup)

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
            logger.info("login [%s]: failed (no user) total %.3fs", username, time.monotonic() - t_start)
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
            logger.info("login [%s]: failed (inactive) total %.3fs", username, time.monotonic() - t_start)
            return {
                'status': 401,
                'message': MESSAGES.get('ACCOUNT_NOT_ACTIVE')
            }

        logger.info("login [%s]: failed (wrong password) total %.3fs", username, time.monotonic() - t_start)
        return {
            'status': 401,
            'message': MESSAGES.get('WRONG_PASSWORD')
        }

    # Platform staff authenticate on the admin origin only. Refused HERE — after
    # authenticate() so this is not an account-type oracle for an anonymous prober,
    # and before the customer token mint below, so no customer token is ever
    # minted for an admin account. Placing it above the `source`
    # branch also means the client-supplied source='diner' cannot route around it.
    if auth_user.account_type == ACCOUNT_TYPE_PLATFORM_STAFF:
        logger.info("login [%s]: refused (platform staff on customer origin)", username)
        return {
            'status': 401,
            'message': MESSAGES.get('WRONG_PASSWORD')
        }

    # An identity provisioned by Admin that has not completed its first owner claim
    # is not on this plane yet. Refused HERE, beside the account_type refusal and for
    # the same structural reason: this is after authenticate() has resolved the row
    # (so it is not an oracle for an anonymous prober) and BEFORE every customer-access
    # side effect below it — the last_login write, the token mint, the success action
    # log, the role traversal, and the OTP issuance that traversal can lead to.
    #
    # THE PASSWORD IS NOT THE GATE. Reaching this line at all means authenticate()
    # succeeded, so the account's password was usable — which for a pending identity
    # should be impossible and is exactly why the check does not read password state.
    # If some other path ever establishes a password before claim, this still refuses.
    #
    # The message is the generic wrong-password one, identical to what a bad password,
    # an unknown username and a platform-staff account already receive: login is
    # AllowAny, so a distinct response here would be a new account-state oracle.
    if customer_access.is_refused(auth_user):
        logger.info(
            "login [%s]: refused (customer access not established)", username)
        return {
            'status': 401,
            'message': MESSAGES.get('WRONG_PASSWORD')
        }

    # when the login is successful
    # Set last_login via update() to write a single column without a full model save.
    # Reuse the auth_user object from authenticate() — no need to re-fetch.
    login_time = timezone.now()
    User.objects.filter(username=username).update(last_login=login_time)
    auth_user.last_login = login_time
    # The single sanctioned customer mint (users_app.customer_access). The gate above
    # has already refused a non-established identity; this is the backstop that makes
    # forgetting such a gate a loud failure rather than a quiet token.
    token = customer_access.issue_customer_tokens(auth_user)

    t_token = time.monotonic()
    logger.info("login [%s]: update + token %.3fs", username, t_token - t_auth)

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

    # OTP escalation is driven purely by RESTAURANT roles now. The
    # `is_dinify_admin(...) or is_dinify_superuser(...)` clause that used to sit
    # here read User.roles for platform authority; a restaurant_user can no
    # longer hold a platform role (enforced at every write path), and platform
    # staff are refused above, so the branch had no reachable holder.
    require_otp = False
    restaurant_roles = get_any_restaurant_roles(user=auth_user)

    for restaurant_role in restaurant_roles:
        if any(role in [
            RESTAURANT_OWNER,
            RESTAURANT_FINANCE,
            RESTAURANT_MANAGER
        ] for role in restaurant_role['roles']):
            require_otp = True
            break

    t_roles = time.monotonic()
    logger.info("login [%s]: roles + permissions %.3fs", username, t_roles - t_token)

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

        logger.info("login [%s]: otp created, total %.3fs", username, time.monotonic() - t_start)

        if otp:
            return {
                'status': 200,
                'message': 'Please enter the OTP',
                'data': data
            }

        # Fail CLOSED. Without this explicit return, a make_otp failure would
        # fall through to the token branch below and hand a privileged user a
        # session with NO OTP — the exact opposite of the gate above.
        logger.error("login [%s]: OTP delivery failed — refusing token", username)
        return {
            'status': 500,
            'message': "We couldn't send your verification code. Please try again."
        }

    logger.info("login [%s]: complete (no otp), total %.3fs", username, time.monotonic() - t_start)
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
