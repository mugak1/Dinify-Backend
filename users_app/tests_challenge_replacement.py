"""
D11 E-R2 — purpose-safe challenge replacement: P1 + P2 + P3, delivered together.

WHAT WAS WRONG
━━━━━━━━━━━━━━

Three independent mechanisms let one OTP flow interfere with another for the same
account. Each was reproduced through the real routes before anything changed.

1. REPLACEMENT WAS PURPOSE-BLIND (P1). ``make_otp`` deleted every earlier challenge
   in the ``(user, msisdn)`` bucket before inserting the new one, whatever it was
   issued for. A password login and an anonymous reset initiation share the
   ``msisdn IS NULL`` bucket, so either one silently destroyed the other. Every
   account-resolving resend shares the phone bucket with the owner-claim challenge,
   so a reset or null-purpose resend destroyed a live claim challenge. The owner's
   CORRECT claim code was then refused and charged against the invitation's attempt
   budget.
2. GENERIC VERIFICATION WAS UNBOUND (P2). ``verify-otp`` (the login route) selected
   the newest live challenge of ANY purpose for the named user. A newer reset or claim
   row therefore hid the login code, a wrong guess charged that row's attempts, and a
   correct reset or claim code was consumed there, so it could no longer complete its
   own flow.
3. THE RESEND PURPOSE WAS CLIENT-CHOSEN AND UNBOUNDED (P3). Any string, including
   ``owner-claim``, was accepted, and each new value occupied its own row. A
   same-purpose ``owner-claim`` resend replaced the dedicated claim challenge, so P1
   cannot reach it.

THE CONTRACT
━━━━━━━━━━━━

* P1 — ``make_otp`` replaces only within ``(user, msisdn, purpose)``. The
  transaction is unchanged: the user row is taken FOR KEY SHARE first, the challenge
  and its ledger row are written, and everything commits before any sender runs.
* P2 — the ``verify-otp`` endpoint passes ``expected_purpose='login'``. The binding
  NARROWS the locked query, so a non-login row is never selected, charged or
  consumed there. A non-login code is now refused on that route, which is a
  client-visible change. The primitive's default and every other reader are
  untouched.
* P3 — the generic resend accepts only the exact values ``'login'``,
  ``'reset-password'``, ``'register'`` and ``None``, including an omitted purpose. Every
  other value gets ``400 {"status": 400, "message": "Invalid purpose"}`` BEFORE any
  account is resolved. Nothing is issued, deleted, recorded or sent, whatever the
  identifier names. The dedicated claim challenge stays the only supported channel
  for ``owner-claim``.

HOW THESE TESTS DISCRIMINATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Under ``ENV=dev`` every code is ``1234``, so digits cannot tell two challenges apart.
The cross-purpose cases therefore run under ``ENV=test`` with fixed, DISTINCT codes.
The DEV cases keep ``1234`` and assert WHICH ROW a route selected and consumed; they
never infer a purpose from the digits. Every success is checked as a principal, not
as a status code. The access and refresh tokens are decoded and must name the
intended account, and the access token must open that account's own profile through
the real customer authentication stack. Only delivery is replaced (SMS, e-mail,
notification, and the dispatch thread run inline). Selection, locking, accounting
and the consumers are the real code.

WHAT THIS DOES NOT PIN, ON PURPOSE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Several things are recorded residuals rather than guarantees, and no test here
enshrines them:

* whether a completed reset should invalidate a login challenge issued before it;
* registration's purpose-unbound msisdn verification;
* two concurrent same-purpose issuances leaving two live rows;
* requester-bound resend and discovery;
* numeric abuse limits.

The coexistence cases below verify the login BEFORE completing the reset for the same
reason: the reverse order would pin a coordinated-invalidation question this change
does not decide.
"""
import contextlib
import os
import threading
import time
from unittest import mock

import psycopg
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.db.models.signals import post_save
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from platform_admin_app import onboarding_creation
from platform_admin_app.models import OwnerInvitation
from platform_admin_app.onboarding_creation import ExistingOwner, NewOwner
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers import otp_manager
from users_app.controllers.otp_manager import OTP_MAX_ATTEMPTS, OtpManager
from users_app.models import OtpIssuance, OtpVerificationFailure, User, UserOtp


LOGIN = '/api/v1/users/auth/login/'
VERIFY = '/api/v1/users/auth/verify-otp/'
RESEND = '/api/v1/users/auth/resend-otp/'
INITIATE = '/api/v1/users/auth/initiate-reset-password/'
COMPLETE = '/api/v1/users/auth/reset-password/'
CHANGE = '/api/v1/users/auth/change-password/'
REGISTER = '/api/v1/users/auth/register/'
PROFILE = '/api/v1/users/user-profile/'
CLAIM_CHALLENGE = '/api/v1/users/owner-claim/challenge/'
CLAIM_REDEEM = '/api/v1/users/owner-claim/redeem/'
CLAIM_HEADER = 'X-Owner-Claim-Token'

PASSWORD = 'Er2-Synthetic-Passw0rd!'
CLAIM_PASSWORD = 'Er2-Claim-Synthetic-Passw0rd!'
NEW_PASSWORD = 'Er2-Brand-New-Passw0rd!'

DEV_OTP = '1234'
WRONG = '3999'   # below every fixed code drawn here, so it never matches by accident

INVALID_PURPOSE = {'status': 400, 'message': 'Invalid purpose'}
INVALID_OTP = {'status': 200, 'message': 'Invalid OTP', 'data': {'valid': False}}
RESET_INVALID = {'status': 400, 'message': 'Invalid OTP.'}
CLAIM_REFUSAL = {
    'status': 400,
    'message': 'This owner claim or verification code is invalid or no longer available.',
}
LOGIN_RESEND_REFUSAL = 'Please provide your username and password again to get a login OTP'

WAIT = 30   # seconds; the bound on every barrier, join and lock-wait observation

# A synthetic phone range distinct from every other suite (never dialled: the SMS
# sender is a mock for every test in this module).
_PHONES = iter(f'2567724{n:05d}' for n in range(20000, 29999))


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _phone():
    return next(_PHONES)


def _account(tag, phone=None, **extra):
    phone = phone or _phone()
    return User.objects.create_user(
        first_name='Er', last_name=tag.title(), email=f'er2-{tag}-{phone}@example.test',
        phone_number=phone, username=phone, country='UG', password=PASSWORD, roles=[],
        **extra,
    )


def _owner(tag):
    """An owner of a live restaurant: password login requires, and writes, a login OTP."""
    user = _account(tag)
    restaurant = Restaurant.objects.create(
        name=f'Er2 House {user.phone_number}', location='Synthetic',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
    )
    return user


def _staff(tag='ops'):
    return User.objects.create_user(
        username=f'er2-{tag}-{_phone()}', email=f'er2-{tag}-{_phone()}@example.test',
        password=PASSWORD, account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


def _claim(pending):
    """
    An admin-created restaurant and its owner invitation, through the real Step-2D
    domain service. ``pending`` chooses a brand-new owner (``pending_initial_claim``,
    no password) or an existing, established restaurant user.
    """
    owner = (
        NewOwner('Er', 'Pending', _phone(), None) if pending
        else ExistingOwner(str(_account('established').pk))
    )
    creation = onboarding_creation.create_admin_restaurant(
        name=f'Er2 Claim {_phone()}', location='Synthetic', is_test=True,
        owner=owner, actor=_staff(), reason='D11 E-R2 Stage B synthetic fixture.',
    )
    return creation.owner, creation.claim_token, creation.invitation.pk


class _Codes:
    """
    Stands in for ``secrets`` inside ``otp_manager``, so that each challenge draws the
    next code from a fixed list. Only the digit draw is replaced; the per-row salt still
    comes from the real ``secrets``. Thread-safe, because the race schedules draw codes
    from worker threads.
    """

    def __init__(self, *codes):
        self._codes = iter(codes)
        self._lock = threading.Lock()

    def randbelow(self, n):
        with self._lock:
            return int(next(self._codes)) - 1000

    @staticmethod
    def token_hex(nbytes=None):
        import secrets
        return secrets.token_hex(nbytes)


class _InlineThread:
    """``otp_manager``'s dispatch thread, run inline so delivery stays inside the patches."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


@contextlib.contextmanager
def _delivery_intercepted():
    """
    Replace every channel a challenge can leave the process by. Returns the SMS and
    e-mail mocks so a test can count what was attempted, and whether any transaction
    was open when it was.
    """
    sms = mock.Mock(return_value=True)
    email = mock.Mock(return_value=True)
    with mock.patch.object(otp_manager, 'send_sms', sms), \
            mock.patch('notifications_app.controllers.messenger.Messenger.send_email', email), \
            mock.patch('misc_app.controllers.notifications.notification.Notification'
                       '.create_notification', return_value=None), \
            mock.patch.object(otp_manager, 'threading',
                              mock.Mock(Thread=_InlineThread)):
        yield sms, email


@contextlib.contextmanager
def _test_env(*codes):
    """``ENV=test`` (real random-code path, synchronous mocked SMS) with fixed codes."""
    with mock.patch.dict(os.environ, {'ENV': 'test'}), \
            mock.patch.object(otp_manager, 'secrets', _Codes(*codes)):
        yield


class _Er2Case(TestCase):
    """Shared request, row and principal helpers for the route-level cases."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        self._delivery = _delivery_intercepted()
        self.sms, self.email = self._delivery.__enter__()
        self.addCleanup(self._delivery.__exit__, None, None, None)

    # --- requests ---

    def post(self, url, body, headers=None):
        # Every OTP, reset and claim route is throttled per client or per identity;
        # throttling is not what these tests are about.
        cache.clear()
        return self.client.post(url, body, format='json', headers=headers or {})

    def issued(self, url, body, headers=None):
        """POST, and return the response with the challenge rows it created."""
        before = set(UserOtp.objects.values_list('pk', flat=True))
        response = self.post(url, body, headers)
        new = list(UserOtp.objects.exclude(pk__in=before).values_list('pk', flat=True))
        return response, new

    def login(self, user):
        response, new = self.issued(LOGIN, {'username': user.phone_number, 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['require_otp'], response.content)
        self.assertEqual(len(new), 1, 'a password login writes exactly one login challenge')
        self.assertEqual(self.row(new[0])['purpose'], 'login')
        return new[0]

    def initiate(self, user):
        response, new = self.issued(INITIATE, {'identifier': user.phone_number,
                                               'identification': 'phone'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(new), 1, 'an eligible initiation writes one reset challenge')
        self.assertEqual(self.row(new[0])['purpose'], 'reset-password')
        return new[0]

    def resend(self, identification, identifier, purpose, expect=200):
        response, new = self.issued(RESEND, {'identification': identification,
                                             'identifier': identifier, 'purpose': purpose})
        self.assertEqual(response.status_code, expect, response.content)
        return new[0] if new else None

    def challenge(self, token):
        response, new = self.issued(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(new), 1)
        self.assertEqual(self.row(new[0])['purpose'], 'owner-claim')
        return new[0]

    def verify(self, user, otp):
        return self.post(VERIFY, {'user': str(user.pk), 'otp': otp})

    def complete_reset(self, user, otp):
        return self.post(COMPLETE, {'identifier': user.phone_number, 'otp': otp})

    def redeem(self, token, otp, new_password=None):
        body = {'otp': otp}
        if new_password is not None:
            body['new_password'] = new_password
        return self.post(CLAIM_REDEEM, body, headers={CLAIM_HEADER: token})

    # --- state ---

    @staticmethod
    def row(pk):
        r = UserOtp.objects.filter(pk=pk).first()
        if r is None:
            return None
        return {'purpose': r.purpose, 'msisdn': r.msisdn, 'attempts': r.attempts,
                'consumed': r.consumed_at is not None,
                'live': r.consumed_at is None and r.expiry_time >= timezone.now()}

    def assertUntouched(self, pk, msg=None):
        """Present, live, never charged and with no recorded failure."""
        state = self.row(pk)
        self.assertIsNotNone(state, msg or 'the challenge was deleted')
        self.assertEqual((state['attempts'], state['consumed'], state['live']), (0, False, True),
                         msg or state)
        self.assertEqual(OtpVerificationFailure.objects.filter(issuance_id=pk).count(), 0, msg)

    @staticmethod
    def invitation(pk):
        inv = OwnerInvitation.objects.get(pk=pk)
        return {'claim_failed_attempts': inv.claim_failed_attempts,
                'consumed': inv.consumed_at is not None}

    # --- principals ---

    def assertCustomerTokens(self, data, user):
        """
        The answer carries a session for THIS account, and nobody else's. Token-key
        presence is not enough: the access and refresh tokens are decoded, and the access
        token has to open the account's own profile through the real customer stack.
        """
        self.assertEqual(str(AccessToken(data['token'])['user_id']), str(user.pk))
        self.assertEqual(str(RefreshToken(data['refresh'])['user_id']), str(user.pk))
        cache.clear()
        profile = self.client.get(PROFILE, headers={'Authorization': f"Bearer {data['token']}"})
        self.assertEqual(profile.status_code, 200, profile.content)
        self.assertEqual(str(profile.json()['data']['profile']['id']), str(user.pk))

    def assertLoggedIn(self, response, user):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        # A valid answer WITHOUT a session is what the login route gives for a spent
        # non-login row, so the shape is asserted before the tokens are read.
        self.assertEqual(set(data), {'valid', 'token', 'refresh'},
                         f'the login route did not mint a session: {data}')
        self.assertIs(data['valid'], True, data)
        self.assertCustomerTokens(data, user)

    def assertNotLoggedIn(self, response):
        """The generic verifier's one refusal: the shared ``invalid`` answer, no session."""
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), INVALID_OTP)

    def assertResetCompleted(self, response, user):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(set(data), {'token', 'refresh', 'temp_password', 'prompt_password_change'})
        self.assertCustomerTokens(data, user)
        fresh = User.objects.get(pk=user.pk)
        self.assertTrue(fresh.check_password(data['temp_password']))
        self.assertTrue(fresh.prompt_password_change)
        return data

    def assertClaimRedeemed(self, response, owner, invitation_pk):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertCustomerTokens(data, owner)
        self.assertEqual(self.invitation(invitation_pk),
                         {'claim_failed_attempts': 0, 'consumed': True})
        self.assertEqual(User.objects.get(pk=owner.pk).customer_access_state,
                         CUSTOMER_ACCESS_ESTABLISHED)


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — login and reset initiation share the NULL bucket (both orders)
# ═══════════════════════════════════════════════════════════════════════════════

L_CODE, R_CODE, C_CODE, X_CODE = '4101', '4202', '4303', '4404'


class LoginAndResetInitiationCoexistTests(_Er2Case):
    """
    S1 and S2. A password login and an anonymous reset initiation both store
    ``msisdn IS NULL``. Before P1 each one deleted the other's live challenge: an
    anonymous reset request interrupted a login in progress, and a login interrupted a
    reset. This is DISTINCT from a reset-purpose token being accepted as a login token
    (P2, below) and is not conflated with it.
    """

    def test_S1_a_reset_initiation_does_not_interrupt_a_login(self):
        with _test_env(L_CODE, R_CODE):
            user = _owner('s1')
            login_pk = self.login(user)
            reset_pk = self.initiate(user)                 # anonymous, NULL bucket

            self.assertUntouched(login_pk, 'P1: the reset initiation deleted the login challenge')
            self.assertLoggedIn(self.verify(user, L_CODE), user)
            self.assertTrue(self.row(login_pk)['consumed'])
            self.assertUntouched(reset_pk, 'the login verification spent the reset challenge')
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S1_the_login_resend_anchor_survives_a_reset_initiation(self):
        """
        D11 B1 admits a login resend only within five minutes of the password-created
        login row. Before P1 an anonymous reset initiation deleted that row, so the
        user's "send the code again" was refused as though they had never entered their
        password. The resent code completes the login.
        """
        with _test_env(L_CODE, R_CODE, X_CODE):
            user = _owner('s1-anchor')
            self.login(user)
            reset_pk = self.initiate(user)

            resent_pk = self.resend('id', str(user.pk), 'login')   # anchor still there
            self.assertLoggedIn(self.verify(user, X_CODE), user)
            self.assertTrue(self.row(resent_pk)['consumed'])
            self.assertUntouched(reset_pk, 'the login verification spent the reset challenge')
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S2_a_login_does_not_interrupt_a_reset(self):
        with _test_env(R_CODE, L_CODE):
            user = _owner('s2')
            reset_pk = self.initiate(user)
            login_pk = self.login(user)

            self.assertUntouched(reset_pk, 'P1: the password login deleted the reset challenge')
            self.assertUntouched(login_pk)
            self.assertLoggedIn(self.verify(user, L_CODE), user)
            self.assertUntouched(reset_pk)
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S1_dev_the_login_route_consumes_the_login_row_and_mints_for_its_owner(self):
        """ENV=dev, identical digits: the observable is WHICH ROW is selected and spent."""
        user = _owner('s1dev')
        login_pk = self.login(user)
        reset_pk = self.initiate(user)
        self.assertUntouched(login_pk, 'P1: the reset initiation deleted the login challenge')

        self.assertLoggedIn(self.verify(user, DEV_OTP), user)
        self.assertTrue(self.row(login_pk)['consumed'], 'the login route spent another row')
        self.assertUntouched(reset_pk, 'the login route spent the reset challenge')
        self.assertResetCompleted(self.complete_reset(user, DEV_OTP), user)
        self.assertTrue(self.row(reset_pk)['consumed'])


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — owner-claim and account-resolving resends share the phone bucket
# ═══════════════════════════════════════════════════════════════════════════════

class ClaimAndResendCoexistTests(_Er2Case):
    """
    S3, S4 and S6. The claim challenge stores its delivery destination, so it shares
    the phone bucket with every account-resolving resend. Before P1 a resend REPLACED
    the claim challenge. The owner's correct code was then refused and charged against
    the INVITATION's attempt budget. That budget, unlike a challenge's own counter,
    survives re-issuing the challenge.
    """

    def test_S3_a_reset_resend_does_not_cost_an_established_owner_their_claim(self):
        with _test_env(C_CODE, R_CODE):
            owner, token, invitation = _claim(pending=False)
            claim_pk = self.challenge(token)
            reset_pk = self.resend('phone', owner.phone_number, 'reset-password')
            self.assertEqual(self.row(reset_pk)['msisdn'], owner.phone_number)  # same bucket

            self.assertUntouched(claim_pk, 'P1: the reset resend deleted the claim challenge')
            self.assertClaimRedeemed(self.redeem(token, C_CODE), owner, invitation)
            self.assertTrue(self.row(claim_pk)['consumed'])
            self.assertUntouched(reset_pk)
            self.assertResetCompleted(self.complete_reset(owner, R_CODE), owner)

    def test_S4_a_claim_challenge_does_not_cost_a_pending_reset_resend(self):
        with _test_env(R_CODE, C_CODE):
            owner, token, invitation = _claim(pending=False)
            reset_pk = self.resend('phone', owner.phone_number, 'reset-password')
            claim_pk = self.challenge(token)

            self.assertUntouched(reset_pk, 'P1: the claim challenge deleted the reset resend')
            self.assertResetCompleted(self.complete_reset(owner, R_CODE), owner)
            self.assertUntouched(claim_pk)
            self.assertClaimRedeemed(self.redeem(token, C_CODE), owner, invitation)

    def test_S6_a_null_purpose_resend_does_not_cost_a_pending_owner_their_claim(self):
        """
        Request shape: the register screen's ``{identification: 'msisdn', purpose: null}``.
        For a number that already has an account it resolves to that account, and a null
        purpose is outside the customer-auth issuance gate, so it reaches a pending owner.
        """
        with _test_env(C_CODE, X_CODE):
            owner, token, invitation = _claim(pending=True)
            claim_pk = self.challenge(token)
            null_pk = self.resend('msisdn', owner.phone_number, None)
            self.assertIsNone(self.row(null_pk)['purpose'])

            self.assertUntouched(claim_pk, 'P1: the null-purpose resend deleted the claim challenge')
            self.assertClaimRedeemed(self.redeem(token, C_CODE, CLAIM_PASSWORD), owner, invitation)
            self.assertTrue(User.objects.get(pk=owner.pk).check_password(CLAIM_PASSWORD))

    def test_the_claim_still_binds_purpose_and_destination(self):
        """
        CONTROL. Redemption selects only an ``owner-claim`` row sent to the CURRENT
        canonical phone. A coexisting login code is refused there, charges the invitation
        as an ordinary wrong guess, and leaves the login row alone.
        """
        with _test_env(C_CODE, L_CODE):
            owner, token, invitation = _claim(pending=False)
            claim_pk = self.challenge(token)
            # A login challenge written directly, in the NULL bucket beside the claim row.
            self.assertTrue(OtpManager().make_otp(user=owner, purpose='login'))
            login_pk = UserOtp.objects.get(user=owner, purpose='login').pk

            response = self.redeem(token, L_CODE)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json(), CLAIM_REFUSAL)
            self.assertEqual(self.invitation(invitation),
                             {'claim_failed_attempts': 1, 'consumed': False})
            self.assertEqual(self.row(claim_pk)['attempts'], 1)
            self.assertUntouched(login_pk)

            response = self.redeem(token, C_CODE)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertCustomerTokens(response.json()['data'], owner)
            self.assertEqual(self.invitation(invitation),
                             {'claim_failed_attempts': 1, 'consumed': True})
            self.assertUntouched(login_pk)


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — the generic verifier selects login challenges only
# ═══════════════════════════════════════════════════════════════════════════════

class GenericVerificationIsLoginOnlyTests(_Er2Case):
    """
    S7, S8, S10 and S11, plus the null-purpose case. Before P2 ``verify-otp`` took the
    newest live row of ANY purpose for the named user. Where two rows coexist in
    DIFFERENT buckets, which happened before P1 too, a newer non-login row hid the
    login code. A wrong guess was charged to that row. A correct reset or claim code
    was CONSUMED by the login route, where it mints nothing, so its own flow then
    failed.
    """

    def test_S7_a_newer_reset_resend_does_not_shadow_the_login(self):
        with _test_env(L_CODE, R_CODE):
            user = _owner('s7')
            login_pk = self.login(user)
            reset_pk = self.resend('phone', user.phone_number, 'reset-password')
            self.assertUntouched(login_pk)                   # different buckets

            self.assertLoggedIn(self.verify(user, L_CODE), user)
            self.assertUntouched(reset_pk, 'P2: the login attempt was charged to the reset row')
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S8_wrong_login_guesses_cannot_burn_a_claim_challenge(self):
        with _test_env(C_CODE):
            owner, token, invitation = _claim(pending=True)
            claim_pk = self.challenge(token)
            for _ in range(OTP_MAX_ATTEMPTS):
                self.assertNotLoggedIn(self.verify(owner, WRONG))

            self.assertUntouched(claim_pk, 'P2: verify-otp charged the claim challenge')
            self.assertFalse(OtpVerificationFailure.objects.exists())
            self.assertClaimRedeemed(self.redeem(token, C_CODE, CLAIM_PASSWORD), owner, invitation)

    def test_a_correct_claim_code_cannot_be_spent_at_the_login_route(self):
        with _test_env(C_CODE):
            owner, token, invitation = _claim(pending=False)
            claim_pk = self.challenge(token)
            self.assertNotLoggedIn(self.verify(owner, C_CODE))
            self.assertUntouched(claim_pk, 'P2: the login route consumed the claim challenge')
            self.assertClaimRedeemed(self.redeem(token, C_CODE), owner, invitation)

    def test_S10_a_reset_code_is_refused_at_the_login_route_and_still_resets(self):
        with _test_env(L_CODE, R_CODE):
            user = _owner('s10')
            login_pk = self.login(user)
            reset_pk = self.resend('phone', user.phone_number, 'reset-password')

            self.assertNotLoggedIn(self.verify(user, R_CODE))
            self.assertUntouched(reset_pk, 'P2: the login route spent or charged the reset row')
            # The submission was compared against the LOGIN challenge, as a wrong code.
            self.assertEqual(self.row(login_pk)['attempts'], 1)
            self.assertFalse(self.row(login_pk)['consumed'])
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S10_dev_the_login_route_selects_the_login_row_not_the_reset_row(self):
        """
        ENV=dev. The submitted digits are identical for both rows, so a valid answer here
        is CORRECT. What P2 decides is which row answers: the login row is consumed and
        mints for its owner, and the reset row is left to complete its own flow.
        """
        user = _owner('s10dev')
        login_pk = self.login(user)
        reset_pk = self.resend('phone', user.phone_number, 'reset-password')

        self.assertLoggedIn(self.verify(user, DEV_OTP), user)
        self.assertTrue(self.row(login_pk)['consumed'], 'P2: the login row was not the one selected')
        self.assertUntouched(reset_pk, 'P2: the login route consumed the reset row')
        self.assertResetCompleted(self.complete_reset(user, DEV_OTP), user)

    def test_only_a_reset_challenge_exists_and_the_login_route_cannot_spend_it(self):
        with _test_env(R_CODE):
            user = _owner('only-reset')
            reset_pk = self.initiate(user)
            self.assertNotLoggedIn(self.verify(user, R_CODE))
            self.assertUntouched(reset_pk, 'P2: the login route consumed the reset challenge')
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_S11_a_reset_resend_does_not_hide_a_resent_login_code(self):
        """Both resends store the phone: before P1 the second deleted the first."""
        with _test_env(L_CODE, X_CODE, R_CODE):
            user = _owner('s11')
            self.login(user)
            resent_pk = self.resend('id', str(user.pk), 'login')
            reset_pk = self.resend('phone', user.phone_number, 'reset-password')

            self.assertUntouched(resent_pk, 'P1: the reset resend deleted the resent login code')
            self.assertLoggedIn(self.verify(user, X_CODE), user)
            self.assertTrue(self.row(resent_pk)['consumed'])
            self.assertUntouched(reset_pk)
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)

    def test_a_null_purpose_row_is_never_selected_by_the_login_route(self):
        with _test_env(X_CODE):
            user = _account('null-row')
            null_pk = self.resend('msisdn', user.phone_number, None)   # resolves to the account
            self.assertNotLoggedIn(self.verify(user, WRONG))
            self.assertNotLoggedIn(self.verify(user, X_CODE))
            self.assertUntouched(null_pk, 'P2: the login route charged or spent a null-purpose row')

    def test_the_dev_login_route_ignores_a_claim_row_and_mints_only_from_login(self):
        """
        ENV=dev. A pending owner has no login challenge at all, so ``1234`` at the login
        route selects nothing. Before P2 it consumed the claim challenge.
        """
        owner, token, invitation = _claim(pending=True)
        claim_pk = self.challenge(token)
        self.assertNotLoggedIn(self.verify(owner, DEV_OTP))
        self.assertUntouched(claim_pk, 'P2: the dev code consumed the claim challenge')
        self.assertClaimRedeemed(self.redeem(token, DEV_OTP, CLAIM_PASSWORD), owner, invitation)


# ═══════════════════════════════════════════════════════════════════════════════
# P3 — the generic resend accepts four exact purpose values
# ═══════════════════════════════════════════════════════════════════════════════

REFUSED_PURPOSES = {
    'owner-claim': 'owner-claim',
    'first-time-payment': 'first-time-payment',
    'unknown word': 'not-a-purpose',
    'empty string': '',
    'upper case': 'LOGIN',
    'mixed case': 'Reset-Password',
    'leading space': ' login',
    'trailing space': 'register ',
    'underscore variant': 'reset_password',
    'JSON array': ['login'],
    'empty JSON array': [],
    'JSON object': {'purpose': 'login'},
    'empty JSON object': {},
    'JSON true': True,
    'JSON false': False,
    'JSON zero': 0,
    'JSON integer': 1,
    'JSON float': 1.5,
}


class ResendPurposeGuardTests(_Er2Case):
    """
    The refusal is a fact about the REQUEST, decided before any account is resolved.
    So it is byte-identical whether or not the identifier names an account, and nothing
    is issued, deleted, recorded or sent. The guard is a tuple membership test and so is
    total over JSON values. An unhashable array or object cannot raise inside it.
    """

    def setUp(self):
        super().setUp()
        self.user = _owner('guard')
        self.login(self.user)        # a live challenge that a refusal must not touch
        self.unknown_phone = _phone()

    def identifiers(self):
        """Every identification mode, for an account that exists and for one that does not."""
        import uuid
        return {
            'known by phone': ('phone', self.user.phone_number),
            'unknown phone': ('phone', self.unknown_phone),
            'known by id': ('id', str(self.user.pk)),
            'unknown id': ('id', str(uuid.uuid4())),
            'known by email': ('email', self.user.email),
            'unknown email': ('email', 'nobody-er2@example.test'),
            'known msisdn': ('msisdn', self.user.phone_number),
            'unknown msisdn': ('msisdn', self.unknown_phone),
        }

    def state(self):
        return (
            sorted(UserOtp.objects.values_list('pk', 'attempts', 'consumed_at')),
            OtpIssuance.objects.count(),
            self.sms.call_count + self.email.call_count,
        )

    def test_every_refused_value_gets_one_answer_for_every_identifier(self):
        before = self.state()
        for label, purpose in REFUSED_PURPOSES.items():
            for who, (identification, identifier) in self.identifiers().items():
                with self.subTest(purpose=label, identifier=who):
                    response = self.post(RESEND, {'identification': identification,
                                                  'identifier': identifier, 'purpose': purpose})
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(response.json(), INVALID_PURPOSE)
                    self.assertEqual(response.content, b'{"status":400,"message":"Invalid purpose"}')
        self.assertEqual(self.state(), before, 'a refused resend issued, deleted or sent something')

    def test_the_refusal_resolves_no_account(self):
        """BEFORE ACCOUNT RESOLUTION: the refused request reads no account and no challenge."""
        for identification, identifier in self.identifiers().values():
            with self.subTest(identification=identification), \
                    CaptureQueriesContext(connection) as queries:
                response = self.post(RESEND, {'identification': identification,
                                              'identifier': identifier,
                                              'purpose': 'owner-claim'})
            self.assertEqual(response.json(), INVALID_PURPOSE)
            touched = [q['sql'] for q in queries.captured_queries
                       if any(t in q['sql'] for t in ('"users"', '"user_otps"', '"otp_issuances"'))]
            self.assertEqual(touched, [], 'the refusal looked an account or a challenge up')

    def test_an_authenticated_caller_is_refused_the_same_way(self):
        """The profile screen resends while signed in; the server then forces ``identification='id'``."""
        tokens = RefreshToken.for_user(self.user)
        before = self.state()
        cache.clear()
        response = self.client.post(
            RESEND, {'identification': 'msisdn', 'identifier': self.user.phone_number,
                     'purpose': 'owner-claim'},
            format='json', headers={'Authorization': f'Bearer {tokens.access_token}'},
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), INVALID_PURPOSE)
        self.assertEqual(self.state(), before)

    def test_the_controller_refuses_before_it_checks_the_identifier(self):
        self.assertEqual(OtpManager().resend_otp(purpose='owner-claim'), INVALID_PURPOSE)
        self.assertEqual(
            OtpManager().resend_otp(identification='msisdn', identifier=None, purpose='LOGIN'),
            INVALID_PURPOSE,
        )
        # An allowed purpose keeps the existing presence check and wording.
        self.assertEqual(OtpManager().resend_otp(purpose='register')['message'],
                         'Please provide both identification and identifier')

    def test_S6b_the_generic_resend_cannot_replace_the_claim_challenge(self):
        """The same-purpose door P1 cannot close: only P3 keeps the claim challenge alive."""
        with _test_env(C_CODE, X_CODE):
            owner, token, invitation = _claim(pending=True)
            claim_pk = self.challenge(token)
            sends = self.sms.call_count
            response = self.post(RESEND, {'identification': 'phone',
                                          'identifier': owner.phone_number,
                                          'purpose': 'owner-claim'})
            self.assertEqual(response.json(), INVALID_PURPOSE)
            self.assertEqual(self.sms.call_count, sends)
            self.assertUntouched(claim_pk, 'P3: the generic resend replaced the claim challenge')
            self.assertEqual(UserOtp.objects.filter(user=owner).count(), 1)
            self.assertClaimRedeemed(self.redeem(token, C_CODE, CLAIM_PASSWORD), owner, invitation)

    def test_S9_client_chosen_purposes_cannot_pile_up_rows_or_hide_the_login(self):
        # Spare codes, so that on code that still ISSUES for these words the failure is
        # the answer itself rather than an exhausted fixture.
        with _test_env(L_CODE, '4191', '4192', '4193'):
            user = _owner('s9')
            login_pk = self.login(user)
            for word in ('probe-unknown-1', 'probe-unknown-2', 'probe-unknown-3'):
                response = self.post(RESEND, {'identification': 'phone',
                                              'identifier': user.phone_number, 'purpose': word})
                self.assertEqual(response.json(), INVALID_PURPOSE)
            self.assertEqual(list(UserOtp.objects.filter(user=user).values_list('pk', flat=True)),
                             [login_pk])
            self.assertLoggedIn(self.verify(user, L_CODE), user)


class ResendAllowedPurposesAreUnchangedTests(_Er2Case):
    """
    The four accepted values keep their existing behaviour and their existing gates. An
    omitted purpose behaves exactly as ``null`` always has.
    """

    def test_an_omitted_and_an_explicit_null_purpose_still_issue(self):
        for label, body in (
            ('omitted', {'identification': 'msisdn', 'identifier': _phone()}),
            ('explicit null', {'identification': 'msisdn', 'identifier': _phone(), 'purpose': None}),
        ):
            with self.subTest(label):
                response, new = self.issued(RESEND, body)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json(), {'status': 200, 'message': 'OTP sent successfully'})
                self.assertEqual(len(new), 1)
                row = UserOtp.objects.get(pk=new[0])
                self.assertIsNone(row.user_id)
                self.assertIsNone(row.purpose)
                self.assertEqual(row.msisdn, body['identifier'])

    def test_register_still_issues_for_an_account_and_for_a_new_number(self):
        user = _account('register-control')
        self.assertIsNotNone(self.resend('id', str(user.pk), 'register'))
        self.assertIsNotNone(self.resend('msisdn', _phone(), 'register'))

    def test_reset_password_keeps_its_gates(self):
        established = _account('reset-control')
        pending = _account('reset-pending',
                           customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        self.assertIsNotNone(self.resend('phone', established.phone_number, 'reset-password'))
        # The customer-auth issuance gate: refused, nothing written.
        self.assertIsNone(self.resend('phone', pending.phone_number, 'reset-password', expect=500))
        # An absent account is still answered as before (no new disclosure, none removed).
        response = self.post(RESEND, {'identification': 'phone', 'identifier': _phone(),
                                      'purpose': 'reset-password'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['message'], 'User not found')

    def test_login_keeps_the_five_minute_password_anchor(self):
        user = _owner('anchor')
        refused = self.post(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                     'purpose': 'login'})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['message'], LOGIN_RESEND_REFUSAL)
        self.login(user)
        self.assertIsNotNone(self.resend('id', str(user.pk), 'login'))
        self.assertIsNotNone(self.resend('id', str(user.pk), 'login'))
        UserOtp.objects.filter(user=user).update(
            time_created=timezone.now() - timezone.timedelta(minutes=6))
        self.assertIsNone(self.resend('id', str(user.pk), 'login', expect=400))


# ═══════════════════════════════════════════════════════════════════════════════
# Positive controls — what the change must not move
# ═══════════════════════════════════════════════════════════════════════════════

class SamePurposeReplacementTests(_Er2Case):
    """Within one ``(user, msisdn, purpose)`` bucket a re-request still supersedes."""

    def test_a_second_reset_initiation_replaces_the_first(self):
        with _test_env('4111', '4112'):
            user = _account('pc1')
            first = self.initiate(user)
            second = self.initiate(user)
            self.assertIsNone(self.row(first))
            self.assertEqual(list(UserOtp.objects.filter(user=user).values_list('pk', flat=True)),
                             [second])
            self.assertEqual(self.complete_reset(user, '4111').json(), RESET_INVALID)
            self.assertResetCompleted(self.complete_reset(user, '4112'), user)

    def test_a_second_password_login_replaces_the_first(self):
        with _test_env('4121', '4122'):
            user = _owner('login-twice')
            first = self.login(user)
            second = self.login(user)
            self.assertIsNone(self.row(first))
            self.assertNotLoggedIn(self.verify(user, '4121'))
            self.assertLoggedIn(self.verify(user, '4122'), user)
            self.assertTrue(self.row(second)['consumed'])

    def test_a_second_claim_challenge_replaces_the_first(self):
        with _test_env('4131', '4132'):
            owner, token, invitation = _claim(pending=True)
            first = self.challenge(token)
            second = self.challenge(token)
            self.assertIsNone(self.row(first))
            self.assertUntouched(second)
            self.assertClaimRedeemed(self.redeem(token, '4132', CLAIM_PASSWORD), owner, invitation)

    def test_a_second_registration_resend_replaces_the_first(self):
        """
        PC10, in ``ENV=test`` where the two codes differ. The older code is refused and
        the newest registers. It is not asserted under dev, where both codes are
        ``1234``.
        """
        with _test_env('4141', '4142'):
            phone = _phone()
            first = self.resend('msisdn', phone, None)
            second = self.resend('msisdn', phone, None)
            self.assertIsNone(self.row(first))
            body = {'first_name': 'Er', 'last_name': 'Registrant', 'phone_number': phone,
                    'country': 'UG', 'password': PASSWORD}
            refused = self.post(REGISTER, {**body, 'otp': '4141'})
            self.assertEqual(refused.status_code, 400, refused.content)
            registered = self.post(REGISTER, {**body, 'otp': '4142'})
            self.assertEqual(registered.status_code, 200, registered.content)
            self.assertTrue(self.row(second)['consumed'])
            self.assertTrue(User.objects.filter(phone_number=phone).exists())

    def test_dev_a_second_registration_resend_leaves_only_the_newer_row(self):
        """ENV=dev: row identity, not digits. The surviving newer row is the one consumed."""
        phone = _phone()
        first = self.resend('msisdn', phone, None)
        second = self.resend('msisdn', phone, None)
        self.assertIsNone(self.row(first))
        response = self.post(REGISTER, {'first_name': 'Er', 'last_name': 'Registrant',
                                        'phone_number': phone, 'country': 'UG',
                                        'password': PASSWORD, 'otp': DEV_OTP})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.row(second)['consumed'])


class IsolationTests(_Er2Case):

    def test_an_unrelated_principal_is_untouched(self):
        with _test_env('4151', '4152'):
            first, second = _owner('pc3a'), _owner('pc3b')
            first_login = self.login(first)
            second_reset = self.initiate(second)
            self.assertLoggedIn(self.verify(first, '4151'), first)
            self.assertTrue(self.row(first_login)['consumed'])
            self.assertUntouched(second_reset)

    def test_an_unrelated_destination_is_untouched(self):
        with _test_env('4161', '4162'):
            p1, p2 = _phone(), _phone()
            m1 = self.resend('msisdn', p1, None)
            m2 = self.resend('msisdn', p2, None)
            self.assertUntouched(m1)
            self.assertUntouched(m2)
            response = self.post(REGISTER, {'first_name': 'Er', 'last_name': 'Registrant',
                                            'phone_number': p1, 'country': 'UG',
                                            'password': PASSWORD, 'otp': '4161'})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertUntouched(m2)

    def test_a_claim_challenge_and_a_reset_initiation_both_complete(self):
        """PC9: different buckets and two bound readers. Unchanged by P1, P2 and P3."""
        with _test_env(C_CODE, R_CODE):
            owner, token, invitation = _claim(pending=False)
            claim_pk = self.challenge(token)
            reset_pk = self.initiate(owner)
            self.assertUntouched(claim_pk)
            self.assertClaimRedeemed(self.redeem(token, C_CODE), owner, invitation)
            self.assertResetCompleted(self.complete_reset(owner, R_CODE), owner)
            self.assertTrue(self.row(reset_pk)['consumed'])


class LoginChallengeLifecycleTests(_Er2Case):
    """Expiry, the attempt cap and single use are unchanged at the bound login route."""

    def setUp(self):
        super().setUp()
        self.user = _owner('lifecycle')
        self.login_pk = self.login(self.user)

    def test_an_expired_login_challenge_is_not_selected_or_charged(self):
        UserOtp.objects.filter(pk=self.login_pk).update(
            expiry_time=timezone.now() - timezone.timedelta(seconds=1))
        self.assertNotLoggedIn(self.verify(self.user, DEV_OTP))
        self.assertEqual(self.row(self.login_pk)['attempts'], 0)
        self.assertFalse(self.row(self.login_pk)['consumed'])

    def test_the_fifth_wrong_attempt_locks_the_login_challenge(self):
        for _ in range(OTP_MAX_ATTEMPTS):
            self.assertNotLoggedIn(self.verify(self.user, WRONG))
        self.assertEqual(self.row(self.login_pk)['attempts'], OTP_MAX_ATTEMPTS)
        self.assertNotLoggedIn(self.verify(self.user, DEV_OTP))
        self.assertTrue(self.row(self.login_pk)['consumed'])
        self.assertEqual(OtpVerificationFailure.objects.filter(
            issuance_id=self.login_pk).count(), OTP_MAX_ATTEMPTS)

    def test_a_login_code_is_single_use(self):
        self.assertLoggedIn(self.verify(self.user, DEV_OTP), self.user)
        self.assertNotLoggedIn(self.verify(self.user, DEV_OTP))


class CustomerGatesTests(_Er2Case):
    """The pending and platform-staff refusals at the login sink are unchanged."""

    def _login_row_then(self, user, **update):
        self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        User.objects.filter(pk=user.pk).update(**update)
        return UserOtp.objects.get(user=user, purpose='login').pk

    def test_a_pending_identity_cannot_mint_from_a_login_code_in_flight(self):
        user = _account('pending-sink')
        self._login_row_then(user, customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        sessions = OutstandingToken.objects.filter(user=user).count()
        self.assertNotLoggedIn(self.verify(user, DEV_OTP))
        self.assertEqual(OutstandingToken.objects.filter(user=user).count(), sessions)

    def test_platform_staff_cannot_mint_from_a_login_code_in_flight(self):
        user = _account('staff-sink')
        self._login_row_then(user, account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        sessions = OutstandingToken.objects.filter(user=user).count()
        self.assertNotLoggedIn(self.verify(user, DEV_OTP))
        self.assertEqual(OutstandingToken.objects.filter(user=user).count(), sessions)


class ResetCompletionControlTests(_Er2Case):
    """E-R1's completion contract, followed through to an actual password change."""

    def test_initiate_complete_and_change_password(self):
        with _test_env(R_CODE):
            user = _account('er1-control')
            reset_pk = self.initiate(user)
            data = self.assertResetCompleted(self.complete_reset(user, R_CODE), user)
            self.assertTrue(self.row(reset_pk)['consumed'])
        cache.clear()
        response = self.client.post(
            CHANGE, {'old_password': data['temp_password'], 'new_password': NEW_PASSWORD},
            format='json', headers={'Authorization': f"Bearer {data['token']}"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        fresh = User.objects.get(pk=user.pk)
        self.assertTrue(fresh.check_password(NEW_PASSWORD))
        self.assertFalse(fresh.check_password(PASSWORD))
        self.assertFalse(fresh.prompt_password_change)

    def test_a_reset_code_issued_by_the_resend_route_still_completes(self):
        with _test_env(R_CODE):
            user = _account('er1-resend')
            self.resend('phone', user.phone_number, 'reset-password')
            self.assertResetCompleted(self.complete_reset(user, R_CODE), user)


class AccountingTests(_Er2Case):
    """The B2-C ledger keeps its origins, and a wrong guess is attributed only to the challenge it hit."""

    def origin(self, pk):
        return OtpIssuance.objects.get(pk=pk).origin

    def test_origins_are_recorded_per_path(self):
        # The login resend is taken while its password anchor is certainly live, so this
        # control asks only about origins and not about which challenges survive.
        with _test_env(L_CODE, X_CODE, R_CODE, '4171', C_CODE):
            user = _owner('ledger')
            self.assertEqual(self.origin(self.login(user)), 'password_login')
            self.assertEqual(self.origin(self.resend('id', str(user.pk), 'login')), 'login_resend')
            self.assertEqual(self.origin(self.initiate(user)), 'reset_initiation')
            self.assertEqual(
                self.origin(self.resend('phone', user.phone_number, 'reset-password')),
                'resend_request',
            )
            owner, token, _ = _claim(pending=False)
            self.assertEqual(self.origin(self.challenge(token)), 'owner_claim_challenge')

    def test_a_wrong_login_guess_is_attributed_to_the_login_challenge_only(self):
        with _test_env(L_CODE, R_CODE):
            user = _owner('ledger-guess')
            login_pk = self.login(user)
            reset_pk = self.initiate(user)
            self.assertNotLoggedIn(self.verify(user, WRONG))
            failure = OtpVerificationFailure.objects.get()
            self.assertEqual(failure.issuance_id, login_pk)
            self.assertEqual(failure.origin, 'password_login')
            self.assertFalse(failure.bound_redemption)
            self.assertUntouched(reset_pk)

    def test_a_refused_purpose_records_nothing(self):
        issuances, failures = OtpIssuance.objects.count(), OtpVerificationFailure.objects.count()
        self.post(RESEND, {'identification': 'msisdn', 'identifier': _phone(),
                           'purpose': 'owner-claim'})
        self.assertEqual((OtpIssuance.objects.count(), OtpVerificationFailure.objects.count()),
                         (issuances, failures))


class SupportedClientShapesTests(_Er2Case):
    """
    The request bodies the supported Frontend (Dinify-Frontend 0cdffe2) sends, traced
    into the real routes. These clients are compatible with P2 and P3. Nothing is
    claimed here about any other external caller.
    """

    def test_login_screen(self):
        # authentication.service: login {username, password}; setOtp {user, otp};
        # resendOtp('id', profile.id) -> {identification, identifier, purpose: 'login'}
        user = _owner('fe-login')
        response = self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD})
        self.assertTrue(response.json()['data']['require_otp'])
        resent = self.post(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                    'purpose': 'login'})
        self.assertEqual(resent.status_code, 200, resent.content)
        self.assertLoggedIn(self.post(VERIFY, {'user': str(user.pk), 'otp': DEV_OTP}), user)

    def test_register_screen(self):
        # register.component: sendOtp('msisdn', phone, null), then register with the form.
        phone = _phone()
        sent = self.post(RESEND, {'identification': 'msisdn', 'identifier': phone,
                                  'purpose': None})
        self.assertEqual(sent.status_code, 200, sent.content)
        response = self.post(REGISTER, {'first_name': 'Fe', 'last_name': 'Registrant',
                                        'email': f'fe-{phone}@example.test',
                                        'phone_number': phone, 'country': 'UG',
                                        'password': PASSWORD, 'otp': DEV_OTP})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(User.objects.filter(phone_number=phone).exists())

    def test_profile_screen_resend(self):
        # common-user-profile: signed in, sendOtp('msisdn', old_phone, null). The server
        # forces identification='id'. The profile PUT refuses any real phone change.
        user = _account('fe-profile')
        tokens = RefreshToken.for_user(user)
        cache.clear()
        response = self.client.post(
            RESEND, {'identification': 'msisdn', 'identifier': user.phone_number,
                     'purpose': None},
            format='json', headers={'Authorization': f'Bearer {tokens.access_token}'},
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = UserOtp.objects.get(user=user)
        self.assertIsNone(row.purpose)
        self.assertEqual(row.msisdn, user.phone_number)

    def test_forgot_password_screen(self):
        # forgot-password: initiate {identifier, identification}; reset-password {identifier, otp}
        user = _account('fe-forgot')
        response = self.post(INITIATE, {'identifier': user.phone_number,
                                        'identification': 'phone'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertResetCompleted(
            self.post(COMPLETE, {'identifier': user.phone_number, 'otp': DEV_OTP}), user)

    def test_owner_claim_screen(self):
        # owner-claim.service: challenge {} + header; redeem {otp, new_password?} + header.
        owner, token, invitation = _claim(pending=True)
        challenged = self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual(challenged.status_code, 200, challenged.content)
        self.assertTrue(challenged.json()['data']['credential_setup_required'])
        self.assertClaimRedeemed(self.redeem(token, DEV_OTP, CLAIM_PASSWORD), owner, invitation)


# ═══════════════════════════════════════════════════════════════════════════════
# PostgreSQL schedules — real transactions on independent connections
# ═══════════════════════════════════════════════════════════════════════════════

class _Schedule(TransactionTestCase):
    """
    Two workers on their own connections, ordered by NAMED SEAMS and events, never by
    sleeping. A third, monitor-only connection observes ``pg_stat_activity`` under a
    deadline, recording whether the second worker WAITED on a lock and on what.

    Every barrier, join and observation is bounded by ``WAIT``. A worker that never
    finishes fails the test instead of hanging the suite.
    """
    reset_sequences = False
    # The codes the schedule's challenges draw, in issuance order (setUp first).
    CODES = ()

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('Real-transaction semantics are asserted on PostgreSQL.')
        cache.clear()
        self.addCleanup(cache.clear)
        self.sends = []   # (thread name, in_atomic_block) for every SMS attempted

        def sms(message, msisdn, **kwargs):
            self.sends.append((threading.current_thread().name,
                               transaction.get_connection().in_atomic_block))
            return True

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(otp_manager, 'send_sms', side_effect=sms))
        stack.enter_context(mock.patch(
            'notifications_app.controllers.messenger.Messenger.send_email', return_value=True))
        stack.enter_context(mock.patch(
            'misc_app.controllers.notifications.notification.Notification.create_notification',
            return_value=None))
        stack.enter_context(mock.patch.object(otp_manager, 'threading',
                                              mock.Mock(Thread=_InlineThread)))
        stack.enter_context(_test_env(*self.CODES))

    def tearDown(self):
        connections.close_all()
        super().tearDown()

    # --- connections ---

    def monitor(self):
        d = connection.settings_dict
        return psycopg.connect(
            dbname=d['NAME'], user=d['USER'], password=d['PASSWORD'] or None,
            host=d['HOST'], port=d['PORT'], autocommit=True,
        )

    @staticmethod
    def name_connection(name):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('application_name', %s, false)", [name])

    def worker(self, name, target, outcomes, done):
        def body():
            try:
                self.name_connection(f'er2-{name}')
                outcomes[name] = ('ok', target())
            except Exception as exc:  # noqa: BLE001 - reported by the assertions
                outcomes[name] = ('error', repr(exc))
            finally:
                done.set()
                connections.close_all()
        return threading.Thread(target=body, name=name, daemon=True)

    def observe(self, name, done, holder):
        """
        ``finished`` if ``name`` completes without waiting, or ``waiting`` with the lock
        it waits on, the statement waiting and whether ``holder`` is the session blocking it.
        """
        deadline = time.monotonic() + WAIT
        with self.monitor() as m:
            while time.monotonic() < deadline:
                if done.is_set():
                    return {'state': 'finished'}
                row = m.execute(
                    'SELECT pid, wait_event_type, wait_event, query FROM pg_stat_activity '
                    'WHERE application_name = %s', [f'er2-{name}'],
                ).fetchone()
                if row and row[1] == 'Lock':
                    holder_pid = m.execute(
                        'SELECT pid FROM pg_stat_activity WHERE application_name = %s',
                        [f'er2-{holder}'],
                    ).fetchone()
                    blockers = m.execute('SELECT pg_blocking_pids(%s)', [row[0]]).fetchone()[0]
                    return {'state': 'waiting', 'event': row[2], 'query': row[3],
                            'blocked_by_holder': bool(holder_pid) and holder_pid[0] in blockers}
                time.sleep(0.02)
        self.fail(f'{name} neither waited on a lock nor finished within {WAIT}s')

    def join(self, *threads):
        for thread in threads:
            thread.join(timeout=WAIT)
        stuck = [t.name for t in threads if t.is_alive()]
        self.assertEqual(stuck, [], 'a worker did not finish')

    # --- seams ---

    @contextlib.contextmanager
    def park_issuer_after_insert(self, parked, resume):
        """Inside the issuance transaction: after its replacement DELETE and INSERT, before COMMIT."""
        def seam(sender, instance, created, **kwargs):
            if created and threading.current_thread().name == 'issuer' and not parked.is_set():
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the issuer seam was never released')

        post_save.connect(seam, sender=UserOtp, weak=False, dispatch_uid='er2-issuer-seam')
        try:
            yield
        finally:
            post_save.disconnect(sender=UserOtp, dispatch_uid='er2-issuer-seam')

    @contextlib.contextmanager
    def park_verifier_after_lock(self, parked, resume):
        """Inside ``verify_otp``: the first pepper read follows the locked SELECT."""
        real = otp_manager._otp_pepper

        def pepper():
            if threading.current_thread().name == 'verifier' and not parked.is_set():
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the verifier seam was never released')
            return real()

        with mock.patch.object(otp_manager, '_otp_pepper', pepper):
            yield

    @contextlib.contextmanager
    def park_redeemer_at_consistency(self, parked, resume):
        """
        Inside redemption at ``assert_owner_consistency``. It holds the restaurant, the
        onboarding row, the head invitation and the owner's ``users`` row FOR UPDATE. It
        does NOT yet hold any challenge row: ``verify_otp`` runs later.
        """
        from platform_admin_app import owner_claim_redemption
        real = owner_claim_redemption.assert_owner_consistency

        def parked_assert(restaurant):
            result = real(restaurant)
            if threading.current_thread().name == 'redeemer' and not parked.is_set():
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the redeemer seam was never released')
            return result

        with mock.patch.object(owner_claim_redemption, 'assert_owner_consistency', parked_assert):
            yield

    # --- requests (each worker builds its own client) ---

    @staticmethod
    def request(url, body, headers=None):
        response = APIClient(raise_request_exception=False).post(
            url, body, format='json', headers=headers or {})
        return response.status_code, response.json()

    def assertTokensFor(self, data, user):
        self.assertEqual(str(AccessToken(data['token'])['user_id']), str(user.pk))
        self.assertEqual(str(RefreshToken(data['refresh'])['user_id']), str(user.pk))

    def assertNothingHeldAcrossDelivery(self):
        self.assertTrue(self.sends)
        self.assertEqual([held for _, held in self.sends], [False] * len(self.sends),
                         self.sends)

    @staticmethod
    def row(pk):
        r = UserOtp.objects.filter(pk=pk).first()
        return None if r is None else (r.purpose, r.attempts, r.consumed_at is not None)


class LoginVersusResetInitiationScheduleTests(_Schedule):
    """C1 and C1r: a correct login verification and a reset initiation, in both arrival orders."""
    CODES = (L_CODE, R_CODE)     # the password login in setUp, then the initiation

    def setUp(self):
        super().setUp()
        self.user = _owner('c1')
        status, body = self.request(LOGIN, {'username': self.user.phone_number,
                                            'password': PASSWORD})
        self.assertTrue(body['data']['require_otp'], body)
        self.login_pk = UserOtp.objects.get(user=self.user, purpose='login').pk

    def initiate(self):
        return self.request(INITIATE, {'identifier': self.user.phone_number})

    def verify(self):
        return self.request(VERIFY, {'user': str(self.user.pk), 'otp': L_CODE})

    def test_C1_initiation_parked_mid_transaction_then_the_correct_login_code(self):
        parked, resume = threading.Event(), threading.Event()
        issued, verified, outcomes = threading.Event(), threading.Event(), {}
        with self.park_issuer_after_insert(parked, resume):
            issuer = self.worker('issuer', self.initiate, outcomes, issued)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            verifier = self.worker('verifier', self.verify, outcomes, verified)
            verifier.start()
            try:
                seen = self.observe('verifier', verified, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, verifier)

        self.assertEqual(seen, {'state': 'finished'},
                         'P1: the verifier waited on the issuer (it was deleting the login row)')
        self.assertEqual(outcomes['issuer'][0], 'ok', outcomes)
        self.assertEqual(outcomes['issuer'][1][0], 200)
        self.assertEqual(outcomes['verifier'][0], 'ok', outcomes)
        status, body = outcomes['verifier'][1]
        self.assertEqual(status, 200)
        self.assertIs(body['data']['valid'], True, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertEqual(self.row(self.login_pk), ('login', 0, True))
        reset = UserOtp.objects.get(user=self.user, purpose='reset-password')
        self.assertEqual(self.row(reset.pk), ('reset-password', 0, False))
        self.assertNothingHeldAcrossDelivery()

    def test_C1r_verification_holding_its_row_then_an_initiation_arrives(self):
        parked, resume = threading.Event(), threading.Event()
        issued, verified, outcomes = threading.Event(), threading.Event(), {}
        with self.park_verifier_after_lock(parked, resume):
            verifier = self.worker('verifier', self.verify, outcomes, verified)
            verifier.start()
            self.assertTrue(parked.wait(WAIT), 'the verifier never reached its seam')
            issuer = self.worker('issuer', self.initiate, outcomes, issued)
            issuer.start()
            try:
                seen = self.observe('issuer', issued, holder='verifier')
            finally:
                resume.set()
            self.join(issuer, verifier)

        self.assertEqual(seen, {'state': 'finished'},
                         'P1: the initiation waited on the locked login row')
        self.assertEqual(outcomes['issuer'][0], 'ok', outcomes)
        self.assertEqual(outcomes['issuer'][1][0], 200)
        self.assertEqual(outcomes['verifier'][0], 'ok', outcomes)
        status, body = outcomes['verifier'][1]
        self.assertEqual(status, 200)
        self.assertTokensFor(body['data'], self.user)
        self.assertEqual(self.row(self.login_pk), ('login', 0, True))
        reset = UserOtp.objects.get(user=self.user, purpose='reset-password')
        self.assertEqual(self.row(reset.pk), ('reset-password', 0, False))
        self.assertNothingHeldAcrossDelivery()


class ClaimVersusResetResendScheduleTests(_Schedule):
    """
    C2 and C2r: an established owner's redemption and a reset resend to the same phone,
    in both arrival orders. The user-first lock order is preserved, so the later worker
    WAITS on the ``users`` row and then succeeds. That wait is expected and is not a
    defect. These two schedules are what was observed here; they are not a claim that
    no other interleaving can deadlock.
    """
    CODES = (C_CODE, R_CODE)     # the claim challenge in setUp, then the reset resend

    def setUp(self):
        super().setUp()
        self.owner, self.token, self.invitation = _claim(pending=False)
        status, body = self.request(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: self.token})
        self.assertEqual(status, 200, body)
        self.claim_pk = UserOtp.objects.get(user=self.owner, purpose='owner-claim').pk

    def resend(self):
        return self.request(RESEND, {'identification': 'phone',
                                     'identifier': self.owner.phone_number,
                                     'purpose': 'reset-password'})

    def redeem(self):
        return self.request(CLAIM_REDEEM, {'otp': C_CODE}, headers={CLAIM_HEADER: self.token})

    def assertOutcome(self, outcomes):
        self.assertEqual(outcomes['issuer'][0], 'ok', outcomes)
        self.assertEqual(outcomes['issuer'][1], (200, {'status': 200,
                                                       'message': 'OTP sent successfully'}))
        self.assertEqual(outcomes['redeemer'][0], 'ok', outcomes)
        status, body = outcomes['redeemer'][1]
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.owner)
        invitation = OwnerInvitation.objects.get(pk=self.invitation)
        self.assertEqual((invitation.claim_failed_attempts, invitation.consumed_at is not None),
                         (0, True))
        self.assertEqual(self.row(self.claim_pk), ('owner-claim', 0, True))
        reset = UserOtp.objects.get(user=self.owner, purpose='reset-password')
        self.assertEqual(self.row(reset.pk), ('reset-password', 0, False))
        self.assertNothingHeldAcrossDelivery()

    def test_C2_resend_parked_mid_transaction_then_the_correct_claim_code(self):
        parked, resume = threading.Event(), threading.Event()
        issued, redeemed, outcomes = threading.Event(), threading.Event(), {}
        with self.park_issuer_after_insert(parked, resume):
            issuer = self.worker('issuer', self.resend, outcomes, issued)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            redeemer = self.worker('redeemer', self.redeem, outcomes, redeemed)
            redeemer.start()
            try:
                seen = self.observe('redeemer', redeemed, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, redeemer)

        # The redeemer waits for the issuance's FOR KEY SHARE on the owner row...
        self.assertEqual(seen['state'], 'waiting', seen)
        self.assertIn('FROM "users"', seen['query'])
        self.assertIn('FOR UPDATE', seen['query'])
        self.assertTrue(seen['blocked_by_holder'], seen)
        # ...and then redeems with the code it was sent (P1: nothing deleted it).
        self.assertOutcome(outcomes)

    def test_C2r_redemption_holding_the_owner_then_a_resend_arrives(self):
        parked, resume = threading.Event(), threading.Event()
        issued, redeemed, outcomes = threading.Event(), threading.Event(), {}
        with self.park_redeemer_at_consistency(parked, resume):
            redeemer = self.worker('redeemer', self.redeem, outcomes, redeemed)
            redeemer.start()
            self.assertTrue(parked.wait(WAIT), 'the redeemer never reached its seam')
            issuer = self.worker('issuer', self.resend, outcomes, issued)
            issuer.start()
            try:
                seen = self.observe('issuer', issued, holder='redeemer')
            finally:
                resume.set()
            self.join(issuer, redeemer)

        # The issuance takes the owner row FIRST (KEY SHARE) and waits there, holding no
        # challenge row; the redemption holds no challenge row yet either.
        self.assertEqual(seen['state'], 'waiting', seen)
        self.assertIn('FOR KEY SHARE', seen['query'])
        self.assertTrue(seen['blocked_by_holder'], seen)
        self.assertOutcome(outcomes)
