"""
implementation to reset a user's password

Flow:
1. Client calls reset-password with username — backend sends OTP.
2. Client calls reset-password with username + OTP — backend verifies,
   generates a temporary password (never sent externally), sets
   prompt_password_change=True, and returns a short-lived JWT so the
   client can immediately call change-password.

The plaintext/generated password is never sent over SMS or email.

D11 E-R1 — WHAT THE TWO STAGES SAY, AND WHAT THEY NO LONGER SAY:

* INITIATION ANSWERS ONE ENVELOPE FOR EVERY IDENTITY IT ACKNOWLEDGES. An eligible
  account whose challenge was issued, an unknown identifier, platform staff, an
  identity still pending its first owner claim, and an email several accounts share
  all receive the same 200 ``RESET_ACKNOWLEDGEMENT`` body, with no ``user_id``. It
  used to answer the first with ``data.user_id`` and the rest with a 400, which made
  the route an account-existence oracle for anyone holding a phone number or email.
  FAILURES ARE NOT ACKNOWLEDGED: when ``make_otp`` reports that it could not record
  or send the challenge, the existing 500 is returned unchanged, so a diner is never
  told to wait for a code that is not coming. That 500 is reachable only for an
  eligible identity — for as long as its issuance fails, which for an account with no
  destination the environment can send to is permanently — so failure and timing still
  disclose. The generic ``resend-otp`` route is unchanged and still answers an absent
  account differently. These are stated limits, not claims this module closes them.

* COMPLETION IS BOUND TO A RESET CHALLENGE. ``verify_otp`` is asked for
  ``purpose='reset-password'`` only, so a newer login or owner-claim challenge for the
  same account is never selected, charged or consumed here, and its code cannot be
  spent to reset a password. No origin is required: a reset challenge issued by
  initiation and one issued by the generic resend route both complete. Every way a
  completion can fail — no such identity, a refused one, an ambiguous email, a wrong
  code, or no live reset challenge — answers the same 400 ``Invalid OTP.``.
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


RESET_PURPOSE = 'reset-password'

RESET_ACKNOWLEDGEMENT = (
    'If these details match an eligible account, check its registered '
    'phone or email for a reset code.'
)

INVALID_RESET_CODE = 'Invalid OTP.'


def _acknowledgement():
    """THE one initiation answer for every identity this flow acknowledges."""
    return {'status': 200, 'message': RESET_ACKNOWLEDGEMENT}


def _invalid_code():
    """THE one completion refusal, whatever the reason."""
    return {'status': 400, 'message': INVALID_RESET_CODE}


def _make_random_password(length=20):
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def initiate_password_reset(username):
    """
    Step 1: issue an OTP for purpose='reset-password' to an ELIGIBLE identity.

    An identity ``_resolve_user`` does not resolve gets the same acknowledgement and
    nothing else: no challenge, no accounting row, no SMS or email. A ``make_otp``
    failure keeps its own 500 — it is never turned into an acknowledgement.
    """
    user = _resolve_user(username)
    if user is None:
        return _acknowledgement()

    otp_sent = OtpManager().make_otp(user=user, purpose=RESET_PURPOSE, origin='reset_initiation')
    if otp_sent:
        return _acknowledgement()

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
        # The legacy call without an OTP is an initiation, and is answered as one.
        return _acknowledgement() if otp is None else _invalid_code()

    if otp is None:
        return initiate_password_reset(username)

    # Verify against a RESET challenge only. Without the purpose, verify_otp takes
    # the newest live challenge of any purpose, so a login or owner-claim challenge
    # issued after the reset one would be charged, and its code would reset the
    # password. No origin is required: initiation and resend both issue reset codes.
    verified_otp = OtpManager().verify_otp(
        user_id=str(user.id), otp=otp, expected_purpose=RESET_PURPOSE)
    if not verified_otp['data']['valid']:
        return _invalid_code()

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

    Three kinds of identity resolve to ``None`` — the same result as "no such user",
    so nothing is disclosed either way. Initiation answers every one of them with the
    E-R1 acknowledgement and completion with ``Invalid OTP.``.

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

    AN EMAIL SEVERAL ACCOUNTS SHARE EXACTLY (D11 E-R1). ``get_user_by_email`` raises
    ``MultipleObjectsReturned`` for it, which reached the caller as a 500. Reset now
    treats it as ineligible: choosing one of the accounts would send a code to a
    phone the requester may not own, and merging them is not this flow's decision.
    The catch is HERE and nowhere else — the shared resolver and login keep their own
    behaviour — and each account still resets by its own phone number.

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
    except User.MultipleObjectsReturned:
        # This line is bounded and address-free: the identifier is personal data. (The
        # legacy completion route's existing save_action still records the username, as
        # it does for every unresolved identity; that contract is unchanged.)
        logger.info(
            'password reset: refused (identifier matches more than one account)')
        return None

    if user.account_type == ACCOUNT_TYPE_PLATFORM_STAFF:
        return None
    if customer_access.is_refused(user):
        logger.info(
            'password reset: refused (customer access not established)')
        return None
    return user
