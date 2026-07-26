"""
PR-C atomicity suite — nothing is consumed unless a session comes out of it.

``verify/`` and ``elevate/`` used to consume the second factor OUTSIDE the transaction
that minted the session, and audit AFTER it committed. So a failure anywhere downstream
left the factor spent with nothing to show for it: a recovery code permanently burned,
or a TOTP counter advanced past a code the operator had just typed correctly. And the
inverse — a live ``AdminSession`` with no successful-login audit row — contradicted the
"no audit, no action" contract ``audit.py`` documents.

Every test here injects a failure at one of those two points and asserts the whole
request unwound. Before PR-C every one of them failed — the recovery code WAS consumed,
the TOTP counter HAD advanced, and (for the audit case) the session survived. That is
what they exist to keep from coming back.

These are plain ``TestCase`` — no threads needed, so no Postgres requirement. The
concurrency half of PR-C lives in ``tests_admin_auth_concurrency.py``.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from platform_admin_app import recovery
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_ELEVATED,
    ADMIN_AUTH_LOGIN_SUCCESS,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import AdminLoginChallenge, AdminSession
from platform_admin_app.second_factor import METHOD_RECOVERY, METHOD_TOTP
from platform_admin_app.tests_auth import (
    _ADMIN_OVERRIDES,
    PASSWORD,
    ThrottleIsolationMixin,
    _code,
    _make_admin,
)

# Injected at the two points that used to sit outside the transaction.
BOOM = RuntimeError('injected failure')


def _explode(*args, **kwargs):
    raise BOOM


@override_settings(**_ADMIN_OVERRIDES)
class VerifyAtomicityTests(ThrottleIsolationMixin, TestCase):
    """A failure after the factor is checked must leave the factor unspent."""

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='atomic-verify',
        )
        self.client = Client()

    def _login(self):
        cache.clear()
        return self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': PASSWORD},
            content_type='application/json',
        )

    def _verify(self, code, method=METHOD_TOTP):
        return self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': method, 'code': code},
            content_type='application/json',
        )

    def _assert_nothing_consumed(self):
        """The invariant oracle: no session, and every factor still spendable."""
        self.auth.refresh_from_db()
        self.assertEqual(
            AdminSession.objects.count(), 0, 'a session survived a failed verify',
        )
        self.assertIsNone(
            self.auth.last_totp_counter,
            'the TOTP replay counter advanced despite the failure',
        )
        self.assertEqual(
            recovery.remaining(self.auth), 10,
            'a recovery code was consumed despite the failure',
        )
        self.assertIsNone(
            AdminLoginChallenge.objects.get().consumed_at,
            'the challenge was consumed despite the failure',
        )

    def test_session_creation_failure_unwinds_a_totp_verification(self):
        self._login()
        with patch(
            'platform_admin_app.sessions.create_session', side_effect=_explode,
        ):
            with self.assertRaises(RuntimeError):
                self._verify(_code(self.secret), METHOD_TOTP)
        self._assert_nothing_consumed()

    def test_session_creation_failure_unwinds_a_recovery_verification(self):
        """The expensive one: a burned recovery code cannot be re-issued."""
        self._login()
        with patch(
            'platform_admin_app.sessions.create_session', side_effect=_explode,
        ):
            with self.assertRaises(RuntimeError):
                self._verify(self.codes[0], METHOD_RECOVERY)
        self._assert_nothing_consumed()

        # And the code really is still good afterwards.
        self._login()
        self.assertEqual(self._verify(self.codes[0], METHOD_RECOVERY).status_code, 200)

    def test_success_audit_failure_unwinds_everything_including_the_session(self):
        """
        No audit, no action — the contract ``audit.py`` documents.

        Only the SUCCESS entry is made to fail; the failure-path entries must keep
        working, or this would prove nothing about ordering.
        """
        self._login()
        with patch(
            'platform_admin_app.audit.record_auth_event',
            side_effect=self._raise_on_success,
        ):
            with self.assertRaises(RuntimeError):
                self._verify(_code(self.secret), METHOD_TOTP)
        self._assert_nothing_consumed()

    @staticmethod
    def _raise_on_success(request, action, **kwargs):
        if action == ADMIN_AUTH_LOGIN_SUCCESS:
            raise BOOM
        return None

    def test_elevate_stamp_failure_unwinds_the_factor(self):
        """Same property on the other endpoint, which had no transaction at all."""
        self._login()
        self.assertEqual(self._verify(_code(self.secret), METHOD_TOTP).status_code, 200)
        AdminSession.objects.update(elevated_at=None)
        self.auth.refresh_from_db()
        counter_before = self.auth.last_totp_counter

        with patch('platform_admin_app.sessions.elevate', side_effect=_explode):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    '/admin/v1/auth/elevate/',
                    data={'method': METHOD_RECOVERY, 'code': self.codes[0]},
                    content_type='application/json',
                )

        self.auth.refresh_from_db()
        self.assertIsNone(AdminSession.objects.get().elevated_at)
        self.assertEqual(recovery.remaining(self.auth), 10)
        self.assertEqual(self.auth.last_totp_counter, counter_before)

    def test_elevate_audit_failure_unwinds_the_elevation(self):
        self._login()
        self.assertEqual(self._verify(_code(self.secret), METHOD_TOTP).status_code, 200)
        AdminSession.objects.update(elevated_at=None)

        def raise_on_elevated(request, action, **kwargs):
            if action == ADMIN_AUTH_ELEVATED:
                raise BOOM
            return None

        with patch(
            'platform_admin_app.audit.record_auth_event',
            side_effect=raise_on_elevated,
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    '/admin/v1/auth/elevate/',
                    data={'method': METHOD_TOTP,
                          'code': _code(self.secret, offset=1)},
                    content_type='application/json',
                )

        self.auth.refresh_from_db()
        self.assertIsNone(
            AdminSession.objects.get().elevated_at,
            'elevation committed without its audit row',
        )

    def test_lost_challenge_race_gives_the_factor_back(self):
        """
        The one path that must roll back — and still audit.

        Forcing the conditional consume to report a loss stands in for another request
        having spent the challenge first. The factor was already consumed in that
        transaction, so it has to be returned; the audit row is written outside the
        aborted block and must survive.
        """
        self._login()
        with patch('platform_admin_app.challenges.consume', return_value=False):
            response = self._verify(self.codes[0], METHOD_RECOVERY)

        self.assertEqual(response.status_code, 401)
        self._assert_nothing_consumed()
        from platform_admin_app.models import AdminAuditLog
        self.assertTrue(
            AdminAuditLog.objects.filter(error_code='challenge_race').exists(),
            'the lost race rolled back its own audit row',
        )


@override_settings(**_ADMIN_OVERRIDES)
class SuccessPathStillCommitsTests(ThrottleIsolationMixin, TestCase):
    """The mirror image: with nothing injected, everything lands exactly once."""

    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='atomic-happy',
        )
        self.client = Client()

    def test_ordinary_login_commits_session_audit_and_cookie_together(self):
        cache.clear()
        self.client.post(
            '/admin/v1/auth/login/',
            data={'username': self.user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        response = self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': METHOD_TOTP, 'code': _code(self.secret)},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(cookie_name(), response.cookies)
        self.assertEqual(AdminSession.objects.count(), 1)
        self.assertIsNotNone(AdminLoginChallenge.objects.get().consumed_at)
        self.auth.refresh_from_db()
        self.assertIsNotNone(self.auth.last_totp_counter)

        from platform_admin_app.models import AdminAuditLog
        self.assertEqual(
            AdminAuditLog.objects.filter(action=ADMIN_AUTH_LOGIN_SUCCESS).count(), 1,
        )
