import hashlib
from unittest.mock import patch
from django.test import TestCase
from django.core.cache import cache
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from users_app.controllers.self_register import self_register
from users_app.controllers.login import login
from users_app.controllers.change_password import change_password
from users_app.controllers.reset_password import (
    RESET_ACKNOWLEDGEMENT, reset_password, initiate_password_reset,
)
from users_app.models import User, UserOtp
from users_app.controllers.otp_manager import OtpManager, OTP_MAX_ATTEMPTS
from users_app.throttles import OtpIdentifierThrottle


TEST_PHONE = '1234567890'
TEST_EMAIL = 'test@user.com'

# Patch targets for external I/O used across many tests
_PATCH_YO_SMS = 'users_app.controllers.otp_manager.send_sms'
_PATCH_MESSENGER_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_NOTIFICATION = 'misc_app.controllers.notifications.notification.Notification.create_notification'


def seed_user():
    # `roles=[]`. This fixture used to carry 'dinify_admin', which conferred
    # platform-wide authority across half the suite; the role vocabulary no longer
    # grants anything, and a restaurant_user may not hold a platform role at all.
    # Tests that need PRIVILEGE give the user a real RestaurantEmployee row —
    # see `seed_privileged_user` below.
    return User.objects.create_user(
        first_name='Test',
        last_name='User',
        email=TEST_EMAIL,
        phone_number=TEST_PHONE,
        username=TEST_PHONE,
        country='Uganda',
        password='password',
        roles=[],
    )


def seed_privileged_user():
    """
    Seed TEST_PHONE as an active OWNER of a live restaurant.

    Login escalates to OTP for owner/finance/manager EMPLOYMENTS — the only
    privilege axis left, now that the dinify-admin arm of that branch is gone. The
    security property under test ("a privileged login never returns tokens before
    OTP") is unchanged; only the way the account becomes privileged is real.
    """
    from restaurants_app.models import Restaurant, RestaurantEmployee

    user = seed_user()
    restaurant = Restaurant.objects.create(
        name='Login Security Restaurant', location='loc',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER],
    )
    return user


def seed_regular_user():
    User.objects.create_user(
        first_name='Regular',
        last_name='User',
        email='regular@user.com',
        phone_number='9876543210',
        username='9876543210',
        country='Uganda',
        password='password',
        roles=['diner'],
        prompt_password_change=False
    )


@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_YO_SMS, return_value=None)
class UsersAppTestFunctions(TestCase):
    def setUp(self):
        seed_user()

    def test_self_registration(self, *mocks):
        data = {
            'first_name': 'John',
            'last_name': 'Doe',
            'email': 'john@doe.com',
            'phone_number': '256712345678',
            'password': 'password',
            'country': 'Uganda'
        }
        response = self_register(data, skip_otp=True, send_credentials=True)
        self.assertEqual(response.get('status'), 200)
        self.assertEqual(response.get('message'), MESSAGES.get('OK_SELF_REGISTER'))

        # duplicate phone
        response = self_register(data, send_credentials=True)
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(response.get('message'), MESSAGES.get('PHONE_NUMBER_EXISTS'))

        # duplicate email
        data['phone_number'] = '256712345679'
        response = self_register(data)
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(response.get('message'), MESSAGES.get('EMAIL_EXISTS'))

    def test_login_success(self, *mocks):
        response = login('1234567890', 'password')
        self.assertEqual(response.get('status'), 200)

    def test_login_wrong_password(self, *mocks):
        response = login('1234567890', 'wrong_password')
        self.assertEqual(response.get('status'), 401)
        self.assertEqual(response.get('message'), MESSAGES.get('WRONG_PASSWORD'))

    def test_login_no_username(self, *mocks):
        # D11 B1: an unknown identity is refused with the wrong-password envelope, so
        # login no longer discloses which phone numbers and emails hold accounts.
        response = login('123456780', 'password')
        self.assertEqual(response, {'status': 401, 'message': MESSAGES.get('WRONG_PASSWORD')})

    def test_change_password(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)
        response = change_password(str(user.id), 'password', 'new_password')
        self.assertEqual(response.get('status'), 200)
        self.assertEqual(response.get('message'), MESSAGES.get('OK_PASSWORD_CHANGE'))

    def test_change_password_wrong_old(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)
        response = change_password(str(user.id), 'wrong_password', 'new_password')
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(response.get('message'), MESSAGES.get('WRONG_PASSWORD'))

    def test_otp_manager(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)
        otp_manager = OtpManager()

        # make otp
        otp_manager.make_otp(user=user)
        user_otp = UserOtp.objects.get(user_id=user.id)
        self.assertTrue(user_otp)

        # verify correct otp
        self.assertTrue(otp_manager.verify_otp(user_id=user.id, otp='1234')['data']['valid'])
        # verify wrong otp (otp is consumed above, re-make)
        otp_manager.make_otp(user=user)
        self.assertFalse(otp_manager.verify_otp(user_id=user.id, otp='1111')['data']['valid'])

    def test_otp_resend(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)
        otp_manager = OtpManager()
        # make an initial otp so resend can find it
        otp_manager.make_otp(user=user, purpose='login')
        result = otp_manager.resend_otp(
            identification='id',
            identifier=str(user.id),
            purpose='login'
        )
        self.assertEqual(result.get('status'), 200)

    def test_msisdn_otp(self, *mocks):
        otp_manager = OtpManager()
        result = otp_manager.make_otp(msisdn=TEST_PHONE, purpose='test')
        self.assertTrue(result)

        result = otp_manager.verify_otp(msisdn=TEST_PHONE, otp='1234')
        self.assertEqual(result.get('status'), 200)
        self.assertTrue(result['data']['valid'])


@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_YO_SMS, return_value=None)
class LoginSecurityTests(TestCase):
    """Tests for auth-flow security fixes."""

    def setUp(self):
        seed_privileged_user()
        seed_regular_user()

    def test_privileged_login_requires_otp_no_token_leak(self, *mocks):
        """A privileged login must NOT return tokens before OTP verification."""
        response = login(TEST_PHONE, 'password', source='restaurant')
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['data']['require_otp'])
        # Tokens must NOT be present before OTP
        self.assertNotIn('token', response['data'])
        self.assertNotIn('refresh', response['data'])
        # user_id should be present so frontend can call verify-otp
        self.assertIn('user_id', response['data'])

    def test_privileged_login_with_prompt_password_change_no_token_leak(self, *mocks):
        """Even with prompt_password_change=True, tokens must not leak before OTP."""
        user = User.objects.get(phone_number=TEST_PHONE)
        user.prompt_password_change = True
        user.save()

        response = login(TEST_PHONE, 'password', source='restaurant')
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['data']['require_otp'])
        self.assertNotIn('token', response['data'])
        self.assertNotIn('refresh', response['data'])

    def test_regular_user_login_returns_tokens(self, *mocks):
        """Non-admin users get tokens directly (no OTP required)."""
        response = login('9876543210', 'password', source='diner')
        self.assertEqual(response['status'], 200)
        self.assertFalse(response['data']['require_otp'])
        self.assertIn('token', response['data'])
        self.assertIn('refresh', response['data'])

    def test_otp_verify_returns_token_for_login_purpose(self, *mocks):
        """After OTP verification with purpose='login', tokens are returned."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user, purpose='login')

        result = OtpManager().verify_otp(user_id=str(user.id), otp='1234')
        self.assertTrue(result['data']['valid'])
        self.assertIn('token', result['data'])
        self.assertIn('refresh', result['data'])


@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_YO_SMS, return_value=None)
class PasswordResetSecurityTests(TestCase):
    """Tests for the new password-reset flow."""

    def setUp(self):
        seed_user()

    def test_initiate_sends_otp(self, *mocks):
        # D11 E-R1: the answer is the uniform acknowledgement and names no account;
        # the reset challenge issued for this user is what shows it was started.
        response = initiate_password_reset(TEST_PHONE)
        self.assertEqual(response, {'status': 200, 'message': RESET_ACKNOWLEDGEMENT})
        self.assertTrue(UserOtp.objects.filter(
            user__phone_number=TEST_PHONE, purpose='reset-password').exists())

    def test_initiate_unknown_user(self, *mocks):
        # D11 E-R1: acknowledged exactly like a known user, and nothing is issued.
        response = initiate_password_reset('0000000000')
        self.assertEqual(response, {'status': 200, 'message': RESET_ACKNOWLEDGEMENT})
        self.assertFalse(UserOtp.objects.exists())

    def test_reset_with_valid_otp(self, *mocks):
        """Reset with valid OTP returns a token and sets prompt_password_change."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user, purpose='reset-password')

        response = reset_password(TEST_PHONE, '1234')
        self.assertEqual(response.get('status'), 200)
        self.assertIn('token', response['data'])
        self.assertIn('refresh', response['data'])
        self.assertTrue(response['data']['prompt_password_change'])

        user.refresh_from_db()
        self.assertTrue(user.prompt_password_change)

    def test_reset_with_invalid_otp(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user, purpose='reset-password')

        response = reset_password(TEST_PHONE, '9999')
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(response.get('message'), 'Invalid OTP.')

    def test_reset_unknown_user(self, *mocks):
        response = reset_password('0000000000', '1234')
        self.assertEqual(response.get('status'), 400)

    def test_full_reset_then_change_flow(self, *mocks):
        """End-to-end: initiate -> verify OTP -> change password."""
        user = User.objects.get(phone_number=TEST_PHONE)

        # Step 1: initiate
        resp1 = initiate_password_reset(TEST_PHONE)
        self.assertEqual(resp1['status'], 200)

        # Step 2: reset with OTP
        OtpManager().make_otp(user=user, purpose='reset-password')
        resp2 = reset_password(TEST_PHONE, '1234')
        self.assertEqual(resp2['status'], 200)
        temp_pw = resp2['data']['temp_password']

        # Step 3: change password
        resp3 = change_password(str(user.id), temp_pw, 'my_new_secure_password')
        self.assertEqual(resp3['status'], 200)

        # Verify the new password works
        user.refresh_from_db()
        self.assertTrue(user.check_password('my_new_secure_password'))
        self.assertFalse(user.prompt_password_change)

    def test_no_plaintext_password_in_notification(self, *mocks):
        """Verify that no Notification is created with a plaintext password."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user, purpose='reset-password')

        reset_password(TEST_PHONE, '1234')

        # The old code called Notification with msg_type='forgot-password'
        # containing the password. The new code should NOT call Notification at all.
        notification_mock = mocks[2]  # _PATCH_NOTIFICATION is outermost
        for call in notification_mock.call_args_list:
            args, kwargs = call
            if args:
                msg_data = args[0] if isinstance(args[0], dict) else kwargs.get('msg_data', {})
            else:
                msg_data = kwargs.get('msg_data', {})
            self.assertNotEqual(
                msg_data.get('msg_type'), 'forgot-password',
                "Notification with msg_type='forgot-password' should not be created"
            )
            self.assertNotIn(
                'password', msg_data,
                "Notification msg_data should not contain a 'password' key"
            )


class _FakeThrottleRequest:
    """Minimal stand-in for a DRF request.

    OtpIdentifierThrottle.get_cache_key only reads .user and .data, so a tiny
    object is enough to exercise the key-derivation logic deterministically
    (no cache/timing involved).
    """

    def __init__(self, user, data):
        self.user = user
        self.data = data


@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_YO_SMS, return_value=None)
class OtpHardeningTests(TestCase):
    """OTP verify hardening: lockout, single-use, salted HMAC, per-id throttle."""

    def setUp(self):
        seed_user()
        # DRF throttles use the default (LocMemCache) cache, which persists
        # across requests and tests in a run — clear it so tests don't leak.
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_lockout_after_max_attempts(self, *mocks):
        """5 wrong guesses lock the code; a 6th correct guess is rejected."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user)

        for _ in range(OTP_MAX_ATTEMPTS):
            result = OtpManager().verify_otp(user_id=user.id, otp='0000')
            self.assertFalse(result['data']['valid'])

        # The challenge is now locked, so even the correct code fails.
        result = OtpManager().verify_otp(user_id=user.id, otp='1234')
        self.assertFalse(result['data']['valid'])

    def test_verified_code_cannot_be_reused(self, *mocks):
        """A consumed (already-verified) code cannot be replayed."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user)

        first = OtpManager().verify_otp(user_id=user.id, otp='1234')
        self.assertTrue(first['data']['valid'])

        second = OtpManager().verify_otp(user_id=user.id, otp='1234')
        self.assertFalse(second['data']['valid'])

    def test_otp_hash_is_salted_hmac_not_raw_sha256(self, *mocks):
        """Stored hash is a salted HMAC, not the reversible sha256 of the code."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user)

        challenge = UserOtp.objects.get(user_id=user.id)
        self.assertNotEqual(challenge.otp_hash, hashlib.sha256(b'1234').hexdigest())
        self.assertTrue(challenge.salt)

    def test_make_otp_populates_salt_and_identifier(self, *mocks):
        """make_otp writes salt + identifier; dev still yields a verifiable 1234."""
        user = User.objects.get(phone_number=TEST_PHONE)
        OtpManager().make_otp(user=user)

        challenge = UserOtp.objects.get(user_id=user.id)
        self.assertEqual(len(challenge.salt), 32)  # secrets.token_hex(16)
        self.assertEqual(challenge.identifier, f"user:{user.id}")
        self.assertEqual(challenge.attempts, 0)
        self.assertIsNone(challenge.consumed_at)
        # Dev override → still verifiable with '1234' through the new path.
        self.assertTrue(
            OtpManager().verify_otp(user_id=user.id, otp='1234')['data']['valid']
        )

    def test_resend_otp_msisdn_no_user_does_not_crash(self, *mocks):
        """
        The resend msisdn path no longer dereferences a None user (was a 500).

        The purpose is ``register`` because generic resend accepts only ``login``,
        ``reset-password``, ``register`` and null since D11 E-R2; this used to send
        ``first-time-payment``, which is now refused before any lookup and so could no
        longer reach the no-user path this pins.
        """
        result = OtpManager().resend_otp(
            identification='msisdn',
            identifier='256700000000',
            purpose='register',
        )
        self.assertEqual(result.get('status'), 200)

    def test_throttle_cache_key_derivation(self, *mocks):
        """OtpIdentifierThrottle keys on the target identity, not the client IP."""
        throttle = OtpIdentifierThrottle()
        user = User.objects.get(phone_number=TEST_PHONE)

        # Authenticated → keyed on the user id.
        key = throttle.get_cache_key(_FakeThrottleRequest(user, {}), None)
        self.assertIsNotNone(key)
        self.assertTrue(key.endswith(f"user:{user.id}"))

        # Anonymous with a `user` field in the body.
        key = throttle.get_cache_key(
            _FakeThrottleRequest(AnonymousUser(), {'user': '42'}), None
        )
        self.assertIn('id:42', key)

        # Anonymous phone identifier is canonicalised into a single bucket.
        key = throttle.get_cache_key(
            _FakeThrottleRequest(AnonymousUser(), {'msisdn': '0700000000'}), None
        )
        self.assertIn('id:256700000000', key)

        # Nothing derivable → None (falls back to the per-IP throttle).
        key = throttle.get_cache_key(
            _FakeThrottleRequest(AnonymousUser(), {}), None
        )
        self.assertIsNone(key)

    @patch.object(OtpIdentifierThrottle, 'get_rate', return_value='2/min')
    def test_verify_otp_endpoint_throttled_per_identity(self, _rate, *mocks):
        """Per-identity throttle returns 429 after its limit, even across IPs."""
        user = User.objects.get(phone_number=TEST_PHONE)
        client = APIClient()
        url = '/api/v1/users/auth/verify-otp/'
        payload = {'user': str(user.id), 'otp': '9999'}

        # Rotate REMOTE_ADDR so the per-IP OtpThrottle never accumulates; only
        # the per-identity throttle (same `user`) can trip.
        statuses = []
        for i in range(3):
            resp = client.post(
                url, payload, format='json', REMOTE_ADDR=f'10.0.0.{i + 1}'
            )
            statuses.append(resp.status_code)

        self.assertNotEqual(statuses[0], 429)  # under the limit
        self.assertEqual(statuses[-1], 429)     # over the per-identity limit


class RefreshRotationAndLogoutTests(TestCase):
    """
    Auth-hardening regression guards:
      - refresh-token rotation issues a new refresh
      - the original refresh is blacklisted after rotation
      - /auth/logout/ blacklists the refresh server-side
      - /auth/logout/ requires a valid access token
      - /auth/logout/ is tolerant of missing/invalid refresh in the body
    """

    REFRESH_URL = '/api/v1/users/auth/token/refresh/'
    LOGOUT_URL = '/api/v1/users/auth/logout/'

    def setUp(self):
        seed_regular_user()
        self.user = User.objects.get(phone_number='9876543210')
        token = RefreshToken.for_user(self.user)
        self.access = str(token.access_token)
        self.refresh = str(token)
        self.client = APIClient()

    def _auth_client(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access}')
        return client

    def test_refresh_returns_new_rotated_refresh(self):
        response = self.client.post(
            self.REFRESH_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertNotEqual(response.data['refresh'], self.refresh)

    def test_old_refresh_blacklisted_after_rotation(self):
        first = self.client.post(
            self.REFRESH_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(first.status_code, 200)

        # Re-using the original refresh must now fail (blacklisted).
        second = self.client.post(
            self.REFRESH_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(second.status_code, 401)

        # The new refresh issued by the first call still works.
        third = self.client.post(
            self.REFRESH_URL, {'refresh': first.data['refresh']}, format='json'
        )
        self.assertEqual(third.status_code, 200)

    def test_logout_with_valid_refresh_blacklists(self):
        client = self._auth_client()
        response = client.post(
            self.LOGOUT_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], MESSAGES['OK_LOGOUT'])

        # The refresh is now blacklisted — using it must 401.
        retry = APIClient().post(
            self.REFRESH_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(retry.status_code, 401)

    def test_logout_without_auth_header_returns_401(self):
        response = self.client.post(
            self.LOGOUT_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(response.status_code, 401)

    def test_logout_with_invalid_refresh_returns_200(self):
        client = self._auth_client()
        response = client.post(
            self.LOGOUT_URL, {'refresh': 'not-a-real-token'}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], MESSAGES['OK_LOGOUT'])

    def test_logout_with_already_blacklisted_refresh_returns_200(self):
        # Blacklist once, then call logout again with the same token.
        RefreshToken(self.refresh).blacklist()

        client = self._auth_client()
        response = client.post(
            self.LOGOUT_URL, {'refresh': self.refresh}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], MESSAGES['OK_LOGOUT'])

    def test_logout_with_no_refresh_in_body_returns_200(self):
        client = self._auth_client()
        response = client.post(self.LOGOUT_URL, {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], MESSAGES['OK_LOGOUT'])


def _env_config(env):
    """config() stub for otp_manager: fixes ENV, defaults everything else
    (OTP_HMAC_PEPPER falls back to its SECRET_KEY derivation)."""
    def _cfg(key, **kwargs):
        if key == 'ENV':
            return env
        return kwargs.get('default')
    return _cfg


class OtpDeliveryTruthTests(TestCase):
    """make_otp's per-ENV truth contract, plus the fail-CLOSED caller envelopes.

    dev is UNCHANGED (immediate True, threaded fire-and-forget — the '1234'
    flow); test falls back to a SYNCHRONOUS email when the SMS fails; prod is
    SMS-only. The caller tests pin that a delivery failure can never fall
    through to the token branch.
    """

    def setUp(self):
        seed_privileged_user()
        self.user = User.objects.get(phone_number=TEST_PHONE)

    def test_dev_returns_true_immediately_even_when_sender_would_fail(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('dev')), \
                patch(_PATCH_YO_SMS, return_value=False), \
                patch(_PATCH_MESSENGER_EMAIL, return_value=False):
            self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))

    def test_prod_gateway_ok_returns_true_with_tight_timeout(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('prod')), \
                patch(_PATCH_YO_SMS, return_value=True) as mock_sms, \
                patch(_PATCH_MESSENGER_EMAIL) as mock_email:
            self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
        self.assertEqual(mock_sms.call_args.kwargs.get('timeout'), 3)
        mock_email.assert_not_called()

    def test_prod_gateway_failure_returns_false_and_never_emails(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('prod')), \
                patch(_PATCH_YO_SMS, return_value=False), \
                patch(_PATCH_MESSENGER_EMAIL) as mock_email:
            self.assertFalse(OtpManager().make_otp(user=self.user, purpose='login'))
        mock_email.assert_not_called()

    def test_test_env_sms_ok_returns_true(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('test')), \
                patch(_PATCH_YO_SMS, return_value=True):
            self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))

    def test_test_env_sms_fail_falls_back_to_synchronous_email(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('test')), \
                patch(_PATCH_YO_SMS, return_value=False), \
                patch(_PATCH_MESSENGER_EMAIL, return_value=True) as mock_email:
            self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
        mock_email.assert_called_once()

    def test_test_env_both_channels_fail_returns_false(self):
        with patch('users_app.controllers.otp_manager.config', side_effect=_env_config('test')), \
                patch(_PATCH_YO_SMS, return_value=False), \
                patch(_PATCH_MESSENGER_EMAIL, return_value=False):
            self.assertFalse(OtpManager().make_otp(user=self.user, purpose='login'))

    def test_login_fails_closed_when_otp_delivery_fails(self):
        """A make_otp failure must NEVER fall through to the token branch."""
        with patch('users_app.controllers.login.OtpManager') as mock_manager:
            mock_manager.return_value.make_otp.return_value = False
            response = login(TEST_PHONE, 'password', source='restaurant')
        self.assertEqual(response['status'], 500)
        self.assertIn("couldn't send your verification code", response['message'])
        self.assertNotIn('token', response.get('data') or {})
        self.assertNotIn('refresh', response.get('data') or {})

    def test_initiate_password_reset_fails_closed_when_otp_delivery_fails(self):
        with patch('users_app.controllers.reset_password.OtpManager') as mock_manager:
            mock_manager.return_value.make_otp.return_value = False
            response = initiate_password_reset(TEST_PHONE)
        self.assertEqual(response['status'], 500)
        self.assertIn("couldn't send your verification code", response['message'])
