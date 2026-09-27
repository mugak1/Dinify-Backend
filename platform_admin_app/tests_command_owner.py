"""
Tests for the admin command-owner precondition (D10 B1).

THE DEFECT. A matched CSRF pair says a request came from this origin. It does not say
which admin SESSION the command was issued under. ``session/`` calls ``get_token``,
which re-emits whatever CSRF secret the request carried, so a ``session/`` response
that was sent before another sign-in and arrives after it puts the OLD CSRF cookie back
beside the NEW session cookie. A tab still holding the old token then passes CSRF and
its command runs as whoever signed in since. ``REGRESSION`` tests reproduce that with
real ``Set-Cookie`` headers on the real middleware, authenticator and views.

THE CONTRACT. ``verify/`` and ``session/`` publish
``command_owner: {version: 1, actor: <User.pk>, session: <AdminSession.id>}``. A client
names that owner on an unsafe request as ``X-Admin-Command-Owner: 1;<actor>;<session>``,
and the server refuses the request when it does not describe the session that
authenticated it:

    malformed header                     -> 400 admin_command_owner_malformed
    different actor                      -> 409 admin_command_actor_changed
    same actor, different session        -> 409 admin_command_session_changed

The SESSION comparison is what enforces. The actor comparison only decides which of the
two 409s is returned. Only an ABSENT header keeps the old behaviour.

WHY THE CONSTANTS ARE LITERALS. Nothing here imports ``platform_admin_app.command_owner``.
On a tree without the contract every test therefore fails on an ASSERTION (the command
ran, a cookie was cleared, a key is missing), which is the defect itself, rather than
on an ImportError, which proves nothing.

HELD RESPONSES. A "held" response is an ordering fixture. The request is sent now, with
a copy of the browser's cookie jar, and its real ``Set-Cookie`` headers are applied to
the jar later, by the Django test client's own rule. That is the network delay, and
nothing about the response is fabricated.

WHAT "NOTHING HAPPENED" MEANS. Owner refusals are deliberately NOT audited: they are
refused inside authentication, like a CSRF failure, before any administrative decision
exists. So an unchanged audit count is not the proof here. Each refusal test asserts
the effects directly: no business row, an unchanged ``PlatformStaffAuth`` (no factor
spent, no failure counted), an unchanged ``AdminSession``. Several also re-send the same
request WITHOUT the header to show that the header was the only thing stopping it.
"""
import copy
import uuid
from datetime import timedelta

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commercial_app.models import RestaurantServiceConfiguration
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
)
from platform_admin_app import sessions
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_LOGOUT,
    ADMIN_RESTAURANT_PAYMENT_TIMING_SET,
)
from platform_admin_app.cookies import challenge_cookie_name, cookie_name
from platform_admin_app.models import (
    RESULT_SUCCESS,
    AdminAuditLog,
    AdminSession,
    PlatformStaffAuth,
)
from platform_admin_app.tests_auth import (
    _ADMIN_OVERRIDES,
    PASSWORD,
    ThrottleIsolationMixin,
    _code,
    _make_admin,
    _make_user,
)
from restaurants_app.models import Restaurant
from users_app.models import User

# The wire contract, spelled out rather than imported (see the module docstring).
OWNER_META = 'HTTP_X_ADMIN_COMMAND_OWNER'
MALFORMED = 'admin_command_owner_malformed'
ACTOR_CHANGED = 'admin_command_actor_changed'
SESSION_CHANGED = 'admin_command_session_changed'
DETAILS = {
    MALFORMED: (
        'The command-owner precondition could not be read. The command was not run.'
    ),
    ACTOR_CHANGED: (
        'This browser is now signed in as a different administrator. '
        'The command was not run.'
    ),
    SESSION_CHANGED: (
        'This browser has started a new admin session since the command was '
        'issued. The command was not run.'
    ),
}
CANONICAL_UUID = r'\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z'

LOGIN = '/admin/v1/auth/login/'
VERIFY = '/admin/v1/auth/verify/'
LOGOUT = '/admin/v1/auth/logout/'
SESSION = '/admin/v1/auth/session/'
ELEVATE = '/admin/v1/auth/elevate/'
DIRECTORY = '/admin/v1/restaurants/'
REASON = 'Synthetic reason for the D10 command-owner tests.'
SIGNED_OUT = {'status': 200, 'message': 'Signed out.'}

# Sentinels for "leave this header off entirely", which is not the same as ''.
ABSENT = object()
CURRENT = object()


def owner_header(session):
    """The header a client builds from a published owner, from database facts."""
    return f'1;{session.user_id};{session.id}'


@override_settings(**_ADMIN_OVERRIDES)
class _OwnerCase(ThrottleIsolationMixin, TestCase):
    """Two administrators, one restaurant and one browser cookie jar."""

    def setUp(self):
        super().setUp()
        self.alice, _auth, self.alice_secret, self.alice_codes = _make_admin(
            email='d10-alice@example.invalid', username='d10-alice',
        )
        self.bob, _auth, self.bob_secret, self.bob_codes = _make_admin(
            email='d10-bob@example.invalid', username='d10-bob',
        )
        owner = _make_user('d10-owner@example.invalid', phone=False)
        self.restaurant = Restaurant.objects.create(
            name='D10 Kitchen', location='D10 Road', status=RestaurantStatus_Live,
            owner=owner,
        )
        self.browser = Client(enforce_csrf_checks=True)
        # Each administrator spends TOTP steps -1, 0, +1 in order: all inside the
        # verification window and each strictly later than the last, so a later code
        # is never a replay of an earlier one.
        self._step = {self.alice.pk: -1, self.bob.pk: -1}

    # --- the browser -------------------------------------------------------------

    def next_totp(self, user, secret):
        offset = self._step[user.pk]
        self._step[user.pk] += 1
        return _code(secret, offset)

    def sign_in(self, user, secret):
        """A real two-step sign-in in the shared browser. Returns the verify/ response."""
        response = self.browser.post(
            LOGIN, {'username': user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = self.browser.post(
            VERIFY, {'method': 'totp', 'code': self.next_totp(user, secret)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def jar_session(self):
        """The AdminSession the browser's session cookie currently names."""
        session = sessions.resolve_session(self.browser.cookies[cookie_name()].value)
        self.assertIsNotNone(session)
        return session

    def csrf(self):
        morsel = self.browser.cookies.get(dj_settings.CSRF_COOKIE_NAME)
        return morsel.value if morsel else None

    def snapshot_client(self):
        """A second tab holding a copy of the jar as it is now."""
        tab = Client(enforce_csrf_checks=True)
        tab.cookies = copy.deepcopy(self.browser.cookies)
        return tab

    def _headers(self, owner, csrf, extra):
        headers = {}
        token = self.csrf() if csrf is CURRENT else csrf
        if token is not None:
            headers['HTTP_X_CSRFTOKEN'] = token
        if owner is not ABSENT:
            headers[OWNER_META] = owner
        headers.update(extra)
        return headers

    def write(self, owner=ABSENT, csrf=CURRENT, client=None, **extra):
        """The representative business write: record a payment timing."""
        return (client or self.browser).post(
            f'/admin/v1/restaurants/{self.restaurant.id}/commercial/payment-timing/',
            {'value': 'pay_first', 'expected_current': None, 'reason': REASON},
            content_type='application/json', **self._headers(owner, csrf, extra),
        )

    def elevate(self, method, code, owner=ABSENT, csrf=CURRENT):
        return self.browser.post(
            ELEVATE, {'method': method, 'code': code},
            content_type='application/json', **self._headers(owner, csrf, {}),
        )

    def logout(self, owner=ABSENT, client=None):
        return (client or self.browser).post(
            LOGOUT, data='{}', content_type='application/json',
            **self._headers(owner, None, {}),
        )

    def echo_alice_then_bob(self):
        """
        The counterexample, with real Set-Cookie headers.

        Alice signs in. A session/ read is sent from her tab, and its response is held.
        Bob signs in in the same browser. Then the held response lands: it re-emits
        Alice's CSRF secret, so the jar ends up with BOB's session cookie beside ALICE's
        CSRF cookie, and Alice's tab reads a CSRF token that matches.
        """
        self.sign_in(self.alice, self.alice_secret)
        alice_session = self.jar_session()
        csrf_a = self.csrf()
        held = self.snapshot_client().get(SESSION)
        self.assertEqual(held.status_code, 200)
        # get_token re-emits the secret the request carried; it does not mint one.
        self.assertEqual(held.cookies[dj_settings.CSRF_COOKIE_NAME].value, csrf_a)

        self.sign_in(self.bob, self.bob_secret)
        bob_session = self.jar_session()
        self.assertNotEqual(self.csrf(), csrf_a)          # verify/ rotated it

        self.browser.cookies.update(held.cookies)         # the late response lands
        self.assertEqual(self.csrf(), csrf_a)
        self.assertEqual(self.jar_session().pk, bob_session.pk)
        return owner_header(alice_session), csrf_a, bob_session

    # --- what happened -----------------------------------------------------------

    def business_effects(self):
        return (
            RestaurantServiceConfiguration.objects.filter(
                restaurant=self.restaurant,
            ).exists(),
            # SUCCESSFUL entries only: an elevation denial legitimately writes a
            # denied entry under the same action, and that is not a business write.
            AdminAuditLog.objects.filter(
                action=ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
            ).count(),
        )

    def assert_no_business_write(self):
        self.assertEqual(self.business_effects(), (False, 0))

    def factor_state(self, user):
        auth = PlatformStaffAuth.objects.get(user=user)
        return (
            auth.failed_attempts, auth.locked_until, auth.last_totp_counter,
            list(auth.recovery_code_hashes or []),
        )

    def session_state(self, session):
        row = AdminSession.objects.get(pk=session.pk)
        return (row.revoked_at, row.revoked_reason, row.elevated_at, row.last_seen)

    def logout_audits(self):
        return AdminAuditLog.objects.filter(action=ADMIN_AUTH_LOGOUT).count()

    def assert_refused(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json(), {'detail': DETAILS[code], 'code': code})
        self.assertEqual(list(response.cookies), [])

    def assert_no_set_cookie(self, response):
        self.assertEqual(list(response.cookies), [])


# --- publication --------------------------------------------------------------------


class CommandOwnerPublicationTests(_OwnerCase):

    def test_verify_publishes_the_owner_of_the_session_it_just_issued(self):
        response = self.sign_in(self.alice, self.alice_secret)
        body = response.json()
        self.assertEqual((body['status'], body['message']), (200, 'Signed in.'))
        data = body['data']
        self.assertIn('command_owner', data)

        raw = response.cookies[cookie_name()].value
        issued = AdminSession.objects.get(token_hash=sessions.hash_token(raw))
        self.assertEqual(
            data['command_owner'],
            {'version': 1, 'actor': str(self.alice.pk), 'session': str(issued.pk)},
        )
        self.assertRegex(data['command_owner']['actor'], CANONICAL_UUID)
        self.assertRegex(data['command_owner']['session'], CANONICAL_UUID)
        # Every existing field stays, and nothing else was added.
        self.assertEqual(set(data), {
            'username', 'expires_at', 'used_recovery_code', 'lockout_cleared',
            'recovery_codes_remaining', 'command_owner',
        })
        # The owner is an identifier, never the credential or its hash.
        content = response.content.decode()
        self.assertNotIn(raw, content)
        self.assertNotIn(issued.token_hash, content)
        # The cookie controls are the existing ones.
        morsel = response.cookies[cookie_name()]
        self.assertTrue(morsel['httponly'])
        self.assertTrue(morsel['secure'])
        self.assertEqual(morsel['samesite'], 'Strict')
        self.assertEqual(morsel['path'], '/')
        self.assertIn(dj_settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertEqual(response.cookies[challenge_cookie_name()].value, '')

    def test_a_second_sign_in_publishes_its_own_session_not_the_first(self):
        first = self.sign_in(self.alice, self.alice_secret).json()['data']
        second = self.sign_in(self.alice, self.alice_secret).json()['data']
        self.assertIn('command_owner', first)
        self.assertIn('command_owner', second)
        self.assertEqual(first['command_owner']['actor'], second['command_owner']['actor'])
        self.assertNotEqual(
            first['command_owner']['session'], second['command_owner']['session'],
        )
        self.assertEqual(second['command_owner']['session'], str(self.jar_session().pk))

    def test_session_read_names_the_session_that_authenticated_it(self):
        raw_1, first = sessions.create_session(self.alice)
        raw_2, second = sessions.create_session(self.alice)
        for raw, session in ((raw_1, first), (raw_2, second)):
            with self.subTest(session=str(session.pk)):
                client = Client(enforce_csrf_checks=True)
                client.cookies[cookie_name()] = raw
                response = client.get(SESSION)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual((body['status'], body['message']), (200, 'ok'))
                self.assertEqual(set(body['data']), {
                    'username', 'email', 'issued_at', 'expires_at', 'elevated_at',
                    'server_time', 'command_owner',
                })
                self.assertEqual(body['data']['command_owner'], {
                    'version': 1, 'actor': str(self.alice.pk),
                    'session': str(session.pk),
                })
                content = response.content.decode()
                self.assertNotIn(raw, content)
                self.assertNotIn(session.token_hash, content)
                # Still the CSRF issuer it was.
                self.assertIn(dj_settings.CSRF_COOKIE_NAME, response.cookies)


# --- refusals -----------------------------------------------------------------------


class CommandOwnerRefusalTests(_OwnerCase):

    def test_REGRESSION_a_delayed_session_read_cannot_run_the_previous_actors_command(self):
        alice_owner, csrf_a, bob_session = self.echo_alice_then_bob()
        # Age last_seen past the touch throttle, so a request that reached touch()
        # would move it.
        AdminSession.objects.filter(pk=bob_session.pk).update(
            last_seen=timezone.now() - timedelta(minutes=10),
        )
        before = self.session_state(bob_session)
        audits = AdminAuditLog.objects.count()

        response = self.write(owner=alice_owner, csrf=csrf_a)

        self.assert_refused(response, 409, ACTOR_CHANGED)
        self.assert_no_business_write()
        self.assertEqual(self.session_state(bob_session), before)
        self.assertEqual(AdminAuditLog.objects.count(), audits)

    def test_REGRESSION_the_echoed_jar_cannot_spend_a_recovery_code(self):
        alice_owner, csrf_a, bob_session = self.echo_alice_then_bob()
        before = self.factor_state(self.bob)
        before_session = self.session_state(bob_session)
        audits = AdminAuditLog.objects.count()

        # Alice's tab asked for re-authentication; Bob, at the keyboard, types his own
        # recovery code into it.
        response = self.elevate(
            'recovery', self.bob_codes[0], owner=alice_owner, csrf=csrf_a,
        )

        self.assert_refused(response, 409, ACTOR_CHANGED)
        self.assertEqual(self.factor_state(self.bob), before)
        self.assertEqual(self.session_state(bob_session), before_session)
        self.assertEqual(AdminAuditLog.objects.count(), audits)

        # CAUSE: without the header the identical request spends the code.
        control = self.elevate('recovery', self.bob_codes[0], csrf=csrf_a)
        self.assertEqual(control.status_code, 200, control.content)
        self.assertEqual(len(self.factor_state(self.bob)[3]), len(before[3]) - 1)

    def test_REGRESSION_the_echoed_jar_cannot_spend_or_fail_a_totp_code(self):
        alice_owner, csrf_a, bob_session = self.echo_alice_then_bob()
        before = self.factor_state(self.bob)
        before_session = self.session_state(bob_session)

        # A code that is wrong for the new session would count a failure against it.
        wrong = self.elevate(
            'totp', _code(self.alice_secret, 0), owner=alice_owner, csrf=csrf_a,
        )
        self.assert_refused(wrong, 409, ACTOR_CHANGED)
        # A code that is right for it would be spent on it.
        bob_code = self.next_totp(self.bob, self.bob_secret)
        right = self.elevate('totp', bob_code, owner=alice_owner, csrf=csrf_a)
        self.assert_refused(right, 409, ACTOR_CHANGED)

        self.assertEqual(self.factor_state(self.bob), before)
        self.assertEqual(self.session_state(bob_session), before_session)

        # CAUSE: without the header the same valid code is spent.
        control = self.elevate('totp', bob_code, csrf=csrf_a)
        self.assertEqual(control.status_code, 200, control.content)
        self.assertNotEqual(self.factor_state(self.bob)[2], before[2])

    def test_REGRESSION_a_new_session_for_the_same_actor_refuses_the_old_sessions_command(self):
        self.sign_in(self.alice, self.alice_secret)
        first = self.jar_session()
        self.sign_in(self.alice, self.alice_secret)
        second = self.jar_session()
        self.assertEqual(first.user_id, second.user_id)
        self.assertNotEqual(first.pk, second.pk)

        # The CSRF token is current: nothing about CSRF is stale here.
        response = self.write(owner=owner_header(first))

        self.assert_refused(response, 409, SESSION_CHANGED)
        self.assert_no_business_write()

    def test_REGRESSION_a_new_session_for_the_same_actor_refuses_elevation_before_any_factor(self):
        self.sign_in(self.alice, self.alice_secret)
        first = self.jar_session()
        self.sign_in(self.alice, self.alice_secret)
        second = self.jar_session()
        before = self.factor_state(self.alice)
        before_session = self.session_state(second)
        code = self.next_totp(self.alice, self.alice_secret)

        recovery = self.elevate(
            'recovery', self.alice_codes[0], owner=owner_header(first),
        )
        totp = self.elevate('totp', code, owner=owner_header(first))

        self.assert_refused(recovery, 409, SESSION_CHANGED)
        self.assert_refused(totp, 409, SESSION_CHANGED)
        self.assertEqual(self.factor_state(self.alice), before)
        self.assertEqual(self.session_state(second), before_session)

        # CAUSE: naming the session that is actually current, the same code is spent.
        control = self.elevate('totp', code, owner=owner_header(second))
        self.assertEqual(control.status_code, 200, control.content)
        self.assertNotEqual(self.factor_state(self.alice)[2], before[2])

    def test_the_session_comparison_enforces_and_the_actor_comparison_classifies(self):
        self.sign_in(self.alice, self.alice_secret)
        current = self.jar_session()
        actor, session = str(current.user_id), str(current.pk)
        stranger, unknown = str(self.bob.pk), str(uuid.uuid4())
        cases = (
            ('both differ', f'1;{stranger};{unknown}', ACTOR_CHANGED),
            ('actor differs, session names this one', f'1;{stranger};{session}',
             ACTOR_CHANGED),
            ('same actor, other session', f'1;{actor};{unknown}', SESSION_CHANGED),
        )
        for label, header, code in cases:
            with self.subTest(label):
                self.assert_refused(self.write(owner=header), 409, code)
        self.assert_no_business_write()

    def test_REGRESSION_an_owner_refusal_is_answered_before_csrf(self):
        """
        A CSRF 403 here would be worse than useless: the client's one recovery is to
        re-read session/ and retry, and that retry would carry the NEW session's
        token and run the command as the new actor.
        """
        self.sign_in(self.alice, self.alice_secret)
        alice_owner = owner_header(self.jar_session())
        csrf_a = self.csrf()
        self.sign_in(self.bob, self.bob_secret)             # no echo: the jar is Bob's
        for label, token in (
            ('the old token', csrf_a), ('no token', None), ('a wrong token', 'x' * 32),
        ):
            with self.subTest(label):
                self.assert_refused(
                    self.write(owner=alice_owner, csrf=token), 409, ACTOR_CHANGED,
                )
        self.assert_no_business_write()

    def test_an_owner_refusal_is_answered_before_the_elevation_permission(self):
        self.sign_in(self.alice, self.alice_secret)
        first = self.jar_session()
        self.sign_in(self.alice, self.alice_secret)
        second = self.jar_session()
        AdminSession.objects.filter(pk=second.pk).update(elevated_at=None)
        audits = AdminAuditLog.objects.count()

        response = self.write(owner=owner_header(first))

        self.assert_refused(response, 409, SESSION_CHANGED)
        # The elevation denial would have written its own audit row; this never got
        # that far.
        self.assertEqual(AdminAuditLog.objects.count(), audits)

    def test_a_malformed_header_is_answered_before_csrf_and_the_permission(self):
        self.sign_in(self.alice, self.alice_secret)
        AdminSession.objects.filter(pk=self.jar_session().pk).update(elevated_at=None)
        self.assert_refused(self.write(owner='garbage', csrf=None), 400, MALFORMED)
        self.assert_no_business_write()

    def test_refusals_carry_no_identifier_header_or_credential(self):
        self.sign_in(self.alice, self.alice_secret)
        alice_session = self.jar_session()
        alice_owner = owner_header(alice_session)
        csrf_a = self.csrf()
        self.sign_in(self.bob, self.bob_secret)
        bob_session = self.jar_session()
        raw_bob = self.browser.cookies[cookie_name()].value
        csrf_b = self.csrf()
        responses = (
            self.write(owner=alice_owner),
            self.write(owner=f'1;{self.bob.pk};{alice_session.pk}'),
            self.write(owner=alice_owner.upper()),
            self.logout(owner=alice_owner),
        )
        secrets_ = (
            str(self.alice.pk), str(self.bob.pk), str(alice_session.pk),
            str(bob_session.pk), alice_owner, raw_bob, bob_session.token_hash,
            alice_session.token_hash, csrf_a, csrf_b,
        )
        for response in responses:
            self.assertIn(response.status_code, (400, 409))
            self.assertEqual(set(response.json()), {'detail', 'code'})
            content = response.content.decode().lower()
            for value in secrets_:
                self.assertNotIn(value.lower(), content)


# --- controls: the header only ever refuses ------------------------------------------


class CommandOwnerControlTests(_OwnerCase):

    def test_CONTROL_a_matching_owner_runs_the_command(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()

        response = self.write(owner=owner_header(session))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.business_effects(), (True, 1))
        entry = AdminAuditLog.objects.get(action=ADMIN_RESTAURANT_PAYMENT_TIMING_SET)
        self.assertEqual(entry.actor_id, self.alice.pk)
        self.assertEqual(entry.session_id, session.pk)
        self.assertEqual(entry.result, RESULT_SUCCESS)

    def test_CONTROL_an_absent_header_is_the_legacy_contract(self):
        self.sign_in(self.alice, self.alice_secret)
        response = self.write()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.business_effects(), (True, 1))

    def test_LIMIT_an_absent_header_in_the_echoed_jar_still_runs_as_the_new_actor(self):
        """
        B1 protects a client that names its owner, and no other. A client that sends
        no header keeps the exposure this change exists to close, and the command runs
        as whoever the jar now holds. This pins that limit so it is not mistaken for
        coverage.
        """
        _alice_owner, csrf_a, bob_session = self.echo_alice_then_bob()
        response = self.write(csrf=csrf_a)
        self.assertEqual(response.status_code, 200, response.content)
        entry = AdminAuditLog.objects.get(action=ADMIN_RESTAURANT_PAYMENT_TIMING_SET)
        self.assertEqual(entry.actor_id, self.bob.pk)
        self.assertEqual(entry.session_id, bob_session.pk)

    def test_CONTROL_safe_methods_ignore_the_header_entirely(self):
        self.sign_in(self.alice, self.alice_secret)
        current = self.jar_session()
        values = (
            '', 'garbage', f'1;{self.bob.pk};{uuid.uuid4()}',
            f'1;{current.user_id};{uuid.uuid4()}', owner_header(current),
        )
        for value in values:
            with self.subTest(value=value):
                response = self.browser.get(SESSION, **{OWNER_META: value})
                self.assertEqual(response.status_code, 200, response.content)
                data = response.json()['data']
                self.assertIn('command_owner', data)
                self.assertEqual(data['command_owner']['session'], str(current.pk))
                listing = self.browser.get(DIRECTORY, **{OWNER_META: value})
                self.assertEqual(listing.status_code, 200, listing.content)

    def test_CONTROL_no_live_session_is_still_the_existing_401(self):
        raw_revoked, revoked = sessions.create_session(self.alice)
        sessions.revoke(revoked, 'test')
        raw_expired, expired = sessions.create_session(self.alice)
        AdminSession.objects.filter(pk=expired.pk).update(
            absolute_expiry=timezone.now() - timedelta(seconds=1),
        )
        cookies = (
            ('no cookie', None, 'Authentication credentials were not provided.'),
            ('unknown cookie', 'not-a-session', 'Invalid or expired admin session.'),
            ('revoked session', raw_revoked, 'Invalid or expired admin session.'),
            ('expired session', raw_expired, 'Invalid or expired admin session.'),
        )
        for label, raw, detail in cookies:
            for value in (f'1;{self.alice.pk};{revoked.pk}', 'garbage', ''):
                with self.subTest(label, header=value):
                    client = Client(enforce_csrf_checks=True)
                    if raw is not None:
                        client.cookies[cookie_name()] = raw
                    response = self.write(owner=value, csrf=None, client=client)
                    self.assertEqual(response.status_code, 401, response.content)
                    self.assertEqual(response.json(), {'detail': detail})
        self.assert_no_business_write()

    def test_CONTROL_an_eligibility_denial_is_answered_before_the_owner_check(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()
        denials = (
            ('inactive', {'is_active': False}, 'Account is inactive.'),
            ('not platform staff', {'account_type': ACCOUNT_TYPE_RESTAURANT_USER},
             'Not a platform-staff account.'),
        )
        for label, change, detail in denials:
            User.objects.filter(pk=self.alice.pk).update(**change)
            for value in ('garbage', f'1;{self.bob.pk};{session.pk}',
                          owner_header(session)):
                with self.subTest(label, header=value):
                    response = self.write(owner=value)
                    self.assertEqual(response.status_code, 401, response.content)
                    self.assertEqual(response.json(), {'detail': detail})
            User.objects.filter(pk=self.alice.pk).update(
                is_active=True, account_type=self.alice.account_type,
            )
        self.assert_no_business_write()

    def test_CONTROL_a_matching_owner_does_not_bypass_csrf(self):
        self.sign_in(self.alice, self.alice_secret)
        header = owner_header(self.jar_session())
        cases = (
            ('no token', {'csrf': None}, 'CSRF Failed: CSRF token missing.'),
            ('wrong token', {'csrf': 'a' * 32},
             "CSRF Failed: CSRF token from the 'X-Csrftoken' HTTP header incorrect."),
            ('foreign origin', {'HTTP_ORIGIN': 'https://evil.example.invalid'},
             'CSRF Failed: Origin checking failed - https://evil.example.invalid '
             'does not match any trusted origins.'),
        )
        for label, kwargs, detail in cases:
            with self.subTest(label):
                response = self.write(owner=header, **kwargs)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json(), {'detail': detail})
        self.assert_no_business_write()

    def test_CONTROL_a_matching_owner_does_not_bypass_recent_elevation(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()
        AdminSession.objects.filter(pk=session.pk).update(elevated_at=None)

        response = self.write(owner=owner_header(session))

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(
            response.json()['detail'], 'This action requires recent re-authentication.',
        )
        self.assert_no_business_write()

    def test_CONTROL_one_same_session_csrf_recovery_still_works(self):
        self.sign_in(self.alice, self.alice_secret)
        header = owner_header(self.jar_session())
        stale = self.csrf()
        # The same session's CSRF cookie is lost, and the bootstrap read issues another.
        del self.browser.cookies[dj_settings.CSRF_COOKIE_NAME]
        read = self.browser.get(SESSION)
        renewed = self.csrf()
        self.assertNotEqual(stale, renewed)

        first = self.write(owner=header, csrf=stale)
        self.assertEqual(first.status_code, 403, first.content)
        self.assertTrue(first.json()['detail'].startswith('CSRF Failed'))

        # The recovery read proves continuity in the server's own words.
        self.assertIn('command_owner', read.json()['data'])
        published = read.json()['data']['command_owner']
        self.assertEqual(f"1;{published['actor']};{published['session']}", header)

        retry = self.write(owner=header, csrf=renewed)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(self.business_effects(), (True, 1))


# --- the header grammar -------------------------------------------------------------


class CommandOwnerGrammarTests(_OwnerCase):

    def malformed_values(self, session):
        actor, sid = str(session.user_id), str(session.pk)
        valid = f'1;{actor};{sid}'
        return {
            'empty': '',
            'whitespace only': '   ',
            'not a triple': 'garbage',
            'version only': '1',
            'actor only': f'1;{actor}',
            'missing session': f'1;{actor};',
            'missing actor': f'1;;{sid}',
            'extra field': f'{valid};{sid}',
            'trailing separator': f'{valid};',
            'version 2': f'2;{actor};{sid}',
            'version 0': f'0;{actor};{sid}',
            'zero-padded version': f'01;{actor};{sid}',
            'prefixed version': f'v1;{actor};{sid}',
            'uppercase ids': f'1;{actor.upper()};{sid.upper()}',
            'unhyphenated id': f"1;{actor.replace('-', '')};{sid}",
            'braced id': f'1;{{{actor}}};{sid}',
            'urn id': f'1;urn:uuid:{actor};{sid}',
            'non-hex id': f'1;{actor[:-1]}g;{sid}',
            'leading space': f' {valid}',
            'trailing space': f'{valid} ',
            'trailing newline': f'{valid}\n',
            'comma separated': f'1,{actor},{sid}',
            'space inside': f'1; {actor};{sid}',
            'two headers joined by the server': f'{valid}, {valid}',
            'two headers joined without a space': f'{valid},{valid}',
            'two values concatenated': f'{valid}{valid}',
            'overlong': valid + 'x' * 4096,
            'non-ascii': f'1;{actor};{sid[:-1]}é',
        }

    def test_REGRESSION_every_malformed_value_is_refused_and_never_read_as_absent(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()
        for label, value in self.malformed_values(session).items():
            with self.subTest(label):
                response = self.write(owner=value)
                self.assert_refused(response, 400, MALFORMED)
                if value.strip():
                    self.assertNotIn(value.strip(), response.content.decode())
        self.assert_no_business_write()

    def test_REGRESSION_an_empty_header_is_not_an_absent_one(self):
        self.sign_in(self.alice, self.alice_secret)
        self.assert_refused(self.write(owner=''), 400, MALFORMED)
        self.assert_no_business_write()
        absent = self.write()
        self.assertEqual(absent.status_code, 200, absent.content)

    def test_REGRESSION_a_malformed_header_spends_no_factor(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()
        before = self.factor_state(self.alice)
        before_session = self.session_state(session)
        values = self.malformed_values(session)
        code = self.next_totp(self.alice, self.alice_secret)
        for label in ('empty', 'uppercase ids', 'extra field'):
            with self.subTest(label):
                self.assert_refused(
                    self.elevate('recovery', self.alice_codes[0], owner=values[label]),
                    400, MALFORMED,
                )
                self.assert_refused(
                    self.elevate('totp', code, owner=values[label]), 400, MALFORMED,
                )
        self.assertEqual(self.factor_state(self.alice), before)
        self.assertEqual(self.session_state(session), before_session)


# --- sign-out -----------------------------------------------------------------------


class CommandOwnerLogoutTests(_OwnerCase):

    def test_REGRESSION_a_stale_tab_cannot_sign_out_a_later_actor(self):
        self.sign_in(self.alice, self.alice_secret)
        alice_owner = owner_header(self.jar_session())
        self.sign_in(self.bob, self.bob_secret)
        bob_session = self.jar_session()
        before = self.session_state(bob_session)
        audits = self.logout_audits()

        response = self.logout(owner=alice_owner)

        self.assert_refused(response, 409, ACTOR_CHANGED)
        self.assertEqual(self.session_state(bob_session), before)
        self.assertEqual(self.logout_audits(), audits)
        # The browser is still Bob's: nothing cleared his cookie.
        self.assertEqual(self.browser.get(SESSION).status_code, 200)

    def test_REGRESSION_a_stale_tab_cannot_sign_out_the_same_actors_successor(self):
        self.sign_in(self.alice, self.alice_secret)
        first = self.jar_session()
        self.sign_in(self.alice, self.alice_secret)
        second = self.jar_session()
        before = self.session_state(second)
        audits = self.logout_audits()

        response = self.logout(owner=owner_header(first))

        self.assert_refused(response, 409, SESSION_CHANGED)
        self.assertEqual(self.session_state(second), before)
        self.assertEqual(self.logout_audits(), audits)
        self.assertEqual(self.browser.get(SESSION).status_code, 200)

    def test_CONTROL_a_matching_named_logout_signs_out_as_before(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()

        response = self.logout(owner=owner_header(session))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), SIGNED_OUT)
        row = AdminSession.objects.get(pk=session.pk)
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(row.revoked_reason, 'logout')
        for name in (cookie_name(), challenge_cookie_name()):
            self.assertEqual(response.cookies[name].value, '')
            self.assertEqual(response.cookies[name]['max-age'], 0)
        entry = AdminAuditLog.objects.get(action=ADMIN_AUTH_LOGOUT)
        self.assertEqual((entry.actor_id, entry.session_id), (self.alice.pk, session.pk))
        self.assertEqual(self.browser.get(SESSION).status_code, 401)

    def test_LIMIT_an_absent_header_logout_still_ends_whatever_session_the_jar_holds(self):
        """The legacy contract, unchanged: an old client's stale tab can still sign out
        whoever signed in since. Only a client that names its owner is protected."""
        self.sign_in(self.alice, self.alice_secret)
        self.sign_in(self.bob, self.bob_secret)
        bob_session = self.jar_session()

        response = self.logout()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(AdminSession.objects.get(pk=bob_session.pk).revoked_at)
        self.assertEqual(response.cookies[cookie_name()].value, '')
        self.assertEqual(self.logout_audits(), 1)

    def test_REGRESSION_a_named_logout_with_no_live_session_is_a_quiet_no_op(self):
        raw_revoked, revoked = sessions.create_session(self.alice)
        sessions.revoke(revoked, 'test')
        raw_expired, expired = sessions.create_session(self.alice)
        AdminSession.objects.filter(pk=expired.pk).update(
            absolute_expiry=timezone.now() - timedelta(seconds=1),
        )
        raw_idle, idle = sessions.create_session(self.alice)
        AdminSession.objects.filter(pk=idle.pk).update(
            last_seen=timezone.now() - timedelta(minutes=31),
        )
        cases = (
            ('no cookie', None, revoked),
            ('unknown cookie', 'not-a-session', revoked),
            ('revoked session', raw_revoked, revoked),
            ('expired session', raw_expired, expired),
            ('idle session', raw_idle, idle),
        )
        for label, raw, named in cases:
            with self.subTest(label):
                client = Client(enforce_csrf_checks=True)
                if raw is not None:
                    client.cookies[cookie_name()] = raw
                before = self.session_state(named)

                response = self.logout(owner=owner_header(named), client=client)

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json(), SIGNED_OUT)
                self.assert_no_set_cookie(response)
                self.assertEqual(self.session_state(named), before)
                self.assertEqual(self.logout_audits(), 0)

    def test_REGRESSION_a_malformed_logout_header_is_refused_with_or_without_a_session(self):
        self.sign_in(self.alice, self.alice_secret)
        session = self.jar_session()
        before = self.session_state(session)
        for value in ('', 'garbage', owner_header(session).upper(),
                      f'{owner_header(session)}, {owner_header(session)}'):
            with self.subTest('live session', header=value):
                self.assert_refused(self.logout(owner=value), 400, MALFORMED)
            with self.subTest('no session', header=value):
                anonymous = Client(enforce_csrf_checks=True)
                self.assert_refused(
                    self.logout(owner=value, client=anonymous), 400, MALFORMED,
                )
        self.assertEqual(self.session_state(session), before)
        self.assertEqual(self.logout_audits(), 0)
        self.assertEqual(self.browser.get(SESSION).status_code, 200)

    def test_LIMIT_a_delayed_matching_logout_response_can_remove_a_later_cookie(self):
        """
        A sign-out that MATCHED when it was sent is answered with the ordinary
        cookie-clearing response. If that response arrives after somebody else has
        signed in, it removes the later session's cookie from the browser. The later
        session's ROW is untouched: the cost is that browser having to sign in again,
        not a revocation and not anybody's command running. B1 does not change this.
        """
        self.sign_in(self.alice, self.alice_secret)
        alice_owner = owner_header(self.jar_session())
        held = self.logout(owner=alice_owner, client=self.snapshot_client())
        self.assertEqual(held.status_code, 200, held.content)

        self.sign_in(self.bob, self.bob_secret)
        bob_session = self.jar_session()
        self.browser.cookies.update(held.cookies)           # the late response lands

        self.assertEqual(self.browser.get(SESSION).status_code, 401)
        self.assertIsNone(AdminSession.objects.get(pk=bob_session.pk).revoked_at)
