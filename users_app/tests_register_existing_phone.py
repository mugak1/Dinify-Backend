"""
A phone number that already has an account is REFUSED, and the refusal names nobody
(REGISTER-EXISTING-PHONE-00).

``self_register``'s phone-already-exists branch returned the existing ``User``
INSTANCE under ``user_profile``, beside its id. Two routes reach that branch and both
hand the dict to a DRF ``Response``: ``POST users/auth/register/`` returns it as is,
and ``POST restaurant-setup/create-employee/`` returns it unchanged from
``create_employee``. Rendering a model instance as JSON raises, so the 400 the branch
meant to send never left the server and both routes answered 500. The only earlier
test of the branch (``users_app/tests.py``) calls the controller in process and never
renders the dict, which is why the suite stayed green.

The refusal now carries exactly ``status`` and ``message``. The account id is gone
too. The register route is anonymous, so the id told any caller which account holds a
number, and nothing read it: ``create_employee`` passes the dict through unchanged,
and neither the sign-up page nor the staff form reads either key on an error.

Every route test here goes through the test client, so the response is RENDERED, and
asserts on the decoded body. The client is built with
``raise_request_exception=False`` so that, on the unfixed code, the failure is an
assertion about a 500 rather than an exception escaping the test.

What is deliberately unchanged, and pinned as a control: the message itself, which
still tells an anonymous caller that a number has an account (that is how sign-up
works today); the email-exists refusal; the order of checks (a new number still
reaches the OTP check, and the staff route still asks for permission first); and a
successful registration or staff creation.
"""
import contextlib
import itertools
import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.utils.encoders import JSONEncoder
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configs import ROLES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from restaurants_app.controllers import create_employee as create_employee_module
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers import otp_manager
from users_app.controllers import self_register as self_register_module
from users_app.controllers.self_register import self_register
from users_app.models import OtpIssuance, User, UserOtp


REGISTER = '/api/v1/users/auth/register/'
RESEND = '/api/v1/users/auth/resend-otp/'
CREATE_EMPLOYEE = '/api/v1/restaurant-setup/create-employee/'

OWNER = ROLES.get('RESTAURANT_OWNER')
KITCHEN = ROLES.get('RESTAURANT_KITCHEN')

PASSWORD = 'Reg-Synthetic-Passw0rd!'
DEV_OTP = '1234'

PHONE_EXISTS = {'status': 400, 'message': MESSAGES['PHONE_NUMBER_EXISTS']}
EMAIL_EXISTS = {'status': 400, 'message': MESSAGES['EMAIL_EXISTS']}
REGISTERED = {'status': 200, 'message': MESSAGES['OK_SELF_REGISTER']}
NO_OTP = {'status': 400, 'message': 'Please provide the OTP.'}

# Synthetic numbers only, in a block no other test module draws from.
_PHONES = (f'2567012{n:05d}' for n in itertools.count(30001))


def _phone():
    return next(_PHONES)


def _spellings(canonical):
    """The same Ugandan number as a person might type it; all canonicalise alike."""
    national = canonical[3:]
    return (
        canonical,
        f'+256 {national[:3]} {national[3:6]} {national[6:]}',
        f'0{national}',
    )


def _user(tag, phone=None, email=None):
    phone = phone or _phone()
    return User.objects.create_user(
        first_name='Existing', last_name=tag.title(),
        email=email if email is not None else f'{tag}@reg-existing.test',
        phone_number=phone, username=phone, country='UG', password=PASSWORD,
    )


def _snapshot(user):
    user.refresh_from_db()
    return (user.first_name, user.last_name, user.email, user.phone_number,
            user.username, user.is_active, user.password, user.roles,
            user.customer_access_state, user.account_type)


class _InlineThread:
    """``otp_manager``'s dispatch thread, run inline so delivery stays inside the patches."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


@contextlib.contextmanager
def _delivery_intercepted():
    """Replace every channel a challenge can leave the process by."""
    with mock.patch.object(otp_manager, 'send_sms', mock.Mock(return_value=True)), \
            mock.patch('notifications_app.controllers.messenger.Messenger.send_email',
                       mock.Mock(return_value=True)), \
            mock.patch('misc_app.controllers.notifications.notification.Notification'
                       '.create_notification', return_value=None), \
            mock.patch.object(otp_manager, 'threading',
                              mock.Mock(Thread=_InlineThread)):
        yield


class _RouteCase(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)

    def post(self, url, body, user=None):
        headers = {}
        if user is not None:
            token = RefreshToken.for_user(user).access_token
            headers['Authorization'] = f'Bearer {token}'
        cache.clear()
        return self.client.post(url, body, format='json', headers=headers)

    def assertAnswer(self, response, expected):
        self.assertEqual(response.status_code, expected['status'],
                         response.content[:500])
        self.assertEqual(response.json(), expected)

    def assertNamesNobody(self, response, *users):
        body = response.json()
        self.assertNotIn('user_id', body)
        self.assertNotIn('user_profile', body)
        for user in users:
            for value in (str(user.pk), user.pk.hex, user.email):
                self.assertNotIn(value.encode(), response.content)


class RegisterRefusesAnExistingPhoneTests(_RouteCase):

    def setUp(self):
        super().setUp()
        self.existing = _user('holder')

    def body(self, phone, **extra):
        return {'first_name': 'New', 'last_name': 'Registrant',
                'email': 'new.registrant@reg-existing.test', 'phone_number': phone,
                'country': 'UG', 'password': PASSWORD, 'otp': DEV_OTP, **extra}

    def test_REGRESSION_an_existing_number_is_refused_not_a_server_error(self):
        for spelling in _spellings(self.existing.phone_number):
            with self.subTest(spelling=spelling):
                self.assertAnswer(self.post(REGISTER, self.body(spelling)), PHONE_EXISTS)

    def test_REGRESSION_the_refusal_names_no_account(self):
        response = self.post(REGISTER, self.body(self.existing.phone_number))
        self.assertAnswer(response, PHONE_EXISTS)
        self.assertNamesNobody(response, self.existing)

    def test_REGRESSION_nothing_is_verified_written_or_sent(self):
        before = (User.objects.count(), UserOtp.objects.count(),
                  OtpIssuance.objects.count())
        held_by = _snapshot(self.existing)
        with mock.patch.object(self_register_module, 'OtpManager') as otp, \
                mock.patch.object(self_register_module, 'Notification') as notification:
            response = self.post(REGISTER, self.body(self.existing.phone_number))
        self.assertAnswer(response, PHONE_EXISTS)
        otp.assert_not_called()
        notification.assert_not_called()
        self.assertEqual((User.objects.count(), UserOtp.objects.count(),
                          OtpIssuance.objects.count()), before)
        self.assertEqual(_snapshot(self.existing), held_by)
        self.assertTrue(self.existing.check_password(PASSWORD))

    def test_CONTROL_an_existing_email_is_still_refused_as_before(self):
        response = self.post(REGISTER, self.body(_phone(), email=self.existing.email))
        self.assertAnswer(response, EMAIL_EXISTS)

    def test_CONTROL_a_new_number_still_reaches_the_otp_check(self):
        body = self.body(_phone())
        del body['otp']
        self.assertAnswer(self.post(REGISTER, body), NO_OTP)

    def test_CONTROL_a_new_number_still_registers(self):
        phone = _phone()
        with _delivery_intercepted():
            seeded = self.post(RESEND, {'identification': 'msisdn', 'identifier': phone,
                                        'purpose': 'register'})
            self.assertEqual(seeded.status_code, 200, seeded.content)
            response = self.post(REGISTER, self.body(phone))
        self.assertAnswer(response, REGISTERED)
        self.assertTrue(User.objects.filter(phone_number=phone).exists())


class CreateEmployeeRefusesAnExistingPhoneTests(_RouteCase):

    def setUp(self):
        super().setUp()
        self.owner = _user('owner')
        self.restaurant = Restaurant.objects.create(
            name='Existing Phone Kitchen', location='Kampala',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[OWNER],
        )
        self.outsider = _user('outsider')

    def body(self, phone, restaurant=None):
        return {'first_name': 'New', 'last_name': 'Hire',
                'email': 'new.hire@reg-existing.test', 'phone_number': phone,
                'restaurant': str((restaurant or self.restaurant).pk),
                'roles': [KITCHEN]}

    def test_REGRESSION_an_existing_number_is_refused_not_a_server_error(self):
        for spelling in _spellings(self.outsider.phone_number):
            with self.subTest(spelling=spelling):
                response = self.post(CREATE_EMPLOYEE, self.body(spelling), self.owner)
                self.assertAnswer(response, PHONE_EXISTS)

    def test_REGRESSION_the_refusal_names_no_account(self):
        response = self.post(CREATE_EMPLOYEE, self.body(self.outsider.phone_number),
                             self.owner)
        self.assertAnswer(response, PHONE_EXISTS)
        self.assertNamesNobody(response, self.outsider)

    def test_REGRESSION_a_member_s_own_number_is_refused_the_same_way(self):
        response = self.post(CREATE_EMPLOYEE, self.body(self.owner.phone_number),
                             self.owner)
        self.assertAnswer(response, PHONE_EXISTS)
        self.assertNamesNobody(response, self.owner)

    def test_REGRESSION_nothing_is_written_or_sent(self):
        before = (User.objects.count(), RestaurantEmployee.objects.count())
        held_by = _snapshot(self.outsider)
        with mock.patch.object(self_register_module, 'Notification') as registered, \
                mock.patch.object(create_employee_module, 'Notification') as employed:
            response = self.post(CREATE_EMPLOYEE, self.body(self.outsider.phone_number),
                                 self.owner)
        self.assertAnswer(response, PHONE_EXISTS)
        registered.assert_not_called()
        employed.assert_not_called()
        self.assertEqual((User.objects.count(), RestaurantEmployee.objects.count()),
                         before)
        self.assertFalse(RestaurantEmployee.objects.filter(user=self.outsider).exists())
        self.assertEqual(_snapshot(self.outsider), held_by)

    def test_CONTROL_permission_is_still_asked_before_the_number(self):
        elsewhere = _user('elsewhere')
        their_restaurant = Restaurant.objects.create(
            name='Another Kitchen', location='Entebbe',
            status=RestaurantStatus_Live, owner=elsewhere,
        )
        RestaurantEmployee.objects.create(
            user=elsewhere, restaurant=their_restaurant, roles=[OWNER],
        )
        response = self.post(CREATE_EMPLOYEE, self.body(self.outsider.phone_number),
                             elsewhere)
        self.assertEqual(response.status_code, 403, response.content[:500])
        self.assertNotIn(MESSAGES['PHONE_NUMBER_EXISTS'].encode(), response.content)

    def test_CONTROL_a_new_number_still_creates_the_employee(self):
        phone = _phone()
        with mock.patch.object(self_register_module, 'Notification'), \
                mock.patch.object(create_employee_module, 'Notification'):
            response = self.post(CREATE_EMPLOYEE, self.body(phone), self.owner)
        self.assertEqual(response.status_code, 200, response.content[:500])
        hired = User.objects.get(phone_number=phone)
        self.assertTrue(RestaurantEmployee.objects.filter(
            user=hired, restaurant=self.restaurant, roles=[KITCHEN]).exists())


class ControllerRefusalContractTests(TestCase):
    """The dict the branch returns is the dict a route renders, so it must render."""

    def setUp(self):
        self.existing = _user('controller')

    def test_REGRESSION_the_refusal_is_two_keys_and_renders_as_json(self):
        data = {'first_name': 'New', 'last_name': 'Person', 'country': 'UG',
                'phone_number': self.existing.phone_number, 'password': PASSWORD}
        for kwargs in ({}, {'return_user_id': True, 'send_credentials': True,
                            'skip_otp': True}):
            with self.subTest(**kwargs):
                refusal = self_register(data, **kwargs)
                self.assertEqual(refusal, PHONE_EXISTS)
                self.assertEqual(json.loads(json.dumps(refusal, cls=JSONEncoder)),
                                 PHONE_EXISTS)
