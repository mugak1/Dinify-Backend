"""
PR-C lockout suite — progressive backoff, and a way out that an attacker cannot trigger.

The old policy (5 combined failures → a flat 15-minute window, with locked accounts
refusing even a correct password) was a denial-of-service on a platform with ONE
administrator and a discoverable username: anybody who learned it could hold the
founder out indefinitely for the cost of five wrong passwords every quarter of an hour.

The policy now escalates instead of latching, and there are two ways out that a lockout
attacker cannot reach because both need a secret they do not hold:

* over HTTP — a correct password plus a one-shot RECOVERY code. ``login/`` issues a
  ``recovery_only`` challenge to a locked account, ``verify/`` refuses TOTP against it,
  and a valid recovery code clears the lock and signs in;
* on the box — ``manage.py unlock_platform_admin``.

Throughout, denial bodies stay generic: the lockout must never become an
account-existence oracle.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_RESTAURANT_USER
from platform_admin_app import lockout, recovery
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_LOCKOUT,
    ADMIN_AUTH_LOCKOUT_CLEARED,
    ADMIN_AUTH_LOGIN_FAILURE,
)
from platform_admin_app.cookies import challenge_cookie_name, cookie_name
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_SUCCESS,
    AdminLoginChallenge,
    AdminSession,
    PlatformStaffAuth,
)
from platform_admin_app.second_factor import METHOD_RECOVERY, METHOD_TOTP
from platform_admin_app.testing import AuditAssertionsMixin
from platform_admin_app.tests_auth import (
    _ADMIN_OVERRIDES,
    PASSWORD,
    ThrottleIsolationMixin,
    _code,
    _make_admin,
    _make_user,
)


# --- Policy arithmetic --------------------------------------------------------------

class BackoffPolicyTests(TestCase):
    """The approved table: threshold 10, doubling from 1 minute, capped at 60."""

    def test_threshold_is_ten(self):
        self.assertEqual(lockout.threshold(), 10)

    def test_backoff_table(self):
        expected = {
            10: 1, 11: 2, 12: 4, 13: 8, 14: 16, 15: 32,
            16: 60, 17: 60, 40: 60,          # capped
        }
        for attempts, minutes in expected.items():
            with self.subTest(failures=attempts):
                self.assertEqual(
                    lockout.backoff_for(attempts), timedelta(minutes=minutes),
                )

    def test_below_the_threshold_never_locks(self):
        _user, auth, _secret, _codes = _make_admin(username='lock-below')
        for expected in range(1, lockout.threshold()):
            self.assertFalse(lockout.register_failure(auth), f'locked at {expected}')
            self.assertFalse(lockout.is_locked(auth))
        self.assertEqual(auth.failed_attempts, lockout.threshold() - 1)

    def test_threshold_locks_and_each_further_failure_escalates(self):
        _user, auth, _secret, _codes = _make_admin(username='lock-escalate')
        for _ in range(lockout.threshold() - 1):
            lockout.register_failure(auth)

        self.assertTrue(lockout.register_failure(auth))
        first_window = auth.locked_until - timezone.now()
        self.assertLessEqual(first_window, timedelta(minutes=1))

        self.assertTrue(lockout.register_failure(auth))
        second_window = auth.locked_until - timezone.now()
        self.assertGreater(second_window, first_window)

    def test_counter_is_cumulative_across_an_expired_window(self):
        """
        An elapsed window does not forgive the count.

        This is deliberate: it is what makes the backoff escalate rather than reset to
        one minute forever. Only a successful verification, the break-glass path, or
        the unlock command clears it.
        """
        _user, auth, _secret, _codes = _make_admin(username='lock-cumulative')
        for _ in range(lockout.threshold()):
            lockout.register_failure(auth)

        auth.locked_until = timezone.now() - timedelta(seconds=1)
        auth.save(update_fields=['locked_until'])
        self.assertFalse(lockout.is_locked(auth))

        # One more failure re-locks immediately, at the next step up.
        self.assertTrue(lockout.register_failure(auth))
        self.assertTrue(lockout.is_locked(auth))

    def test_reset_clears_counter_and_lock(self):
        _user, auth, _secret, _codes = _make_admin(username='lock-reset')
        for _ in range(lockout.threshold()):
            lockout.register_failure(auth)
        lockout.reset(auth)
        auth.refresh_from_db()
        self.assertEqual(auth.failed_attempts, 0)
        self.assertIsNone(auth.locked_until)

    @override_settings(
        ADMIN_LOCKOUT_THRESHOLD=3,
        ADMIN_LOCKOUT_BACKOFF_BASE=timedelta(seconds=30),
        ADMIN_LOCKOUT_BACKOFF_CAP=timedelta(minutes=2),
    )
    def test_policy_is_settings_driven(self):
        """
        The numbers are configuration, not constants baked into the logic.

        Worth asserting because the test suite never loads ``settings_admin`` — the
        production values live there and only the in-code defaults are exercised
        otherwise, so the override path has to work.
        """
        self.assertEqual(lockout.threshold(), 3)
        self.assertEqual(lockout.backoff_for(3), timedelta(seconds=30))
        self.assertEqual(lockout.backoff_for(4), timedelta(minutes=1))
        self.assertEqual(lockout.backoff_for(99), timedelta(minutes=2))


# --- The break-glass path over HTTP ------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class LockoutBreakGlassTests(ThrottleIsolationMixin, AuditAssertionsMixin, TestCase):
    """A correct password plus a recovery code gets a locked-out founder back in."""

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='breakglass-lock',
        )
        self.client = Client()
        self._lock()

    def _lock(self, minutes=60):
        """Lock the account directly — the arithmetic is covered above."""
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            failed_attempts=lockout.threshold(),
            locked_until=timezone.now() + timedelta(minutes=minutes),
        )
        self.auth.refresh_from_db()
        self.assertTrue(lockout.is_locked(self.auth))

    def _login(self, password=PASSWORD):
        cache.clear()
        return self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': password},
            content_type='application/json',
        )

    def _verify(self, code, method=METHOD_TOTP):
        return self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': method, 'code': code},
            content_type='application/json',
        )

    def test_wrong_password_while_locked_is_still_refused_generically(self):
        response = self._login(password='wrong')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(), {'status': 401, 'message': 'Invalid credentials.'},
        )
        self.assertNotIn(challenge_cookie_name(), response.cookies)
        self.assertEqual(AdminLoginChallenge.objects.count(), 0)

    def test_correct_password_while_locked_yields_a_recovery_only_challenge(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['recovery_code_required'])
        self.assertIn(challenge_cookie_name(), response.cookies)
        self.assertTrue(AdminLoginChallenge.objects.get().recovery_only)
        self.assertEqual(AdminSession.objects.count(), 0)

    def test_totp_is_refused_on_a_recovery_only_challenge(self):
        """
        The property that keeps this path safe.

        A lockout attacker can make TOTP fail; they cannot produce a recovery code. So
        the break-glass challenge must accept nothing else, or it would just be a
        lockout with extra steps.
        """
        self._login()
        response = self._verify(_code(self.secret), METHOD_TOTP)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(AdminSession.objects.count(), 0)
        self.auth.refresh_from_db()
        self.assertTrue(lockout.is_locked(self.auth))

    def test_recovery_code_clears_the_lockout_and_signs_in(self):
        self._login()
        response = self._verify(self.codes[0], METHOD_RECOVERY)

        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertTrue(data['used_recovery_code'])
        self.assertTrue(data['lockout_cleared'])
        self.assertEqual(data['recovery_codes_remaining'], 9)
        self.assertIn(cookie_name(), response.cookies)
        self.assertEqual(AdminSession.objects.count(), 1)

        self.auth.refresh_from_db()
        self.assertFalse(lockout.is_locked(self.auth))
        self.assertEqual(self.auth.failed_attempts, 0)
        self.assertIsNone(self.auth.locked_until)

    def test_the_unlock_is_audited_distinctly_and_exactly_once(self):
        self._login()
        self._verify(self.codes[0], METHOD_RECOVERY)
        entry = self.assertAudited(
            ADMIN_AUTH_LOCKOUT_CLEARED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.actor_id, self.user.id)
        self.assertEqual(entry.reason, f'method={METHOD_RECOVERY}')
        # Emitted INSTEAD of the ordinary success entry, so the count stays one.
        self.assertNotAudited(ADMIN_AUTH_LOCKOUT, result=RESULT_SUCCESS)

    def test_normal_login_is_unaffected_once_unlocked(self):
        lockout.reset(self.auth)
        cache.clear()
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['recovery_code_required'])
        self.assertFalse(AdminLoginChallenge.objects.get().recovery_only)

        verified = self._verify(_code(self.secret), METHOD_TOTP)
        self.assertEqual(verified.status_code, 200)
        self.assertFalse(verified.json()['data']['lockout_cleared'])

    def test_lockout_is_not_an_account_existence_oracle(self):
        """A locked real account and an unknown username look identical."""
        locked = self._login(password='wrong')
        cache.clear()
        unknown = Client().post(
            '/admin/v1/auth/login/',
            data={'username': 'no-such-admin', 'password': 'wrong'},
            content_type='application/json',
        )
        self.assertEqual(locked.status_code, unknown.status_code)
        self.assertEqual(locked.content, unknown.content)

    def test_a_restaurant_user_cannot_reach_the_break_glass_path(self):
        """Eligibility still gates it: only platform staff get a challenge."""
        outsider = _make_user('bg-rest@t.com', ACCOUNT_TYPE_RESTAURANT_USER,
                              username='bg-rest-user')
        cache.clear()
        response = Client().post(
            '/admin/v1/auth/login/',
            data={'username': outsider.username, 'password': PASSWORD},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(AdminLoginChallenge.objects.count(), 0)


# --- Elevation under lockout --------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class ElevationEligibilityTests(ThrottleIsolationMixin, TestCase):
    """``elevate/`` re-checks eligibility under the lock for the first time."""

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='elev-eligible',
        )
        self.client = Client()
        cache.clear()
        self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': METHOD_TOTP, 'code': _code(self.secret)},
            content_type='application/json',
        )
        AdminSession.objects.update(elevated_at=None)

    def _elevate(self, code, method=METHOD_TOTP):
        return self.client.post(
            '/admin/v1/auth/elevate/',
            data={'method': method, 'code': code},
            content_type='application/json',
        )

    def test_a_deactivated_account_cannot_elevate_and_spends_nothing(self):
        """
        A session minted before the account was disabled must not step up.

        ``AdminSessionAuthentication`` already refuses an inactive account before
        dispatch (401), so the view's own eligibility re-check is defence in depth
        rather than a hole being closed — what matters here is that the request is
        refused AND no second factor is spent on the way to the refusal.
        """
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])

        response = self._elevate(self.codes[0], METHOD_RECOVERY)
        self.assertIn(response.status_code, (401, 403))
        self.assertIsNone(AdminSession.objects.get().elevated_at)
        self.auth.refresh_from_db()
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_an_unenrolled_auth_row_denies_rather_than_elevating(self):
        """
        The one eligibility condition authentication does NOT cover.

        Clearing the stored secret leaves the session valid as far as the
        authenticator is concerned, so the view is the only thing standing between a
        credential-less account and an elevated session.
        """
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            totp_secret_encrypted='',
        )
        response = self._elevate(self.codes[0], METHOD_RECOVERY)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(AdminSession.objects.get().elevated_at)
        self.auth.refresh_from_db()
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_a_locked_account_cannot_elevate(self):
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            failed_attempts=lockout.threshold(),
            locked_until=timezone.now() + timedelta(minutes=30),
        )
        response = self._elevate(_code(self.secret, offset=1))
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(AdminSession.objects.get().elevated_at)
        # No recovery code was spent reaching that denial.
        self.auth.refresh_from_db()
        self.assertEqual(recovery.remaining(self.auth), 10)

    def test_an_eligible_account_still_elevates(self):
        response = self._elevate(_code(self.secret, offset=1))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(AdminSession.objects.get().elevated_at)


# --- The shell unlock path ----------------------------------------------------------

class UnlockCommandTests(AuditAssertionsMixin, TestCase):
    """``manage.py unlock_platform_admin`` — the documented shell escape."""

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='unlock-admin',
        )

    def _lock(self):
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            failed_attempts=lockout.threshold() + 3,
            locked_until=timezone.now() + timedelta(minutes=60),
        )
        self.auth.refresh_from_db()

    def test_clears_the_counter_and_the_lock(self):
        self._lock()
        call_command('unlock_platform_admin', username='unlock-admin')

        self.auth.refresh_from_db()
        self.assertEqual(self.auth.failed_attempts, 0)
        self.assertIsNone(self.auth.locked_until)
        self.assertFalse(lockout.is_locked(self.auth))

    def test_audits_the_unlock(self):
        self._lock()
        call_command('unlock_platform_admin', username='unlock-admin')
        entry = self.assertAudited(
            ADMIN_AUTH_LOCKOUT_CLEARED, result=RESULT_SUCCESS,
        )
        self.assertIn('unlock_platform_admin', entry.reason)
        self.assertEqual(entry.before_state['failed_attempts'], lockout.threshold() + 3)
        self.assertEqual(entry.after_state['failed_attempts'], 0)

    def test_leaves_credentials_untouched(self):
        """It is a latch, not the reset_platform_admin_totp hammer."""
        self._lock()
        secret_before = self.auth.totp_secret_encrypted
        hashes_before = list(self.auth.recovery_code_hashes)

        call_command('unlock_platform_admin', username='unlock-admin')

        self.auth.refresh_from_db()
        self.assertEqual(self.auth.totp_secret_encrypted, secret_before)
        self.assertEqual(self.auth.recovery_code_hashes, hashes_before)
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_works_without_the_encryption_key(self):
        """It never decrypts, so it works in the middle of a Fernet-key incident."""
        self._lock()
        with patch('platform_admin_app.crypto.config', return_value=None):
            call_command('unlock_platform_admin', username='unlock-admin')
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.failed_attempts, 0)

    def test_is_idempotent_on_an_unlocked_account(self):
        call_command('unlock_platform_admin', username='unlock-admin')
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.failed_attempts, 0)

    def test_refuses_unknown_and_non_platform_accounts(self):
        with self.assertRaises(CommandError):
            call_command('unlock_platform_admin', username='no-such-account')

        outsider = _make_user('unlock-rest@t.com', ACCOUNT_TYPE_RESTAURANT_USER,
                              username='unlock-rest-user')
        with self.assertRaises(CommandError):
            call_command('unlock_platform_admin', username=outsider.username)
