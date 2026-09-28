"""
D11 Stage B1 — what an anonymous caller of the customer auth routes may learn, and
what the OTP delivery path may write to a log.

Three properties, each driven through the REAL endpoint or service path:

1. UNIFORM REFUSAL BEFORE AUTHENTICATION. When ``authenticate()`` resolves no user,
   ``POST auth/login/`` answers with ONE envelope — the existing wrong-password one —
   whether the identity is unknown, the password is wrong, or the account is
   inactive, by phone or by email in any capitalisation. This pins a BODY/STATUS
   property only. It is not a claim of timing indistinguishability, and it does not
   close the reset, registration or lookup disclosures, which are separate D11 work.

2. A RESEND CANNOT RENEW ITS OWN PASSWORD PROOF. ``resend-otp`` with
   ``purpose='login'`` requires the same user's PASSWORD-CREATED login challenge
   (``msisdn IS NULL``) inside the existing five-minute window. A row created by a
   resend stores the phone and must not qualify, so one genuine login can no longer
   keep the window open for ever. The clock is simulated by back-dating rows in the
   test database.

3. THE OTP DELIVERY CATCHES DO NOT RE-LOG A SECRET-BEARING EXCEPTION. The sender-side
   half (SMS and Messenger) is pinned in ``notifications_app/tests_sms.py``.
"""
import datetime
import logging
import re
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

LOGIN_URL = '/api/v1/users/auth/login/'
RESEND_URL = '/api/v1/users/auth/resend-otp/'
VERIFY_URL = '/api/v1/users/auth/verify-otp/'
PASSWORD = 'Discovery-Pass-4417'

# The ONE refusal every pre-authentication failure must produce: the existing
# wrong-password envelope, reused as is.
REFUSAL = {'status': 401, 'message': MESSAGES.get('WRONG_PASSWORD')}

_PATCH_SEND_SMS = 'users_app.controllers.otp_manager.send_sms'
_PATCH_SEND_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_OTP_CONFIG = 'users_app.controllers.otp_manager.config'


def _env(value):
    def _cfg(key, **kwargs):
        if key == 'ENV':
            return value
        return kwargs.get('default')
    return _cfg


def _user(phone, email=None, **extra):
    return User.objects.create_user(
        first_name='Disc', last_name='Overy', email=email,
        phone_number=phone, username=phone, country='UG',
        password=PASSWORD, roles=[], **extra,
    )


def _owner(phone, email=None):
    from restaurants_app.models import Restaurant, RestaurantEmployee
    user = _user(phone, email)
    restaurant = Restaurant.objects.create(
        name=f'Discovery {phone}', location='loc',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER],
    )
    return user


class _Api(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def post(self, url, body):
        # Each call starts from an empty throttle cache: these tests are about what
        # the answer SAYS, never about the rate limit.
        cache.clear()
        return self.client.post(url, body, format='json')


@mock.patch(_PATCH_SEND_EMAIL, return_value=True)
@mock.patch(_PATCH_SEND_SMS, return_value=True)
class UniformLoginRefusalTests(_Api):
    """Property 1, through the real endpoint."""

    def setUp(self):
        super().setUp()
        self.active = _user('256701230001', email='known.person@example.test')
        self.inactive = _user('256701230002', email='gone.person@example.test')
        self.inactive.is_active = False
        self.inactive.save()
        # A privileged account: a correct password would issue a login OTP, so any
        # refusal leaking into the OTP path is observable.
        self.owner = _owner('256701230003', email='owner.disc@example.test')

    def assertRefused(self, username, password):
        before_otps = UserOtp.objects.count()
        response = self.post(LOGIN_URL, {'username': username, 'password': password})
        self.assertEqual(response.status_code, 401, username)
        self.assertEqual(response.json(), REFUSAL, username)
        self.assertEqual(UserOtp.objects.count(), before_otps, 'no OTP may be issued')
        return response

    def test_every_pre_authentication_failure_is_the_same_envelope(self, sms, email):
        cases = {
            'unknown phone': ('256701239999', PASSWORD),
            'unknown email': ('nobody.here@example.test', PASSWORD),
            'unknown email, other case': ('NOBODY.HERE@EXAMPLE.TEST', PASSWORD),
            'wrong password, phone': (self.active.phone_number, 'not-the-password'),
            'wrong password, email as stored': (self.active.email, 'not-the-password'),
            'wrong password, email lower-cased': (self.active.email.lower(), 'not-the-password'),
            'wrong password, email upper-cased': (self.active.email.upper(), 'not-the-password'),
            'wrong password, owner phone': (self.owner.phone_number, 'not-the-password'),
            'inactive, correct password, phone': (self.inactive.phone_number, PASSWORD),
            'inactive, correct password, email': (self.inactive.email, PASSWORD),
            'inactive, wrong password, phone': (self.inactive.phone_number, 'nope-nope'),
        }
        bodies = {}
        for label, (username, password) in cases.items():
            with self.subTest(label):
                bodies[label] = self.assertRefused(username, password).content
        # Byte-identical, not merely equal after parsing.
        self.assertEqual(len(set(bodies.values())), 1, bodies)
        sms.assert_not_called()
        email.assert_not_called()

    def test_the_refusal_carries_no_token_profile_or_user_id(self, *mocks):
        for username in ('256701239999', self.inactive.phone_number, self.active.phone_number):
            with self.subTest(username):
                body = self.post(
                    LOGIN_URL, {'username': username, 'password': 'wrong-password'},
                ).json()
                self.assertNotIn('data', body)

    # --- controls that must NOT change -----------------------------------------

    def test_control_successful_login_still_returns_tokens_and_profile(self, *mocks):
        body = self.post(
            LOGIN_URL, {'username': self.active.phone_number, 'password': PASSWORD},
        ).json()
        self.assertEqual(body['status'], 200)
        self.assertFalse(body['data']['require_otp'])
        for key in ('token', 'refresh', 'profile', 'prompt_password_change'):
            self.assertIn(key, body['data'])

    def test_control_email_alias_still_signs_in(self, *mocks):
        body = self.post(
            LOGIN_URL, {'username': self.active.email.upper(), 'password': PASSWORD},
        ).json()
        self.assertEqual(body['status'], 200)

    def test_control_diner_source_still_skips_the_otp(self, sms, email):
        body = self.post(LOGIN_URL, {
            'username': self.owner.phone_number, 'password': PASSWORD, 'source': 'diner',
        }).json()
        self.assertEqual(body['status'], 200)
        self.assertIn('token', body['data'])
        sms.assert_not_called()

    def test_control_privileged_login_still_issues_otp_and_no_token(self, sms, email):
        with mock.patch(_PATCH_OTP_CONFIG, side_effect=_env('test')):
            body = self.post(
                LOGIN_URL, {'username': self.owner.phone_number, 'password': PASSWORD},
            ).json()
        self.assertEqual(body['status'], 200)
        self.assertTrue(body['data']['require_otp'])
        self.assertEqual(body['data']['user_id'], str(self.owner.id))
        self.assertNotIn('token', body['data'])
        self.assertEqual(sms.call_count, 1)

    def test_control_delivery_failure_still_withholds_the_token(self, sms, email):
        sms.return_value = False
        email.return_value = False
        with mock.patch(_PATCH_OTP_CONFIG, side_effect=_env('prod')):
            response = self.post(
                LOGIN_URL, {'username': self.owner.phone_number, 'password': PASSWORD},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('data', response.json())

    def test_control_platform_staff_and_pending_are_the_same_envelope(self, *mocks):
        _user('256701230004', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        _user('256701230005', customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        for phone in ('256701230004', '256701230005'):
            with self.subTest(phone):
                self.assertRefused(phone, PASSWORD)


def _age(queryset, minutes):
    queryset.update(time_created=timezone.now() - datetime.timedelta(minutes=minutes))


@mock.patch(_PATCH_SEND_EMAIL, return_value=True)
@mock.patch(_PATCH_OTP_CONFIG, side_effect=_env('test'))
class LoginResendAnchorTests(_Api):
    """Property 2, through the real login, resend and verify endpoints."""

    def setUp(self):
        super().setUp()
        self.owner = _owner('256701240001')
        self.sent = []
        patcher = mock.patch(_PATCH_SEND_SMS, side_effect=self._record)
        self.sms = patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, message, msisdn, **kwargs):
        self.sent.append(message)
        return True

    def last_code(self):
        return re.search(r'(\d{4})', self.sent[-1]).group(1)

    def login(self):
        body = self.post(
            LOGIN_URL, {'username': self.owner.phone_number, 'password': PASSWORD},
        ).json()
        self.assertTrue(body['data']['require_otp'])

    def resend(self, identification='id', identifier=None):
        return self.post(RESEND_URL, {
            'identification': identification,
            'identifier': identifier or str(self.owner.id),
            'purpose': 'login',
        })

    def anchor(self):
        return UserOtp.objects.filter(user=self.owner, purpose='login', msisdn__isnull=True)

    def resend_rows(self):
        return UserOtp.objects.filter(
            user=self.owner, purpose='login', msisdn=self.owner.phone_number,
        )

    def test_a_four_minute_old_password_challenge_permits_a_resend(self, *mocks):
        self.login()
        _age(self.anchor(), 4)
        sends = len(self.sent)
        response = self.resend()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.sent), sends + 1)

    def test_THE_REGRESSION_a_resend_row_cannot_renew_an_expired_proof(self, *mocks):
        self.login()
        _age(self.anchor(), 4)
        self.assertEqual(self.resend().status_code, 200)
        # The password proof is now outside the window; only the resend's own row
        # is recent. Before B1 that row satisfied the gate.
        _age(self.anchor(), 10)
        _age(self.resend_rows(), 4)
        rows, sends = UserOtp.objects.count(), len(self.sent)
        response = self.resend()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.sent), sends, 'a refused resend must deliver nothing')
        self.assertEqual(UserOtp.objects.count(), rows, 'and must create no challenge')

    def test_THE_REGRESSION_by_msisdn_identification_too(self, *mocks):
        self.login()
        _age(self.anchor(), 10)
        UserOtp.objects.create(
            user=self.owner, msisdn=self.owner.phone_number, purpose='login',
            otp_hash='x', salt='y', identifier=f'user:{self.owner.id}',
        )
        response = self.resend(identification='msisdn', identifier=self.owner.phone_number)
        self.assertEqual(response.status_code, 400)

    def test_no_password_challenge_at_all_refuses(self, *mocks):
        # A recent resend-shaped row with no password-created challenge beside it.
        UserOtp.objects.create(
            user=self.owner, msisdn=self.owner.phone_number, purpose='login',
            otp_hash='x', salt='y', identifier=f'user:{self.owner.id}',
        )
        self.assertEqual(self.resend().status_code, 400)
        self.assertEqual(self.sent, [])

    def test_resends_do_not_refresh_the_anchor(self, *mocks):
        self.login()
        _age(self.anchor(), 2)
        anchored_at = self.anchor().get().time_created
        for _ in range(3):
            self.assertEqual(self.resend().status_code, 200)
        self.assertEqual(self.anchor().count(), 1)
        self.assertEqual(self.anchor().get().time_created, anchored_at)

    def test_a_fresh_genuine_login_restores_resend_and_verification(self, *mocks):
        self.login()
        _age(self.anchor(), 10)
        _age(self.resend_rows(), 10)
        self.assertEqual(self.resend().status_code, 400)

        self.login()   # a new password proof
        self.assertEqual(self.resend().status_code, 200)
        verified = self.post(
            VERIFY_URL, {'user': str(self.owner.id), 'otp': self.last_code()},
        ).json()
        self.assertTrue(verified['data']['valid'])
        self.assertIn('token', verified['data'])

    def test_an_account_without_a_phone_cannot_renew_through_the_null_key(self, *mocks):
        # Its resend row would also store `msisdn IS NULL` and replace the anchor
        # through make_otp's `(user, None)` key — renewing the proof. Refused instead.
        phoneless = User.objects.create_user(
            first_name='No', last_name='Phone', email='no.phone@example.test',
            username='no-phone-user', country='UG', password=PASSWORD, roles=[],
        )
        OtpManager().make_otp(user=phoneless, purpose='login')   # the password proof
        response = self.post(RESEND_URL, {
            'identification': 'id', 'identifier': str(phoneless.id), 'purpose': 'login',
        })
        self.assertEqual(response.status_code, 400)

    def test_control_non_login_purposes_are_unchanged(self, *mocks):
        # `register` has never needed a login proof and still does not.
        response = self.post(RESEND_URL, {
            'identification': 'id', 'identifier': str(self.owner.id), 'purpose': 'register',
        })
        self.assertEqual(response.status_code, 200)

    def test_control_the_existing_refusal_wording_is_kept(self, *mocks):
        body = self.resend().json()
        self.assertEqual(body['status'], 400)
        self.assertEqual(
            body['message'],
            'Please provide your username and password again to get a login OTP',
        )


# --- property 3: the OTP manager's delivery catches -------------------------------

CANARY_EXC = 'EXC-CANARY-https://gw.invalid/sendsms?password=PW-CANARY&destinations=256700111222'


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def everything(self):
        """Every string a log pipeline could ever see from these records."""
        standard = set(vars(logging.LogRecord('x', 0, 'x', 0, 'x', None, None)))
        formatter = logging.Formatter('%(message)s')
        parts = []
        for r in self.records:
            parts += [str(r.msg), repr(r.args), r.getMessage(), formatter.format(r)]
            if r.exc_info:
                parts.append(formatter.formatException(r.exc_info))
            parts += [repr(v) for k, v in vars(r).items() if k not in standard]
            parts += [str(r.exc_text or ''), str(r.stack_info or '')]
        return '\n'.join(parts)


class _InlineThread:
    """Runs a would-be background delivery thread synchronously."""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class OtpDeliveryCatchSanitizationTests(TestCase):

    def setUp(self):
        self.user = _user('256701250001', email='catch.canary@example.test')
        self.capture = _Capture()
        log = logging.getLogger('users_app.controllers.otp_manager')
        log.addHandler(self.capture)
        self.addCleanup(log.removeHandler, self.capture)

    def assertNoCanary(self):
        text = self.capture.everything()
        for canary in ('EXC-CANARY', 'PW-CANARY', '256700111222', 'gw.invalid'):
            self.assertNotIn(canary, text)

    def test_dev_thread_catches_do_not_log_the_exception(self):
        with mock.patch(_PATCH_OTP_CONFIG, side_effect=_env('dev')), \
                mock.patch('users_app.controllers.otp_manager.threading.Thread', _InlineThread), \
                mock.patch(_PATCH_SEND_SMS, side_effect=RuntimeError(CANARY_EXC)), \
                mock.patch(_PATCH_SEND_EMAIL, side_effect=RuntimeError(CANARY_EXC)):
            self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
        self.assertNoCanary()
        messages = [r.getMessage() for r in self.capture.records]
        self.assertTrue(any('OTP SMS' in m for m in messages), messages)
        self.assertTrue(any('OTP email' in m for m in messages), messages)

    def test_test_env_email_catches_do_not_log_the_exception(self):
        with mock.patch(_PATCH_OTP_CONFIG, side_effect=_env('test')), \
                mock.patch('users_app.controllers.otp_manager.threading.Thread', _InlineThread), \
                mock.patch(_PATCH_SEND_EMAIL, side_effect=RuntimeError(CANARY_EXC)):
            # SMS accepted: the async email catch runs.
            with mock.patch(_PATCH_SEND_SMS, return_value=True):
                self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
            # SMS failed: the synchronous email fallback catch runs, and still
            # reports the truth (False).
            with mock.patch(_PATCH_SEND_SMS, return_value=False):
                self.assertFalse(OtpManager().make_otp(user=self.user, purpose='login'))
        self.assertNoCanary()
        self.assertTrue(self.capture.records)
