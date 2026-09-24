"""
The password-reset request the web client actually sends.

THE DEFECT THIS PINS. The forgot-password screen (Dinify-Frontend,
``src/app/auth/forgot-password/forgot-password.component.ts``) posts
``users/auth/initiate-reset-password/`` with ``{identifier, identification}`` and
``users/auth/reset-password/`` with ``{identifier, otp}``. ``identifier`` is the typed
email, or ``256`` + the national number; ``identification`` is ``email`` or ``phone``.
Its "Resend code" button posts the same initiate request again. The view read
``request.data.get('phone_number')`` for both actions, so the web client's body reached
``_resolve_user(None)``, whose ``'@' in username`` raised ``TypeError``. Nothing caught
it, so every reset from the web app answered 500, by email and by phone alike.

The view now reads ``identifier``, falling back to ``phone_number`` so an older client
keeps working, and answers a request that names no one with a 400. ``identification``
stays advisory: ``_resolve_user`` already tells an email from a phone by the ``@``.
Every refusal ``_resolve_user`` makes (no such account, a platform-staff account, an
identity not yet claimed) still reaches the caller as the same ``NO_PHONE_NUMBER`` 400,
so the new field discloses nothing the old one did not.

Every request goes through the real URL, throttle and view, because the defect lived
between the request body and the controller, where a controller-level test cannot see.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
)
from users_app.models import User


INITIATE = '/api/v1/users/auth/initiate-reset-password/'
COMPLETE = '/api/v1/users/auth/reset-password/'
EMAIL = 'diner@example.com'
PHONE = '256772000101'

_PATCHES = (
    'misc_app.controllers.notifications.notification.Notification.create_notification',
    'notifications_app.controllers.messenger.Messenger.send_email',
    'users_app.controllers.otp_manager.send_sms',
)


@patch(_PATCHES[0], return_value=None)
@patch(_PATCHES[1], return_value=None)
@patch(_PATCHES[2], return_value=None)
class PasswordResetRequestShapeTests(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()  # both actions share one per-client throttle
        self.client = APIClient(raise_request_exception=False)
        self.account = User.objects.create_user(
            first_name='Reset', last_name='Me', email=EMAIL,
            phone_number=PHONE, username=PHONE, country='UG', password='password',
        )

    def _post(self, url, body):
        return self.client.post(url, body, format='json')

    def _assert_started(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['user_id'], str(self.account.id))

    def _assert_completed(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('token', response.json()['data'])
        self.assertIn('temp_password', response.json()['data'])

    def test_the_web_clients_email_request_starts_a_reset(self, *mocks):
        self._assert_started(self._post(INITIATE, {'identifier': EMAIL, 'identification': 'email'}))

    def test_the_web_clients_phone_request_starts_a_reset(self, *mocks):
        self._assert_started(self._post(INITIATE, {'identifier': PHONE, 'identification': 'phone'}))

    def test_the_web_clients_email_request_completes_the_reset(self, *mocks):
        self._assert_started(self._post(INITIATE, {'identifier': EMAIL, 'identification': 'email'}))
        # ENV=dev fixes the code at 1234.
        self._assert_completed(self._post(COMPLETE, {'identifier': EMAIL, 'otp': '1234'}))

    def test_the_web_clients_phone_request_completes_the_reset(self, *mocks):
        self._assert_started(self._post(INITIATE, {'identifier': PHONE, 'identification': 'phone'}))
        self._assert_completed(self._post(COMPLETE, {'identifier': PHONE, 'otp': '1234'}))

    def test_a_legacy_phone_number_body_still_works(self, *mocks):
        """CONTROL: the shape the view used to read, so an older client keeps working."""
        self._assert_started(self._post(INITIATE, {'phone_number': PHONE}))
        self._assert_completed(self._post(COMPLETE, {'phone_number': PHONE, 'otp': '1234'}))

    def test_the_identifier_wins_when_both_fields_are_sent(self, *mocks):
        self._assert_started(self._post(INITIATE, {
            'identifier': EMAIL, 'identification': 'email', 'phone_number': '256700009999',
        }))

    def test_a_request_naming_no_one_is_a_400_not_a_500(self, *mocks):
        for url in (INITIATE, COMPLETE):
            for body in (
                {},
                {'identification': 'email'},
                {'identifier': ''},
                {'identifier': '   '},
                {'identifier': None},
                {'identifier': 256772000101},  # a number, not the string the contract names
            ):
                with self.subTest(url=url, body=body):
                    cache.clear()
                    response = self._post(url, body)
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(
                        response.json()['message'], MESSAGES.get('NO_RESET_IDENTIFIER'),
                    )

    def test_every_refusal_is_still_the_same_generic_400(self, *mocks):
        """No such account, a platform-staff account and an unclaimed one look alike."""
        User.objects.create_user(
            username='ops', email='staff@example.com', password='password',
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        User.objects.create_user(
            first_name='Not', last_name='Claimed', email='owner@example.com',
            phone_number='256772000102', username='256772000102', country='UG',
            password='password',
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        bodies = (
            {'identifier': 'nobody@example.com', 'identification': 'email'},
            {'identifier': 'staff@example.com', 'identification': 'email'},
            {'identifier': '256772000102', 'identification': 'phone'},
        )
        answers = []
        for body in bodies:
            response = self._post(INITIATE, body)
            self.assertEqual(response.status_code, 400, (body, response.content))
            answers.append(response.json())
        self.assertEqual(answers[0], {'status': 400, 'message': MESSAGES.get('NO_PHONE_NUMBER')})
        self.assertEqual(answers[1], answers[0])
        self.assertEqual(answers[2], answers[0])

    def test_the_reset_is_still_throttled(self, *mocks):
        """CONTROL: five requests a minute per client, shared by both actions."""
        body = {'identifier': PHONE, 'identification': 'phone'}
        statuses = [self._post(INITIATE, body).status_code for _ in range(5)]
        statuses.append(self._post(COMPLETE, {'identifier': PHONE, 'otp': '1234'}).status_code)

        self.assertNotIn(429, statuses[:5])
        self.assertEqual(statuses[5], 429)
