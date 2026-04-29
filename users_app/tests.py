from unittest.mock import patch, MagicMock
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from dinify_backend.configss.messages import MESSAGES
from users_app.controllers.self_register import self_register
from users_app.controllers.login import login
from users_app.controllers.change_password import change_password
from users_app.controllers.reset_password import reset_password, initiate_password_reset
from users_app.models import User, UserOtp
from users_app.controllers.otp_manager import OtpManager
from users_app.controllers.update_user_profile import update_user_profile


TEST_PHONE = '1234567890'
TEST_EMAIL = 'test@user.com'

# Patch targets for external I/O used across many tests
_PATCH_YO_SMS = 'payment_integrations_app.controllers.yo_integrations.YoIntegration.send_sms'
_PATCH_MESSENGER_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_NOTIFICATION = 'misc_app.controllers.notifications.notification.Notification.create_notification'


def seed_user():
    User.objects.create_user(
        first_name='Test',
        last_name='User',
        email=TEST_EMAIL,
        phone_number=TEST_PHONE,
        username=TEST_PHONE,
        country='Uganda',
        password='password',
        roles=['dinify_admin']
    )


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
        response = login('123456780', 'password')
        self.assertEqual(response.get('status'), 401)
        self.assertEqual(response.get('message'), MESSAGES.get('NO_USERNAME'))

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

    def test_update_user_profile(self, *mocks):
        user = User.objects.get(phone_number=TEST_PHONE)

        # no otp provided
        response = update_user_profile(actor=user, user_id=user.id, phone_number='256712345678')
        self.assertEqual(response.get('status'), 400)

        # invalid otp
        OtpManager().make_otp(user)
        response = update_user_profile(actor=user, user_id=user.id, phone_number='256712345678', otp='1235')
        self.assertEqual(response.get('status'), 400)

        # valid otp
        OtpManager().make_otp(user)
        response = update_user_profile(actor=user, user_id=user.id, phone_number='256712345678', otp='1234')
        user.refresh_from_db()
        self.assertEqual(user.phone_number, '256712345678')
        self.assertEqual(response.get('status'), 200)

        # no otp needed (non-phone field)
        response = update_user_profile(actor=user, user_id=user.id, first_name='John')
        self.assertEqual(response.get('status'), 200)


@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_YO_SMS, return_value=None)
class LoginSecurityTests(TestCase):
    """Tests for auth-flow security fixes."""

    def setUp(self):
        seed_user()
        seed_regular_user()

    def test_admin_login_requires_otp_no_token_leak(self, *mocks):
        """Admin login must NOT return tokens before OTP verification."""
        response = login(TEST_PHONE, 'password', source='restaurant')
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['data']['require_otp'])
        # Tokens must NOT be present before OTP
        self.assertNotIn('token', response['data'])
        self.assertNotIn('refresh', response['data'])
        # user_id should be present so frontend can call verify-otp
        self.assertIn('user_id', response['data'])

    def test_admin_login_with_prompt_password_change_no_token_leak(self, *mocks):
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
        response = initiate_password_reset(TEST_PHONE)
        self.assertEqual(response.get('status'), 200)
        self.assertIn('OTP has been sent', response.get('message'))

    def test_initiate_unknown_user(self, *mocks):
        response = initiate_password_reset('0000000000')
        self.assertEqual(response.get('status'), 400)

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
