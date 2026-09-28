"""
An email address typed in different capitals still names its account.

THE DEFECT THIS PINS. ``login()`` asked whether any account held the address
LOWER-CASED, then fetched the account by the address AS TYPED. Registration
(``self_register``) and admin onboarding (``onboarding_creation``) store emails
lower-cased, so ``Diner@Example.com`` passed the check and the fetch raised
``User.DoesNotExist``. ``users_app/endpoints/auth.py`` calls ``login()`` with no handler
around it, so the endpoint answered 500. ``reset_password._resolve_user`` fetched by the
address as typed and nothing else, so the same address could not start a password
reset: a clean 400, but for an account that exists.

Both now resolve through ``get_user_by_email``, which answers exactly as
``User.objects.get(email=...)`` did except in ONE case: the address as typed names no
account and its lower-cased form names exactly one. Two things around that case are
pinned as CONTROLS, because the obvious fixes get them wrong:

- a profile edit (``self_update_user_profile``) stores an email as typed, so an account
  can hold ``Diner@Example.com`` beside another holding ``diner@example.com``. Lower-
  casing every lookup would send the first account's owner to the second account.
- ``User.email`` is not unique. An address several accounts share is refused exactly as
  it was before — this change does not fix that, and must not make it worse.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from dinify_backend.configss.messages import MESSAGES
from users_app.controllers.login import login
from users_app.controllers.otp_manager import OtpManager
from users_app.controllers.reset_password import initiate_password_reset, reset_password
from users_app.controllers.update_user_profile import self_update_user_profile
from users_app.models import User


PASSWORD = 'correct-horse'
OTHER_PASSWORD = 'battery-staple'

_PATCHES = (
    'misc_app.controllers.notifications.notification.Notification.create_notification',
    'notifications_app.controllers.messenger.Messenger.send_email',
    'users_app.controllers.otp_manager.send_sms',
)


def _account(phone, email, password=PASSWORD):
    """An account created the way registration creates one: email lower-cased."""
    return User.objects.create_user(
        first_name='Email', last_name='Case', email=email,
        phone_number=phone, username=phone, country='UG',
        password=password, prompt_password_change=False,
    )


def _twin_accounts(test):
    """
    ``diner@example.com`` and ``Diner@Example.com`` held by two different accounts.

    The second is set through the profile edit, which stores what it is given and
    whose duplicate check is exact, so the state is reachable through the product.
    """
    lower = _account('256772000201', 'diner@example.com', password=PASSWORD)
    mixed = _account('256772000202', 'other@example.com', password=OTHER_PASSWORD)
    response = self_update_user_profile(user_id=str(mixed.id), email='Diner@Example.com')
    test.assertEqual(response['status'], 200, response)
    mixed.refresh_from_db()
    test.assertEqual(mixed.email, 'Diner@Example.com')  # the premise
    return lower, mixed


@patch(_PATCHES[0], return_value=None)
@patch(_PATCHES[1], return_value=None)
@patch(_PATCHES[2], return_value=None)
class LoginEmailCaseTests(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()  # the login endpoint is throttled per client
        self.diner = _account('256772000101', 'diner@example.com')

    def test_a_mixed_case_email_logs_in_as_the_lower_case_one_does(self, *mocks):
        lower = login('diner@example.com', PASSWORD)
        mixed = login('Diner@Example.com', PASSWORD)

        self.assertEqual(lower['status'], 200, lower)
        self.assertEqual(mixed['status'], 200, mixed)
        self.assertEqual(mixed['data']['profile']['id'], str(self.diner.id))

    def test_the_login_endpoint_answers_a_mixed_case_email(self, *mocks):
        client = APIClient(raise_request_exception=False)

        response = client.post(
            '/api/v1/users/auth/login/',
            {'username': 'Diner@Example.com', 'password': PASSWORD},
            format='json',
        )

        self.assertEqual(response.status_code, 200)

    def test_a_wrong_password_on_a_mixed_case_email_is_refused_cleanly(self, *mocks):
        response = login('Diner@Example.com', 'not-the-password')

        self.assertEqual(response['status'], 401)
        self.assertEqual(response['message'], MESSAGES.get('WRONG_PASSWORD'))

    def test_a_phone_number_username_is_unchanged(self, *mocks):
        """CONTROL: the phone-number path never touches the email lookup."""
        self.assertEqual(login('256772000101', PASSWORD)['status'], 200)

        wrong = login('256772000101', 'not-the-password')
        self.assertEqual(wrong['status'], 401)
        self.assertEqual(wrong['message'], MESSAGES.get('WRONG_PASSWORD'))

        # D11 B1: an unknown identity gets the SAME refusal as a wrong password, so
        # the answer no longer says whether the number holds an account.
        unknown = login('256772000999', PASSWORD)
        self.assertEqual(unknown, wrong)

    def test_an_address_typed_exactly_still_logs_into_its_own_account(self, *mocks):
        """CONTROL: lower-casing every lookup would log this owner into the twin."""
        User.objects.filter(pk=self.diner.pk).delete()
        lower, mixed = _twin_accounts(self)

        as_typed = login('Diner@Example.com', OTHER_PASSWORD)
        self.assertEqual(as_typed['status'], 200, as_typed)
        self.assertEqual(as_typed['data']['profile']['id'], str(mixed.id))

        lower_case = login('diner@example.com', PASSWORD)
        self.assertEqual(lower_case['status'], 200, lower_case)
        self.assertEqual(lower_case['data']['profile']['id'], str(lower.id))

    def test_a_shared_address_is_refused_exactly_as_before(self, *mocks):
        """
        CONTROL, RECORDED NOT ENDORSED: an address two accounts share.

        Both casings raise what they raised before this change, and both still reach
        the endpoint as a 500. Fixing that is out of scope; this pins that the
        lower-cased fallback did not turn a missing account into an ambiguous one.
        """
        _account('256772000102', 'diner@example.com')

        with self.assertRaises(User.MultipleObjectsReturned):
            login('diner@example.com', PASSWORD)
        with self.assertRaises(User.DoesNotExist):
            login('Diner@Example.com', PASSWORD)


@patch(_PATCHES[0], return_value=None)
@patch(_PATCHES[1], return_value=None)
@patch(_PATCHES[2], return_value=None)
class PasswordResetEmailCaseTests(TestCase):

    def setUp(self):
        super().setUp()
        self.diner = _account('256772000101', 'diner@example.com')

    def test_a_mixed_case_email_can_reset_the_password(self, *mocks):
        started = initiate_password_reset('Diner@Example.com')
        self.assertEqual(started['status'], 200, started)
        self.assertEqual(started['data']['user_id'], str(self.diner.id))

        OtpManager().make_otp(user=self.diner, purpose='reset-password')
        finished = reset_password('Diner@Example.com', '1234')  # ENV=dev fixes the code
        self.assertEqual(finished['status'], 200, finished)

    def test_a_phone_number_reset_is_unchanged(self, *mocks):
        """CONTROL."""
        started = initiate_password_reset('256772000101')
        self.assertEqual(started['status'], 200, started)
        self.assertEqual(started['data']['user_id'], str(self.diner.id))

    def test_an_address_typed_exactly_still_resets_its_own_account(self, *mocks):
        """CONTROL: lower-casing every lookup would reset the twin's password instead."""
        User.objects.filter(pk=self.diner.pk).delete()
        lower, mixed = _twin_accounts(self)

        self.assertEqual(
            initiate_password_reset('Diner@Example.com')['data']['user_id'], str(mixed.id),
        )
        self.assertEqual(
            initiate_password_reset('diner@example.com')['data']['user_id'], str(lower.id),
        )

    def test_a_shared_address_is_refused_exactly_as_before(self, *mocks):
        """
        CONTROL, RECORDED NOT ENDORSED: an address two accounts share.

        Typed exactly it raises ``MultipleObjectsReturned``, as it always has. Typed in
        other capitals it is still the clean "no such account" 400 — a fallback that
        simply fetched the lower-cased address would have turned it into a 500.
        """
        _account('256772000102', 'diner@example.com')

        with self.assertRaises(User.MultipleObjectsReturned):
            initiate_password_reset('diner@example.com')

        refused = initiate_password_reset('Diner@Example.com')
        self.assertEqual(refused['status'], 400)
        self.assertEqual(refused['message'], MESSAGES.get('NO_PHONE_NUMBER'))
