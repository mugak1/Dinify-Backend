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
import logging
import secrets
import string
from users_app.models import User
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from misc_app.controllers.save_action_log import save_action
from users_app.controllers.email_lookup import get_user_by_email
from users_app.controllers.otp_manager import OtpManager
from users_app import customer_access

logger = logging.getLogger(__name__)


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
        'message': "We couldn't send your verification code. Please try again."
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
    #
    # Through the single sanctioned customer mint. Unreachable for a non-established
    # identity — `_resolve_user` refused it long before this line — so this is the
    # backstop, and the direction of its failure is deliberate: an ungated mint here
    # is exactly the defect Step 2D.1 exists to close, and it must never come back as
    # a quiet success.
    token = customer_access.issue_customer_tokens(user)

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
    """
    Resolve a user ELIGIBLE FOR GENERIC PASSWORD RESET, by email or phone number.

    Two kinds of account resolve to ``None`` — the same result as "no such user", so
    nothing is disclosed either way, and both refusals therefore reach the caller as
    the identical ``NO_PHONE_NUMBER`` 400.

    PLATFORM STAFF. This flow ends in a customer token mint and, before that,
    overwrites the account password; leaving it open would let anyone who knows an
    admin's email mint a customer session as them and lock them out of the admin
    plane. Admin credential recovery is the ``reset_platform_admin_totp`` management
    command, not this path.

    NOT-YET-CLAIMED IDENTITIES (Step 2D.1). This is the bypass Step 2D.1 closes, and
    it was the reason an unusable password was never a sufficient invariant: an owner
    provisioned by Admin has no password precisely so that only the invitation can
    establish one — but generic reset needed nothing except their phone number to
    install one and hand out a session. The platform would then hold ``owner_control:
    not_established`` and ``invitation: pending`` for an account already exercising
    owner authority.

    GUARDING THE RESOLVER CLOSES BOTH STAGES AT ONCE, which is why the check lives
    here rather than in ``initiate_password_reset``. A caller can invoke the
    completion route directly, and an OTP may already exist from before the state was
    written — neither matters if the identity cannot be resolved into this flow at
    all.

    AND RESET IS NOT CLAIM. This must never be "fixed" by consuming the
    ``OwnerInvitation`` from here: password reset never sees the claim credential, so
    it cannot know the right person is on the other end — which is the whole thing the
    invitation is for.
    """
    try:
        if '@' in username:
            user = get_user_by_email(username)
        else:
            user = User.objects.get(phone_number=username)
    except User.DoesNotExist:
        return None

    if user.account_type == ACCOUNT_TYPE_PLATFORM_STAFF:
        return None
    if customer_access.is_refused(user):
        logger.info(
            'password reset: refused (customer access not established)')
        return None
    return user
