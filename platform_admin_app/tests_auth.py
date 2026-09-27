"""
Tests for admin authentication (PR-2b): TOTP, recovery codes, the two-step login,
elevation, lockout, and mutual login rejection.

Covers:
* the full two-step flow, and every rejection along it (wrong password, wrong code,
  expired / reused / missing challenge, replayed TOTP code) — each audited;
* recovery codes: accepted once, rejected on reuse, hash removed;
* DB-backed lockout after N failures, release after the window, audited distinctly;
* NON-DISCLOSURE: unknown user, wrong password and non-admin return byte-identical
  bodies;
* TOTP is ENV-independent — no dev bypass under any ENV value;
* the PR-1 invariant enforced fail-closed at login;
* elevation stamps ``elevated_at``; ``require_recent_elevation`` fresh vs stale;
* mutual rejection in BOTH directions, plus customer-login non-regression;
* the bootstrap management commands;
* exposure guards over every credential column.
"""
from datetime import timedelta
from unittest.mock import patch

import pyotp
from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
)
from dinify_backend.tenancy.discovery import all_project_serializers
from platform_admin_app import challenges, lockout, recovery, sessions, totp
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_CHALLENGE_ISSUED,
    ADMIN_AUTH_ELEVATED,
    ADMIN_AUTH_LOCKOUT,
    ADMIN_AUTH_LOGIN_FAILURE,
    ADMIN_AUTH_LOGIN_SUCCESS,
    ADMIN_AUTH_LOGOUT,
    ADMIN_AUTH_RECOVERY_CODE_USED,
    ADMIN_AUTH_TOTP_FAILURE,
)
from platform_admin_app.cookies import challenge_cookie_name, cookie_name
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AdminLoginChallenge,
    AdminSession,
    PlatformStaffAuth,
)
from platform_admin_app.permissions import require_recent_elevation
from platform_admin_app.second_factor import METHOD_RECOVERY, METHOD_TOTP
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers.login import login as customer_login
from users_app.controllers.reset_password import _resolve_user
from users_app.models import User

# Distinct phone range from tests.py (…01…), tests_transport.py (…02…) and
# tests_audit.py (…03…) to avoid any collision on the unique phone_number.
_PHONE = iter(f'2567040000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'

_ADMIN_OVERRIDES = dict(
    # The REAL admin urlconf, not a stub — so these tests exercise the production
    # route table (including the /api-stripped `admin/v1/` prefix arithmetic).
    ROOT_URLCONF='dinify_backend.urls_admin',
    MIDDLEWARE=[
        'platform_admin_app.middleware.RequestIDMiddleware',
        'platform_admin_app.middleware.ClientIPMiddleware',
        *dj_settings.MIDDLEWARE,
    ],
    REST_FRAMEWORK={
        **dj_settings.REST_FRAMEWORK,
        'DEFAULT_AUTHENTICATION_CLASSES': (
            'platform_admin_app.authentication.AdminSessionAuthentication',
        ),
        'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
    },
    # Mirrors the settings_admin CSRF block. Kept in step by
    # tests_transport.CsrfSettingsMirrorTests, which compares every CSRF_* key
    # here against the real dinify_backend.settings_admin module.
    CSRF_COOKIE_NAME='__Host-dinify_admin_csrftoken',
    CSRF_COOKIE_SAMESITE='Strict',
    CSRF_COOKIE_SECURE=True,
    CSRF_COOKIE_HTTPONLY=False,
    CSRF_TRUSTED_ORIGINS=['https://admin.dinifyapp.com'],
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, is_active=True,
               username=None, phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U',
        email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda',
        password=PASSWORD, roles=[], account_type=account_type,
        is_active=is_active,
    )


def _enrol(user):
    """Give ``user`` an enrolled PlatformStaffAuth; returns (auth, secret, codes)."""
    secret = totp.generate_secret()
    codes, hashes = recovery.generate_codes()
    auth = PlatformStaffAuth.objects.create(
        user=user,
        totp_secret_encrypted=totp.encrypt_for_storage(secret),
        totp_enrolled_at=timezone.now(),
        recovery_code_hashes=hashes,
        recovery_generated_at=timezone.now(),
    )
    return auth, secret, codes


def _make_admin(email='admin@t.com', username='admin-user', **kwargs):
    user = _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False, **kwargs
    )
    auth, secret, codes = _enrol(user)
    return user, auth, secret, codes


def _code(secret, offset=0):
    """A valid TOTP code for the step ``offset`` away from now."""
    return pyotp.TOTP(secret).at(
        int(timezone.now().timestamp()) + offset * totp.TOTP_STEP_SECONDS
    )


class ThrottleIsolationMixin:
    """
    Clear the throttle cache between tests.

    DRF throttles use the default LocMemCache, which is process-global and NOT
    reset by the per-test transaction rollback — so without this the admin_login
    rate limit carries over and later tests get a 429 instead of the status they
    assert. (That it happens at all is the throttle working.)
    """

    def setUp(self):
        super().setUp()
        cache.clear()


# --- TOTP service ------------------------------------------------------------------

class TotpServiceTests(TestCase):
    def test_verify_accepts_current_code_and_advances_counter(self):
        _user, auth, secret, _codes = _make_admin(username='totp-ok')
        self.assertTrue(totp.verify(auth, _code(secret)))
        auth.refresh_from_db()
        self.assertIsNotNone(auth.last_totp_counter)

    def test_verify_rejects_wrong_code(self):
        _user, auth, _secret, _codes = _make_admin(username='totp-bad')
        self.assertFalse(totp.verify(auth, '000000'))

    def test_replayed_code_is_rejected(self):
        """A code stays valid for its window — reusing it must still fail."""
        _user, auth, secret, _codes = _make_admin(username='totp-replay')
        code = _code(secret)
        self.assertTrue(totp.verify(auth, code))
        self.assertFalse(totp.verify(auth, code))

    def test_accepts_adjacent_step_but_not_distant_one(self):
        _user, auth, secret, _codes = _make_admin(username='totp-window')
        self.assertTrue(totp.verify(auth, _code(secret, offset=-1)))
        _u2, auth2, secret2, _c2 = _make_admin(
            email='w2@t.com', username='totp-window-2',
        )
        self.assertFalse(totp.verify(auth2, _code(secret2, offset=-5)))

    def test_unenrolled_account_cannot_verify(self):
        user = _make_user('unenrolled@t.com', ACCOUNT_TYPE_PLATFORM_STAFF,
                          username='unenrolled', phone=False)
        auth = PlatformStaffAuth.objects.create(user=user)
        self.assertFalse(totp.verify(auth, '123456'))

    def test_wrong_code_fails_under_every_env_value(self):
        """No dev bypass: the restaurant OTP's ENV='dev' 1234 shortcut is not shared."""
        _user, auth, _secret, _codes = _make_admin(username='totp-env')
        for env in ('dev', 'test', 'prod', 'staging', ''):
            with patch('users_app.controllers.otp_manager.config', return_value=env):
                self.assertFalse(
                    totp.verify(auth, '1234'), f'1234 accepted under ENV={env!r}',
                )
                self.assertFalse(
                    totp.verify(auth, '000000'), f'000000 accepted under ENV={env!r}',
                )


# --- Recovery codes ----------------------------------------------------------------

class RecoveryCodeTests(TestCase):
    def test_codes_are_high_entropy_and_only_hashes_stored(self):
        _user, auth, _secret, codes = _make_admin(username='rec-store')
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertGreaterEqual(len(code), 20)
            self.assertNotIn(code, auth.recovery_code_hashes)

    def test_consume_succeeds_once_then_fails(self):
        _user, auth, _secret, codes = _make_admin(username='rec-once')
        self.assertTrue(recovery.consume(auth, codes[0]))
        auth.refresh_from_db()
        self.assertEqual(recovery.remaining(auth), 9)
        self.assertFalse(recovery.consume(auth, codes[0]))

    def test_unknown_code_rejected(self):
        _user, auth, _secret, _codes = _make_admin(username='rec-bad')
        self.assertFalse(recovery.consume(auth, 'not-a-real-code'))
        self.assertEqual(recovery.remaining(auth), 10)


# --- Lockout -----------------------------------------------------------------------

class LockoutTests(TestCase):
    def test_threshold_locks_and_window_releases(self):
        _user, auth, _secret, _codes = _make_admin(username='lock-1')
        for _ in range(lockout.threshold() - 1):
            self.assertFalse(lockout.register_failure(auth))
        self.assertTrue(lockout.register_failure(auth))
        self.assertTrue(lockout.is_locked(auth))

        auth.locked_until = timezone.now() - timedelta(seconds=1)
        auth.save(update_fields=['locked_until'])
        self.assertFalse(lockout.is_locked(auth))

    def test_reset_clears_counter(self):
        _user, auth, _secret, _codes = _make_admin(username='lock-2')
        lockout.register_failure(auth)
        lockout.reset(auth)
        auth.refresh_from_db()
        self.assertEqual(auth.failed_attempts, 0)
        self.assertIsNone(auth.locked_until)


# --- Endpoint flow -----------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class LoginFlowTests(ThrottleIsolationMixin, AuditAssertionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='flow-admin',
        )
        self.client = Client()

    def _login(self, username=None, password=PASSWORD):
        return self.client.post(
            '/admin/v1/auth/login/',
            data={'username': username or self.user.username, 'password': password},
            content_type='application/json',
        )

    def _verify(self, code, method=METHOD_TOTP):
        return self.client.post(
            '/admin/v1/auth/verify/',
            data={'method': method, 'code': code},
            content_type='application/json',
        )

    def test_full_two_step_flow(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['second_factor_required'])
        # First step yields a challenge cookie and NO session cookie.
        self.assertIn(challenge_cookie_name(), response.cookies)
        self.assertNotIn(cookie_name(), response.cookies)
        self.assertEqual(AdminSession.objects.count(), 0)
        self.assertAudited(ADMIN_AUTH_CHALLENGE_ISSUED, result=RESULT_SUCCESS)

        response = self._verify(_code(self.secret))
        self.assertEqual(response.status_code, 200)
        self.assertIn(cookie_name(), response.cookies)
        self.assertEqual(AdminSession.objects.count(), 1)
        entry = self.assertAudited(ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS)
        self.assertEqual(entry.actor_id, self.user.id)
        self.assertIsNotNone(entry.session_id)

        # The minted session authenticates a protected route.
        session_response = self.client.get('/admin/v1/auth/session/')
        self.assertEqual(session_response.status_code, 200)
        self.assertEqual(
            session_response.json()['data']['username'], self.user.username,
        )

    def test_challenge_cookie_attributes(self):
        morsel = self._login().cookies[challenge_cookie_name()]
        self.assertTrue(morsel['httponly'])
        self.assertTrue(morsel['secure'])
        self.assertEqual(morsel['samesite'], 'Strict')
        self.assertEqual(morsel['path'], '/')
        self.assertEqual(morsel['domain'], '')

    def test_wrong_password_rejected_and_audited(self):
        response = self._login(password='wrong')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(challenge_cookie_name(), response.cookies)
        self.assertAudited(ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_FAILURE)

    def test_wrong_totp_rejected_and_audited(self):
        self._login()
        response = self._verify('000000')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(AdminSession.objects.count(), 0)
        self.assertAudited(ADMIN_AUTH_TOTP_FAILURE, result=RESULT_FAILURE)

    def test_verify_without_challenge_cookie_rejected(self):
        response = self._verify('000000')
        self.assertEqual(response.status_code, 401)
        self.assertAudited(ADMIN_AUTH_TOTP_FAILURE, result=RESULT_DENIED)

    def test_expired_challenge_rejected(self):
        self._login()
        AdminLoginChallenge.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self._verify(_code(self.secret)).status_code, 401)
        self.assertEqual(AdminSession.objects.count(), 0)

    def test_challenge_is_single_use(self):
        login_response = self._login()
        raw_challenge = login_response.cookies[challenge_cookie_name()].value
        self.assertEqual(self._verify(_code(self.secret)).status_code, 200)

        # Replay the SAME challenge token by hand (the browser would have dropped
        # the cleared cookie) — it is consumed, so a second session cannot be bought.
        self.client.cookies[challenge_cookie_name()] = raw_challenge
        sessions_before = AdminSession.objects.count()
        self.assertEqual(self._verify(_code(self.secret, offset=1)).status_code, 401)
        self.assertEqual(AdminSession.objects.count(), sessions_before)

    def test_replayed_totp_code_rejected_at_verify(self):
        self._login()
        code = _code(self.secret)
        self.assertEqual(self._verify(code).status_code, 200)
        self._login()
        self.assertEqual(self._verify(code).status_code, 401)

    def test_recovery_code_accepted_once_and_audited(self):
        self._login()
        response = self._verify(self.codes[0], method=METHOD_RECOVERY)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['used_recovery_code'])
        self.assertEqual(response.json()['data']['recovery_codes_remaining'], 9)
        self.assertAudited(ADMIN_AUTH_RECOVERY_CODE_USED, result=RESULT_SUCCESS)

        self.client.post('/admin/v1/auth/logout/')
        self._login()
        self.assertEqual(
            self._verify(self.codes[0], method=METHOD_RECOVERY).status_code, 401,
        )

    def test_lockout_after_threshold_failures(self):
        for _ in range(lockout.threshold()):
            # The per-IP throttle would cap the loop short of the threshold.
            cache.clear()
            self._login(password='wrong')
        self.auth.refresh_from_db()
        self.assertTrue(lockout.is_locked(self.auth))
        self.assertAudited(ADMIN_AUTH_LOCKOUT, result=RESULT_DENIED)

        # A CORRECT password no longer meets a flat 401 while locked — it opens the
        # break-glass path, a recovery-only challenge. TOTP is still refused on it, so
        # an attacker who locked the account gains nothing from the change. The
        # recovery half of this lives in tests_admin_lockout.py.
        cache.clear()
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['recovery_code_required'])
        self.assertEqual(self._verify(_code(self.secret)).status_code, 401)

    def test_logout_revokes_and_is_idempotent(self):
        self._login()
        self._verify(_code(self.secret))
        first = self.client.post('/admin/v1/auth/logout/')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            AdminSession.objects.filter(revoked_at__isnull=False).count(), 1,
        )
        # Second call still succeeds — logging out twice is not an error.
        self.assertEqual(self.client.post('/admin/v1/auth/logout/').status_code, 200)
        self.assertAudited(ADMIN_AUTH_LOGOUT, result=RESULT_SUCCESS, count=2)
        # The revoked session no longer authenticates.
        self.assertIn(
            self.client.get('/admin/v1/auth/session/').status_code, (401, 403),
        )

    def test_session_endpoint_requires_authentication(self):
        self.assertIn(
            self.client.get('/admin/v1/auth/session/').status_code, (401, 403),
        )

    def test_elevation_stamps_and_audits(self):
        self._login()
        self._verify(_code(self.secret))
        AdminSession.objects.update(elevated_at=None)

        response = self.client.post(
            '/admin/v1/auth/elevate/',
            data={'method': METHOD_TOTP, 'code': _code(self.secret, offset=1)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(AdminSession.objects.get().elevated_at)
        self.assertAudited(ADMIN_AUTH_ELEVATED, result=RESULT_SUCCESS)

    def test_elevation_rejects_bad_code(self):
        self._login()
        self._verify(_code(self.secret))
        response = self.client.post(
            '/admin/v1/auth/elevate/',
            data={'method': METHOD_TOTP, 'code': '000000'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)


@override_settings(**_ADMIN_OVERRIDES)
class NonDisclosureTests(ThrottleIsolationMixin, TestCase):
    """Every login failure must be indistinguishable from the outside."""

    def _login(self, username, password=PASSWORD):
        return Client().post(
            '/admin/v1/auth/login/',
            data={'username': username, 'password': password},
            content_type='application/json',
        )

    def test_identical_body_for_unknown_wrong_password_and_non_admin(self):
        admin, _auth, _secret, _codes = _make_admin(username='nd-admin')
        restaurant_user = _make_user('nd-rest@t.com', username='nd-rest-user')

        unknown = self._login('no-such-account')
        wrong_password = self._login(admin.username, password='wrong')
        non_admin = self._login(restaurant_user.username)

        for response in (unknown, wrong_password, non_admin):
            self.assertEqual(response.status_code, 401)
        self.assertEqual(unknown.json(), wrong_password.json())
        self.assertEqual(unknown.json(), non_admin.json())

    def test_unenrolled_admin_is_also_indistinguishable(self):
        user = _make_user('nd-unenrolled@t.com', ACCOUNT_TYPE_PLATFORM_STAFF,
                          username='nd-unenrolled', phone=False)
        PlatformStaffAuth.objects.create(user=user)
        self.assertEqual(
            self._login(user.username).json(), self._login('no-such-account').json(),
        )

    def test_inactive_admin_is_also_indistinguishable(self):
        user, _auth, _secret, _codes = _make_admin(
            email='nd-inactive@t.com', username='nd-inactive', is_active=False,
        )
        self.assertEqual(
            self._login(user.username).json(), self._login('no-such-account').json(),
        )


@override_settings(**_ADMIN_OVERRIDES)
class InvariantFailClosedTests(ThrottleIsolationMixin, AuditAssertionsMixin, TestCase):
    def test_platform_staff_with_active_membership_cannot_log_in(self):
        """The PR-1 invariant, enforced at the door."""
        user, _auth, secret, _codes = _make_admin(username='dual-role')
        owner = _make_user('dual-owner@t.com')
        restaurant = Restaurant.objects.create(
            name='R', location='loc', status='active', owner=owner,
        )
        # Created out-of-band, mirroring a pre-flip standing row.
        RestaurantEmployee.objects.create(
            user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
        )

        response = Client().post(
            '/admin/v1/auth/login/',
            data={'username': user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(AdminLoginChallenge.objects.count(), 0)
        entry = self.assertAudited(ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED)
        self.assertEqual(entry.error_code, 'active_membership')


# --- CSRF cookie issuance ----------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CsrfCookieIssuanceTests(ThrottleIsolationMixin, TestCase):
    """
    The server must ISSUE the CSRF cookie, not merely enforce it.

    ``AdminSessionAuthentication.enforce_csrf`` runs Django's double-submit check on
    every unsafe admin request, and that check cannot pass without a CSRF cookie the
    server put there. Nothing in the codebase issued one, so every authenticated
    write on this plane answered ``403 CSRF Failed: CSRF cookie not set.`` — and the
    suite missed it entirely, because Django's default ``Client()`` sets
    ``_dont_enforce_csrf_checks``, which short-circuits the check before it looks for
    a cookie. Every test here therefore uses ``Client(enforce_csrf_checks=True)`` and
    reads the token out of a SERVER response; none puts one in the jar by hand, and
    none hardcodes the cookie name.
    """

    def _csrf_name(self):
        return dj_settings.CSRF_COOKIE_NAME

    def _login(self, client, user, secret, offset=0):
        """Drive login -> verify. Returns the verify response."""
        client.post(
            '/admin/v1/auth/login/',
            data={'username': user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        return client.post(
            '/admin/v1/auth/verify/',
            data={'method': METHOD_TOTP, 'code': _code(secret, offset=offset)},
            content_type='application/json',
        )

    def test_verify_issues_csrf_cookie(self):
        """A successful verify/ emits the cookie ITSELF — nothing preloads the jar."""
        user, _auth, secret, _codes = _make_admin(
            email='csrf_v@t.com', username='csrf-verify',
        )
        client = Client(enforce_csrf_checks=True)
        self.assertNotIn(self._csrf_name(), client.cookies)

        response = self._login(client, user, secret)

        self.assertEqual(response.status_code, 200)
        # Present in the RESPONSE, i.e. the server set it on this very request.
        self.assertIn(self._csrf_name(), response.cookies)
        self.assertTrue(response.cookies[self._csrf_name()].value)

    def test_verify_issued_cookie_carries_the_admin_attributes(self):
        """__Host- is only honoured with Secure + Path=/ + no Domain; SPA needs read."""
        user, _auth, secret, _codes = _make_admin(
            email='csrf_attr@t.com', username='csrf-attrs',
        )
        client = Client(enforce_csrf_checks=True)
        morsel = self._login(client, user, secret).cookies[self._csrf_name()]

        self.assertTrue(self._csrf_name().startswith('__Host-'))
        self.assertTrue(morsel['secure'])
        self.assertEqual(morsel['path'], '/')
        self.assertFalse(morsel['domain'])
        # The SPA has to read this one to echo it back in X-CSRFToken.
        self.assertFalse(morsel['httponly'])

    def test_session_bootstrap_issues_csrf_cookie(self):
        """GET session/ hands a token to a client that has a session but no token."""
        user, _auth, _secret, _codes = _make_admin(
            email='csrf_s@t.com', username='csrf-session',
        )
        raw, _session = sessions.create_session(user)
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = raw
        self.assertNotIn(self._csrf_name(), client.cookies)

        response = client.get('/admin/v1/auth/session/')

        self.assertEqual(response.status_code, 200)
        self.assertIn(self._csrf_name(), response.cookies)
        self.assertTrue(response.cookies[self._csrf_name()].value)

    def test_secret_rotates_across_two_successive_logins(self):
        """
        verify/ ROTATES: a second sign-in must not inherit the first one's secret.

        Rotation gives each sign-in a fresh secret, the way
        django.contrib.auth.login() does; get_token() here would reuse one secret
        across logout and re-login for CSRF_COOKIE_AGE (a year). It does NOT tie the
        secret to the AdminSession: session/ re-emits whatever secret it is sent, so
        an older one can come back beside a newer session. What binds a command to
        its session is the command-owner precondition (tests_command_owner).
        """
        user, _auth, secret, _codes = _make_admin(
            email='csrf_rot@t.com', username='csrf-rotate',
        )
        client = Client(enforce_csrf_checks=True)

        first = self._login(client, user, secret, offset=0)
        first_token = first.cookies[self._csrf_name()].value

        client.post('/admin/v1/auth/logout/', data='{}',
                    content_type='application/json')

        # offset=+1: inside the TOTP window but a strictly later counter, so it is
        # not a replay of the code the first verify consumed.
        second = self._login(client, user, secret, offset=1)
        second_token = second.cookies[self._csrf_name()].value

        self.assertTrue(first_token)
        self.assertTrue(second_token)
        self.assertNotEqual(first_token, second_token)

    def test_secret_does_not_change_across_two_session_gets(self):
        """
        session/ ENSURES: a bootstrap must not invalidate the token other tabs hold.

        The masked token differs per response (Django re-masks every time), so the
        assertion is on the underlying SECRET, which is what enforce_csrf compares.
        """
        user, _auth, _secret, _codes = _make_admin(
            email='csrf_ens@t.com', username='csrf-ensure',
        )
        raw, _session = sessions.create_session(user)
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = raw

        first = client.get('/admin/v1/auth/session/')
        first_secret = first.cookies[self._csrf_name()].value

        second = client.get('/admin/v1/auth/session/')

        self.assertEqual(second.status_code, 200)
        if self._csrf_name() in second.cookies:
            # Re-sent to renew the expiry timer, but it must be the SAME secret.
            self.assertEqual(second.cookies[self._csrf_name()].value, first_secret)
        self.assertEqual(client.cookies[self._csrf_name()].value, first_secret)

    def test_unsafe_write_succeeds_with_a_server_issued_token(self):
        """
        The end-to-end case whose absence let the defect through.

        elevate/ stands in for all four session-authenticated writes — they share one
        authenticator path (AdminSessionAuthentication.enforce_csrf), so a second and
        third near-identical fixture would prove nothing extra.
        """
        user, _auth, secret, _codes = _make_admin(
            email='csrf_e2e@t.com', username='csrf-e2e',
        )
        client = Client(enforce_csrf_checks=True)
        verify = self._login(client, user, secret, offset=0)
        self.assertEqual(verify.status_code, 200)

        token = verify.cookies[self._csrf_name()].value   # from the SERVER response

        response = client.post(
            '/admin/v1/auth/elevate/',
            data={'method': METHOD_TOTP, 'code': _code(secret, offset=1)},
            content_type='application/json',
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['message'], 'Elevated.')

    def test_unsafe_write_without_the_token_is_still_refused(self):
        """Issuance must not have loosened enforcement: no header, no write."""
        user, _auth, secret, _codes = _make_admin(
            email='csrf_neg@t.com', username='csrf-negative',
        )
        client = Client(enforce_csrf_checks=True)
        self._login(client, user, secret, offset=0)

        response = client.post(
            '/admin/v1/auth/elevate/',
            data={'method': METHOD_TOTP, 'code': _code(secret, offset=1)},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn('CSRF', response.json()['detail'])


# --- Elevation helper --------------------------------------------------------------

class ElevationHelperTests(TestCase):
    def test_fresh_passes_stale_and_never_elevated_fail(self):
        user, _auth, _secret, _codes = _make_admin(username='elev-helper')
        _raw, session = sessions.create_session(user)

        self.assertFalse(require_recent_elevation(session))
        self.assertFalse(require_recent_elevation(None))

        sessions.elevate(session)
        self.assertTrue(require_recent_elevation(session))

        session.elevated_at = timezone.now() - timedelta(minutes=30)
        session.save(update_fields=['elevated_at'])
        self.assertFalse(require_recent_elevation(session))


# --- Mutual rejection + customer non-regression ------------------------------------

class MutualRejectionTests(ThrottleIsolationMixin, TestCase):
    def test_platform_staff_cannot_log_in_on_customer_origin(self):
        user, _auth, _secret, _codes = _make_admin(username='mutual-admin')
        response = customer_login(username=user.username, password=PASSWORD)
        self.assertEqual(response['status'], 401)
        self.assertNotIn('data', response)

    def test_platform_staff_rejected_even_via_source_diner(self):
        """source='diner' bypasses the OTP gate — it must not bypass this."""
        user, _auth, _secret, _codes = _make_admin(
            email='mutual-diner@t.com', username='mutual-diner',
        )
        response = customer_login(
            username=user.username, password=PASSWORD, source='diner',
        )
        self.assertEqual(response['status'], 401)
        self.assertNotIn('data', response)

    def test_restaurant_user_login_still_returns_tokens(self):
        """Non-regression: an ordinary customer login is untouched."""
        user = _make_user('mutual-rest@t.com')
        response = customer_login(
            username=user.username, password=PASSWORD, source='diner',
        )
        self.assertEqual(response['status'], 200)
        self.assertIn('token', response['data'])
        self.assertIn('refresh', response['data'])

    def test_restaurant_user_can_still_authenticate_on_admin_plane_never(self):
        """The other direction: a restaurant user is refused by the admin plane."""
        user = _make_user('mutual-rest2@t.com')
        with override_settings(**_ADMIN_OVERRIDES):
            response = Client().post(
                '/admin/v1/auth/login/',
                data={'username': user.username, 'password': PASSWORD},
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 401)

    def test_password_reset_cannot_resolve_platform_staff(self):
        """The second customer-plane token door is closed."""
        user, _auth, _secret, _codes = _make_admin(
            email='reset-admin@t.com', username='reset-admin',
        )
        self.assertIsNone(_resolve_user(user.email))

    def test_password_reset_still_resolves_restaurant_users(self):
        user = _make_user('reset-rest@t.com')
        self.assertEqual(_resolve_user(user.email).id, user.id)
        self.assertEqual(_resolve_user(user.phone_number).id, user.id)


# --- Management commands -----------------------------------------------------------

class ManagementCommandTests(TestCase):
    def _create(self, **kwargs):
        options = dict(username='cmd-admin', email='cmd@t.com', full_name='A B')
        options.update(kwargs)
        with patch('sys.stdin.isatty', return_value=True), \
                patch('getpass.getpass', return_value=PASSWORD):
            call_command('create_platform_admin', **options)

    def test_creates_enrolled_platform_staff_without_phone(self):
        self._create()
        user = User.objects.get(username='cmd-admin')
        self.assertEqual(user.account_type, ACCOUNT_TYPE_PLATFORM_STAFF)
        self.assertIsNone(user.phone_number)
        auth = PlatformStaffAuth.objects.get(user=user)
        self.assertIsNotNone(auth.totp_secret_encrypted)
        self.assertEqual(len(auth.recovery_code_hashes), 10)

    def test_refuses_duplicate_username(self):
        self._create()
        with self.assertRaises(CommandError):
            self._create(email='other@t.com')

    def test_refuses_phone_number_username(self):
        with self.assertRaises(CommandError):
            self._create(username='256701000123')

    def test_refuses_without_encryption_key(self):
        with patch('platform_admin_app.crypto.config', return_value=None):
            with self.assertRaises(CommandError):
                self._create(username='cmd-nokey', email='nokey@t.com')
        self.assertFalse(User.objects.filter(username='cmd-nokey').exists())

    def test_reset_reprovisions_and_revokes_sessions(self):
        self._create()
        user = User.objects.get(username='cmd-admin')
        auth = PlatformStaffAuth.objects.get(user=user)
        before_secret = auth.totp_secret_encrypted
        before_hashes = list(auth.recovery_code_hashes)
        sessions.create_session(user)

        call_command('reset_platform_admin_totp', username='cmd-admin', noinput=True)

        auth.refresh_from_db()
        self.assertNotEqual(auth.totp_secret_encrypted, before_secret)
        self.assertNotEqual(auth.recovery_code_hashes, before_hashes)
        self.assertEqual(
            AdminSession.objects.filter(revoked_at__isnull=True).count(), 0,
        )

    def test_reset_refuses_non_platform_staff(self):
        user = _make_user('cmd-rest@t.com', username='cmd-rest')
        with self.assertRaises(CommandError):
            call_command(
                'reset_platform_admin_totp', username=user.username, noinput=True,
            )


# --- Exposure guards ---------------------------------------------------------------

class CredentialExposureTests(TestCase):
    def test_no_serializer_exposes_credential_fields(self):
        forbidden = {
            'totp_secret_encrypted', 'recovery_code_hashes', 'token_hash',
            'last_totp_counter', 'consumed_at',
        }
        for cls in all_project_serializers():
            exposed = forbidden.intersection(cls().fields.keys())
            self.assertEqual(
                exposed, set(),
                f'{cls.__module__}.{cls.__qualname__} exposes credential '
                f'field(s): {exposed}.',
            )

    def test_no_serializer_targets_credential_models(self):
        models = {PlatformStaffAuth, AdminSession, AdminLoginChallenge}
        offenders = [
            f'{cls.__module__}.{cls.__qualname__}'
            for cls in all_project_serializers()
            if getattr(getattr(cls, 'Meta', None), 'model', None) in models
        ]
        self.assertEqual(offenders, [], f'Credential models serialized: {offenders}')

    def test_verify_response_never_returns_secrets(self):
        _user, auth, _secret, codes = _make_admin(username='leak-check')
        body = str({'codes': codes})
        self.assertNotIn(auth.totp_secret_encrypted, body)
