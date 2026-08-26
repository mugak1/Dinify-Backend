"""
THE PRE-CLAIM CUSTOMER-ACCESS GATE (Step 2D.1).

Step 2D creates an owner with an unusable password and hands the operator an
``OwnerInvitation`` as the account-claim credential. The architecture said customer
access does not exist until that invitation is redeemed; nothing enforced it. Generic
password reset needed only the owner's phone number:

    initiate-reset-password(phone) -> OTP -> reset-password(phone, otp)
        -> set_password() -> RefreshToken.for_user() -> a customer session

leaving the platform asserting ``owner_control: not_established`` and ``invitation:
pending`` about an account that was already exercising owner authority.

AN UNUSABLE PASSWORD WAS NEVER THE INVARIANT — it is what password reset exists to
replace. So the suite is organised around the doors rather than around the password:
login, password reset, OTP, token presentation, refresh, and the structural rule that
keeps the next direct mint from reopening it.

TWO PROPERTIES ARE ADVERSARIAL AND CARRY THE MOST WEIGHT:

* a pending identity whose password is FORCED USABLE is still refused everywhere —
  the gate does not rest on password state;
* an ESTABLISHED user with a pending invitation for a second restaurant is untouched
  — the gate is identity-scoped, and invitation state stays restaurant evidence.
"""
import ast
import os
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
)
from users_app import customer_access
from users_app.controllers.login import login
from users_app.controllers.otp_manager import OtpManager
from users_app.controllers.reset_password import (
    _resolve_user, initiate_password_reset, reset_password,
)
from users_app.models import User, UserOtp

REPO_ROOT = Path(__file__).resolve().parent.parent

PROFILE_URL = '/api/v1/users/user-profile/'
REFRESH_URL = '/api/v1/users/auth/token/refresh/'

# External I/O the OTP path touches, patched exactly as users_app.tests does.
_PATCH_SMS = 'users_app.controllers.otp_manager.send_sms'
_PATCH_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_NOTIFICATION = (
    'misc_app.controllers.notifications.notification.Notification.create_notification'
)

# ENV is 'dev' under test settings, so make_otp hardcodes this. That is the point of
# the dev-OTP tests below: the gate must not depend on the code being secret.
DEV_OTP = '1234'

_PHONE = iter(f'25670980{n:05d}' for n in range(1, 9999))


def make_user(*, state=CUSTOMER_ACCESS_ESTABLISHED, password='password', **extra):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='Case', last_name='User', email=f'{phone}@t.com',
        phone_number=phone, username=phone, country='Uganda',
        password=password, roles=[], customer_access_state=state, **extra,
    )


def make_pending_user(**kwargs):
    """
    A Step-2D new owner as the creation service leaves it: pending, unusable password.

    Built with the same three independent facts rather than by calling the Admin
    endpoint, so this suite tests the GATE rather than the creation flow — the
    creation flow's own tests pin that it produces exactly this shape.
    """
    user = make_user(state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM, **kwargs)
    user.set_unusable_password()
    user.save(update_fields=['password'])
    return user


def force_usable_password(user, password='forced-password'):
    """
    THE ADVERSARIAL SETUP: give a pending identity a working password anyway.

    Written straight to the column, bypassing every gated path, to model the thing
    the gate must survive — a future operation, or a bug, that establishes a password
    before claim. If the gate rested on password usability this would defeat it.
    """
    user.set_password(password)
    user.save(update_fields=['password'])
    return password


# --- §27 the field, the default, the vocabulary ------------------------------

class CustomerAccessFieldTests(TestCase):
    """The new identity fact, and the things it must never be derived from."""

    def test_an_ordinary_new_user_defaults_to_established(self):
        user = User.objects.create_user(
            first_name='A', last_name='B', email='ordinary@t.com',
            phone_number='256700111001', username='256700111001',
            country='Uganda', password='password',
        )
        self.assertEqual(user.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)

    def test_self_registration_produces_an_established_identity(self):
        from users_app.controllers.self_register import self_register

        with patch(_PATCH_NOTIFICATION):
            result = self_register(
                data={
                    'first_name': 'Self', 'last_name': 'Registered',
                    'email': 'self.registered@t.com',
                    'phone_number': '0772333444', 'country': 'UG',
                    'password': 'password',
                },
                skip_otp=True, return_user_id=True,
            )
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(
            User.objects.get(pk=result['user_id']).customer_access_state,
            CUSTOMER_ACCESS_ESTABLISHED,
        )

    def test_the_database_default_is_established(self):
        """
        The rollback property, asserted against the DATABASE rather than Django.

        An INSERT that never names the column — which is exactly what OLD code does
        after a rollback onto this schema — must succeed and land on `established`.
        Django drops the DB default after an ordinary ``AddField``; migration 0014
        sets ``db_default`` precisely so this holds.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO users '
                '(id, password, is_superuser, username, is_staff, is_active, '
                ' date_joined, roles, prompt_password_change, account_type, '
                ' first_name, last_name) '
                "VALUES (%s, '', false, %s, false, true, NOW(), '[]', true, "
                "'restaurant_user', 'Old', 'Code')",
                ['11111111-1111-1111-1111-111111111111', '256700111099'],
            )
            cursor.execute(
                'SELECT customer_access_state FROM users WHERE username = %s',
                ['256700111099'],
            )
            self.assertEqual(cursor.fetchone()[0], CUSTOMER_ACCESS_ESTABLISHED)

    def test_the_vocabulary_is_a_database_constraint(self):
        user = make_user()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.filter(pk=user.pk).update(
                    customer_access_state='whatever-i-like',
                )

    def test_state_is_not_derived_from_password_usability(self):
        """Both directions. Password state and access state are different questions."""
        pending = make_pending_user()
        self.assertFalse(pending.has_usable_password())
        force_usable_password(pending)
        pending.refresh_from_db()
        self.assertTrue(pending.has_usable_password())
        self.assertEqual(
            pending.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
            'a usable password must not promote a pending identity',
        )

        established = make_user()
        established.set_unusable_password()
        established.save(update_fields=['password'])
        established.refresh_from_db()
        self.assertEqual(
            established.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
            'an unusable password must not demote an established identity',
        )

    def test_state_is_not_is_active(self):
        """A pending identity is NOT a deactivated one — each axis says one thing."""
        pending = make_pending_user()
        self.assertTrue(pending.is_active)

    def test_state_is_not_prompt_password_change(self):
        """That flag defaults True for every account, so it distinguishes nothing."""
        established = make_user()
        pending = make_pending_user()
        self.assertEqual(
            established.prompt_password_change, pending.prompt_password_change,
        )


class CustomerAccessPolicyTests(TestCase):
    """The helper answers one axis and fails closed."""

    def test_established_is_established(self):
        self.assertTrue(customer_access.is_established(make_user()))
        self.assertFalse(customer_access.is_refused(make_user()))

    def test_pending_is_refused(self):
        pending = make_pending_user()
        self.assertFalse(customer_access.is_established(pending))
        self.assertTrue(customer_access.is_refused(pending))

    def test_it_fails_closed_on_anything_unrecognised(self):
        from django.contrib.auth.models import AnonymousUser

        for subject in (None, AnonymousUser(), object(), 'a string'):
            with self.subTest(subject=type(subject).__name__):
                self.assertTrue(customer_access.is_refused(subject))

    def test_it_does_not_absorb_the_neighbouring_questions(self):
        """
        Deliberately narrow: a deactivated or platform-staff account is still
        ESTABLISHED on this axis. Those refusals belong to their own existing gates,
        and folding them in here is how a helper's meaning starts to drift.
        """
        deactivated = make_user()
        User.objects.filter(pk=deactivated.pk).update(is_active=False)
        deactivated.refresh_from_db()
        self.assertTrue(customer_access.is_established(deactivated))

        staff = make_user()
        User.objects.filter(pk=staff.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        staff.refresh_from_db()
        self.assertTrue(customer_access.is_established(staff))

    def test_the_mint_chokepoint_refuses_a_pending_identity(self):
        with self.assertRaises(customer_access.CustomerAccessRefused):
            customer_access.issue_customer_tokens(make_pending_user())

    def test_the_mint_chokepoint_serves_an_established_identity(self):
        token = customer_access.issue_customer_tokens(make_user())
        self.assertTrue(str(token.access_token))


# --- §28 login ---------------------------------------------------------------

@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class PendingLoginRefusedTests(TestCase):
    """A pre-claim identity gets no session, no OTP and leaves no trace."""

    def setUp(self):
        self.user = make_pending_user()

    def test_unusable_password_is_refused_as_before(self, *mocks):
        response = login(self.user.username, 'anything')
        self.assertEqual(response['status'], 401)

    def test_a_forced_usable_password_is_STILL_refused(self, *mocks):
        """
        The headline test. ``authenticate()`` succeeds here — so the refusal can only
        be coming from the identity gate, which is exactly the invariant an unusable
        password could not provide.
        """
        password = force_usable_password(self.user)
        response = login(self.user.username, password)
        self.assertEqual(response['status'], 401)
        self.assertEqual(response['message'], MESSAGES.get('WRONG_PASSWORD'))
        self.assertNotIn('data', response)

    def test_the_refusal_mints_no_customer_token(self, *mocks):
        password = force_usable_password(self.user)
        response = login(self.user.username, password)
        self.assertNotIn('token', response.get('data', {}))
        self.assertNotIn('refresh', response.get('data', {}))

    def test_the_refusal_issues_no_login_otp(self, *mocks):
        force_usable_password(self.user)
        login(self.user.username, 'forced-password')
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_the_refusal_does_not_move_last_login(self, *mocks):
        force_usable_password(self.user)
        login(self.user.username, 'forced-password')
        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_login)

    def test_the_refusal_does_not_change_the_access_state(self, *mocks):
        force_usable_password(self.user)
        login(self.user.username, 'forced-password')
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_a_privileged_pending_owner_still_gets_nothing(self, *mocks):
        """
        The gate sits ABOVE the role traversal, so an owner membership — the thing
        that would normally escalate login to an OTP — cannot route around it.
        """
        from restaurants_app.models import Restaurant, RestaurantEmployee
        from dinify_backend.configss.string_definitions import (
            RESTAURANT_OWNER, RestaurantStatus_Live,
        )

        restaurant = Restaurant.objects.create(
            name='Pending Owner House', location='Kololo',
            status=RestaurantStatus_Live, owner=self.user,
        )
        RestaurantEmployee.objects.create(
            user=self.user, restaurant=restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        force_usable_password(self.user)
        response = login(self.user.username, 'forced-password')
        self.assertEqual(response['status'], 401)
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_the_refusal_is_not_an_account_state_oracle(self, *mocks):
        """A pending identity, a wrong password and an unknown user look alike."""
        force_usable_password(self.user)
        pending = login(self.user.username, 'forced-password')
        wrong = login(make_user().username, 'not-the-password')
        self.assertEqual(pending, wrong)


@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class EstablishedLoginUnchangedTests(TestCase):
    """§15 — the existing corpus must not regress."""

    def test_an_ordinary_login_still_returns_tokens(self, *mocks):
        user = make_user()
        response = login(user.username, 'password')
        self.assertEqual(response['status'], 200)
        self.assertIn('token', response['data'])
        self.assertIn('refresh', response['data'])
        self.assertFalse(response['data']['require_otp'])

    def test_a_privileged_login_still_escalates_to_otp(self, *mocks):
        from restaurants_app.models import Restaurant, RestaurantEmployee
        from dinify_backend.configss.string_definitions import (
            RESTAURANT_OWNER, RestaurantStatus_Live,
        )

        user = make_user()
        restaurant = Restaurant.objects.create(
            name='Established House', location='Ntinda',
            status=RestaurantStatus_Live, owner=user,
        )
        RestaurantEmployee.objects.create(
            user=user, restaurant=restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        response = login(user.username, 'password')
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['data']['require_otp'])
        self.assertNotIn('token', response['data'])
        self.assertTrue(
            UserOtp.objects.filter(user=user, purpose='login').exists()
        )

    def test_last_login_still_moves(self, *mocks):
        user = make_user()
        login(user.username, 'password')
        user.refresh_from_db()
        self.assertIsNotNone(user.last_login)


# --- §29 password reset ------------------------------------------------------

@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class PendingPasswordResetRefusedTests(TestCase):
    """The original bypass, closed at the resolver so BOTH stages fail."""

    def setUp(self):
        self.user = make_pending_user()

    def test_the_resolver_refuses_a_pending_identity(self, *mocks):
        self.assertIsNone(_resolve_user(self.user.username))

    def test_initiate_reset_is_refused_and_sends_no_otp(self, *mocks):
        response = initiate_password_reset(self.user.username)
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], MESSAGES.get('NO_PHONE_NUMBER'))
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_completing_the_reset_directly_is_refused(self, *mocks):
        """
        A caller can invoke stage two on its own, so stage one refusing is not
        enough — which is why the gate lives in the shared resolver.
        """
        response = reset_password(self.user.username, DEV_OTP)
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], MESSAGES.get('NO_PHONE_NUMBER'))

    def test_even_a_valid_pre_existing_reset_otp_cannot_claim_the_account(self, *mocks):
        """
        THE FULL BYPASS, END TO END. The OTP is created while the identity is still
        established — so it is genuinely valid — and only then is the pending state
        written, modelling a code already in flight. It must still buy nothing.
        """
        User.objects.filter(pk=self.user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
        )
        self.user.refresh_from_db()
        self.assertTrue(
            OtpManager().make_otp(user=self.user, purpose='reset-password')
        )
        User.objects.filter(pk=self.user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

        response = reset_password(self.user.username, DEV_OTP)

        self.assertEqual(response['status'], 400)
        self.assertNotIn('data', response)
        self.user.refresh_from_db()
        self.assertFalse(
            self.user.has_usable_password(),
            'password reset installed a password on a pre-claim identity',
        )
        self.assertEqual(
            self.user.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_the_dev_otp_does_not_bypass_the_gate(self, *mocks):
        """
        §14. ``ENV=dev`` hardcodes the OTP to 1234, so the gate must not rest on the
        code being secret. It rests on durable account state instead.
        """
        response = reset_password(self.user.username, DEV_OTP)
        self.assertEqual(response['status'], 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())

    def test_prompt_password_change_is_untouched(self, *mocks):
        before = self.user.prompt_password_change
        reset_password(self.user.username, DEV_OTP)
        self.user.refresh_from_db()
        self.assertEqual(self.user.prompt_password_change, before)

    def test_reset_never_consumes_an_owner_invitation(self, *mocks):
        """
        Reset is NOT claim, and must never be "fixed" into it: it never sees the
        claim credential, so it cannot know the right person is on the other end.
        """
        from platform_admin_app.models import OwnerInvitation

        reset_password(self.user.username, DEV_OTP)
        self.assertFalse(
            OwnerInvitation.objects.filter(consumed_at__isnull=False).exists()
        )

    def test_the_refusal_is_not_an_account_state_oracle(self, *mocks):
        pending = initiate_password_reset(self.user.username)
        unknown = initiate_password_reset('256700999999')
        self.assertEqual(pending, unknown)


@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class EstablishedPasswordResetUnchangedTests(TestCase):
    """§15 — the ordinary reset contract is untouched."""

    def test_the_full_reset_flow_still_works(self, *mocks):
        user = make_user()
        initiated = initiate_password_reset(user.username)
        self.assertEqual(initiated['status'], 200)

        response = reset_password(user.username, DEV_OTP)
        self.assertEqual(response['status'], 200, response)
        self.assertIn('token', response['data'])
        self.assertIn('refresh', response['data'])
        self.assertTrue(response['data']['prompt_password_change'])
        user.refresh_from_db()
        self.assertTrue(user.has_usable_password())


# --- §30 OTP -----------------------------------------------------------------

@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class PendingOtpTests(TestCase):
    """
    Customer-auth OTP purposes are refused for a pending identity — and ONLY those.
    """

    def setUp(self):
        self.user = make_pending_user()

    def test_a_login_otp_is_not_issued(self, *mocks):
        self.assertFalse(
            OtpManager().make_otp(user=self.user, purpose='login')
        )
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_a_reset_otp_is_not_issued(self, *mocks):
        self.assertFalse(
            OtpManager().make_otp(user=self.user, purpose='reset-password')
        )
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_other_purposes_are_untouched(self, *mocks):
        """
        NARROW BY DESIGN. The future owner-invitation redemption may want a factor of
        its own, and the one identity it needs to reach is precisely this one — so
        the policy names the two customer-auth purposes, never the user.
        """
        self.assertTrue(
            OtpManager().make_otp(user=self.user, purpose='owner-claim')
        )
        self.assertTrue(
            UserOtp.objects.filter(user=self.user, purpose='owner-claim').exists()
        )

    def test_resend_cannot_issue_a_customer_auth_otp(self, *mocks):
        response = OtpManager().resend_otp(
            identification='phone', identifier=self.user.phone_number,
            purpose='reset-password',
        )
        self.assertNotEqual(response['status'], 200)
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())

    def test_a_login_otp_already_in_flight_cannot_mint_at_the_sink(self, *mocks):
        """
        The INDEPENDENT token sink. The code is created while the identity is still
        established, so it is genuinely valid and genuinely ``purpose='login'``; the
        state is written afterwards, modelling a code in flight. The refusal must be
        byte-identical to a wrong code — verify-otp is AllowAny with a
        client-supplied user id.
        """
        User.objects.filter(pk=self.user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
        )
        self.user.refresh_from_db()
        self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
        User.objects.filter(pk=self.user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

        result = OtpManager().verify_otp(user_id=str(self.user.id), otp=DEV_OTP)

        self.assertFalse(result['data']['valid'])
        self.assertNotIn('token', result['data'])
        self.assertNotIn('refresh', result['data'])

        wrong_code = OtpManager().verify_otp(
            user_id=str(make_user().id), otp='0000',
        )
        self.assertEqual(result, wrong_code)


@patch(_PATCH_NOTIFICATION)
@patch(_PATCH_EMAIL, return_value=True)
@patch(_PATCH_SMS, return_value=True)
class EstablishedOtpUnchangedTests(TestCase):
    def test_a_login_otp_still_mints_tokens(self, *mocks):
        user = make_user()
        self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        result = OtpManager().verify_otp(user_id=str(user.id), otp=DEV_OTP)
        self.assertTrue(result['data']['valid'])
        self.assertIn('token', result['data'])
        self.assertIn('refresh', result['data'])


# --- §31 presentation and refresh --------------------------------------------

class PendingJwtPresentationTests(TestCase):
    """
    Defence in depth: a pending identity cannot EXERCISE customer authority, not
    merely "cannot be handed a token by today's known mint paths".
    """

    def setUp(self):
        self.user = make_user()

    def _fabricate(self, user):
        """
        Mint directly, bypassing the chokepoint. TEST ONLY — this is the shell /
        fixture / forgotten-mint-path scenario the presentation gate exists for.
        """
        return str(RefreshToken.for_user(user).access_token)

    def _flip_to_pending(self):
        User.objects.filter(pk=self.user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_a_token_minted_before_the_transition_is_refused_after_it(self):
        token = self._fabricate(self.user)
        self.assertNotEqual(
            self.client.get(
                PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {token}',
            ).status_code,
            401,
        )
        self._flip_to_pending()
        self.assertEqual(
            self.client.get(
                PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {token}',
            ).status_code,
            401,
        )

    def test_a_directly_issued_token_for_a_pending_identity_is_refused(self):
        token = self._fabricate(make_pending_user())
        self.assertEqual(
            self.client.get(
                PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {token}',
            ).status_code,
            401,
        )

    def test_the_decode_auth_token_path_is_gated_too(self):
        """The ~30 endpoints that bypass the DRF chain share the same authenticator."""
        from misc_app.controllers.decode_auth_token import decode_jwt_token

        factory = APIRequestFactory()
        established_token = self._fabricate(self.user)
        request = factory.get('/', HTTP_AUTHORIZATION=f'Bearer {established_token}')
        self.assertEqual(decode_jwt_token(request)['id'], str(self.user.id))

        pending_token = self._fabricate(make_pending_user())
        request = factory.get('/', HTTP_AUTHORIZATION=f'Bearer {pending_token}')
        with self.assertRaises(Exception):
            decode_jwt_token(request)

    def test_change_password_is_unreachable_with_a_fabricated_token(self):
        """
        §13. ``change-password`` is reached only through ``decode_jwt_token``, so the
        presentation gate is what closes it — and its own ``check_password(old)``
        could never have matched an unusable password anyway. Two independent
        reasons; this pins the first.
        """
        pending = make_pending_user()
        token = self._fabricate(pending)
        response = self.client.post(
            '/api/v1/users/auth/change-password/',
            {'old_password': 'anything', 'new_password': 'newpassword'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertNotEqual(response.status_code, 200)
        pending.refresh_from_db()
        self.assertFalse(pending.has_usable_password())

    def test_the_refusal_is_not_an_account_state_oracle(self):
        pending_token = self._fabricate(make_pending_user())
        pending_response = self.client.get(
            PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {pending_token}',
        )

        deactivated = make_user()
        deactivated_token = self._fabricate(deactivated)
        User.objects.filter(pk=deactivated.pk).update(is_active=False)
        deactivated_response = self.client.get(
            PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {deactivated_token}',
        )

        self.assertEqual(pending_response.status_code, 401)
        self.assertEqual(deactivated_response.status_code, 401)
        self.assertEqual(pending_response.json(), deactivated_response.json())

    def test_an_established_token_is_unaffected(self):
        token = self._fabricate(self.user)
        self.assertNotEqual(
            self.client.get(
                PROFILE_URL, HTTP_AUTHORIZATION=f'Bearer {token}',
            ).status_code,
            401,
        )


class PendingRefreshTests(TestCase):
    """The refresh route re-reads the account, so a pending identity cannot rotate."""

    def _refresh(self, raw):
        return self.client.post(
            REFRESH_URL, {'refresh': raw}, content_type='application/json',
        )

    def test_a_pending_identity_cannot_refresh(self):
        raw = str(RefreshToken.for_user(make_pending_user()))
        self.assertEqual(self._refresh(raw).status_code, 401)

    def test_a_refresh_token_held_across_the_transition_stops_working(self):
        user = make_user()
        raw = str(RefreshToken.for_user(user))
        self.assertEqual(self._refresh(raw).status_code, 200)

        User.objects.filter(pk=user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertEqual(self._refresh(raw).status_code, 401)

    def test_an_established_refresh_still_works_and_keeps_its_shape(self):
        raw = str(RefreshToken.for_user(make_user()))
        response = self._refresh(raw)
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.json())

    def test_platform_staff_refresh_refusal_is_unchanged(self):
        user = make_user()
        raw = str(RefreshToken.for_user(user))
        User.objects.filter(pk=user.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertEqual(self._refresh(raw).status_code, 401)

    def test_the_two_refusals_are_indistinguishable(self):
        """
        The property that matters for the NEW axis: a pending identity and a
        platform-staff one are refused identically, so the endpoint does not tell a
        prober which of the two it is holding a token for.

        Stated no more broadly than that. SimpleJWT's own detail strings DIFFER per
        failure reason — a malformed token says "Token is invalid", a blacklisted one
        "Token is blacklisted" — so both gated refusals landing on the generic
        ``InvalidToken`` message makes them indistinguishable from each other and from
        a generic rejection, not from literally every failure. That was already true
        of the platform-staff gate before this change; the ``code`` is the standard
        ``token_not_valid`` in every case.
        """
        staff = make_user()
        staff_raw = str(RefreshToken.for_user(staff))
        User.objects.filter(pk=staff.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        pending_raw = str(RefreshToken.for_user(make_pending_user()))

        staff_response = self._refresh(staff_raw)
        pending_response = self._refresh(pending_raw)

        self.assertEqual(staff_response.status_code, pending_response.status_code)
        self.assertEqual(staff_response.json(), pending_response.json())
        self.assertEqual(pending_response.json()['code'], 'token_not_valid')
        self.assertEqual(
            self._refresh('not-a-token').json()['code'], 'token_not_valid',
        )

    def test_a_malformed_body_still_falls_through_to_the_standard_error(self):
        self.assertEqual(
            self.client.post(
                REFRESH_URL, {}, content_type='application/json',
            ).status_code,
            400,
        )


# --- §33 the structural ratchet ----------------------------------------------

# Directories outside the production Python surface.
PRUNE_DIRS = frozenset({
    'migrations', '__pycache__', 'node_modules', 'site-packages',
    'venv', 'env', 'staticfiles', 'media',
})

# The ONE production module entitled to call `RefreshToken.for_user`. Everything
# else must go through `customer_access.issue_customer_tokens`.
MINT_CHOKEPOINT = 'users_app/customer_access.py'


def _production_modules():
    """Yield (relative_path, source) for every production module worth scanning."""
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith('.')
        ]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            # Tests legitimately fabricate tokens to probe the boundaries — including
            # this file. The property being protected is about PRODUCTION code.
            if filename.startswith('test') or '/tests' in dirpath:
                continue
            path = Path(dirpath) / filename
            yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(
                encoding='utf-8', errors='replace',
            )


def _mints_directly(source):
    """
    Whether ``source`` CALLS ``RefreshToken.for_user(...)``.

    An AST walk over call nodes rather than a substring match, so a docstring or a
    comment explaining the chokepoint is not a false positive — several of the
    modules this scans discuss it at length.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'for_user':
            return True
    return False


class TokenMintChokepointTests(TestCase):
    """
    The next direct ``RefreshToken.for_user`` must fail a test, not ship.

    The bypass this PR closes was ONE innocent-looking mint in a flow nobody thought
    of as authentication. Auditing the three that existed fixes today; routing them
    through a checked helper and scanning for a fourth is what fixes tomorrow.
    """

    def test_only_the_chokepoint_mints_customer_tokens(self):
        offenders = [
            relative for relative, source in _production_modules()
            if _mints_directly(source) and relative != MINT_CHOKEPOINT
        ]
        self.assertEqual(
            offenders, [],
            'these production modules mint a customer token directly; call '
            f'{MINT_CHOKEPOINT}::issue_customer_tokens instead, which refuses an '
            f'identity that may not hold a customer session: {offenders}',
        )

    def test_the_chokepoint_itself_still_mints(self):
        """Guards the scan above from passing because nothing mints at all."""
        source = (REPO_ROOT / MINT_CHOKEPOINT).read_text(encoding='utf-8')
        self.assertTrue(_mints_directly(source))

    def test_the_three_historical_sinks_now_route_through_it(self):
        """
        Named explicitly, because these are the sinks the recon found and the reader
        of this test should be able to see which ones were covered.
        """
        import inspect

        from users_app.controllers import login as login_module
        from users_app.controllers import otp_manager, reset_password as reset_module

        for module in (login_module, otp_manager, reset_module):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertIn('issue_customer_tokens', source)
                self.assertFalse(_mints_directly(source))


# --- §24 no customer-plane write surface -------------------------------------

class NoCustomerWriteSurfaceTests(TestCase):
    """The state lives on ``User``; that must not make it customer-editable."""

    def test_the_profile_serializer_does_not_expose_it(self):
        from users_app.serializers import SerGetUserProfile

        self.assertNotIn('customer_access_state', SerGetUserProfile.Meta.fields)

    def test_it_is_absent_from_the_serialized_profile(self):
        from users_app.serializers import SerGetUserProfile

        self.assertNotIn(
            'customer_access_state', SerGetUserProfile(make_user()).data,
        )

    def test_no_edit_information_section_exposes_it(self):
        """Secretary builds its payload solely from EDIT_INFORMATION keys."""
        from dinify_backend.configss.edit_information import EDIT_INFORMATION

        for section, entries in EDIT_INFORMATION.items():
            with self.subTest(section=section):
                keys = {
                    entry['key'] if isinstance(entry, dict) else entry
                    for entry in entries
                }
                self.assertNotIn('customer_access_state', keys)

    def test_the_self_service_profile_update_cannot_set_it(self):
        """
        The one customer-plane path that writes ``User``. It takes named keyword
        arguments, so an extra key in the request body reaches nothing — asserted
        rather than assumed.
        """
        from users_app.controllers.update_user_profile import self_update_user_profile

        pending = make_pending_user()
        with self.assertRaises(TypeError):
            self_update_user_profile(
                user_id=str(pending.id),
                customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            )
        pending.refresh_from_db()
        self.assertEqual(
            pending.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_a_profile_put_body_cannot_smuggle_it(self):
        """End to end through HTTP, with a token fabricated for an ESTABLISHED user."""
        user = make_user()
        token = str(RefreshToken.for_user(user).access_token)
        self.client.put(
            PROFILE_URL,
            {'first_name': 'Renamed',
             'customer_access_state': CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        user.refresh_from_db()
        self.assertEqual(user.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)

    def test_there_is_no_general_establish_service(self):
        """
        §18. The first supported writer of pending -> established belongs inside the
        future redemption transaction, alongside consuming the invitation. A general
        helper would be a way to establish access without claim evidence.
        """
        self.assertFalse(hasattr(customer_access, 'establish_customer_access'))
        self.assertFalse(hasattr(customer_access, 'establish'))
