"""
Password reset: one acknowledgement, and completion bound to reset challenges (D11 E-R1).

WHAT THIS PINS
━━━━━━━━━━━━━━

1. ONE ACKNOWLEDGEMENT AT INITIATION. ``initiate-reset-password`` (and the legacy
   ``reset-password`` without an OTP) answered an existing account with 200 and its
   ``user_id``, and every other identifier with 400 ``NO_PHONE_NUMBER``: an anonymous
   account-existence answer, plus an account id. Both routes now answer an eligible
   account whose code was issued, an absent identifier, a platform-staff account, an
   identity not yet claimed and an email address two accounts share with the SAME
   bytes, and carry no ``user_id`` at all. For everything but the eligible account,
   nothing is issued, recorded or sent.

2. FAILURE STAYS TRUTHFUL. When ``make_otp`` reports that it could not issue or deliver
   a code (the ledger or database refused it before anything was sent, or the sender
   failed), the existing 500 is unchanged. An exception still surfaces as an error. A
   failure is never turned into the acknowledgement. This keeps a disclosure: an
   eligible account is distinguishable for as long as its issuance fails — permanently,
   for an account with no destination the environment can send to — and response time
   still differs. The generic ``resend-otp`` route, which E-R1 leaves unchanged, still
   answers an absent account differently for ``purpose='reset-password'``. E-R1 claims
   no account indistinguishability.

3. ONE INVALID ANSWER AT COMPLETION. An absent, refused or ambiguous identity, a wrong
   code, and an account with no live reset challenge all receive 400 ``Invalid OTP.``.

4. COMPLETION SPENDS ONLY RESET CHALLENGES. Completion called ``verify_otp`` with no
   purpose, so it selected the user's most recent live challenge of ANY purpose. An
   anonymous reset guess could charge, and lock, an in-flight login or owner-claim code;
   that code could complete a reset; and a newer unrelated challenge hid the user's own
   reset code. Completion now binds ``expected_purpose='reset-password'``. There is no
   origin restriction: a reset challenge from initiation or from an account-resolving
   resend is equally valid.

5. AN EXACTLY SHARED EMAIL. ``get_user_by_email`` raises ``MultipleObjectsReturned``
   when two accounts hold the address exactly. The reset resolver alone now treats that
   as ineligible, selects neither account, and logs a bounded line that names no
   address. The shared resolver and login are unchanged, and both accounts can still
   reset by phone.

HOW THE CROSS-PURPOSE CASES DISCRIMINATE. Under ``ENV=dev`` every code is ``1234``, so
digits cannot tell two challenges apart. Those cases therefore run under ``ENV=test``
with deterministic, distinct codes. The reset challenge is created FIRST and the
unrelated one LAST, so an unbound verifier selects the unrelated row. Each case drives
the real issuing routes and the unrelated challenge's own consumer. Only delivery
(SMS, e-mail, notifications) is mocked; selection, accounting and the consumers are not.
"""
import os
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from platform_admin_app import onboarding_creation
from platform_admin_app.models import OwnerInvitation
from platform_admin_app.onboarding_creation import ExistingOwner
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers.login import login
from users_app.controllers.otp_manager import OTP_MAX_ATTEMPTS, OtpManager
from users_app.controllers.reset_password import (
    RESET_ACKNOWLEDGEMENT,
    initiate_password_reset,
    reset_password,
)
from users_app.models import OtpIssuance, OtpVerificationFailure, User, UserOtp


INITIATE = '/api/v1/users/auth/initiate-reset-password/'
COMPLETE = '/api/v1/users/auth/reset-password/'
RESEND = '/api/v1/users/auth/resend-otp/'
LOGIN = '/api/v1/users/auth/login/'
VERIFY = '/api/v1/users/auth/verify-otp/'
CHANGE = '/api/v1/users/auth/change-password/'
CLAIM_CHALLENGE = '/api/v1/users/owner-claim/challenge/'
CLAIM_REDEEM = '/api/v1/users/owner-claim/redeem/'
CLAIM_HEADER = 'X-Owner-Claim-Token'

ACK_TEXT = (
    'If these details match an eligible account, check its registered phone or email '
    'for a reset code.'
)
ACK = {'status': 200, 'message': ACK_TEXT}
INVALID = {'status': 400, 'message': 'Invalid OTP.'}
SEND_FAILURE = {
    'status': 500, 'message': "We couldn't send your verification code. Please try again.",
}

DEV_OTP = '1234'
WRONG = '9999'
PASSWORD = 'Original-Passw0rd!'

PATCH_SMS = 'users_app.controllers.otp_manager.send_sms'
PATCH_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
PATCH_NOTIFICATION = (
    'misc_app.controllers.notifications.notification.Notification.create_notification'
)
PATCH_SAVE_ACTION = 'users_app.controllers.reset_password.save_action'

# A phone range distinct from every other suite.
_PHONES = iter(f'256772{n:06d}' for n in range(410000, 419999))


def _account(email=None, phone=None, **extra):
    phone = phone or next(_PHONES)
    return User.objects.create_user(
        first_name='Reset', last_name='Ack', email=email, phone_number=phone,
        username=phone, country='UG', password=PASSWORD, roles=[], **extra,
    )


def _owner_with_live_restaurant(email=None):
    """An account whose password login requires an OTP, so login writes a challenge."""
    user = _account(email=email)
    restaurant = Restaurant.objects.create(
        name=f'Ack House {user.phone_number}', location='Ntinda',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
    )
    return user


class _Codes:
    """
    Stands in for ``secrets`` inside ``otp_manager`` so that each challenge gets the next
    code from a fixed list. Only the digit draw is replaced; the per-row salt still comes
    from the real ``secrets``.
    """

    def __init__(self, *codes):
        self._codes = iter(codes)

    def randbelow(self, n):
        return int(next(self._codes)) - 1000

    @staticmethod
    def token_hex(nbytes=None):
        import secrets
        return secrets.token_hex(nbytes)


def _not_dev(*codes):
    """``ENV=test`` (real random-code path, synchronous mocked SMS) with fixed codes."""
    return (
        mock.patch.dict(os.environ, {'ENV': 'test'}),
        mock.patch('users_app.controllers.otp_manager.secrets', _Codes(*codes)),
    )


class _ResetTestCase(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        for target, value in ((PATCH_SMS, True), (PATCH_EMAIL, True),
                              (PATCH_NOTIFICATION, None)):
            patcher = mock.patch(target, return_value=value)
            setattr(self, {PATCH_SMS: 'sms', PATCH_EMAIL: 'email',
                           PATCH_NOTIFICATION: 'notify'}[target], patcher.start())
            self.addCleanup(patcher.stop)

    def post(self, url, body, headers=None):
        # Every reset, OTP and claim route is throttled per client or per identifier;
        # throttling is not what these tests are about.
        cache.clear()
        return self.client.post(url, body, format='json', headers=headers or {})

    def snapshot(self, user):
        """The facts a refused or failed reset must leave exactly as they were."""
        user = User.objects.get(pk=user.pk)
        return {
            'password': user.password,
            'prompt_password_change': user.prompt_password_change,
            'customer_access_state': user.customer_access_state,
            'is_active': user.is_active,
            'otps': list(UserOtp.objects.filter(user=user).order_by('pk')
                         .values_list('pk', 'attempts', 'consumed_at')),
            'sessions': OutstandingToken.objects.filter(user=user).count(),
        }

    @staticmethod
    def row(otp_pk):
        r = UserOtp.objects.get(pk=otp_pk)
        return {'attempts': r.attempts, 'consumed_at': r.consumed_at,
                'expiry_time': r.expiry_time, 'purpose': r.purpose, 'msisdn': r.msisdn}

    @staticmethod
    def failures_for(otp_pk):
        return OtpVerificationFailure.objects.filter(issuance_id=otp_pk).count()

    def assert_completed(self, response, user):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(
            set(data), {'token', 'refresh', 'temp_password', 'prompt_password_change'},
        )
        self.assertTrue(data['prompt_password_change'])
        user = User.objects.get(pk=user.pk)
        self.assertTrue(user.check_password(data['temp_password']))
        self.assertTrue(user.prompt_password_change)
        return data


# ═══════════════════════════════════════════════════════════════════════════════
# 1. One acknowledgement at initiation, through both routes
# ═══════════════════════════════════════════════════════════════════════════════

class InitiationEnvelopeTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        self.eligible = _account(email='eligible@example.com')
        self.staff = User.objects.create_user(
            username='ops-ack', email='staff-ack@example.com', password=PASSWORD,
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.pending = _account(
            email='pending-ack@example.com',
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.twin_a = _account(email='twin@example.com')
        self.twin_b = _account(email='twin@example.com')
        self.refused = [self.staff, self.pending, self.twin_a, self.twin_b]
        self.ineligible_identifiers = {
            'absent email': 'nobody@example.com',
            'absent phone': '256772999999',
            'platform staff': 'staff-ack@example.com',
            'pending by email': 'pending-ack@example.com',
            'pending by phone': self.pending.phone_number,
            'ambiguous email': 'twin@example.com',
        }

    def bodies(self, identifier):
        """Every body shape the two initiation routes accept for one identifier."""
        kind = 'email' if '@' in identifier else 'phone'
        return {
            'initiate, web client': (INITIATE, {'identifier': identifier, 'identification': kind}),
            'initiate, legacy phone_number': (INITIATE, {'phone_number': identifier}),
            'reset-password without otp': (COMPLETE, {'identifier': identifier}),
            'reset-password without otp, legacy': (COMPLETE, {'phone_number': identifier}),
        }

    def test_an_eligible_request_is_acknowledged_and_issues_one_reset_challenge(self):
        for identifier in ('eligible@example.com', self.eligible.phone_number):
            for label, (url, body) in self.bodies(identifier).items():
                with self.subTest(identifier=identifier, route=label):
                    UserOtp.objects.all().delete()
                    response = self.post(url, body)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json(), ACK)
                    self.assertNotIn(b'user_id', response.content)
                    otp = UserOtp.objects.get(user=self.eligible)
                    self.assertEqual(otp.purpose, 'reset-password')
                    self.assertEqual(OtpIssuance.objects.get(pk=otp.pk).origin, 'reset_initiation')

    def test_every_ineligible_identifier_gets_the_same_bytes(self):
        # ENV=test sends synchronously, so the sender counts below are exact; under dev
        # the eligible reference would dispatch from a thread and race the count.
        for patcher in _not_dev('5555'):
            patcher.start()
            self.addCleanup(patcher.stop)
        reference = self.post(INITIATE, {'identifier': 'eligible@example.com',
                                         'identification': 'email'})
        self.assertEqual(reference.json(), ACK)
        sends_before = self.sms.call_count + self.email.call_count
        self.assertGreaterEqual(sends_before, 1, 'premise: the reference really sent, synchronously')
        before = {u.pk: self.snapshot(u) for u in self.refused}
        issuances_before = OtpIssuance.objects.count()

        for label, identifier in self.ineligible_identifiers.items():
            for route, (url, body) in self.bodies(identifier).items():
                with self.subTest(identity=label, route=route):
                    response = self.post(url, body)
                    self.assertEqual(response.status_code, reference.status_code)
                    self.assertEqual(response.content, reference.content)
                    self.assertEqual(set(response.json()), {'status', 'message'})

        # nothing issued, recorded, sent, changed or minted for any of them
        self.assertEqual(OtpIssuance.objects.count(), issuances_before)
        self.assertEqual(self.sms.call_count + self.email.call_count, sends_before)
        for user in self.refused:
            self.assertEqual(self.snapshot(user), before[user.pk], user.email)
        self.assertFalse(UserOtp.objects.exclude(user=self.eligible).exists())

    def test_a_request_naming_no_one_keeps_its_own_400(self):
        for url in (INITIATE, COMPLETE):
            with self.subTest(url=url):
                response = self.post(url, {'identifier': '   '})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['message'], MESSAGES.get('NO_RESET_IDENTIFIER'))

    def test_identifier_still_wins_over_the_legacy_field(self):
        response = self.post(INITIATE, {'identifier': 'eligible@example.com',
                                        'phone_number': self.twin_a.phone_number})
        self.assertEqual(response.json(), ACK)
        self.assertTrue(UserOtp.objects.filter(user=self.eligible).exists())
        self.assertFalse(UserOtp.objects.filter(user=self.twin_a).exists())


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Existing failure responses are kept
# ═══════════════════════════════════════════════════════════════════════════════

class FailureIsNotAcknowledgedTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        self.user = _account(email='fails@example.com')

    def routes(self):
        return ((INITIATE, {'identifier': self.user.phone_number}),
                (COMPLETE, {'identifier': self.user.phone_number}))

    def test_a_sender_failure_keeps_the_500(self):
        env, codes = _not_dev('4321', '4321')
        self.sms.return_value = False
        self.email.return_value = False
        with env, codes:
            for url, body in self.routes():
                with self.subTest(url=url):
                    response = self.post(url, body)
                    self.assertEqual(response.status_code, 500, response.content)
                    self.assertEqual(response.json(), SEND_FAILURE)

    def test_a_ledger_or_database_failure_before_sending_keeps_the_500(self):
        with mock.patch('users_app.otp_accounting.record_issuance',
                        side_effect=DatabaseError('synthetic')):
            for url, body in self.routes():
                with self.subTest(url=url):
                    response = self.post(url, body)
                    self.assertEqual(response.status_code, 500, response.content)
                    self.assertEqual(response.json(), SEND_FAILURE)
        self.assertFalse(UserOtp.objects.filter(user=self.user).exists())
        self.sms.assert_not_called()

    def test_a_sender_exception_is_still_an_error_not_an_acknowledgement(self):
        env, codes = _not_dev('4321')
        self.sms.side_effect = RuntimeError('synthetic sender fault')
        with env, codes:
            response = self.post(INITIATE, {'identifier': self.user.phone_number})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(ACK_TEXT.encode(), response.content)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Completion: one invalid answer; success unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class CompletionTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        self.user = _account(email='complete@example.com')

    def test_a_successful_reset_still_leads_to_change_password(self):
        self.assertEqual(self.post(INITIATE, {'identifier': 'complete@example.com'}).json(), ACK)
        sessions = OutstandingToken.objects.filter(user=self.user).count()
        data = self.assert_completed(
            self.post(COMPLETE, {'identifier': 'complete@example.com', 'otp': DEV_OTP}),
            self.user,
        )
        self.assertEqual(OutstandingToken.objects.filter(user=self.user).count(), sessions + 1)

        # the Frontend's next step: change-password with the issued token
        cache.clear()
        response = self.client.post(
            CHANGE, {'old_password': data['temp_password'], 'new_password': 'Brand-New-Pass-42'},
            format='json', headers={'Authorization': f"Bearer {data['token']}"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        user = User.objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password('Brand-New-Pass-42'))
        self.assertFalse(user.prompt_password_change)

    def test_every_invalid_completion_gets_the_same_bytes(self):
        staff = User.objects.create_user(
            username='ops-c', email='staff-c@example.com', password=PASSWORD,
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        pending = _account(email='pending-c@example.com')
        twin_a, twin_b = _account(email='twin-c@example.com'), _account(email='twin-c@example.com')
        no_challenge = _account(email='nochallenge@example.com')
        # REAL reset challenges, issued while each identity could still receive one, so
        # the dev code would spend them: a resolver that stopped refusing any of these
        # would select its challenge and change the snapshot (attempts or consumed_at).
        for user in (staff, pending, twin_a, twin_b):
            self.assertTrue(OtpManager().make_otp(user=user, purpose='reset-password'))
        User.objects.filter(pk=pending.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        self.assertEqual(self.post(INITIATE, {'identifier': 'complete@example.com'}).json(), ACK)
        before = {u.pk: self.snapshot(u) for u in (staff, pending, twin_a, twin_b, no_challenge)}

        cases = {
            'absent': ('nobody@example.com', DEV_OTP),
            'platform staff': ('staff-c@example.com', DEV_OTP),
            'pending': (pending.phone_number, DEV_OTP),
            'ambiguous email': ('twin-c@example.com', DEV_OTP),
            'no live reset challenge': ('nochallenge@example.com', DEV_OTP),
            'wrong code': ('complete@example.com', WRONG),
        }
        for label, (identifier, otp) in cases.items():
            with self.subTest(label):
                response = self.post(COMPLETE, {'identifier': identifier, 'otp': otp})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json(), INVALID)
                self.assertEqual(set(response.json()), {'status', 'message'})
        for user in (staff, pending, twin_a, twin_b, no_challenge):
            self.assertEqual(self.snapshot(user), before[user.pk], user.email)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. The existing action-log contracts
# ═══════════════════════════════════════════════════════════════════════════════

class ActionLogContractTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch(PATCH_SAVE_ACTION)
        self.save_action = patcher.start()
        self.addCleanup(patcher.stop)
        self.user = _account(email='logged@example.com')

    def failed_call(self, username):
        return mock.call(
            affected_model='User', affected_record=None, action='reset-password',
            narration=MESSAGES.get('NO_PHONE_NUMBER'),
            result=ACTION_LOG_STATUSES.get('failed'), user_id=None, username=username,
            submitted_data={'username': username}, changes=None, filter_information=None,
        )

    def test_the_legacy_unresolvable_call_without_otp_is_still_logged(self):
        self.assertEqual(reset_password('nobody@example.com', None), ACK)
        self.save_action.assert_called_once_with(**self.failed_call('nobody@example.com').kwargs)

    def test_an_unresolvable_completion_is_still_logged(self):
        self.assertEqual(reset_password('nobody@example.com', DEV_OTP), INVALID)
        self.save_action.assert_called_once_with(**self.failed_call('nobody@example.com').kwargs)

    def test_initiation_alone_still_writes_no_action_log(self):
        self.assertEqual(initiate_password_reset('nobody@example.com'), ACK)
        self.assertEqual(initiate_password_reset('logged@example.com'), ACK)
        self.save_action.assert_not_called()

    def test_a_successful_reset_is_still_logged(self):
        initiate_password_reset('logged@example.com')
        self.assertEqual(reset_password('logged@example.com', DEV_OTP)['status'], 200)
        self.save_action.assert_called_once_with(
            affected_model='User', affected_record=str(self.user.id), action='reset-password',
            narration='Password reset verified. User must set a new password.',
            result=ACTION_LOG_STATUSES.get('success'), user_id=None,
            username='logged@example.com', submitted_data={'username': 'logged@example.com'},
            changes=None, filter_information=None,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Account selection, and the exactly shared email
# ═══════════════════════════════════════════════════════════════════════════════

class AccountSelectionTests(_ResetTestCase):

    def issued_to(self):
        return list(UserOtp.objects.filter(purpose='reset-password')
                    .values_list('user_id', flat=True))

    def test_an_address_typed_exactly_selects_its_own_account(self):
        lower = _account(email='diner@example.com')
        mixed = _account(email='other@example.com')
        User.objects.filter(pk=mixed.pk).update(email='Diner@Example.com')
        self.assertEqual(initiate_password_reset('Diner@Example.com'), ACK)
        self.assertEqual(self.issued_to(), [mixed.pk])
        UserOtp.objects.all().delete()
        self.assertEqual(initiate_password_reset('diner@example.com'), ACK)
        self.assertEqual(self.issued_to(), [lower.pk])

    def test_a_mixed_case_address_finds_its_unique_lower_case_account(self):
        solo = _account(email='solo@example.com')
        self.assertEqual(initiate_password_reset('Solo@Example.com'), ACK)
        self.assertEqual(self.issued_to(), [solo.pk])

    def test_an_exactly_shared_email_selects_no_account(self):
        twin_a, twin_b = _account(email='twin@example.com'), _account(email='twin@example.com')
        with self.assertLogs('users_app.controllers.reset_password', level='INFO') as logs:
            self.assertEqual(initiate_password_reset('twin@example.com'), ACK)
        self.assertEqual(self.issued_to(), [])
        self.sms.assert_not_called()
        self.assertTrue(logs.output)
        for line in logs.output:
            self.assertNotIn('twin', line)
            self.assertNotIn('@', line)

        # valid reset challenges on both accounts: still spends neither
        for user in (twin_a, twin_b):
            self.assertTrue(OtpManager().make_otp(user=user, purpose='reset-password'))
        before = {u.pk: self.snapshot(u) for u in (twin_a, twin_b)}
        self.assertEqual(reset_password('twin@example.com', DEV_OTP), INVALID)
        for user in (twin_a, twin_b):
            self.assertEqual(self.snapshot(user), before[user.pk])
            self.assertIsNone(UserOtp.objects.get(user=user).consumed_at)

    def test_each_twin_can_still_reset_by_phone(self):
        twin_a, twin_b = _account(email='twin@example.com'), _account(email='twin@example.com')
        for user in (twin_a, twin_b):
            UserOtp.objects.all().delete()
            self.assertEqual(initiate_password_reset(user.phone_number), ACK)
            self.assertEqual(self.issued_to(), [user.pk])
            self.assert_completed(
                self.post(COMPLETE, {'identifier': user.phone_number, 'otp': DEV_OTP}), user,
            )

    def test_login_with_an_exactly_shared_email_is_unchanged(self):
        _account(email='twin@example.com')
        _account(email='twin@example.com')
        with self.assertRaises(User.MultipleObjectsReturned):
            login('twin@example.com', PASSWORD)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Positive reset-purpose controls (no origin restriction), ENV=dev
# ═══════════════════════════════════════════════════════════════════════════════

class ResetPurposeControlTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        self.user = _account(email='control@example.com')

    def complete(self):
        return self.assert_completed(
            self.post(COMPLETE, {'identifier': self.user.phone_number, 'otp': DEV_OTP}),
            self.user,
        )

    def origin_of_only_reset_challenge(self):
        otp = UserOtp.objects.get(user=self.user, purpose='reset-password')
        return OtpIssuance.objects.get(pk=otp.pk).origin

    def test_a_challenge_from_initiation_completes(self):
        self.post(INITIATE, {'identifier': self.user.phone_number})
        self.assertEqual(self.origin_of_only_reset_challenge(), 'reset_initiation')
        self.complete()

    def test_a_challenge_from_resend_completes_for_every_account_resolving_mode(self):
        modes = {
            'id': str(self.user.pk),
            'phone': self.user.phone_number,
            'email': 'control@example.com',
            'msisdn': self.user.phone_number,
        }
        for identification, identifier in modes.items():
            with self.subTest(identification):
                UserOtp.objects.filter(user=self.user).delete()
                response = self.post(RESEND, {'identification': identification,
                                              'identifier': identifier,
                                              'purpose': 'reset-password'})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(self.origin_of_only_reset_challenge(), 'resend_request')
                self.complete()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Single use, expiry and the attempt cap
# ═══════════════════════════════════════════════════════════════════════════════

class ChallengeLifecycleTests(_ResetTestCase):

    def setUp(self):
        super().setUp()
        self.user = _account(email='life@example.com')
        initiate_password_reset(self.user.phone_number)
        self.otp = UserOtp.objects.get(user=self.user, purpose='reset-password')

    def complete(self, otp=DEV_OTP):
        return self.post(COMPLETE, {'identifier': self.user.phone_number, 'otp': otp})

    def test_a_code_is_single_use(self):
        self.assert_completed(self.complete(), self.user)
        self.assertEqual(self.complete().json(), INVALID)

    def test_an_expired_code_is_invalid_and_not_charged(self):
        UserOtp.objects.filter(pk=self.otp.pk).update(
            expiry_time=timezone.now() - timezone.timedelta(seconds=1))
        self.assertEqual(self.complete().json(), INVALID)
        self.assertEqual(self.row(self.otp.pk)['attempts'], 0)
        self.assertEqual(self.failures_for(self.otp.pk), 0)

    def test_the_attempt_cap_locks_the_code(self):
        for _ in range(OTP_MAX_ATTEMPTS):
            self.assertEqual(self.complete(WRONG).json(), INVALID)
        self.assertEqual(self.failures_for(self.otp.pk), OTP_MAX_ATTEMPTS)
        self.assertEqual(self.complete().json(), INVALID)
        self.assertIsNotNone(self.row(self.otp.pk)['consumed_at'])


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Cross-purpose regressions (deterministic, distinct codes; ENV=test)
# ═══════════════════════════════════════════════════════════════════════════════

RESET_CODE = '1111'
OTHER_CODE = '2222'


class _CrossPurpose:
    """
    A MIXIN, deliberately not a TestCase, so the runner never collects it on its own.

    Subclasses build ``self.reset_pk`` FIRST and ``self.other_pk`` LAST, and implement
    ``other_still_works()`` through the unrelated challenge's own consumer.
    """
    reset_origin = None

    def setUp(self):
        super().setUp()
        env, codes = _not_dev(RESET_CODE, OTHER_CODE, '3333', '4444')
        env.start()
        codes.start()
        self.addCleanup(codes.stop)
        self.addCleanup(env.stop)
        self.build()
        # Unambiguous ordering: the reset row is a full minute older, still live.
        UserOtp.objects.filter(pk=self.reset_pk).update(
            time_created=timezone.now() - timezone.timedelta(minutes=1))
        newest = UserOtp.objects.filter(user=self.user).order_by('-time_created').first()
        self.assertEqual(newest.pk, self.other_pk, 'premise: the unrelated row is newest')
        self.assertNotEqual(self.row(self.reset_pk)['purpose'],
                            self.row(self.other_pk)['purpose'])
        self.other_before = self.row(self.other_pk)
        self.password_before = User.objects.get(pk=self.user.pk).password

    def complete(self, otp):
        return self.post(COMPLETE, {'identifier': self.user.phone_number, 'otp': otp})

    def assert_other_untouched(self):
        self.assertEqual(self.row(self.other_pk), self.other_before)
        self.assertEqual(self.failures_for(self.other_pk), 0)

    def test_a_wrong_guess_charges_only_the_reset_challenge(self):
        self.assertEqual(self.complete(WRONG).json(), INVALID)
        self.assertEqual(self.row(self.reset_pk)['attempts'], 1)
        failure = OtpVerificationFailure.objects.get()
        self.assertEqual(failure.issuance_id, self.reset_pk)
        self.assertEqual(failure.origin, self.reset_origin)
        self.assertFalse(failure.bound_redemption)
        self.assert_other_untouched()
        self.other_still_works()

    def test_the_unrelated_code_cannot_complete_a_reset(self):
        self.assertEqual(self.complete(OTHER_CODE).json(), INVALID)
        self.assertEqual(User.objects.get(pk=self.user.pk).password, self.password_before)
        self.assert_other_untouched()
        self.other_still_works()

    def test_the_older_reset_code_still_completes(self):
        self.assert_completed(self.complete(RESET_CODE), self.user)
        self.assertIsNotNone(self.row(self.reset_pk)['consumed_at'])
        self.assert_other_untouched()
        self.other_still_works()


class LoginChallengeIsNotSpentByResetTests(_CrossPurpose, _ResetTestCase):
    """A: reset from RESEND (phone bucket) first; a password-login challenge (NULL) last."""
    reset_origin = 'resend_request'

    def build(self):
        self.user = _owner_with_live_restaurant(email='cross-login@example.com')
        response = self.post(RESEND, {'identification': 'phone',
                                      'identifier': self.user.phone_number,
                                      'purpose': 'reset-password'})
        self.assertEqual(response.status_code, 200, response.content)
        self.reset_pk = UserOtp.objects.get(user=self.user, purpose='reset-password').pk
        response = self.post(LOGIN, {'username': self.user.phone_number, 'password': PASSWORD})
        self.assertTrue(response.json()['data']['require_otp'], response.content)
        self.other_pk = UserOtp.objects.get(user=self.user, purpose='login').pk
        self.assertIsNotNone(self.row(self.reset_pk)['msisdn'])   # phone bucket
        self.assertIsNone(self.row(self.other_pk)['msisdn'])      # NULL bucket

    def other_still_works(self):
        response = self.post(VERIFY, {'user': str(self.user.pk), 'otp': OTHER_CODE})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['valid'])
        self.assertIn('token', response.json()['data'])


class OwnerClaimChallengeIsNotSpentByResetTests(_CrossPurpose, _ResetTestCase):
    """B: reset from INITIATION (NULL bucket) first; an owner-claim challenge (phone) last."""
    reset_origin = 'reset_initiation'

    def build(self):
        self.user = _account(email='cross-claim@example.com')
        admin = User.objects.create_user(
            username='ops-claim', email='ops-claim@example.com', password=PASSWORD,
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        creation = onboarding_creation.create_admin_restaurant(
            name='Cross Claim Grill', location='Kololo', is_test=False,
            owner=ExistingOwner(str(self.user.pk)), actor=admin,
            reason='The E-R1 cross-purpose fixture.',
        )
        self.claim_token = creation.claim_token
        self.invitation_pk = creation.invitation.pk
        # Status only: the envelope has its own tests, and asserting it here would stop
        # this fixture short of the cross-purpose defect on the pre-E-R1 server.
        response = self.post(INITIATE, {'identifier': self.user.phone_number})
        self.assertEqual(response.status_code, 200, response.content)
        self.reset_pk = UserOtp.objects.get(user=self.user, purpose='reset-password').pk
        response = self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: self.claim_token})
        self.assertEqual(response.status_code, 200, response.content)
        self.other_pk = UserOtp.objects.get(user=self.user, purpose='owner-claim').pk
        self.assertIsNone(self.row(self.reset_pk)['msisdn'])      # NULL bucket
        self.assertIsNotNone(self.row(self.other_pk)['msisdn'])   # phone bucket

    def other_still_works(self):
        response = self.post(CLAIM_REDEEM, {'otp': OTHER_CODE},
                             headers={CLAIM_HEADER: self.claim_token})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(OwnerInvitation.objects.get(pk=self.invitation_pk).consumed_at)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Only an unrelated challenge exists
# ═══════════════════════════════════════════════════════════════════════════════

class OnlyUnrelatedChallengeTests(_ResetTestCase):

    def assert_nothing_spent(self, otp_pk, before):
        self.assertEqual(self.row(otp_pk), before)
        self.assertFalse(OtpVerificationFailure.objects.exists())

    def test_dev_a_login_code_alone_cannot_complete_a_reset(self):
        """ENV=dev: identical digits; there is no reset row to select at all."""
        user = _owner_with_live_restaurant()
        self.assertTrue(login(user.phone_number, PASSWORD)['data']['require_otp'])
        login_pk = UserOtp.objects.get(user=user, purpose='login').pk
        before = self.row(login_pk)
        response = self.post(COMPLETE, {'identifier': user.phone_number, 'otp': DEV_OTP})
        self.assertEqual(response.json(), INVALID)
        self.assert_nothing_spent(login_pk, before)
        verified = self.post(VERIFY, {'user': str(user.pk), 'otp': DEV_OTP})
        self.assertIn('token', verified.json()['data'])

    def test_an_owner_claim_code_alone_cannot_complete_a_reset(self):
        env, codes = _not_dev(OTHER_CODE)
        with env, codes:
            user = _account(email='only-claim@example.com')
            admin = User.objects.create_user(
                username='ops-only', email='ops-only@example.com', password=PASSWORD,
                account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
            )
            token = onboarding_creation.create_admin_restaurant(
                name='Only Claim Grill', location='Kololo', is_test=False,
                owner=ExistingOwner(str(user.pk)), actor=admin,
                reason='The only-unrelated fixture.',
            ).claim_token
            self.assertEqual(
                self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token}).status_code, 200)
            claim_pk = UserOtp.objects.get(user=user, purpose='owner-claim').pk
            before = self.row(claim_pk)
            response = self.post(COMPLETE, {'identifier': user.phone_number, 'otp': OTHER_CODE})
            self.assertEqual(response.json(), INVALID)
            self.assert_nothing_spent(claim_pk, before)


class AcknowledgementConstantTests(TestCase):

    def test_the_acknowledgement_is_the_approved_sentence(self):
        self.assertEqual(RESET_ACKNOWLEDGEMENT, ACK_TEXT)
