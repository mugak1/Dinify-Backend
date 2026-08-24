"""
The Admin commercial WRITE endpoints (Phase 1, Step 3D.2a).

The first supported HTTP path for changing canonical commercial configuration, so
this suite is as much about the CONTROL-PLANE discipline around the write —
elevation, CSRF, a substantive reason, exactly one audit row, and the transaction
that binds the mutation to that row — as about the two values themselves.

The domain rules (locking, vocabulary, optimistic concurrency, same-state no-op,
attribution) belong to ``commercial_app`` and are covered by its own suites. What is
pinned HERE is that they are reached through HTTP unchanged, and that the adapter
adds nothing of its own to them.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commercial_app.models import (
    PAYMENT_COLLECTION_MODE_OFFLINE,
    PAYMENT_COLLECTION_MODE_PSP_ONLINE,
    PAYMENT_TIMING_PAY_AFTER,
    PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_LIFECYCLE_STATES,
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
)
from finance_app.models import DinifyTransaction
from platform_admin_app import sessions
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET,
    ADMIN_RESTAURANT_PAYMENT_TIMING_SET,
)
from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES
from platform_admin_app.cookies import cookie_name
from platform_admin_app.endpoints import commercial as commercial_endpoints
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AdminAuditLog,
)
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import Restaurant
from users_app.models import User

# Distinct phone range from the other admin suites.
_PHONE = iter(f'25670710{n:05d}' for n in range(1, 9999))

PASSWORD = 'correct-horse-battery'
REASON = 'Confirmed the service model with the owner on the launch call.'

_ADMIN_OVERRIDES = dict(
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
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type,
    )


def _make_admin(email='cw-admin@t.com', username='cw-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Java House', status=RestaurantStatus_Live, deleted=False,
                     is_test=False):
    return Restaurant.objects.create(
        name=name, location=f'{name} Road', status=status, deleted=deleted,
        is_test=is_test,
        owner=_make_user(f'owner-{name}@t.com'.replace(' ', '-')),
    )


def timing_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/commercial/payment-timing/'


def mode_url(restaurant):
    return (
        f'/admin/v1/restaurants/{restaurant.id}/commercial/payment-collection-mode/'
    )


class _CommercialWriteTestCase(AuditAssertionsMixin, TestCase):
    """An authenticated, RECENTLY ELEVATED admin session pointed at one restaurant."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    # --- requests ---
    def post(self, url, **body):
        return self.client.post(url, data=body, content_type='application/json')

    def set_timing(self, value, expected_current, *, reason=REASON, restaurant=None):
        return self.post(
            timing_url(restaurant or self.restaurant),
            value=value, expected_current=expected_current, reason=reason,
        )

    def set_mode(self, value, expected_current, *, reason=REASON, restaurant=None):
        return self.post(
            mode_url(restaurant or self.restaurant),
            value=value, expected_current=expected_current, reason=reason,
        )

    # --- state ---
    def config(self, restaurant=None):
        return RestaurantServiceConfiguration.objects.filter(
            restaurant=restaurant or self.restaurant,
        ).first()

    def seed(self, *, timing=None, mode=None, at=None, actor=None):
        """Establish stored state directly, so a test can start from any state."""
        at = at or (timezone.now() - timedelta(days=2))
        actor = actor or self.admin
        fields = {}
        if timing is not None:
            fields.update(
                payment_timing=timing, payment_timing_set_at=at,
                payment_timing_set_by=actor,
            )
        if mode is not None:
            fields.update(
                payment_collection_mode=mode,
                payment_collection_mode_set_at=at,
                payment_collection_mode_set_by=actor,
            )
        return RestaurantServiceConfiguration.objects.create(
            restaurant=self.restaurant, **fields,
        )


# --- §37 authentication, elevation, plane isolation ---------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialWriteAuthTests(_CommercialWriteTestCase):
    """
    A valid session is not enough. Both writes are step-up gated.

    Nothing in the order or kitchen path consumes payment timing yet, and that is
    deliberately not treated as a reason to gate it lightly — the decision is
    consequential when it is RECORDED, because the enforcement built later is built
    against whatever the configuration says by then.
    """

    def urls(self):
        return (timing_url(self.restaurant), mode_url(self.restaurant))

    def bodies(self):
        return (
            {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
             'reason': REASON},
            {'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'expected_current': None,
             'reason': REASON},
        )

    def test_anonymous_is_refused(self):
        for url, body in zip(self.urls(), self.bodies()):
            with self.subTest(url=url):
                response = Client().post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 401, response.content)
        self.assertIsNone(self.config())

    def test_anonymous_manufactures_no_audit_actor(self):
        """
        A 401 is about identity, not an administrative decision.

        Auditing it would create a row naming an actor the plane could not name.
        """
        for url, body in zip(self.urls(), self.bodies()):
            Client().post(url, data=body, content_type='application/json')
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_authenticated_but_never_elevated_is_refused(self):
        raw, _session = sessions.create_session(_make_admin(
            email='cw-plain@t.com', username='cw-plain',
        ))
        client = Client()
        client.cookies[cookie_name()] = raw
        for url, body in zip(self.urls(), self.bodies()):
            with self.subTest(url=url):
                response = client.post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 403, response.content)
        self.assertIsNone(self.config())

    def test_stale_elevation_is_refused(self):
        self.session.elevated_at = timezone.now() - timedelta(hours=3)
        self.session.save(update_fields=['elevated_at'])
        for url, body in zip(self.urls(), self.bodies()):
            with self.subTest(url=url):
                response = self.client.post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 403, response.content)
        self.assertIsNone(self.config())

    def test_an_elevation_denial_is_audited_exactly_once_per_route(self):
        """
        DRF rejects a failed permission BEFORE the handler, so without the
        ``permission_denied`` override this refusal would 403 with no entry.
        """
        self.session.elevated_at = timezone.now() - timedelta(hours=3)
        self.session.save(update_fields=['elevated_at'])
        self.client.post(
            timing_url(self.restaurant),
            data=self.bodies()[0], content_type='application/json',
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_DENIED,
        )
        self.assertEqual(entry.error_code, 'elevation_required')
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertEqual(entry.resource_type, 'Restaurant')
        # No after-state was fabricated for a request that never ran.
        self.assertIsNone(entry.after_state)

    def test_a_collection_mode_elevation_denial_is_audited_under_its_own_action(self):
        self.session.elevated_at = timezone.now() - timedelta(hours=3)
        self.session.save(update_fields=['elevated_at'])
        self.client.post(
            mode_url(self.restaurant),
            data=self.bodies()[1], content_type='application/json',
        )
        self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET, result=RESULT_DENIED,
        )
        self.assertNotAudited(ADMIN_RESTAURANT_PAYMENT_TIMING_SET)

    def test_recently_elevated_is_accepted(self):
        self.assertEqual(
            self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).status_code, 200,
        )
        self.assertEqual(
            self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None).status_code, 200,
        )

    def test_a_restaurant_user_session_cannot_reach_these_routes(self):
        """
        The admin authenticator refuses a non-platform-staff account at the door,
        so a tenant credential never becomes an admin one.
        """
        tenant = _make_user('cw-tenant@t.com')
        raw, _session = sessions.create_session(tenant)
        client = Client()
        client.cookies[cookie_name()] = raw
        for url, body in zip(self.urls(), self.bodies()):
            with self.subTest(url=url):
                response = client.post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 401, response.content)
        self.assertIsNone(self.config())

    def test_a_delegated_credential_cannot_reach_these_routes(self):
        """
        A delegated session is a CUSTOMER-plane credential. It carries no admin
        cookie, so the admin authenticator has nothing to resolve.
        """
        response = Client().post(
            timing_url(self.restaurant),
            data=self.bodies()[0], content_type='application/json',
            HTTP_X_DELEGATION_SESSION='not-a-real-token',
        )
        self.assertEqual(response.status_code, 401, response.content)

    def test_no_commercial_write_route_is_on_the_delegated_allowlist(self):
        """
        Structural, not incidental: the allowlist keys customer-plane route
        patterns, and these live on the admin urlconf. A ratchet, so a future
        entry naming a commercial write would fail here.
        """
        for route, _method in ALLOWED_ROUTES:
            self.assertNotIn('commercial/', route)
            self.assertNotIn('payment-timing', route)
            self.assertNotIn('payment-collection-mode', route)

    def test_reads_remain_unelevated(self):
        """Adding elevated writes must not raise the bar for looking."""
        raw, _session = sessions.create_session(_make_admin(
            email='cw-read@t.com', username='cw-read',
        ))
        client = Client()
        client.cookies[cookie_name()] = raw
        self.assertEqual(client.get('/admin/v1/restaurants/').status_code, 200)
        self.assertEqual(
            client.get(f'/admin/v1/restaurants/{self.restaurant.id}/').status_code,
            200,
        )

    def test_get_is_not_allowed_on_a_write_route(self):
        for url in self.urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)


# --- §38 CSRF -----------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialWriteCsrfTests(AuditAssertionsMixin, TestCase):
    """
    The existing admin CSRF policy applies, and is not reimplemented here.

    ``AdminSessionAuthentication.enforce_csrf`` runs Django's double-submit check on
    every unsafe admin request, because DRF marks each ``APIView`` ``csrf_exempt``
    and Django's middleware therefore never sees it. These tests use
    ``Client(enforce_csrf_checks=True)`` — the DEFAULT test client sets
    ``_dont_enforce_csrf_checks``, which short-circuits the check before it looks at
    anything, so a suite that used it would prove nothing at all.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='cw-csrf@t.com', username='cw-csrf')
        self.restaurant = _make_restaurant('CSRF House')
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.raw = raw
        self.body = {
            'value': PAYMENT_TIMING_PAY_FIRST,
            'expected_current': None,
            'reason': REASON,
        }

    def _client(self):
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = self.raw
        return client

    def _token_for(self, client):
        """Have the SERVER issue a token, exactly as the SPA obtains one."""
        response = client.get('/admin/v1/auth/session/')
        self.assertEqual(response.status_code, 200, response.content)
        return client.cookies[dj_settings.CSRF_COOKIE_NAME].value

    def test_missing_csrf_token_is_refused(self):
        for url in (timing_url(self.restaurant), mode_url(self.restaurant)):
            with self.subTest(url=url):
                response = self._client().post(
                    url, data=self.body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_a_wrong_csrf_token_is_refused(self):
        client = self._client()
        self._token_for(client)
        response = client.post(
            timing_url(self.restaurant), data=self.body,
            content_type='application/json',
            HTTP_X_CSRFTOKEN='not-the-token-the-server-issued',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_the_server_issued_token_is_accepted(self):
        for url, value in (
            (timing_url(self.restaurant), PAYMENT_TIMING_PAY_FIRST),
            (mode_url(self.restaurant), PAYMENT_COLLECTION_MODE_OFFLINE),
        ):
            with self.subTest(url=url):
                client = self._client()
                token = self._token_for(client)
                response = client.post(
                    url,
                    data={'value': value, 'expected_current': None,
                          'reason': REASON},
                    content_type='application/json',
                    HTTP_X_CSRFTOKEN=token,
                )
                self.assertEqual(response.status_code, 200, response.content)

    def test_a_refused_csrf_request_writes_no_audit_row(self):
        """
        CSRF is rejected inside authentication, before any administrative decision
        could exist — the same boundary as an anonymous request.
        """
        self._client().post(
            timing_url(self.restaurant), data=self.body,
            content_type='application/json',
        )
        self.assertEqual(AdminAuditLog.objects.count(), 0)


# --- §39 payment timing, success ----------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class PaymentTimingSuccessTests(_CommercialWriteTestCase):

    def test_first_configuration_to_pay_first(self):
        response = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertTrue(data['changed'])
        self.assertEqual(
            data['commercial']['payment_timing']['value'], PAYMENT_TIMING_PAY_FIRST,
        )
        self.assertTrue(data['commercial']['payment_timing']['configured'])
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_FIRST)

    def test_first_configuration_to_pay_after(self):
        response = self.set_timing(PAYMENT_TIMING_PAY_AFTER, None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_AFTER)

    def test_pay_first_to_pay_after(self):
        self.seed(timing=PAYMENT_TIMING_PAY_FIRST)
        response = self.set_timing(
            PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_AFTER)

    def test_pay_after_to_pay_first(self):
        self.seed(timing=PAYMENT_TIMING_PAY_AFTER)
        response = self.set_timing(
            PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_AFTER,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_FIRST)

    def test_the_other_axis_is_untouched(self):
        """
        Two decisions, written independently. Setting timing must not manufacture a
        collection-mode decision as a side effect.
        """
        response = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        mode = response.json()['data']['commercial']['payment_collection_mode']
        self.assertEqual(
            mode, {'configured': False, 'value': None, 'set_at': None},
        )
        config = self.config()
        self.assertIsNone(config.payment_collection_mode)
        self.assertIsNone(config.payment_collection_mode_set_at)
        self.assertIsNone(config.payment_collection_mode_set_by_id)

    def test_attribution_names_the_authenticated_admin(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(self.config().payment_timing_set_by_id, self.admin.id)

    def test_the_actor_cannot_be_supplied_by_the_request(self):
        """
        Actor identity comes from the authenticated control plane and nowhere else.
        A body field naming somebody else is ignored, not honoured.
        """
        impostor = _make_admin(email='cw-other@t.com', username='cw-other')
        self.post(
            timing_url(self.restaurant),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
            actor=str(impostor.id), actor_id=str(impostor.id),
            payment_timing_set_by=str(impostor.id),
        )
        self.assertEqual(self.config().payment_timing_set_by_id, self.admin.id)

    def test_the_attribution_timestamp_moves_only_on_a_real_mutation(self):
        stamp = timezone.now() - timedelta(days=5)
        self.seed(timing=PAYMENT_TIMING_PAY_FIRST, at=stamp)

        self.set_timing(PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_FIRST)
        self.assertEqual(self.config().payment_timing_set_at, stamp)

        self.set_timing(PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST)
        self.assertGreater(self.config().payment_timing_set_at, stamp)

    def test_exactly_one_success_audit(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertAudited(ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS)
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_the_response_carries_the_whole_canonical_object(self):
        data = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).json()['data']
        self.assertEqual(
            set(data['commercial']),
            {'payment_timing', 'payment_collection_mode', 'subscription_terms'},
        )
        self.assertEqual(set(data), {'changed', 'commercial'})

    def test_the_response_omits_the_legacy_compatibility_fields(self):
        """
        They exist for the deployed frontend's GET contract. Teaching a brand-new
        write surface to emit them would recruit a consumer for fields on their way
        out.
        """
        payload = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).json()
        for key in ('payment_mode', 'payment_mode_configured', 'subscription'):
            self.assertNotIn(key, payload['data'])
            self.assertNotIn(key, payload['data']['commercial'])


# --- §40 payment timing, safe retry -------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class PaymentTimingSafeRetryTests(_CommercialWriteTestCase):
    """
    A lost response followed by an identical retry is a success, not a conflict.

    The same-state check runs BEFORE the staleness comparison inside the writer, so
    a retry whose ``expected_current`` has gone stale still succeeds — and, crucially,
    does not rewrite the attribution of the decision somebody else already made.
    """

    def setUp(self):
        super().setUp()
        self.stamp = timezone.now() - timedelta(days=4)
        self.decider = _make_admin(email='cw-first@t.com', username='cw-first')
        self.seed(
            timing=PAYMENT_TIMING_PAY_FIRST, at=self.stamp, actor=self.decider,
        )

    def test_identical_retry_with_stale_expectation_is_a_success_no_op(self):
        response = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])

    def test_identical_retry_with_a_different_stale_expectation_also_no_ops(self):
        response = self.set_timing(
            PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_AFTER,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])

    def test_the_original_attribution_survives(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        config = self.config()
        self.assertEqual(config.payment_timing_set_at, self.stamp)
        self.assertEqual(config.payment_timing_set_by_id, self.decider.id)

    def test_the_retry_is_audited_as_one_success(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.before_state, entry.after_state)
        self.assertEqual(
            entry.before_state, {'payment_timing': PAYMENT_TIMING_PAY_FIRST},
        )
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_no_second_configuration_row_appears(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)

    def test_the_canonical_response_still_reports_the_stored_value(self):
        data = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).json()['data']
        self.assertEqual(
            data['commercial']['payment_timing']['value'], PAYMENT_TIMING_PAY_FIRST,
        )
        self.assertEqual(
            data['commercial']['payment_timing']['set_at'], self.stamp.isoformat(),
        )


# --- §41 payment timing, stale conflict ---------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class PaymentTimingConflictTests(_CommercialWriteTestCase):
    """
    A well-formed request against a world that moved is a 409, never a 400.

    The client's body is syntactically fine; what changed is the state underneath
    it, and the correct reaction is "reload and look again" rather than "fix your
    input".
    """

    def setUp(self):
        super().setUp()
        self.stamp = timezone.now() - timedelta(days=4)
        self.decider = _make_admin(email='cw-held@t.com', username='cw-held')
        self.seed(
            timing=PAYMENT_TIMING_PAY_AFTER, at=self.stamp, actor=self.decider,
        )

    def test_unconfigured_expectation_against_a_configured_row_conflicts(self):
        response = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'stale_service_configuration')
        self.assertEqual(body['status'], 409)

    def test_a_configured_to_configured_stale_expectation_conflicts(self):
        """The other direction: they believed pay_first, it is pay_after."""
        third = _make_restaurant('Third House')
        RestaurantServiceConfiguration.objects.create(
            restaurant=third, payment_timing=PAYMENT_TIMING_PAY_AFTER,
            payment_timing_set_at=self.stamp, payment_timing_set_by=self.admin,
        )
        response = self.post(
            timing_url(third),
            # Believed pay_first; stored is pay_after; requesting a third state.
            value=PAYMENT_TIMING_PAY_FIRST,
            expected_current=PAYMENT_TIMING_PAY_FIRST,
            reason=REASON,
        )
        # Requested == expected but != stored -> genuinely stale.
        self.assertEqual(response.status_code, 409, response.content)

    def test_state_and_attribution_are_untouched(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        config = self.config()
        self.assertEqual(config.payment_timing, PAYMENT_TIMING_PAY_AFTER)
        self.assertEqual(config.payment_timing_set_at, self.stamp)
        self.assertEqual(config.payment_timing_set_by_id, self.decider.id)

    def test_exactly_one_failure_audit(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_FAILURE,
        )
        self.assertEqual(entry.error_code, 'stale_service_configuration')
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_the_conflict_audit_names_the_real_current_value_and_no_after_state(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_FAILURE,
        )
        self.assertEqual(
            entry.before_state, {'payment_timing': PAYMENT_TIMING_PAY_AFTER},
        )
        # Nothing was applied, so no after-state may be invented.
        self.assertIsNone(entry.after_state)

    def test_no_internal_detail_leaks_into_the_body(self):
        body = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).json()
        self.assertEqual(
            body['message'], 'Commercial configuration changed since it was loaded.',
        )
        # No exception text, no SQL, no column names, no ids.
        serialized = str(body).lower()
        for leak in ('traceback', 'select ', 'restaurant_id', 'actual_current',
                     'no longer the value'):
            self.assertNotIn(leak, serialized)


# --- §42/§43 collection mode ---------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CollectionModeSuccessTests(_CommercialWriteTestCase):

    def test_null_to_offline_is_ordinary_configuration(self):
        """
        ``offline`` is a permanent, first-class commercial mode — the state the
        first commercial restaurant is expected to launch in.
        """
        response = self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertEqual(response.status_code, 200, response.content)
        axis = response.json()['data']['commercial']['payment_collection_mode']
        self.assertTrue(axis['configured'])
        self.assertEqual(axis['value'], PAYMENT_COLLECTION_MODE_OFFLINE)
        self.assertIsNotNone(axis['set_at'])

    def test_offline_to_psp_online(self):
        self.seed(mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        response = self.set_mode(
            PAYMENT_COLLECTION_MODE_PSP_ONLINE, PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])
        self.assertEqual(
            self.config().payment_collection_mode,
            PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )

    def test_psp_online_to_offline(self):
        self.seed(mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        response = self.set_mode(
            PAYMENT_COLLECTION_MODE_OFFLINE, PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self.config().payment_collection_mode, PAYMENT_COLLECTION_MODE_OFFLINE,
        )

    def test_psp_online_creates_no_provider_state_whatsoever(self):
        """
        Exactly one commercial configuration mutation. No merchant record, no
        provider call, no transaction, no webhook — none of which exists to create.
        """
        before = DinifyTransaction.objects.count()
        response = self.set_mode(PAYMENT_COLLECTION_MODE_PSP_ONLINE, None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(DinifyTransaction.objects.count(), before)
        blob = str(response.json()).lower()
        for word in ('provider', 'merchant', 'psp_ready', 'psp_status',
                     'flutterwave', 'pesapal', 'webhook'):
            self.assertNotIn(word, blob)

    def test_no_tender_is_inferred_from_either_mode(self):
        for mode in (PAYMENT_COLLECTION_MODE_OFFLINE,
                     PAYMENT_COLLECTION_MODE_PSP_ONLINE):
            with self.subTest(mode=mode):
                restaurant = _make_restaurant(f'Tender {mode}')
                response = self.post(
                    mode_url(restaurant),
                    value=mode, expected_current=None, reason=REASON,
                )
                blob = str(response.json()).lower()
                for word in ('cash', 'momo', 'mobile_money', 'card'):
                    self.assertNotIn(word, blob)

    def test_the_other_axis_is_untouched(self):
        response = self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        timing = response.json()['data']['commercial']['payment_timing']
        self.assertEqual(
            timing, {'configured': False, 'value': None, 'set_at': None},
        )

    def test_exactly_one_success_audit_under_its_own_action(self):
        self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET, result=RESULT_SUCCESS,
        )
        self.assertNotAudited(ADMIN_RESTAURANT_PAYMENT_TIMING_SET)
        self.assertEqual(AdminAuditLog.objects.count(), 1)


@override_settings(**_ADMIN_OVERRIDES)
class CollectionModeRetryAndConflictTests(_CommercialWriteTestCase):
    """
    ``offline`` gets particular attention here.

    It is the value most at risk of being treated as falsy — "not configured",
    "no collection", "off" — by any layer that tests truthiness rather than
    presence. Every assertion below would fail if that crept in.
    """

    def test_stored_offline_plus_requested_offline_is_a_no_op_success(self):
        stamp = timezone.now() - timedelta(days=3)
        self.seed(mode=PAYMENT_COLLECTION_MODE_OFFLINE, at=stamp)
        response = self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])
        self.assertEqual(self.config().payment_collection_mode_set_at, stamp)

    def test_a_no_op_on_offline_still_reports_it_as_configured(self):
        self.seed(mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        data = self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None).json()['data']
        axis = data['commercial']['payment_collection_mode']
        self.assertTrue(axis['configured'])
        self.assertEqual(axis['value'], PAYMENT_COLLECTION_MODE_OFFLINE)

    def test_stored_psp_online_with_a_stale_offline_expectation_conflicts(self):
        """
        Requesting a DIFFERENT value than stored, on a stale belief: a real
        conflict. (Requesting the stored value would be the no-op above — the
        same-state rule wins, and that is the distinction being pinned.)
        """
        self.seed(mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        response = self.set_mode(
            PAYMENT_COLLECTION_MODE_OFFLINE, PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            response.json()['code'], 'stale_service_configuration',
        )
        self.assertEqual(
            self.config().payment_collection_mode,
            PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )

    def test_stored_psp_online_requesting_psp_online_no_ops_despite_stale_belief(self):
        """The same-state rule beats a stale expectation, for this axis too."""
        self.seed(mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        response = self.set_mode(
            PAYMENT_COLLECTION_MODE_PSP_ONLINE, PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])

    def test_the_conflict_is_audited_once_as_a_failure(self):
        self.seed(mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        self.set_mode(
            PAYMENT_COLLECTION_MODE_OFFLINE, PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET, result=RESULT_FAILURE,
        )
        self.assertEqual(entry.error_code, 'stale_service_configuration')
        self.assertEqual(
            entry.before_state,
            {'payment_collection_mode': PAYMENT_COLLECTION_MODE_PSP_ONLINE},
        )
        self.assertEqual(AdminAuditLog.objects.count(), 1)


# --- §44/§26 request validation ------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class RequestValidationTests(_CommercialWriteTestCase):
    """
    A malformed body from an elevated administrator is still an administrative
    ATTEMPT at a consequential change, so it is a 400 AND exactly one audit row.
    """

    TIMING_CASES = (
        ('missing value', {'expected_current': None, 'reason': REASON}, 'value'),
        ('null value',
         {'value': None, 'expected_current': None, 'reason': REASON}, 'value'),
        ('invalid value',
         {'value': 'prepaid', 'expected_current': None, 'reason': REASON}, 'value'),
        ('tender word as value',
         {'value': 'cash', 'expected_current': None, 'reason': REASON}, 'value'),
        ('missing expected_current',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'reason': REASON}, 'expected_current'),
        ('invalid expected_current',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': 'nope',
          'reason': REASON}, 'expected_current'),
        ('missing reason',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None}, 'reason'),
        ('blank reason',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
          'reason': ''}, 'reason'),
        ('whitespace-only reason',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
          'reason': '        '}, 'reason'),
        ('too-short reason',
         {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
          'reason': 'typo'}, 'reason'),
    )

    MODE_CASES = (
        ('missing value', {'expected_current': None, 'reason': REASON}, 'value'),
        ('invalid value',
         {'value': 'cash', 'expected_current': None, 'reason': REASON}, 'value'),
        ('provider name as value',
         {'value': 'flutterwave', 'expected_current': None, 'reason': REASON},
         'value'),
        ('missing expected_current',
         {'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'reason': REASON},
         'expected_current'),
        ('invalid expected_current',
         {'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'expected_current': 'momo',
          'reason': REASON}, 'expected_current'),
        ('missing reason',
         {'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'expected_current': None},
         'reason'),
        ('too-short reason',
         {'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'expected_current': None,
          'reason': 'x'}, 'reason'),
    )

    def _assert_rejected(self, url, body, field):
        # A DELTA, not a reset: `AdminAuditLog` is append-only and refuses bulk
        # delete, which is exactly the property that makes it worth having.
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            url, data=body, content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        payload = response.json()
        self.assertIn(field, payload['errors'])
        self.assertEqual(payload['status'], 400)
        # No commercial state, and exactly one further failure audit.
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertTrue(
            entry.error_code.startswith(field),
            f'error_code {entry.error_code!r} does not name field {field!r}',
        )
        self.assertIsNone(entry.after_state)

    def test_timing_request_validation(self):
        for label, body, field in self.TIMING_CASES:
            with self.subTest(case=label):
                self._assert_rejected(timing_url(self.restaurant), body, field)

    def test_collection_mode_request_validation(self):
        for label, body, field in self.MODE_CASES:
            with self.subTest(case=label):
                self._assert_rejected(mode_url(self.restaurant), body, field)

    def test_unparseable_json_is_a_400_in_the_house_envelope(self):
        """
        A body DRF cannot parse never reaches the serializer.

        ``request.data`` raises ``ParseError`` on access, which DRF turns into its
        own bare ``{"detail": ...}`` 400 — a different shape from every other error
        this endpoint returns, so a client branching on ``errors`` would break on it.
        """
        for url in (timing_url(self.restaurant), mode_url(self.restaurant)):
            with self.subTest(url=url):
                response = self.client.post(
                    url,
                    data='{"value": "pay_first", ',  # truncated
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                payload = response.json()
                self.assertEqual(payload['status'], 400)
                self.assertIn('errors', payload)
                self.assertIn('__all__', payload['errors'])

    def test_unparseable_json_is_audited_exactly_once(self):
        """
        An elevated administrator sent an unsafe request. That it was unreadable
        does not make it a non-event — and the parse failure happens BEFORE the
        serializer exists, so nothing downstream would have recorded it.
        """
        before = AdminAuditLog.objects.count()
        self.client.post(
            timing_url(self.restaurant),
            data='{"value": "pay_first", ',
            content_type='application/json',
        )
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.action, ADMIN_RESTAURANT_PAYMENT_TIMING_SET)
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'malformed_body')
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertIsNone(entry.before_state)
        self.assertIsNone(entry.after_state)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_an_unparseable_body_against_a_missing_target_is_still_a_silent_404(self):
        """The target check comes first, and its no-audit convention still wins."""
        deleted = _make_restaurant('Unparseable House', deleted=True)
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            timing_url(deleted), data='{oops', content_type='application/json',
        )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_a_non_dict_body_is_rejected_and_audited(self):
        """Already correct before the parse fix — pinned so it cannot regress."""
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            timing_url(self.restaurant),
            data='["not", "a", "dict"]', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('non_field_errors', response.json()['errors'])
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'invalid_request')

    def test_an_empty_body_is_rejected_on_every_field(self):
        response = self.client.post(
            timing_url(self.restaurant), data={},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()['errors']
        self.assertEqual(
            set(errors), {'value', 'expected_current', 'reason'},
        )

    # --- §26: the distinction that would be easiest to lose -------------------

    def test_omitting_expected_current_is_not_the_same_as_sending_null(self):
        """
        THE regression this endpoint most needs.

        Explicit ``null`` asserts "nobody has configured this yet" — the ONE
        assertion that succeeds against a fresh restaurant. A client that simply
        omitted the key made no assertion at all, and quietly treating omission as
        null would hand it that claim by accident, defeating the optimistic
        concurrency it was meant to be exercising.
        """
        omitted = self.client.post(
            timing_url(self.restaurant),
            data={'value': PAYMENT_TIMING_PAY_FIRST, 'reason': REASON},
            content_type='application/json',
        )
        self.assertEqual(omitted.status_code, 400, omitted.content)
        self.assertIn('expected_current', omitted.json()['errors'])
        self.assertIsNone(self.config())

        explicit = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.assertEqual(explicit.status_code, 200, explicit.content)
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_FIRST)

    def test_the_same_distinction_holds_for_collection_mode(self):
        omitted = self.client.post(
            mode_url(self.restaurant),
            data={'value': PAYMENT_COLLECTION_MODE_OFFLINE, 'reason': REASON},
            content_type='application/json',
        )
        self.assertEqual(omitted.status_code, 400, omitted.content)
        self.assertIn('expected_current', omitted.json()['errors'])

        explicit = self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertEqual(explicit.status_code, 200, explicit.content)

    def test_the_reason_bar_matches_the_house_standard(self):
        """Imported from ``delegation``, not respelled — so it cannot drift."""
        from platform_admin_app.delegation import MIN_REASON_LENGTH

        short = 'x' * (MIN_REASON_LENGTH - 1)
        self.assertEqual(
            self.set_timing(PAYMENT_TIMING_PAY_FIRST, None,
                            reason=short).status_code,
            400,
        )
        exact = 'y' * MIN_REASON_LENGTH
        self.assertEqual(
            self.set_timing(PAYMENT_TIMING_PAY_FIRST, None,
                            reason=exact).status_code,
            200,
        )

    # --- §8: the reason recorded on a REJECTED request ------------------------

    def test_a_valid_padded_reason_is_audited_trimmed_when_another_field_fails(self):
        """
        The reason field itself validated, so it IS the operator's stated reason —
        and it must be stored normalized, not as the raw padded string that happened
        to be in the JSON.
        """
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            timing_url(self.restaurant),
            data={'value': 'not-a-timing', 'expected_current': None,
                  'reason': f'   {REASON}   '},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'value_invalid_choice')
        self.assertEqual(entry.reason, REASON)

    def test_a_rejected_reason_is_never_stored(self):
        """
        A reason the serializer REFUSED is not a stated reason. Recording it anyway
        would put text in the control-plane log that the system explicitly declined
        to accept — and would make "reasons on file" include the ones nobody gave.
        """
        cases = (
            ('too short', {'value': PAYMENT_TIMING_PAY_FIRST,
                           'expected_current': None, 'reason': 'typo'}),
            ('blank', {'value': PAYMENT_TIMING_PAY_FIRST,
                       'expected_current': None, 'reason': ''}),
            ('whitespace only', {'value': PAYMENT_TIMING_PAY_FIRST,
                                 'expected_current': None, 'reason': '      '}),
            ('missing', {'value': PAYMENT_TIMING_PAY_FIRST,
                         'expected_current': None}),
            ('over maximum', {'value': PAYMENT_TIMING_PAY_FIRST,
                              'expected_current': None, 'reason': 'x' * 5000}),
            ('wrong type', {'value': PAYMENT_TIMING_PAY_FIRST,
                            'expected_current': None, 'reason': {'a': 1}}),
        )
        for label, body in cases:
            with self.subTest(case=label):
                before = AdminAuditLog.objects.count()
                response = self.client.post(
                    timing_url(self.restaurant), data=body,
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(AdminAuditLog.objects.count(), before + 1)
                entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
                self.assertEqual(entry.result, RESULT_FAILURE)
                self.assertEqual(entry.reason, '', f'{label}: stored a rejected reason')

    def test_the_same_reason_rule_applies_to_collection_mode(self):
        before = AdminAuditLog.objects.count()
        self.client.post(
            mode_url(self.restaurant),
            data={'value': 'cash', 'expected_current': None,
                  'reason': f'  {REASON}  '},
            content_type='application/json',
        )
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        self.assertEqual(entry.reason, REASON)

        self.client.post(
            mode_url(self.restaurant),
            data={'value': PAYMENT_COLLECTION_MODE_OFFLINE,
                  'expected_current': None, 'reason': 'no'},
            content_type='application/json',
        )
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.reason, '')

    def test_a_non_dict_body_records_no_reason(self):
        """There is no field to read, and none may be invented."""
        self.client.post(
            timing_url(self.restaurant), data='["nope"]',
            content_type='application/json',
        )
        entry = AdminAuditLog.objects.order_by('-created_at', '-id').first()
        self.assertEqual(entry.reason, '')

    def test_a_padded_reason_is_stored_trimmed(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None, reason=f'   {REASON}   ')
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.reason, REASON)


# --- §45 target resolution -----------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class TargetResolutionTests(_CommercialWriteTestCase):
    """
    A target that is not there is a 404 and is NOT audited.

    That is the existing restaurant-endpoint convention (the transition view says so
    in as many words): nothing was denied and no tenant was touched, so there is no
    administrative decision to record — and manufacturing an audit identity for a
    nonexistent resource would put rows in the log about restaurants that never
    existed. Pinned here so the choice stays deliberate.
    """

    UNKNOWN = '11111111-2222-3333-4444-555555555555'

    def test_unknown_uuid_is_404(self):
        for path in ('payment-timing', 'payment-collection-mode'):
            with self.subTest(path=path):
                response = self.client.post(
                    f'/admin/v1/restaurants/{self.UNKNOWN}/commercial/{path}/',
                    data={'value': PAYMENT_TIMING_PAY_FIRST,
                          'expected_current': None, 'reason': REASON},
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 404, response.content)

    def test_soft_deleted_restaurant_is_404(self):
        deleted = _make_restaurant('Gone House', deleted=True)
        response = self.post(
            timing_url(deleted),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_missing_target_creates_no_commercial_state_and_no_audit(self):
        deleted = _make_restaurant('Gone Too', deleted=True)
        self.post(
            timing_url(deleted),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
        )
        self.client.post(
            f'/admin/v1/restaurants/{self.UNKNOWN}/commercial/payment-timing/',
            data={'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
                  'reason': REASON},
            content_type='application/json',
        )
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_the_404_body_matches_the_admin_plane_shape(self):
        deleted = _make_restaurant('Shape House', deleted=True)
        body = self.post(
            timing_url(deleted),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
        ).json()
        self.assertEqual(body, {'status': 404, 'message': 'Restaurant not found.'})


# --- §46 audit content ---------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class AuditContentTests(_CommercialWriteTestCase):

    def test_a_changed_timing_write_records_every_field_exactly(self):
        self.seed(timing=PAYMENT_TIMING_PAY_FIRST)
        self.set_timing(PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.session_id, self.session.id)
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(
            entry.before_state, {'payment_timing': PAYMENT_TIMING_PAY_FIRST},
        )
        self.assertEqual(
            entry.after_state, {'payment_timing': PAYMENT_TIMING_PAY_AFTER},
        )

    def test_a_first_configuration_records_a_null_before_state(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.before_state, {'payment_timing': None})
        self.assertEqual(
            entry.after_state, {'payment_timing': PAYMENT_TIMING_PAY_FIRST},
        )

    def test_a_changed_collection_mode_write_records_every_field_exactly(self):
        self.seed(mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        self.set_mode(
            PAYMENT_COLLECTION_MODE_PSP_ONLINE, PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(
            entry.before_state,
            {'payment_collection_mode': PAYMENT_COLLECTION_MODE_OFFLINE},
        )
        self.assertEqual(
            entry.after_state,
            {'payment_collection_mode': PAYMENT_COLLECTION_MODE_PSP_ONLINE},
        )

    def test_the_audit_state_names_only_the_axis_that_moved(self):
        """
        One event, one fact. The other axis, the terms, the owner and the rest of
        the restaurant have no business in a record of this decision.
        """
        self.seed(timing=PAYMENT_TIMING_PAY_FIRST,
                  mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        self.set_timing(PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        self.assertEqual(set(entry.before_state), {'payment_timing'})
        self.assertEqual(set(entry.after_state), {'payment_timing'})

    def test_the_audit_carries_no_owner_pii_and_no_credential(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        blob = str([entry.before_state, entry.after_state, entry.reason]).lower()
        self.assertNotIn(self.restaurant.owner.email.lower(), blob)
        self.assertNotIn(str(self.restaurant.owner.phone_number or 'x'), blob)
        for word in ('password', 'token', 'cookie', 'session_key', 'csrf'):
            self.assertNotIn(word, blob)

    def test_the_whole_request_body_is_not_dumped_into_the_audit(self):
        self.post(
            timing_url(self.restaurant),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
            extra_field='should not be recorded anywhere',
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_SUCCESS,
        )
        blob = str([entry.before_state, entry.after_state])
        self.assertNotIn('should not be recorded anywhere', blob)
        self.assertNotIn('expected_current', blob)


# --- §47 audit failure rolls the mutation back ---------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class AuditFailureRollsBackMutationTests(_CommercialWriteTestCase):
    """
    THE atomicity proof: mutation and audit commit together or not at all.

    An administrative action that cannot be attributed must not be allowed to
    stand — which is only achievable because the audit write lives in the ADMIN
    adapter, inside an outer transaction the domain writer nests within as a
    savepoint.

    The fault is injected at ``AdminAuditLog.objects.create``, i.e. AFTER the domain
    writer has genuinely mutated the row. Raising before the domain call would prove
    nothing at all.
    """

    def _post_with_failing_audit(self, url, body):
        client = Client(raise_request_exception=False)
        client.cookies[cookie_name()] = self.raw_token
        # `assertLogs` both captures the deliberately-injected 500 — keeping the
        # suite output clean — and asserts Django really reported it, so a silently
        # swallowed audit failure would fail here rather than look like success.
        with patch(
            'platform_admin_app.audit.AdminAuditLog.objects.create',
            side_effect=RuntimeError('audit table unavailable'),
        ), self.assertLogs('django.request', level='ERROR'):
            return client.post(url, data=body, content_type='application/json')

    def setUp(self):
        super().setUp()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.raw_token = raw

    def test_a_failed_audit_undoes_the_timing_mutation(self):
        response = self._post_with_failing_audit(
            timing_url(self.restaurant),
            {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
             'reason': REASON},
        )
        self.assertEqual(response.status_code, 500)
        # The mutation the writer genuinely performed is gone again.
        self.assertIsNone(self.config())
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_failed_audit_undoes_an_update_to_an_existing_row(self):
        """The same, against a row that already existed — an UPDATE, not an INSERT."""
        stamp = timezone.now() - timedelta(days=6)
        self.seed(timing=PAYMENT_TIMING_PAY_FIRST, at=stamp)
        response = self._post_with_failing_audit(
            timing_url(self.restaurant),
            {'value': PAYMENT_TIMING_PAY_AFTER,
             'expected_current': PAYMENT_TIMING_PAY_FIRST, 'reason': REASON},
        )
        self.assertEqual(response.status_code, 500)
        config = self.config()
        self.assertEqual(config.payment_timing, PAYMENT_TIMING_PAY_FIRST)
        self.assertEqual(config.payment_timing_set_at, stamp)

    def test_a_failed_audit_undoes_the_collection_mode_mutation(self):
        response = self._post_with_failing_audit(
            mode_url(self.restaurant),
            {'value': PAYMENT_COLLECTION_MODE_PSP_ONLINE, 'expected_current': None,
             'reason': REASON},
        )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_the_domain_writer_really_ran_before_the_fault(self):
        """
        Guards the test itself: if the endpoint ever stopped calling the writer, the
        rollback assertions above would pass vacuously.
        """
        seen = {}
        real = commercial_endpoints.service_configuration.set_payment_timing

        def spy(**kwargs):
            result = real(**kwargs)
            seen['changed'] = result.changed
            seen['stored'] = RestaurantServiceConfiguration.objects.filter(
                restaurant=self.restaurant,
            ).values_list('payment_timing', flat=True).first()
            return result

        with patch.object(
            commercial_endpoints.AdminRestaurantPaymentTimingView,
            'writer', staticmethod(spy),
        ):
            self._post_with_failing_audit(
                timing_url(self.restaurant),
                {'value': PAYMENT_TIMING_PAY_FIRST, 'expected_current': None,
                 'reason': REASON},
            )
        # Mid-transaction the row existed and the writer reported a real change...
        self.assertTrue(seen['changed'])
        self.assertEqual(seen['stored'], PAYMENT_TIMING_PAY_FIRST)
        # ...and after the failed audit it is gone.
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())


# --- §48 a domain refusal composes with the outer transaction ------------------

@override_settings(**_ADMIN_OVERRIDES)
class DomainRefusalAtomicityTests(_CommercialWriteTestCase):
    """
    A refusal raised inside the writer unwinds only its savepoint.

    That the failure audit below COMMITS is the proof: had the domain exception
    poisoned the outer transaction, the subsequent audit insert would itself have
    failed and the request would have 500'd instead of answering 409.
    """

    def test_a_stale_conflict_leaves_state_untouched_and_still_audits(self):
        stamp = timezone.now() - timedelta(days=7)
        self.seed(timing=PAYMENT_TIMING_PAY_AFTER, at=stamp)

        response = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)

        self.assertEqual(response.status_code, 409, response.content)
        config = self.config()
        self.assertEqual(config.payment_timing, PAYMENT_TIMING_PAY_AFTER)
        self.assertEqual(config.payment_timing_set_at, stamp)
        self.assertAudited(
            ADMIN_RESTAURANT_PAYMENT_TIMING_SET, result=RESULT_FAILURE,
        )

    def test_a_conflict_then_a_correct_retry_both_land(self):
        """The connection is still healthy after a refusal — the next write works."""
        self.seed(timing=PAYMENT_TIMING_PAY_AFTER)
        self.assertEqual(
            self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).status_code, 409,
        )
        response = self.set_timing(
            PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_AFTER,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.config().payment_timing, PAYMENT_TIMING_PAY_FIRST)
        self.assertEqual(AdminAuditLog.objects.count(), 2)


# --- §49 read/write agreement ---------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CanonicalReadWriteAgreementTests(_CommercialWriteTestCase):
    """
    One representation for reads and successful writes.

    The token a client is handed back by a write is byte-identical to the one a GET
    would have given it, so the next edit can assert against it directly. Nothing
    display-shaped enters the round trip.
    """

    def detail(self):
        response = self.client.get(f'/admin/v1/restaurants/{self.restaurant.id}/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['commercial']

    def test_timing_round_trip(self):
        before = self.detail()
        response = self.set_timing(
            PAYMENT_TIMING_PAY_FIRST, before['payment_timing']['value'],
        )
        self.assertEqual(response.status_code, 200, response.content)
        written = response.json()['data']['commercial']
        self.assertEqual(written, self.detail())
        self.assertEqual(
            written['payment_timing']['value'], PAYMENT_TIMING_PAY_FIRST,
        )

    def test_collection_mode_round_trip(self):
        before = self.detail()
        response = self.set_mode(
            PAYMENT_COLLECTION_MODE_OFFLINE,
            before['payment_collection_mode']['value'],
        )
        self.assertEqual(response.status_code, 200, response.content)
        written = response.json()['data']['commercial']
        self.assertEqual(written, self.detail())

    def test_the_token_from_one_write_is_accepted_by_the_next(self):
        first = self.set_timing(PAYMENT_TIMING_PAY_FIRST, None).json()
        token = first['data']['commercial']['payment_timing']['value']
        second = self.set_timing(PAYMENT_TIMING_PAY_AFTER, token)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()['data']['changed'])

    def test_no_display_prose_enters_the_round_trip(self):
        written = self.set_timing(
            PAYMENT_TIMING_PAY_FIRST, None,
        ).json()['data']['commercial']
        value = written['payment_timing']['value']
        self.assertEqual(value, value.lower())
        self.assertNotIn(' ', value)
        self.assertEqual(value, PAYMENT_TIMING_PAY_FIRST)


# --- §50/§51 nothing else moves --------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class NoSideEffectTests(_CommercialWriteTestCase):
    """
    The canonical writers ignore the legacy fields completely, in both directions,
    and the three commercial facts stay independent.
    """

    def _seed_contradictory_legacy(self):
        self.restaurant.require_order_prepayments = True
        self.restaurant.preferred_subscription_method = 'monthly'
        self.restaurant.subscription_validity = True
        self.restaurant.subscription_expiry_date = (
            timezone.now() + timedelta(days=180)
        )
        self.restaurant.save(update_fields=[
            'require_order_prepayments', 'preferred_subscription_method',
            'subscription_validity', 'subscription_expiry_date',
        ])
        return {
            field: getattr(self.restaurant, field)
            for field in (
                'require_order_prepayments', 'preferred_subscription_method',
                'subscription_validity', 'subscription_expiry_date',
            )
        }

    def test_legacy_fields_are_byte_identical_after_a_timing_write(self):
        before = self._seed_contradictory_legacy()
        # Legacy says prepayment is required; the canonical decision is pay_after.
        response = self.set_timing(PAYMENT_TIMING_PAY_AFTER, None)
        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(self.restaurant, field), value, field)
        self.assertEqual(
            response.json()['data']['commercial']['payment_timing']['value'],
            PAYMENT_TIMING_PAY_AFTER,
        )

    def test_legacy_fields_are_byte_identical_after_a_collection_mode_write(self):
        before = self._seed_contradictory_legacy()
        self.assertEqual(
            self.set_mode(PAYMENT_COLLECTION_MODE_PSP_ONLINE, None).status_code, 200,
        )
        self.restaurant.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(self.restaurant, field), value, field)

    def test_no_transaction_row_is_created_by_either_write(self):
        before = DinifyTransaction.objects.count()
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertEqual(DinifyTransaction.objects.count(), before)

    def test_open_subscription_terms_are_untouched_by_either_write(self):
        terms = RestaurantSubscriptionTerms.objects.create(
            restaurant=self.restaurant, recurring_amount=Decimal('150000.00'),
            currency='UGX', billing_interval_unit='month', billing_interval_count=1,
            effective_from=timezone.now() - timedelta(days=30),
            recorded_by=self.admin,
        )
        snapshot = (
            terms.recurring_amount, terms.currency, terms.billing_interval_unit,
            terms.billing_interval_count, terms.effective_from, terms.ended_at,
            terms.recorded_at, terms.recorded_by_id,
        )

        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)

        terms.refresh_from_db()
        self.assertEqual(
            (terms.recurring_amount, terms.currency, terms.billing_interval_unit,
             terms.billing_interval_count, terms.effective_from, terms.ended_at,
             terms.recorded_at, terms.recorded_by_id),
            snapshot,
        )
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)

    def test_no_subscription_terms_row_is_created_by_either_write(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.set_mode(PAYMENT_COLLECTION_MODE_OFFLINE, None)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())
        data = self.client.get(
            f'/admin/v1/restaurants/{self.restaurant.id}/'
        ).json()['data']['commercial']
        self.assertEqual(
            data['subscription_terms'], {'configured': False, 'current': None},
        )

    def test_readiness_and_attention_are_unchanged(self):
        """
        §29. Making the facts configurable does not complete the readiness system.
        """
        onboarding = _make_restaurant('Ready House', status=RestaurantStatus_Onboarding)
        detail_url = f'/admin/v1/restaurants/{onboarding.id}/'
        before = self.client.get(detail_url).json()['data']

        self.post(
            timing_url(onboarding),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
        )
        self.post(
            mode_url(onboarding),
            value=PAYMENT_COLLECTION_MODE_OFFLINE, expected_current=None,
            reason=REASON,
        )

        after = self.client.get(detail_url).json()['data']
        self.assertEqual(before['readiness'], after['readiness'])
        self.assertEqual(
            after['readiness']['blockers'], ['readiness_not_configured'],
        )
        self.assertEqual(before['needs_attention'], after['needs_attention'])
        self.assertTrue(after['needs_attention'])

    def test_lifecycle_state_is_untouched(self):
        self.set_timing(PAYMENT_TIMING_PAY_FIRST, None)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    def test_no_lifecycle_state_forbids_a_commercial_write(self):
        """
        §33. EVERY lifecycle state, `offboarded` included — the domain deliberately
        permits correction and this adapter invents no new prohibition. Offboarding
        is when a tenant's commercial record most often needs a closing correction,
        so refusing there would leave it permanently wrong with no supported fix.
        Soft-DELETED is the one refusal, and it is a different axis.
        """
        self.assertEqual(len(RESTAURANT_LIFECYCLE_STATES), 4)
        for status in RESTAURANT_LIFECYCLE_STATES:
            with self.subTest(status=status):
                restaurant = _make_restaurant(f'State {status}', status=status)
                response = self.post(
                    timing_url(restaurant),
                    value=PAYMENT_TIMING_PAY_FIRST, expected_current=None,
                    reason=REASON,
                )
                self.assertEqual(response.status_code, 200, response.content)

    def test_a_test_tenant_has_no_special_write_semantics(self):
        test_tenant = _make_restaurant('Test Tenant', is_test=True)
        response = self.post(
            timing_url(test_tenant),
            value=PAYMENT_TIMING_PAY_FIRST, expected_current=None, reason=REASON,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            RestaurantServiceConfiguration.objects.get(
                restaurant=test_tenant,
            ).payment_timing,
            PAYMENT_TIMING_PAY_FIRST,
        )


# --- structural contract ---------------------------------------------------------

class CommercialWriteContractTests(TestCase):
    """Structural facts about the adapter itself, not about one request."""

    def test_the_endpoints_delegate_to_the_step_3c_writers(self):
        """
        Bypassing the domain service is the failure this endpoint most needs to be
        prevented from drifting into, so the binding is asserted by identity.
        """
        from commercial_app import service_configuration

        self.assertIs(
            commercial_endpoints.AdminRestaurantPaymentTimingView.writer,
            service_configuration.set_payment_timing,
        )
        self.assertIs(
            commercial_endpoints.AdminRestaurantPaymentCollectionModeView.writer,
            service_configuration.set_payment_collection_mode,
        )

    def test_the_adapter_never_writes_commercial_models_directly(self):
        """
        An AST scan, so a lazily-imported call inside a function body is caught too.
        The endpoint may READ through ``commercial_reads``; it may not save.
        """
        import ast
        import inspect

        source = inspect.getsource(commercial_endpoints)
        forbidden = {'save', 'create', 'update_or_create', 'get_or_create',
                     'bulk_create', 'update', 'delete'}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr, forbidden,
                    f'{node.func.attr}() is called in the Admin commercial adapter; '
                    f'commercial state must move only through commercial_app.',
                )

    def test_the_serializers_are_plain_and_not_model_bound(self):
        """
        A ``ModelSerializer`` over ``RestaurantServiceConfiguration`` would expose
        the attribution FKs to generic mutation machinery. Both are plain.
        """
        from rest_framework import serializers as drf

        for cls in (
            commercial_endpoints.PaymentTimingRequestSerializer,
            commercial_endpoints.PaymentCollectionModeRequestSerializer,
        ):
            with self.subTest(serializer=cls.__name__):
                self.assertTrue(issubclass(cls, drf.Serializer))
                self.assertFalse(issubclass(cls, drf.ModelSerializer))
                self.assertEqual(
                    set(cls().fields), {'value', 'expected_current', 'reason'},
                )

    def test_expected_current_is_required_and_nullable_on_both(self):
        """The missing-vs-null distinction, asserted at the field declaration."""
        for cls in (
            commercial_endpoints.PaymentTimingRequestSerializer,
            commercial_endpoints.PaymentCollectionModeRequestSerializer,
        ):
            with self.subTest(serializer=cls.__name__):
                field = cls().fields['expected_current']
                self.assertTrue(field.required)
                self.assertTrue(field.allow_null)
                self.assertFalse(cls().fields['value'].allow_null)

    def test_both_views_require_recent_elevation(self):
        from rest_framework.permissions import IsAuthenticated

        from platform_admin_app.permissions import IsRecentlyElevated

        for view in (
            commercial_endpoints.AdminRestaurantPaymentTimingView,
            commercial_endpoints.AdminRestaurantPaymentCollectionModeView,
        ):
            with self.subTest(view=view.__name__):
                self.assertEqual(
                    view.permission_classes, [IsAuthenticated, IsRecentlyElevated],
                )

    def test_the_two_routes_resolve_to_the_two_distinct_views(self):
        from django.urls import resolve

        with override_settings(ROOT_URLCONF='dinify_backend.urls_admin'):
            uid = '11111111-2222-3333-4444-555555555555'
            timing = resolve(f'/admin/v1/restaurants/{uid}/commercial/payment-timing/')
            mode = resolve(
                f'/admin/v1/restaurants/{uid}/commercial/payment-collection-mode/'
            )
        self.assertIs(
            timing.func.view_class,
            commercial_endpoints.AdminRestaurantPaymentTimingView,
        )
        self.assertIs(
            mode.func.view_class,
            commercial_endpoints.AdminRestaurantPaymentCollectionModeView,
        )

    def test_no_subscription_terms_write_route_exists_yet(self):
        """§35. Terms mutations are Step 3D.2b — no URL, no serializer, no action."""
        from django.urls import NoReverseMatch, reverse

        with override_settings(ROOT_URLCONF='dinify_backend.urls_admin'):
            for name in (
                'admin-restaurant-subscription-terms',
                'admin-restaurant-subscription-terms-end',
            ):
                with self.subTest(name=name):
                    with self.assertRaises(NoReverseMatch):
                        reverse(name, args=['x'])
