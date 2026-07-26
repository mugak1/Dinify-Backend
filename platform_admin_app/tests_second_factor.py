"""
PR-B regression suite — recovery codes must work when the Fernet key is lost.

``auth/verify/`` and ``auth/elevate/`` used to try TOTP first and fall through to
recovery. ``totp.verify`` decrypts the stored secret BEFORE it can reject a wrong
code, and ``crypto`` fails closed on a missing or invalid
``ADMIN_SECRET_ENCRYPTION_KEY`` — so the ``ImproperlyConfigured`` propagated out of
the TOTP attempt and the recovery branch was never reached. Two factors meant to be
independent failure paths were chained through one key.

Covers:
* THE regression: with the key missing AND with it invalid, a recovery code completes
  login through ``verify/`` and elevation through ``elevate/``;
* no decryption of any kind happens on the ``method='recovery'`` path — asserted with
  spies on ``totp.decrypt_secret``, ``crypto._fernet`` and ``totp.verify``, not by
  reading the code;
* ``method='totp'`` with an unusable key fails as an ordinary bad code — generic body,
  correct lockout accounting, no 500 and no configuration detail on the wire;
* no oracle: the wrong method for a code, an unrecognised method and an absent method
  are byte-identical to a genuinely wrong code;
* every preserved property — the TOTP replay guard, one-shot recovery consumption,
  lockout increment and reset, ``recovery_codes_remaining``;
* the full documented break-glass sequence end to end: key lost → recovery login →
  new key installed → ``reset_platform_admin_totp`` → TOTP works again.

KEY LOSS IN A TEST. ``crypto`` reads the key with ``decouple.config`` at CALL time,
not from Django settings — ``test_settings`` puts it in the ENVIRONMENT and says so
explicitly, because ``override_settings`` cannot supply it. So key loss is simulated
by patching the ``config`` reference inside ``crypto``, which is already the house
idiom (``tests_auth.ManagementCommandTests.test_refuses_without_encryption_key``).
"""
import io
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from platform_admin_app import crypto, lockout, recovery, second_factor, sessions, totp
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_ELEVATED,
    ADMIN_AUTH_LOGIN_SUCCESS,
    ADMIN_AUTH_RECOVERY_CODE_USED,
    ADMIN_AUTH_TOTP_FAILURE,
)
from platform_admin_app.endpoints.auth import GENERIC_VERIFY_ERROR
from platform_admin_app.models import (
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AdminSession,
    PlatformStaffAuth,
)
from platform_admin_app.second_factor import (
    FAILED_BAD_CODE,
    FAILED_NOT_ENROLLED,
    FAILED_TOTP_KEY_UNAVAILABLE,
    FAILED_UNKNOWN_METHOD,
    METHOD_RECOVERY,
    METHOD_TOTP,
)
from platform_admin_app.testing import AuditAssertionsMixin
from platform_admin_app.tests_auth import (
    _ADMIN_OVERRIDES,
    PASSWORD,
    ThrottleIsolationMixin,
    _code,
    _make_admin,
)

# An obviously non-Fernet string — exercises crypto's "invalid key material" arm,
# which is a different raise site from "key not set".
JUNK_KEY = 'not-a-urlsafe-base64-fernet-key'


def key_unavailable(value=None):
    """
    Simulate a lost (``value=None``) or corrupt ``ADMIN_SECRET_ENCRYPTION_KEY``.

    Patches the ``config`` callable inside ``crypto`` rather than the environment or
    Django settings — see the module docstring for why that is the only thing that
    works here. ``crypto`` looks up exactly one key, so this is precise.
    """
    return patch('platform_admin_app.crypto.config', return_value=value)


# --- The dispatcher, in isolation ---------------------------------------------------

class SecondFactorCheckTests(TestCase):
    """``second_factor.check`` is total: it returns a verdict, never an exception."""

    def setUp(self):
        super().setUp()
        _user, self.auth, self.secret, self.codes = _make_admin(username='sf-unit')

    def test_recovery_method_accepts_a_recovery_code_and_spends_it(self):
        verdict = second_factor.check(self.auth, METHOD_RECOVERY, self.codes[0])
        self.assertTrue(verdict.ok)
        self.assertTrue(verdict.used_recovery)
        self.assertEqual(recovery.remaining(self.auth), 9)

    def test_totp_method_accepts_a_totp_code(self):
        verdict = second_factor.check(self.auth, METHOD_TOTP, _code(self.secret))
        self.assertTrue(verdict.ok)
        self.assertFalse(verdict.used_recovery)

    def test_recovery_method_rejects_a_totp_code(self):
        verdict = second_factor.check(self.auth, METHOD_RECOVERY, _code(self.secret))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.error_code, FAILED_BAD_CODE)
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_totp_method_rejects_a_recovery_code_without_spending_it(self):
        verdict = second_factor.check(self.auth, METHOD_TOTP, self.codes[0])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.error_code, FAILED_BAD_CODE)
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_unknown_and_absent_methods_are_refused(self):
        for method in ('', 'webauthn', 'sms', None):
            with self.subTest(method=method):
                verdict = second_factor.check(
                    self.auth, second_factor.normalise_method(method), self.codes[0],
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.error_code, FAILED_UNKNOWN_METHOD)
        # Nothing above may have spent a code.
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_method_name_is_case_and_whitespace_insensitive(self):
        """The method is an enum, not a credential — the CODE is never normalised."""
        verdict = second_factor.check(
            self.auth, second_factor.normalise_method('  ReCoVeRy '), self.codes[0],
        )
        self.assertTrue(verdict.ok)
        self.assertTrue(verdict.used_recovery)

    def test_an_over_long_method_is_bounded_and_still_refused(self):
        """The normalised value lands in the audit log's unbounded ``reason``."""
        normalised = second_factor.normalise_method('x' * 5000)
        self.assertEqual(len(normalised), second_factor.MAX_METHOD_LENGTH)
        verdict = second_factor.check(self.auth, normalised, self.codes[0])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.error_code, FAILED_UNKNOWN_METHOD)
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_missing_auth_row_returns_a_verdict_not_an_exception(self):
        """``recovery.consume(None, …)`` would raise; elevate has no gate ahead of it."""
        for method in (METHOD_TOTP, METHOD_RECOVERY):
            with self.subTest(method=method):
                verdict = second_factor.check(None, method, 'anything')
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.error_code, FAILED_NOT_ENROLLED)

    def test_recovery_works_with_the_key_missing_or_invalid(self):
        for label, value in (('missing', None), ('invalid', JUNK_KEY)):
            with self.subTest(key=label):
                _u, auth, _s, codes = _make_admin(
                    email=f'sf-{label}@t.com', username=f'sf-key-{label}',
                )
                with key_unavailable(value):
                    verdict = second_factor.check(auth, METHOD_RECOVERY, codes[0])
                self.assertTrue(verdict.ok)
                self.assertTrue(verdict.used_recovery)

    def test_totp_with_an_unusable_key_fails_closed_without_raising(self):
        for label, value in (('missing', None), ('invalid', JUNK_KEY)):
            with self.subTest(key=label):
                with key_unavailable(value):
                    verdict = second_factor.check(
                        self.auth, METHOD_TOTP, _code(self.secret),
                    )
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.error_code, FAILED_TOTP_KEY_UNAVAILABLE)

    def test_the_recovery_branch_performs_no_crypto_at_all(self):
        """The whole point of the PR, asserted rather than reasoned about."""
        with patch('platform_admin_app.totp.decrypt_secret') as decrypt, \
                patch('platform_admin_app.crypto._fernet') as fernet, \
                patch('platform_admin_app.totp.verify') as verify:
            verdict = second_factor.check(self.auth, METHOD_RECOVERY, self.codes[0])

        self.assertTrue(verdict.ok)
        decrypt.assert_not_called()
        fernet.assert_not_called()
        verify.assert_not_called()

    def test_crypto_still_fails_loudly_for_enrolment(self):
        """
        The fail-closed contract is preserved where it belongs.

        The catch lives in the dispatcher, NOT in ``totp.verify`` — so provisioning
        still refuses to half-complete under a broken key.
        """
        with key_unavailable():
            with self.assertRaises(ImproperlyConfigured):
                totp.encrypt_for_storage('probe')
            with self.assertRaises(ImproperlyConfigured):
                totp.verify(self.auth, _code(self.secret))


# --- The endpoints -----------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class RecoveryWithoutKeyEndpointTests(
    ThrottleIsolationMixin, AuditAssertionsMixin, TestCase,
):
    """The key is gone. Recovery codes still get the administrator back in."""

    def setUp(self):
        super().setUp()
        # Enrolment happens with the key PRESENT: losing the key destroys the ability
        # to READ the stored ciphertext, not the ciphertext itself. So the eligibility
        # gate (which requires a non-empty totp_secret_encrypted) still passes.
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='nokey-admin',
        )
        self.client = Client()

    def _login(self, password=PASSWORD):
        # Clear the per-IP/-identity throttle counters: several of these tests make
        # more login attempts than the production rate allows, and the throttles are
        # defence in depth, not the property under test (the DB lockout is).
        cache.clear()
        return self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': password},
            content_type='application/json',
        )

    def _verify(self, code, method=METHOD_TOTP, **extra):
        payload = {'code': code}
        if method is not None:
            payload['method'] = method
        payload.update(extra)
        return self.client.post(
            '/admin/v1/auth/verify/', data=payload,
            content_type='application/json',
        )

    def _elevate(self, code, method=METHOD_TOTP):
        payload = {'code': code}
        if method is not None:
            payload['method'] = method
        return self.client.post(
            '/admin/v1/auth/elevate/', data=payload,
            content_type='application/json',
        )

    def _signed_in(self):
        """Reach a live, un-elevated session the ordinary way (key present)."""
        self._login()
        self.assertEqual(self._verify(_code(self.secret)).status_code, 200)
        AdminSession.objects.update(elevated_at=None)

    # --- the regression ------------------------------------------------------------

    def test_recovery_login_succeeds_with_the_key_missing(self):
        with key_unavailable():
            self.assertEqual(self._login().status_code, 200)
            response = self._verify(self.codes[0], METHOD_RECOVERY)

        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertTrue(data['used_recovery_code'])
        self.assertEqual(data['recovery_codes_remaining'], 9)
        self.assertEqual(AdminSession.objects.count(), 1)
        self.assertAudited(ADMIN_AUTH_RECOVERY_CODE_USED, result=RESULT_SUCCESS)

    def test_recovery_login_succeeds_with_an_invalid_key(self):
        with key_unavailable(JUNK_KEY):
            self.assertEqual(self._login().status_code, 200)
            response = self._verify(self.codes[0], METHOD_RECOVERY)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['used_recovery_code'])
        self.assertEqual(AdminSession.objects.count(), 1)

    def test_recovery_elevation_succeeds_with_the_key_missing(self):
        """Without this, a locked-out admin could sign in but never reach anything."""
        self._signed_in()

        with key_unavailable():
            response = self._elevate(self.codes[0], METHOD_RECOVERY)

        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertTrue(data['used_recovery_code'])
        self.assertEqual(data['recovery_codes_remaining'], 9)
        self.assertIsNotNone(AdminSession.objects.get().elevated_at)
        self.assertAudited(ADMIN_AUTH_RECOVERY_CODE_USED, result=RESULT_SUCCESS)

    def test_recovery_elevation_succeeds_with_an_invalid_key(self):
        self._signed_in()
        with key_unavailable(JUNK_KEY):
            self.assertEqual(
                self._elevate(self.codes[0], METHOD_RECOVERY).status_code, 200,
            )
        self.assertIsNotNone(AdminSession.objects.get().elevated_at)

    def test_no_decryption_on_the_recovery_path_at_either_endpoint(self):
        self._login()
        with patch('platform_admin_app.totp.decrypt_secret') as decrypt, \
                patch('platform_admin_app.crypto._fernet') as fernet, \
                patch('platform_admin_app.totp.verify') as verify:
            verify_response = self._verify(self.codes[0], METHOD_RECOVERY)
            AdminSession.objects.update(elevated_at=None)
            elevate_response = self._elevate(self.codes[1], METHOD_RECOVERY)

        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(elevate_response.status_code, 200)
        self.assertTrue(verify_response.json()['data']['used_recovery_code'])
        self.assertTrue(elevate_response.json()['data']['used_recovery_code'])
        decrypt.assert_not_called()
        fernet.assert_not_called()
        verify.assert_not_called()

    # --- totp with an unusable key ------------------------------------------------

    def test_totp_with_missing_key_fails_generically_and_counts_once(self):
        self._login()
        with key_unavailable():
            # Even a CORRECT code cannot be checked while the secret is unreadable.
            response = self._verify(_code(self.secret), METHOD_TOTP)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(), {'status': 401, 'message': GENERIC_VERIFY_ERROR},
        )
        body = response.content.decode()
        self.assertNotIn('ADMIN_SECRET_ENCRYPTION_KEY', body)
        self.assertNotIn('Fernet', body)
        self.assertEqual(AdminSession.objects.count(), 0)

        self.auth.refresh_from_db()
        self.assertEqual(self.auth.failed_attempts, 1)
        entry = self.assertAudited(ADMIN_AUTH_TOTP_FAILURE, result=RESULT_FAILURE)
        self.assertEqual(entry.error_code, FAILED_TOTP_KEY_UNAVAILABLE)
        self.assertEqual(entry.reason, f'method={METHOD_TOTP}')

    def test_totp_elevation_with_missing_key_fails_generically(self):
        self._signed_in()
        with key_unavailable():
            response = self._elevate(_code(self.secret, offset=1), METHOD_TOTP)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(), {'status': 403, 'message': GENERIC_VERIFY_ERROR},
        )
        self.assertIsNone(AdminSession.objects.get().elevated_at)

    # --- no oracle -----------------------------------------------------------------

    def test_wrong_method_for_the_code_is_byte_identical_to_a_wrong_code(self):
        """
        A recovery code sent as ``totp`` (and vice versa) must look exactly like a
        guess. Each attempt is measured on a fresh challenge, and lockout accounting
        is asserted to advance identically.
        """
        cases = {
            'genuinely wrong code': (METHOD_TOTP, '000000'),
            'recovery code as totp': (METHOD_TOTP, self.codes[0]),
            'totp code as recovery': (METHOD_RECOVERY, _code(self.secret)),
        }
        observed = {}
        for label, (method, code) in cases.items():
            lockout.reset(self.auth)
            self._login()
            response = self._verify(code, method)
            self.auth.refresh_from_db()
            observed[label] = (
                response.status_code, response.content, self.auth.failed_attempts,
            )

        baseline = observed['genuinely wrong code']
        for label, result in observed.items():
            self.assertEqual(result, baseline, f'{label} is distinguishable')

        # None of the mismatched attempts may have spent a recovery code.
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_absent_and_unrecognised_methods_are_byte_identical_to_a_wrong_code(self):
        cases = {
            'genuinely wrong code': dict(method=METHOD_TOTP, code='000000'),
            'method absent': dict(method=None, code='000000'),
            'method empty': dict(method='', code='000000'),
            'method unrecognised': dict(method='webauthn', code='000000'),
        }
        observed = {}
        for label, kwargs in cases.items():
            lockout.reset(self.auth)
            self._login()
            response = self._verify(kwargs['code'], kwargs['method'])
            self.auth.refresh_from_db()
            observed[label] = (
                response.status_code, response.content, self.auth.failed_attempts,
            )

        baseline = observed['genuinely wrong code']
        for label, result in observed.items():
            self.assertEqual(result, baseline, f'{label} is distinguishable')

    def test_unknown_method_is_audited_distinctly_though_invisible_to_the_client(self):
        """Generic to the caller, specific in the log — the DISCLOSURE convention."""
        self._login()
        self.assertEqual(self._verify('000000', 'webauthn').status_code, 401)
        entry = self.assertAudited(ADMIN_AUTH_TOTP_FAILURE, result=RESULT_FAILURE)
        self.assertEqual(entry.error_code, FAILED_UNKNOWN_METHOD)
        self.assertEqual(entry.reason, 'method=webauthn')

    def test_uppercase_method_is_accepted(self):
        self._login()
        response = self._verify(self.codes[0], 'RECOVERY')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['used_recovery_code'])

    # --- preserved properties ------------------------------------------------------

    def test_totp_still_works_normally_when_the_key_is_present(self):
        self._login()
        response = self._verify(_code(self.secret), METHOD_TOTP)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['used_recovery_code'])
        self.assertEqual(response.json()['data']['recovery_codes_remaining'], 10)
        self.assertAudited(ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS)

    def test_replay_guard_survives(self):
        self._login()
        code = _code(self.secret)
        self.assertEqual(self._verify(code, METHOD_TOTP).status_code, 200)
        self._login()
        self.assertEqual(self._verify(code, METHOD_TOTP).status_code, 401)

    def test_recovery_code_is_one_shot(self):
        self._login()
        self.assertEqual(
            self._verify(self.codes[0], METHOD_RECOVERY).status_code, 200,
        )
        self.client.post('/admin/v1/auth/logout/')
        self._login()
        self.assertEqual(
            self._verify(self.codes[0], METHOD_RECOVERY).status_code, 401,
        )

    def test_lockout_increments_then_resets_on_a_recovery_success(self):
        self._login()
        for expected in (1, 2):
            self._verify('000000', METHOD_TOTP)
            self.auth.refresh_from_db()
            self.assertEqual(self.auth.failed_attempts, expected)

        self._login()
        with key_unavailable():
            self.assertEqual(
                self._verify(self.codes[0], METHOD_RECOVERY).status_code, 200,
            )

        self.auth.refresh_from_db()
        self.assertEqual(self.auth.failed_attempts, 0)
        self.assertIsNone(self.auth.locked_until)

    def test_lockout_threshold_still_reached_through_the_new_path(self):
        for _ in range(lockout.threshold()):
            self._login()
            self._verify('000000', METHOD_TOTP)
        self.auth.refresh_from_db()
        self.assertTrue(lockout.is_locked(self.auth))

        # TOTP buys nothing while locked: the challenge a locked account receives is
        # recovery-only. A recovery code DOES clear the lock now — that break-glass
        # path is covered in tests_admin_lockout.py — so this asserts only that no
        # code was spent getting here.
        self._login()
        self.assertEqual(
            self._verify(_code(self.secret), METHOD_TOTP).status_code, 401,
        )
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_elevation_without_an_auth_row_denies_rather_than_500s(self):
        self._signed_in()
        PlatformStaffAuth.objects.filter(user=self.user).delete()
        response = self._elevate('anything', METHOD_RECOVERY)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(AdminSession.objects.get().elevated_at)

    def test_totp_elevation_still_works_normally(self):
        self._signed_in()
        response = self._elevate(_code(self.secret, offset=1), METHOD_TOTP)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['used_recovery_code'])
        self.assertAudited(ADMIN_AUTH_ELEVATED, result=RESULT_SUCCESS)


# --- The documented break-glass procedure ------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class BreakGlassSequenceTests(ThrottleIsolationMixin, TestCase):
    """
    The operator runbook in BACKGROUND_TASKS.md, exercised end to end.

    If this test needs changing, the runbook needs changing with it.
    """

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='breakglass-admin',
        )
        self.client = Client()

    def _login(self):
        cache.clear()
        return self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': PASSWORD},
            content_type='application/json',
        )

    def _verify(self, code, method):
        return self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': method, 'code': code},
            content_type='application/json',
        )

    def test_key_loss_to_working_totp_in_four_steps(self):
        # --- 1. The key is lost. TOTP cannot verify; the recovery code is the way in.
        with key_unavailable():
            self.assertEqual(self._login().status_code, 200)
            self.assertEqual(
                self._verify(_code(self.secret), METHOD_TOTP).status_code, 401,
                'TOTP must not verify while the key is missing',
            )
            self._login()
            self.assertEqual(
                self._verify(self.codes[0], METHOD_RECOVERY).status_code, 200,
                'the recovery code is the ONLY way in and it must work',
            )
        self.assertEqual(AdminSession.objects.filter(revoked_at__isnull=True).count(), 1)

        # --- 2. A NEW key is installed. The old ciphertext is unreadable under it —
        #        exactly the real situation, and why step 3 re-provisions rather than
        #        decrypts.
        new_key = Fernet.generate_key().decode()

        with patch('platform_admin_app.crypto.config', return_value=new_key):
            with self.assertRaises(Exception):
                crypto.decrypt_secret(self.auth.totp_secret_encrypted)

            # --- 3. Re-provision under the new key. Needs the key, hence step 2 first.
            call_command(
                'reset_platform_admin_totp', username=self.user.username,
                noinput=True, stdout=io.StringIO(),
            )

            # Sessions were revoked by the reset, so the operator signs in again.
            self.assertEqual(
                AdminSession.objects.filter(revoked_at__isnull=True).count(), 0,
            )

            # --- 4. Re-enrol: the fresh secret is readable under the new key, and a
            #        code generated from it now verifies.
            self.auth.refresh_from_db()
            new_secret = crypto.decrypt_secret(self.auth.totp_secret_encrypted)
            self.assertNotEqual(new_secret, self.secret)

            self.client = Client()
            self.assertEqual(self._login().status_code, 200)
            response = self._verify(_code(new_secret), METHOD_TOTP)

        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertFalse(data['used_recovery_code'])
        # The reset regenerated all ten codes; the one spent in step 1 is gone with
        # the old set.
        self.assertEqual(data['recovery_codes_remaining'], 10)

    def test_reset_refuses_while_the_key_is_still_missing(self):
        """Why the runbook installs the key BEFORE running the command."""
        with key_unavailable():
            with self.assertRaises(Exception):
                call_command(
                    'reset_platform_admin_totp', username=self.user.username,
                    noinput=True, stdout=io.StringIO(),
                )
        # Nothing was re-provisioned.
        before = self.auth.totp_secret_encrypted
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.totp_secret_encrypted, before)


# --- Guard against regrowth ---------------------------------------------------------

class DispatchStructureTests(TestCase):
    """
    Structural guards: the properties that make the fix hard to undo by accident.

    A future edit that reinstates a TOTP fallback, or that starts inferring the method
    from the code's shape, has to delete one of these to land.
    """

    def test_endpoint_module_does_not_import_totp(self):
        """
        The HTTP layer cannot call ``totp.verify`` at all any more.

        This is what makes "recovery never touches the key" visible at the top of
        ``endpoints/auth.py`` rather than buried in a branch.
        """
        from platform_admin_app.endpoints import auth as auth_endpoints

        self.assertFalse(
            hasattr(auth_endpoints, 'totp'),
            'endpoints/auth.py imports totp again — the second-factor dispatch '
            'belongs to platform_admin_app.second_factor.',
        )

    def test_method_vocabulary_is_exactly_totp_and_recovery(self):
        self.assertEqual(
            second_factor.SECOND_FACTOR_METHODS, (METHOD_TOTP, METHOD_RECOVERY),
        )

    def test_a_passing_verdict_names_at_most_one_factor(self):
        _u, auth, secret, codes = _make_admin(username='sf-structure')
        totp_verdict = second_factor.check(auth, METHOD_TOTP, _code(secret))
        recovery_verdict = second_factor.check(auth, METHOD_RECOVERY, codes[0])
        self.assertFalse(totp_verdict.used_recovery)
        self.assertTrue(recovery_verdict.used_recovery)

    def test_sessions_module_needs_no_encryption_key(self):
        """
        Session resolution must stay key-independent, or ``elevate/`` would 401 before
        the recovery branch could ever run.
        """
        user, _auth, _secret, _codes = _make_admin(username='sf-session')
        raw, _session = sessions.create_session(user)
        with key_unavailable():
            self.assertIsNotNone(sessions.resolve_session(raw))
