"""
The Admin subscription-terms write endpoints (Phase 1, Step 3D.2b).

Three routes over the three Step 3C domain operations. The domain's rules — the
monotonic timeline, the close-then-insert boundary, the exact-retry proofs — belong
to ``commercial_app`` and have their own suites. What is pinned HERE is that they
are reached through HTTP unchanged, and that the control-plane half around them
holds: elevation, CSRF, a substantive reason, exactly one audit row per decision,
the transaction binding mutation to audit, and — the subtlest of them — audit states
that describe what THIS request did rather than replaying a transition it merely
found already done.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commercial_app import subscription_terms
from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    PAYMENT_COLLECTION_MODE_OFFLINE,
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
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
)
from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES
from platform_admin_app.cookies import cookie_name
from platform_admin_app.endpoints import subscription_terms as terms_endpoints
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
_PHONE = iter(f'25670720{n:05d}' for n in range(1, 9999))

PASSWORD = 'correct-horse-battery'
REASON = 'Record the launch pricing agreed on the onboarding call.'
UGX = 'UGX'

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


def _make_admin(email='st-admin@t.com', username='st-admin'):
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


def record_url(restaurant):
    return (
        f'/admin/v1/restaurants/{restaurant.id}/commercial/subscription-terms/'
    )


def replace_url(restaurant):
    return (
        f'/admin/v1/restaurants/{restaurant.id}'
        f'/commercial/subscription-terms/replace/'
    )


def end_url(restaurant):
    return (
        f'/admin/v1/restaurants/{restaurant.id}/commercial/subscription-terms/end/'
    )


def ago(**kwargs):
    """An aware instant in the past, at whole-second precision."""
    return (timezone.now() - timedelta(**kwargs)).replace(microsecond=0)


def iso(moment):
    """ISO-8601 WITH the explicit offset the endpoints require."""
    return moment.isoformat()


class _TermsWriteTestCase(AuditAssertionsMixin, TestCase):
    """An authenticated, RECENTLY ELEVATED admin session pointed at one restaurant."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        self.raw_token, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = self.raw_token
        self.effective = ago(days=30)

    # --- requests ---
    def post(self, url, body):
        return self.client.post(url, data=body, content_type='application/json')

    def record_body(self, **overrides):
        body = {
            'recurring_amount': '150000.00',
            'currency': UGX,
            'billing_interval_unit': 'month',
            'billing_interval_count': 1,
            'effective_from': iso(self.effective),
            'reason': REASON,
        }
        body.update(overrides)
        return body

    def record(self, restaurant=None, **overrides):
        return self.post(
            record_url(restaurant or self.restaurant), self.record_body(**overrides),
        )

    def replace(self, expected_terms_id, restaurant=None, **overrides):
        body = self.record_body(**overrides)
        body['expected_terms_id'] = str(expected_terms_id)
        return self.post(replace_url(restaurant or self.restaurant), body)

    def end(self, expected_terms_id, ended_at, restaurant=None, reason=REASON):
        return self.post(
            end_url(restaurant or self.restaurant),
            {'expected_terms_id': str(expected_terms_id),
             'ended_at': iso(ended_at), 'reason': reason},
        )

    # --- state ---
    def all_terms(self, restaurant=None):
        return RestaurantSubscriptionTerms.objects.filter(
            restaurant=restaurant or self.restaurant,
        )

    def open_terms(self, restaurant=None):
        return self.all_terms(restaurant).filter(ended_at__isnull=True).first()

    def seed_terms(self, *, amount='150000.00', currency=UGX, unit='month', count=1,
                   effective_from=None, ended_at=None, restaurant=None, actor=None):
        return RestaurantSubscriptionTerms.objects.create(
            restaurant=restaurant or self.restaurant,
            recurring_amount=Decimal(amount), currency=currency,
            billing_interval_unit=unit, billing_interval_count=count,
            effective_from=effective_from or self.effective,
            ended_at=ended_at,
            recorded_by=actor or self.admin,
        )

    def newest_audit(self):
        return AdminAuditLog.objects.order_by('-created_at', '-id').first()

    def commercial(self, restaurant=None):
        target = restaurant or self.restaurant
        response = self.client.get(f'/admin/v1/restaurants/{target.id}/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['commercial']


# --- §60 authentication, elevation, plane isolation ---------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsAuthTests(_TermsWriteTestCase):

    def routes(self):
        terms = self.seed_terms()
        return (
            (record_url(self.restaurant), self.record_body()),
            (replace_url(self.restaurant),
             {**self.record_body(recurring_amount='200000.00'),
              'expected_terms_id': str(terms.id)}),
            (end_url(self.restaurant),
             {'expected_terms_id': str(terms.id),
              'ended_at': iso(ago(days=1)), 'reason': REASON}),
        )

    def test_anonymous_is_refused_and_manufactures_no_audit(self):
        for url, body in self.routes():
            with self.subTest(url=url):
                response = Client().post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_restaurant_user_session_is_refused(self):
        raw, _session = sessions.create_session(_make_user('st-tenant@t.com'))
        client = Client()
        client.cookies[cookie_name()] = raw
        for url, body in self.routes():
            with self.subTest(url=url):
                response = client.post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 401, response.content)

    def test_a_delegated_credential_is_refused(self):
        for url, body in self.routes():
            with self.subTest(url=url):
                response = Client().post(
                    url, data=body, content_type='application/json',
                    HTTP_X_DELEGATION_SESSION='not-a-real-token',
                )
                self.assertEqual(response.status_code, 401, response.content)

    def test_a_never_elevated_session_is_refused(self):
        raw, _session = sessions.create_session(_make_admin(
            email='st-plain@t.com', username='st-plain',
        ))
        client = Client()
        client.cookies[cookie_name()] = raw
        for url, body in self.routes():
            with self.subTest(url=url):
                response = client.post(
                    url, data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 403, response.content)

    def test_stale_elevation_is_refused_and_audited_once_per_action(self):
        expected = {
            record_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
            replace_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
            end_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
        }
        self.session.elevated_at = timezone.now() - timedelta(hours=4)
        self.session.save(update_fields=['elevated_at'])

        for url, body in self.routes():
            with self.subTest(url=url):
                before = AdminAuditLog.objects.count()
                response = self.post(url, body)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(AdminAuditLog.objects.count(), before + 1)
                entry = self.newest_audit()
                self.assertEqual(entry.action, expected[url])
                self.assertEqual(entry.result, RESULT_DENIED)
                self.assertEqual(entry.error_code, 'elevation_required')
                self.assertEqual(entry.actor_id, self.admin.id)
                self.assertIsNone(entry.after_state)

    def test_recent_elevation_is_accepted(self):
        self.assertEqual(self.record().status_code, 200)

    def test_get_is_not_allowed_on_any_of_the_three(self):
        for url, _body in self.routes():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_no_terms_route_is_on_the_delegated_allowlist(self):
        for route, _method in ALLOWED_ROUTES:
            self.assertNotIn('subscription-terms', route)


@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsCsrfTests(_TermsWriteTestCase):
    """
    The existing admin CSRF policy, not a second implementation.

    ``Client(enforce_csrf_checks=True)`` throughout — the DEFAULT test client sets
    ``_dont_enforce_csrf_checks``, which short-circuits before the check looks at
    anything, so a suite using it would prove nothing.
    """

    def _client(self):
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = self.raw_token
        return client

    def _token_for(self, client):
        response = client.get('/admin/v1/auth/session/')
        self.assertEqual(response.status_code, 200, response.content)
        return client.cookies[dj_settings.CSRF_COOKIE_NAME].value

    def test_missing_token_is_refused_on_every_route(self):
        for url in (record_url(self.restaurant), replace_url(self.restaurant),
                    end_url(self.restaurant)):
            with self.subTest(url=url):
                response = self._client().post(
                    url, data=self.record_body(), content_type='application/json',
                )
                self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_a_wrong_token_is_refused(self):
        client = self._client()
        self._token_for(client)
        response = client.post(
            record_url(self.restaurant), data=self.record_body(),
            content_type='application/json', HTTP_X_CSRFTOKEN='wrong',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_the_server_issued_token_is_accepted(self):
        client = self._client()
        token = self._token_for(client)
        response = client.post(
            record_url(self.restaurant), data=self.record_body(),
            content_type='application/json', HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_refused_csrf_request_writes_no_audit_row(self):
        self._client().post(
            record_url(self.restaurant), data=self.record_body(),
            content_type='application/json',
        )
        self.assertEqual(AdminAuditLog.objects.count(), 0)


# --- §41 record ---------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class RecordSubscriptionTermsTests(_TermsWriteTestCase):

    def test_first_terms_with_no_history(self):
        response = self.record()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertTrue(data['changed'])
        current = data['commercial']['subscription_terms']
        self.assertTrue(current['configured'])
        self.assertEqual(current['current']['recurring_amount'], '150000.00')
        self.assertEqual(current['current']['currency'], UGX)
        self.assertEqual(
            current['current']['billing_interval'], {'unit': 'month', 'count': 1},
        )
        self.assertEqual(self.all_terms().count(), 1)

    def test_reopening_after_history_was_ended(self):
        end_moment = ago(days=20)
        self.seed_terms(effective_from=ago(days=60), ended_at=end_moment)
        response = self.record(effective_from=iso(ago(days=10)))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])
        self.assertEqual(self.all_terms().count(), 2)
        self.assertEqual(self.all_terms().filter(ended_at__isnull=True).count(), 1)

    def test_back_to_back_reopening_at_the_exact_closure_is_allowed(self):
        end_moment = ago(days=20)
        self.seed_terms(effective_from=ago(days=60), ended_at=end_moment)
        response = self.record(effective_from=iso(end_moment))
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_exact_retry_is_a_no_op_preserving_provenance(self):
        first = self.record()
        self.assertTrue(first.json()['data']['changed'])
        original = self.open_terms()

        second = self.record()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()['data']['changed'])

        current = self.open_terms()
        self.assertEqual(current.id, original.id)
        self.assertEqual(current.recorded_at, original.recorded_at)
        self.assertEqual(current.recorded_by_id, original.recorded_by_id)
        self.assertEqual(self.all_terms().count(), 1)

    def test_different_open_terms_conflict(self):
        self.record()
        response = self.record(recurring_amount='200000.00')
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'subscription_terms_already_open')
        self.assertEqual(
            body['message'],
            'This restaurant already has different open subscription terms.',
        )
        self.assertEqual(self.all_terms().count(), 1)
        self.assertEqual(self.open_terms().recurring_amount, Decimal('150000.00'))

    def test_terms_beginning_before_the_previous_closure_are_refused(self):
        self.seed_terms(effective_from=ago(days=60), ended_at=ago(days=20))
        response = self.record(effective_from=iso(ago(days=40)))
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'invalid_subscription_terms')
        self.assertIn('effective_from', body['errors'])
        self.assertIsNone(self.open_terms())

    def test_future_effective_from_is_refused(self):
        future = (timezone.now() + timedelta(days=3)).replace(microsecond=0)
        response = self.record(effective_from=iso(future))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()['code'], 'future_effective_terms_not_supported',
        )
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_backdated_terms_are_ordinary(self):
        response = self.record(effective_from=iso(ago(days=400)))
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_zero_price_is_a_real_recorded_price(self):
        response = self.record(recurring_amount='0.00')
        self.assertEqual(response.status_code, 200, response.content)
        current = response.json()['data']['commercial']['subscription_terms']
        self.assertTrue(current['configured'])
        self.assertEqual(current['current']['recurring_amount'], '0.00')

    def test_every_interval_unit_round_trips(self):
        for unit in BILLING_INTERVAL_UNIT_VALUES:
            with self.subTest(unit=unit):
                restaurant = _make_restaurant(f'Unit {unit}')
                response = self.post(
                    record_url(restaurant),
                    self.record_body(billing_interval_unit=unit,
                                     billing_interval_count=2),
                )
                self.assertEqual(response.status_code, 200, response.content)
                interval = response.json()['data']['commercial'][
                    'subscription_terms']['current']['billing_interval']
                self.assertEqual(interval, {'unit': unit, 'count': 2})

    def test_the_currency_is_canonicalized_by_the_domain(self):
        """The response shows the persisted value, never an echo of the request."""
        response = self.record(currency='  ugx ')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()['data']['commercial'][
                'subscription_terms']['current']['currency'],
            UGX,
        )

    def test_an_expected_terms_id_on_record_is_ignored_not_honoured(self):
        """
        Record has no concurrency token, and a stray one must not become a silent
        requirement. Plain ``Serializer`` ignores unknown keys — the established
        Admin contract — so the field is simply not read.
        """
        response = self.post(
            record_url(self.restaurant),
            {**self.record_body(),
             'expected_terms_id': '11111111-2222-3333-4444-555555555555'},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])


# --- §42 replace ---------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class ReplaceSubscriptionTermsTests(_TermsWriteTestCase):

    def setUp(self):
        super().setUp()
        self.original = self.seed_terms(
            amount='150000.00', effective_from=ago(days=60),
        )
        self.boundary = ago(days=10)

    def test_a_real_replacement(self):
        response = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(self.boundary),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])

        self.original.refresh_from_db()
        self.assertEqual(self.original.ended_at, self.boundary)
        replacement = self.open_terms()
        self.assertNotEqual(replacement.id, self.original.id)
        self.assertEqual(replacement.effective_from, self.boundary)
        self.assertEqual(replacement.recurring_amount, Decimal('200000.00'))
        self.assertEqual(self.all_terms().count(), 2)
        self.assertEqual(self.all_terms().filter(ended_at__isnull=True).count(), 1)

    def test_the_boundary_is_continuous(self):
        """No gap in which the restaurant had no terms, no overlap in which it had two."""
        self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(self.boundary),
        )
        self.original.refresh_from_db()
        self.assertEqual(self.original.ended_at, self.open_terms().effective_from)

    def test_the_same_commercial_tuple_is_a_no_op(self):
        response = self.replace(
            self.original.id, effective_from=iso(self.original.effective_from),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])
        self.assertEqual(self.all_terms().count(), 1)

    def test_a_stale_expected_id_conflicts(self):
        self.replace(self.original.id, recurring_amount='200000.00',
                     effective_from=iso(self.boundary))
        response = self.replace(
            self.original.id, recurring_amount='300000.00',
            effective_from=iso(ago(days=5)),
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'stale_subscription_terms')
        self.assertEqual(self.open_terms().recurring_amount, Decimal('200000.00'))
        self.assertEqual(self.all_terms().count(), 2)

    def test_an_exact_retry_of_a_completed_replacement_is_a_no_op(self):
        first = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(self.boundary),
        )
        self.assertTrue(first.json()['data']['changed'])
        replacement = self.open_terms()

        second = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(self.boundary),
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()['data']['changed'])
        self.assertEqual(self.all_terms().count(), 2)
        self.assertEqual(self.open_terms().id, replacement.id)
        self.assertEqual(
            second.json()['data']['commercial'][
                'subscription_terms']['current']['id'],
            str(replacement.id),
        )

    def test_a_near_miss_retry_conflicts_rather_than_passing_as_exact(self):
        """A different boundary is not the replacement that already happened."""
        self.replace(self.original.id, recurring_amount='200000.00',
                     effective_from=iso(self.boundary))
        response = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(ago(days=9)),
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'stale_subscription_terms')
        self.assertEqual(self.all_terms().count(), 2)

    def test_a_replacement_before_the_outgoing_effective_from_is_refused(self):
        response = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(ago(days=90)),
        )
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'invalid_subscription_terms')
        self.assertIn('effective_from', body['errors'])
        self.assertEqual(self.all_terms().count(), 1)

    def test_a_future_replacement_is_refused(self):
        future = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
        response = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(future),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()['code'], 'future_effective_terms_not_supported',
        )

    def test_replacing_when_nothing_is_open_conflicts(self):
        self.original.ended_at = ago(days=5)
        self.original.save(update_fields=['ended_at'])
        response = self.replace(
            self.original.id, recurring_amount='200000.00',
            effective_from=iso(ago(days=1)),
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'no_open_subscription_terms')


# --- §43 end -------------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class EndSubscriptionTermsTests(_TermsWriteTestCase):

    def setUp(self):
        super().setUp()
        self.original = self.seed_terms(effective_from=ago(days=60))
        self.closure = ago(days=5)

    def test_a_real_end(self):
        response = self.end(self.original.id, self.closure)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])
        self.assertIsNone(self.open_terms())
        self.original.refresh_from_db()
        self.assertEqual(self.original.ended_at, self.closure)
        self.assertEqual(
            response.json()['data']['commercial']['subscription_terms'],
            {'configured': False, 'current': None},
        )

    def test_the_historical_row_is_retained_never_deleted(self):
        self.end(self.original.id, self.closure)
        self.assertEqual(self.all_terms().count(), 1)
        self.assertTrue(self.all_terms().filter(pk=self.original.pk).exists())

    def test_an_exact_retry_is_a_no_op(self):
        self.end(self.original.id, self.closure)
        response = self.end(self.original.id, self.closure)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])
        self.assertIsNone(self.open_terms())

    def test_a_retry_after_new_terms_opened_conflicts(self):
        """
        Replying "already done" would report success for a postcondition — this
        restaurant now has no open terms — that is no longer true.
        """
        self.end(self.original.id, self.closure)
        self.record(effective_from=iso(ago(days=2)))
        fresh = self.open_terms()

        response = self.end(self.original.id, self.closure)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'stale_subscription_terms')
        self.assertEqual(self.open_terms().id, fresh.id)

    def test_a_stale_terms_id_conflicts(self):
        other = self.seed_terms(
            effective_from=ago(days=200), ended_at=ago(days=150),
        )
        response = self.end(other.id, self.closure)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'stale_subscription_terms')
        self.assertIsNotNone(self.open_terms())

    def test_ending_when_nothing_is_open_conflicts(self):
        self.end(self.original.id, self.closure)
        # A second, distinct historical row so `expected` resolves but nothing is open.
        response = self.end(self.original.id, ago(days=4))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn(
            response.json()['code'],
            ('no_open_subscription_terms', 'stale_subscription_terms'),
        )

    def test_ending_before_the_terms_took_effect_is_refused(self):
        response = self.end(self.original.id, ago(days=90))
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'invalid_subscription_terms')
        self.assertIn('ended_at', body['errors'])
        self.assertIsNotNone(self.open_terms())

    def test_a_future_end_is_refused(self):
        future = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        response = self.end(self.original.id, future)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()['code'], 'future_effective_terms_not_supported',
        )
        self.assertIsNotNone(self.open_terms())

    def test_no_replacement_is_auto_created(self):
        self.end(self.original.id, self.closure)
        self.assertEqual(self.all_terms().count(), 1)
        self.assertIsNone(self.open_terms())


# --- §12 / §15 the strict input contracts --------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class StrictInputContractTests(_TermsWriteTestCase):

    def test_the_amount_must_arrive_as_a_json_string(self):
        """
        A JSON number would put a float in the middle of an otherwise exact round
        trip — and ``0.00`` versus ``0.0`` is exactly the distinction lost.
        """
        for label, value in (
            ('integer', 150000), ('float', 150000.0), ('zero', 0),
            ('boolean', True), ('null', None), ('list', ['150000.00']),
        ):
            with self.subTest(case=label):
                response = self.record(recurring_amount=value)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('recurring_amount', response.json()['errors'])
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_a_decimal_string_is_accepted_and_round_trips_exactly(self):
        for amount in ('0.00', '150000.00', '9999999999.99', '1234567.89'):
            with self.subTest(amount=amount):
                restaurant = _make_restaurant(f'Amt {amount}')
                response = self.post(
                    record_url(restaurant), self.record_body(recurring_amount=amount),
                )
                self.assertEqual(response.status_code, 200, response.content)
                body = response.content.decode()
                self.assertIn(f'"recurring_amount":"{amount}"', body)

    def test_a_datetime_without_an_explicit_offset_is_refused(self):
        """
        The server must not silently substitute its own timezone. Three hours of
        "which terms were in force" hangs on it, and the substitution would be
        invisible to an operator in another zone.
        """
        for label, value in (
            ('naive datetime', '2026-08-24T12:00:00'),
            ('date only', '2026-08-24'),
            ('space separated naive', '2026-08-24 12:00:00'),
        ):
            with self.subTest(case=label):
                response = self.record(effective_from=value)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('effective_from', response.json()['errors'])
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_explicit_offsets_are_accepted_and_mean_what_they_say(self):
        base = ago(days=30).astimezone(timezone.get_fixed_timezone(0))
        utc_form = base.isoformat().replace('+00:00', 'Z')
        offset_form = base.astimezone(timezone.get_fixed_timezone(180)).isoformat()

        first = _make_restaurant('Zulu House')
        second = _make_restaurant('Offset House')
        self.assertEqual(
            self.post(record_url(first),
                      self.record_body(effective_from=utc_form)).status_code,
            200,
        )
        self.assertEqual(
            self.post(record_url(second),
                      self.record_body(effective_from=offset_form)).status_code,
            200,
        )
        # The SAME instant, written two ways.
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.get(
                restaurant=first).effective_from,
            RestaurantSubscriptionTerms.objects.get(
                restaurant=second).effective_from,
        )

    def test_a_malformed_datetime_is_refused(self):
        for value in ('not-a-date', '2026-13-45T12:00:00Z', 12345, None, True):
            with self.subTest(value=value):
                response = self.record(effective_from=value)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('effective_from', response.json()['errors'])

    def test_the_same_timezone_rule_applies_to_ended_at(self):
        terms = self.seed_terms(effective_from=ago(days=60))
        response = self.post(
            end_url(self.restaurant),
            {'expected_terms_id': str(terms.id),
             'ended_at': '2026-08-24T12:00:00', 'reason': REASON},
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('ended_at', response.json()['errors'])
        self.assertIsNotNone(self.open_terms())


# --- §57 / §58 validation matrix -----------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsValidationTests(_TermsWriteTestCase):

    def _assert_rejected(self, url, body, field, *, action):
        before = AdminAuditLog.objects.count()
        response = self.post(url, body)
        self.assertEqual(response.status_code, 400, response.content)
        payload = response.json()
        self.assertIn(field, payload['errors'])
        self.assertEqual(payload['status'], 400)
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        entry = self.newest_audit()
        self.assertEqual(entry.action, action)
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertIsNone(entry.after_state)

    def test_record_and_replace_body_validation(self):
        terms = self.seed_terms()
        cases = (
            ('missing amount', {'recurring_amount': None}, 'recurring_amount'),
            ('malformed decimal', {'recurring_amount': 'abc'}, 'recurring_amount'),
            ('negative', {'recurring_amount': '-1.00'}, 'recurring_amount'),
            ('excess precision', {'recurring_amount': '1.005'}, 'recurring_amount'),
            ('oversized', {'recurring_amount': '1e100'}, 'recurring_amount'),
            ('missing currency', {'currency': None}, 'currency'),
            ('malformed currency', {'currency': 'U1X'}, 'currency'),
            ('two-letter currency', {'currency': 'UG'}, 'currency'),
            ('invalid unit', {'billing_interval_unit': 'monthly'},
             'billing_interval_unit'),
            ('per_order unit', {'billing_interval_unit': 'per_order'},
             'billing_interval_unit'),
            ('zero count', {'billing_interval_count': 0},
             'billing_interval_count'),
            ('negative count', {'billing_interval_count': -3},
             'billing_interval_count'),
            ('oversized count', {'billing_interval_count': 2 ** 40},
             'billing_interval_count'),
            ('missing reason', {'reason': None}, 'reason'),
            ('blank reason', {'reason': ''}, 'reason'),
            ('short reason', {'reason': 'no'}, 'reason'),
        )
        def apply(body, override):
            # A ``None`` value means OMIT the key entirely — the "missing field"
            # case. Every other value is sent verbatim, malformed on purpose.
            for key, value in override.items():
                if value is None:
                    body.pop(key, None)
                else:
                    body[key] = value
            return body

        for label, override, field in cases:
            with self.subTest(op='record', case=label):
                self._assert_rejected(
                    record_url(self.restaurant),
                    apply(self.record_body(), override),
                    field,
                    action=ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
                )
            with self.subTest(op='replace', case=label):
                body = self.record_body()
                body['expected_terms_id'] = str(terms.id)
                self._assert_rejected(
                    replace_url(self.restaurant),
                    apply(body, override),
                    field,
                    action=ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
                )

    def test_replace_requires_a_well_formed_expected_terms_id(self):
        for label, override in (
            ('missing', {}),
            ('null', {'expected_terms_id': None}),
            ('malformed', {'expected_terms_id': 'not-a-uuid'}),
            ('numeric', {'expected_terms_id': 42}),
        ):
            with self.subTest(case=label):
                body = self.record_body()
                body.update(override)
                self._assert_rejected(
                    replace_url(self.restaurant), body, 'expected_terms_id',
                    action=ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
                )

    def test_a_numeric_token_is_a_400_and_never_a_manufactured_409(self):
        """
        A JSON number is a MALFORMED token, not a stale one.

        DRF's own ``UUIDField`` evaluates ``uuid.UUID(int=42)`` for a numeric
        body value, producing a well-formed UUID no row has ever carried. The
        request would then reach the domain, miss, and come back 409 "terms
        changed since they were loaded" — telling the operator the world moved
        when in fact their body was wrong. A conflict is the one error here that
        means something specific, so it must never be manufactured by coercion.
        """
        terms = self.seed_terms()
        for label, url, body in (
            ('replace', replace_url(self.restaurant),
             dict(self.record_body(), expected_terms_id=42)),
            ('end', end_url(self.restaurant),
             {'expected_terms_id': 42, 'ended_at': iso(ago(days=1)),
              'reason': REASON}),
        ):
            with self.subTest(op=label):
                response = self.post(url, body)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('expected_terms_id', response.json()['errors'])
                self.assertNotIn(
                    'subscription_terms', response.json().get('code', ''),
                )
        terms.refresh_from_db()
        self.assertIsNone(terms.ended_at, 'a malformed token moved terms')
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(
                restaurant=self.restaurant).count(),
            1,
        )

    def test_end_body_validation(self):
        terms = self.seed_terms()
        base = {'expected_terms_id': str(terms.id),
                'ended_at': iso(ago(days=1)), 'reason': REASON}
        for label, override, field in (
            ('missing id', {'expected_terms_id': None}, 'expected_terms_id'),
            ('malformed id', {'expected_terms_id': 'nope'}, 'expected_terms_id'),
            ('missing ended_at', {'ended_at': None}, 'ended_at'),
            ('malformed ended_at', {'ended_at': 'soon'}, 'ended_at'),
            ('missing reason', {'reason': None}, 'reason'),
            ('short reason', {'reason': 'x'}, 'reason'),
        ):
            with self.subTest(case=label):
                body = dict(base)
                if override[field] is None:
                    body.pop(field)
                else:
                    body.update(override)
                self._assert_rejected(
                    end_url(self.restaurant), body, field,
                    action=ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
                )

    def test_an_empty_body_names_every_required_field(self):
        response = self.post(record_url(self.restaurant), {})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            set(response.json()['errors']),
            {'recurring_amount', 'currency', 'billing_interval_unit',
             'billing_interval_count', 'effective_from', 'reason'},
        )

    # --- §58: the reason recorded on a rejected terms request ------------------

    def test_a_valid_padded_reason_survives_another_field_failing(self):
        response = self.record(
            recurring_amount='abc', reason=f'   {REASON}   ',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.newest_audit().reason, REASON)

    def test_a_rejected_reason_is_never_stored_on_any_terms_route(self):
        terms = self.seed_terms()
        targets = (
            (record_url(self.restaurant), self.record_body()),
            (replace_url(self.restaurant),
             {**self.record_body(), 'expected_terms_id': str(terms.id)}),
            (end_url(self.restaurant),
             {'expected_terms_id': str(terms.id), 'ended_at': iso(ago(days=1))}),
        )
        for url, base in targets:
            for label, reason in (
                ('too short', 'tiny'), ('blank', ''), ('whitespace', '     '),
            ):
                with self.subTest(url=url, case=label):
                    body = dict(base)
                    body['reason'] = reason
                    self.post(url, body)
                    self.assertEqual(self.newest_audit().reason, '')
            with self.subTest(url=url, case='missing'):
                body = dict(base)
                body.pop('reason', None)
                self.post(url, body)
                self.assertEqual(self.newest_audit().reason, '')


# --- §36 malformed JSON ---------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsMalformedBodyTests(_TermsWriteTestCase):
    """The PR #300 lesson, applied to the new routes from the start."""

    BROKEN = '{"recurring_amount": "150000.00", '

    def urls(self):
        return (record_url(self.restaurant), replace_url(self.restaurant),
                end_url(self.restaurant))

    def test_unparseable_json_is_a_400_in_the_house_envelope(self):
        for url in self.urls():
            with self.subTest(url=url):
                response = self.client.post(
                    url, data=self.BROKEN, content_type='application/json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                payload = response.json()
                self.assertEqual(payload['status'], 400)
                self.assertIn('__all__', payload['errors'])

    def test_unparseable_json_is_audited_once_with_no_reason(self):
        expected = {
            record_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
            replace_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
            end_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
        }
        for url in self.urls():
            with self.subTest(url=url):
                before = AdminAuditLog.objects.count()
                self.client.post(
                    url, data=self.BROKEN, content_type='application/json',
                )
                self.assertEqual(AdminAuditLog.objects.count(), before + 1)
                entry = self.newest_audit()
                self.assertEqual(entry.action, expected[url])
                self.assertEqual(entry.result, RESULT_FAILURE)
                self.assertEqual(entry.error_code, 'malformed_body')
                self.assertEqual(entry.reason, '')
                self.assertIsNone(entry.after_state)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_an_unsupported_content_type_is_a_415_in_the_house_envelope(self):
        """
        A body DRF has no parser for raises ``UnsupportedMediaType``, NOT
        ``ParseError`` — a separate exception that bypassed the parse guard
        entirely, so DRF answered with its own bare ``{"detail": ...}``.

        It stays a **415**, not a 400: the caller's remedy is to send JSON, and
        collapsing it into "the body was wrong" would hide the one clue that
        says so. Only the envelope and the audit were missing.
        """
        for url in self.urls():
            for content_type in ('text/plain', 'application/xml'):
                with self.subTest(url=url, content_type=content_type):
                    response = self.client.post(
                        url, data='reason=this-is-not-json',
                        content_type=content_type,
                    )
                    self.assertEqual(response.status_code, 415, response.content)
                    payload = response.json()
                    self.assertEqual(payload['status'], 415)
                    self.assertIn('__all__', payload['errors'])
                    self.assertNotIn('detail', payload)

    def test_an_unsupported_content_type_is_audited_once_with_no_reason(self):
        """
        The whole point of the parse guard: an elevated administrator's unsafe
        request must not be absent from the control-plane log purely because the
        server could not read it. A reason cannot be recovered from a body no
        parser will touch, so none is invented.
        """
        expected = {
            record_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
            replace_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
            end_url(self.restaurant): ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
        }
        for url in self.urls():
            with self.subTest(url=url):
                before = AdminAuditLog.objects.count()
                self.client.post(
                    url, data='reason=this-is-not-json', content_type='text/plain',
                )
                self.assertEqual(AdminAuditLog.objects.count(), before + 1)
                entry = self.newest_audit()
                self.assertEqual(entry.action, expected[url])
                self.assertEqual(entry.result, RESULT_FAILURE)
                self.assertEqual(entry.error_code, 'unsupported_media_type')
                self.assertEqual(entry.reason, '')
                self.assertIsNone(entry.after_state)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_an_unsupported_content_type_against_a_missing_target_is_a_404(self):
        """
        Target resolution still comes FIRST. A caller who cannot name a real
        restaurant learns nothing about which media types the route accepts, and
        nothing is audited — no tenant was touched.
        """
        deleted = _make_restaurant('Gone House', deleted=True)
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            record_url(deleted), data='reason=this-is-not-json',
            content_type='text/plain',
        )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_an_unelevated_session_is_still_refused_before_the_media_type(self):
        """
        Authorization outranks readability. DRF checks permissions in ``initial()``,
        before the handler touches ``request.data``, so this is a 403 with the
        elevation denial audited — not a 415 that would tell an unelevated caller
        which media types the route accepts.
        """
        self.session.elevated_at = None
        self.session.save(update_fields=['elevated_at'])
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            record_url(self.restaurant), data='reason=this-is-not-json',
            content_type='text/plain',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), before + 1)
        self.assertEqual(self.newest_audit().result, RESULT_DENIED)

    def test_a_malformed_body_against_a_missing_target_is_a_silent_404(self):
        deleted = _make_restaurant('Gone House', deleted=True)
        before = AdminAuditLog.objects.count()
        response = self.client.post(
            record_url(deleted), data=self.BROKEN,
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), before)


# --- §45 target resolution ------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsTargetTests(_TermsWriteTestCase):

    UNKNOWN = '11111111-2222-3333-4444-555555555555'

    def test_unknown_and_soft_deleted_targets_are_silent_404s(self):
        deleted = _make_restaurant('Deleted House', deleted=True)
        paths = ('subscription-terms/', 'subscription-terms/replace/',
                 'subscription-terms/end/')
        before = AdminAuditLog.objects.count()
        for path in paths:
            for target in (self.UNKNOWN, str(deleted.id)):
                with self.subTest(path=path, target=target):
                    response = self.client.post(
                        f'/admin/v1/restaurants/{target}/commercial/{path}',
                        data=self.record_body(), content_type='application/json',
                    )
                    self.assertEqual(response.status_code, 404, response.content)
                    self.assertEqual(
                        response.json(),
                        {'status': 404, 'message': 'Restaurant not found.'},
                    )
        self.assertEqual(AdminAuditLog.objects.count(), before)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())


# --- §40 cross-tenant safety ----------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CrossTenantTermsIdTests(_TermsWriteTestCase):
    """
    A terms UUID belonging to another restaurant must disclose nothing.

    The strongest form of the assertion: the answer to a cross-tenant id and the
    answer to a UUID that never existed are BYTE-IDENTICAL, so the response cannot be
    used to probe for other tenants' terms.
    """

    def setUp(self):
        super().setUp()
        self.other = _make_restaurant('Other House')
        self.other_terms = self.seed_terms(
            restaurant=self.other, amount='777777.00', currency='KES',
            effective_from=ago(days=40),
        )
        self.mine = self.seed_terms(effective_from=ago(days=60))

    def test_replace_with_another_tenants_terms_id(self):
        cross = self.replace(
            self.other_terms.id, recurring_amount='200000.00',
            effective_from=iso(ago(days=5)),
        )
        unknown = self.replace(
            '11111111-2222-3333-4444-555555555555',
            recurring_amount='200000.00', effective_from=iso(ago(days=5)),
        )
        self.assertEqual(cross.status_code, 409, cross.content)
        self.assertEqual(cross.json(), unknown.json())
        blob = json.dumps(cross.json())
        for leak in (str(self.other.id), str(self.other_terms.id), '777777',
                     'KES', 'Other House'):
            self.assertNotIn(leak, blob)

    def test_end_with_another_tenants_terms_id(self):
        cross = self.end(self.other_terms.id, ago(days=5))
        unknown = self.end('11111111-2222-3333-4444-555555555555', ago(days=5))
        self.assertEqual(cross.status_code, 409, cross.content)
        self.assertEqual(cross.json(), unknown.json())

    def test_the_other_tenant_is_untouched(self):
        self.replace(self.other_terms.id, recurring_amount='200000.00',
                     effective_from=iso(ago(days=5)))
        self.end(self.other_terms.id, ago(days=5))
        self.other_terms.refresh_from_db()
        self.assertIsNone(self.other_terms.ended_at)
        self.assertEqual(self.other_terms.recurring_amount, Decimal('777777.00'))
        self.assertEqual(self.all_terms(self.other).count(), 1)

    def test_the_failure_audit_carries_only_this_restaurants_state(self):
        self.replace(self.other_terms.id, recurring_amount='200000.00',
                     effective_from=iso(ago(days=5)))
        entry = self.newest_audit()
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        blob = json.dumps([entry.before_state, entry.after_state], default=str)
        self.assertNotIn(str(self.other_terms.id), blob)
        self.assertNotIn('KES', blob)
        self.assertNotIn('777777', blob)
        # It DOES carry this restaurant's own open terms.
        self.assertEqual(
            entry.before_state['subscription_terms']['id'], str(self.mine.id),
        )


# --- §27–§34 / §59 audit content ------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsAuditContentTests(_TermsWriteTestCase):

    SNAPSHOT_KEYS = {
        'id', 'recurring_amount', 'currency', 'billing_interval', 'effective_from',
    }

    def test_a_record_moves_null_to_the_new_terms(self):
        self.record()
        terms = self.open_terms()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.session_id, self.session.id)
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(entry.before_state, {'subscription_terms': None})
        snapshot = entry.after_state['subscription_terms']
        self.assertEqual(set(snapshot), self.SNAPSHOT_KEYS)
        self.assertEqual(snapshot['id'], str(terms.id))
        self.assertEqual(snapshot['recurring_amount'], '150000.00')
        self.assertEqual(snapshot['currency'], UGX)
        self.assertEqual(
            snapshot['billing_interval'], {'unit': 'month', 'count': 1},
        )

    def test_a_record_retry_reports_equal_current_states(self):
        self.record()
        before_count = AdminAuditLog.objects.count()
        self.record()
        self.assertEqual(AdminAuditLog.objects.count(), before_count + 1)
        entry = self.newest_audit()
        self.assertEqual(entry.result, RESULT_SUCCESS)
        self.assertEqual(entry.before_state, entry.after_state)
        self.assertEqual(
            entry.before_state['subscription_terms']['id'],
            str(self.open_terms().id),
        )

    def test_a_replacement_moves_outgoing_to_replacement(self):
        original = self.seed_terms(effective_from=ago(days=60))
        boundary = ago(days=10)
        self.replace(original.id, recurring_amount='200000.00',
                     effective_from=iso(boundary))
        replacement = self.open_terms()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED, result=RESULT_SUCCESS,
        )
        self.assertEqual(
            entry.before_state['subscription_terms']['id'], str(original.id),
        )
        self.assertEqual(
            entry.before_state['subscription_terms']['recurring_amount'],
            '150000.00',
        )
        self.assertEqual(
            entry.after_state['subscription_terms']['id'], str(replacement.id),
        )
        # The outgoing snapshot describes the TERMS, not the closure that ended them.
        self.assertNotIn('ended_at', entry.before_state['subscription_terms'])

    def test_a_replacement_retry_does_not_replay_the_historical_transition(self):
        """
        THE trap. The domain returns ``previous_terms`` on an exact retry as PROOF
        that the replacement already happened. Using it as this request's before-state
        would write the old -> new transition into the log a second time, as though it
        had occurred twice. This request moved nothing.
        """
        original = self.seed_terms(effective_from=ago(days=60))
        boundary = ago(days=10)
        self.replace(original.id, recurring_amount='200000.00',
                     effective_from=iso(boundary))
        replacement = self.open_terms()

        self.replace(original.id, recurring_amount='200000.00',
                     effective_from=iso(boundary))

        entry = self.newest_audit()
        self.assertEqual(entry.result, RESULT_SUCCESS)
        self.assertEqual(entry.before_state, entry.after_state)
        self.assertEqual(
            entry.before_state['subscription_terms']['id'], str(replacement.id),
        )
        # Emphatically NOT the superseded row.
        self.assertNotEqual(
            entry.before_state['subscription_terms']['id'], str(original.id),
        )

    def test_an_end_moves_the_terms_to_none(self):
        original = self.seed_terms(effective_from=ago(days=60))
        self.end(original.id, ago(days=5))
        entry = self.assertAudited(
            ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED, result=RESULT_SUCCESS,
        )
        self.assertEqual(
            entry.before_state['subscription_terms']['id'], str(original.id),
        )
        self.assertEqual(entry.after_state, {'subscription_terms': None})
        # No status word invented for the after-state.
        blob = json.dumps(entry.after_state)
        for word in ('expired', 'inactive', 'cancelled', 'unpaid'):
            self.assertNotIn(word, blob)

    def test_an_end_retry_does_not_replay_the_historical_transition(self):
        original = self.seed_terms(effective_from=ago(days=60))
        closure = ago(days=5)
        self.end(original.id, closure)
        self.end(original.id, closure)
        entry = self.newest_audit()
        self.assertEqual(entry.result, RESULT_SUCCESS)
        self.assertEqual(entry.before_state, {'subscription_terms': None})
        self.assertEqual(entry.after_state, {'subscription_terms': None})

    def test_a_conflict_records_current_state_and_no_after_state(self):
        self.record()
        current = self.open_terms()
        self.record(recurring_amount='200000.00')
        entry = self.newest_audit()
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'subscription_terms_already_open')
        self.assertEqual(
            entry.before_state['subscription_terms']['id'], str(current.id),
        )
        self.assertIsNone(entry.after_state)

    def test_the_audit_carries_no_pii_credential_or_payment_vocabulary(self):
        self.record()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED, result=RESULT_SUCCESS,
        )
        blob = json.dumps(
            [entry.before_state, entry.after_state, entry.reason], default=str,
        ).lower()
        self.assertNotIn(self.restaurant.owner.email.lower(), blob)
        self.assertNotIn('recorded_by', blob)
        self.assertNotIn('recorded_at', blob)
        for word in ('password', 'token', 'cookie', 'csrf', 'session_key',
                     'paid', 'invoice', 'good_standing', 'active', 'psp',
                     'transaction', 'agreed_by', 'accepted_by', 'signed_by'):
            self.assertNotIn(word, blob)

    def test_the_whole_request_body_is_not_dumped_into_the_audit(self):
        self.post(
            record_url(self.restaurant),
            {**self.record_body(), 'note': 'should never be recorded'},
        )
        entry = self.newest_audit()
        blob = json.dumps([entry.before_state, entry.after_state], default=str)
        self.assertNotIn('should never be recorded', blob)


# --- §53–§55 audit failure rolls the mutation back ------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsAuditRollbackTests(_TermsWriteTestCase):
    """
    Mutation and audit commit together or not at all — proved separately for all
    three operations, because their rollback failure modes differ: a record loses an
    INSERT, an end must un-set a stamp, and a replacement must both un-set a stamp
    AND lose an insert while leaving exactly one open row.
    """

    def _post_with_failing_audit(self, url, body):
        client = Client(raise_request_exception=False)
        client.cookies[cookie_name()] = self.raw_token
        with patch(
            'platform_admin_app.audit.AdminAuditLog.objects.create',
            side_effect=RuntimeError('audit table unavailable'),
        ), self.assertLogs('django.request', level='ERROR'):
            return client.post(url, data=body, content_type='application/json')

    def _spy(self, name, sink):
        """Capture mid-transaction state, proving the writer genuinely ran."""
        real = getattr(subscription_terms, name)

        def spy(**kwargs):
            result = real(**kwargs)
            rows = RestaurantSubscriptionTerms.objects.filter(
                restaurant_id=kwargs['restaurant_id'],
            )
            sink['changed'] = result.changed
            sink['total'] = rows.count()
            sink['open'] = list(
                rows.filter(ended_at__isnull=True).values_list('id', flat=True)
            )
            return result

        return patch.object(subscription_terms, name, spy)

    def test_a_failed_audit_undoes_a_record(self):
        seen = {}
        with self._spy('record_subscription_terms', seen):
            response = self._post_with_failing_audit(
                record_url(self.restaurant), self.record_body(),
            )
        self.assertEqual(response.status_code, 500)
        self.assertTrue(seen['changed'])
        self.assertEqual(seen['total'], 1)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_failed_audit_undoes_a_replacement_completely(self):
        original = self.seed_terms(effective_from=ago(days=60))
        boundary = ago(days=10)
        body = {**self.record_body(recurring_amount='200000.00',
                                   effective_from=iso(boundary)),
                'expected_terms_id': str(original.id)}

        seen = {}
        with self._spy('replace_subscription_terms', seen):
            response = self._post_with_failing_audit(
                replace_url(self.restaurant), body,
            )
        self.assertEqual(response.status_code, 500)
        # Mid-transaction the replacement genuinely existed...
        self.assertTrue(seen['changed'])
        self.assertEqual(seen['total'], 2)
        self.assertEqual(len(seen['open']), 1)
        self.assertNotIn(original.id, seen['open'])

        # ...and afterwards the outgoing row is OPEN again and the successor is gone.
        original.refresh_from_db()
        self.assertIsNone(original.ended_at)
        self.assertEqual(self.all_terms().count(), 1)
        self.assertEqual(self.open_terms().id, original.id)

    def test_a_failed_audit_undoes_an_end(self):
        original = self.seed_terms(effective_from=ago(days=60))
        closure = ago(days=5)
        body = {'expected_terms_id': str(original.id), 'ended_at': iso(closure),
                'reason': REASON}

        seen = {}
        with self._spy('end_subscription_terms', seen):
            response = self._post_with_failing_audit(end_url(self.restaurant), body)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(seen['changed'])
        self.assertEqual(seen['open'], [])

        original.refresh_from_db()
        self.assertIsNone(original.ended_at)
        self.assertIsNotNone(self.open_terms())


# --- §56 a domain refusal composes with the outer transaction -------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsDomainRefusalAtomicityTests(_TermsWriteTestCase):
    """
    A refusal raised inside a writer unwinds only its savepoint.

    That the failure audit COMMITS is the proof: had the domain exception poisoned
    the outer transaction, the subsequent audit insert would itself have failed and
    the request would have 500'd instead of answering 409.
    """

    def test_an_already_open_conflict_audits_and_leaves_the_connection_healthy(self):
        self.record()
        original = self.open_terms()

        conflict = self.record(recurring_amount='200000.00')
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertAudited(
            ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED, result=RESULT_FAILURE,
        )

        # A valid follow-up request still works on the same connection.
        follow_up = self.replace(
            original.id, recurring_amount='200000.00',
            effective_from=iso(ago(days=1)),
        )
        self.assertEqual(follow_up.status_code, 200, follow_up.content)
        self.assertEqual(self.open_terms().recurring_amount, Decimal('200000.00'))

    def test_a_stale_replacement_audits_and_leaves_state_untouched(self):
        original = self.seed_terms(effective_from=ago(days=60))
        self.replace(original.id, recurring_amount='200000.00',
                     effective_from=iso(ago(days=10)))
        settled = self.open_terms()

        stale = self.replace(original.id, recurring_amount='300000.00',
                             effective_from=iso(ago(days=5)))
        self.assertEqual(stale.status_code, 409, stale.content)
        entry = self.newest_audit()
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'stale_subscription_terms')
        self.assertEqual(self.open_terms().id, settled.id)
        self.assertEqual(self.all_terms().count(), 2)


# --- §45 canonical read/write agreement -----------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsCanonicalAgreementTests(_TermsWriteTestCase):

    def test_record_then_detail_agree_and_hand_over_a_usable_token(self):
        written = self.record().json()['data']['commercial']
        self.assertEqual(written, self.commercial())
        token = written['subscription_terms']['current']['id']

        replaced = self.replace(
            token, recurring_amount='200000.00', effective_from=iso(ago(days=5)),
        )
        self.assertEqual(replaced.status_code, 200, replaced.content)
        self.assertTrue(replaced.json()['data']['changed'])

    def test_replace_then_detail_agree_and_the_new_token_ends_the_terms(self):
        self.record()
        first_token = self.commercial()['subscription_terms']['current']['id']
        written = self.replace(
            first_token, recurring_amount='200000.00',
            effective_from=iso(ago(days=5)),
        ).json()['data']['commercial']
        self.assertEqual(written, self.commercial())

        new_token = written['subscription_terms']['current']['id']
        ended = self.end(new_token, ago(days=1))
        self.assertEqual(ended.status_code, 200, ended.content)

    def test_end_leaves_the_canonical_object_unconfigured_and_agreeing(self):
        self.record()
        token = self.commercial()['subscription_terms']['current']['id']
        written = self.end(token, ago(days=1)).json()['data']['commercial']
        self.assertEqual(
            written['subscription_terms'], {'configured': False, 'current': None},
        )
        self.assertEqual(written, self.commercial())

    def test_the_write_response_omits_the_legacy_compatibility_fields(self):
        payload = self.record().json()
        for key in ('payment_mode', 'payment_mode_configured', 'subscription'):
            self.assertNotIn(key, payload['data'])
            self.assertNotIn(key, payload['data']['commercial'])

    def test_no_display_prose_enters_the_round_trip(self):
        current = self.record().json()['data'][
            'commercial']['subscription_terms']['current']
        self.assertEqual(current['currency'], UGX)
        self.assertEqual(current['billing_interval']['unit'], 'month')
        blob = json.dumps(current).lower()
        for word in ('monthly plan', 'annual', 'free tier', 'per month'):
            self.assertNotIn(word, blob)


# --- §46–§51 nothing else moves --------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class SubscriptionTermsNoSideEffectTests(_TermsWriteTestCase):

    def _seed_contradictory_legacy(self):
        self.restaurant.flat_fee = Decimal('500000.00')
        self.restaurant.preferred_subscription_method = 'monthly'
        self.restaurant.subscription_validity = True
        self.restaurant.subscription_expiry_date = (
            timezone.now() + timedelta(days=200)
        )
        self.restaurant.require_order_prepayments = True
        self.restaurant.save(update_fields=[
            'flat_fee', 'preferred_subscription_method', 'subscription_validity',
            'subscription_expiry_date', 'require_order_prepayments',
        ])
        return {
            field: getattr(self.restaurant, field)
            for field in ('flat_fee', 'preferred_subscription_method',
                          'subscription_validity', 'subscription_expiry_date',
                          'require_order_prepayments')
        }

    def test_legacy_subscription_fields_survive_all_three_operations(self):
        before = self._seed_contradictory_legacy()
        self.record()
        token = self.open_terms().id
        self.replace(token, recurring_amount='200000.00',
                     effective_from=iso(ago(days=5)))
        self.end(self.open_terms().id, ago(days=1))

        self.restaurant.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(self.restaurant, field), value, field)

    def test_no_transaction_row_is_created_by_any_operation(self):
        before = DinifyTransaction.objects.count()
        self.record()
        self.replace(self.open_terms().id, recurring_amount='200000.00',
                     effective_from=iso(ago(days=5)))
        self.end(self.open_terms().id, ago(days=1))
        self.assertEqual(DinifyTransaction.objects.count(), before)

    def test_the_service_configuration_axes_are_untouched(self):
        stamp = ago(days=3)
        config = RestaurantServiceConfiguration.objects.create(
            restaurant=self.restaurant,
            payment_timing=PAYMENT_TIMING_PAY_FIRST,
            payment_timing_set_at=stamp, payment_timing_set_by=self.admin,
            payment_collection_mode=PAYMENT_COLLECTION_MODE_OFFLINE,
            payment_collection_mode_set_at=stamp,
            payment_collection_mode_set_by=self.admin,
        )
        snapshot = (
            config.payment_timing, config.payment_timing_set_at,
            config.payment_timing_set_by_id, config.payment_collection_mode,
            config.payment_collection_mode_set_at,
            config.payment_collection_mode_set_by_id,
        )

        self.record()
        self.replace(self.open_terms().id, recurring_amount='200000.00',
                     effective_from=iso(ago(days=5)))
        self.end(self.open_terms().id, ago(days=1))

        config.refresh_from_db()
        self.assertEqual(
            (config.payment_timing, config.payment_timing_set_at,
             config.payment_timing_set_by_id, config.payment_collection_mode,
             config.payment_collection_mode_set_at,
             config.payment_collection_mode_set_by_id),
            snapshot,
        )
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)

    def test_no_service_configuration_row_is_created_by_a_terms_write(self):
        self.record()
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        commercial = self.commercial()
        self.assertFalse(commercial['payment_timing']['configured'])
        self.assertFalse(commercial['payment_collection_mode']['configured'])

    def test_readiness_and_attention_are_unchanged(self):
        onboarding = _make_restaurant('Ready House', status=RestaurantStatus_Onboarding)
        detail_url = f'/admin/v1/restaurants/{onboarding.id}/'
        before = self.client.get(detail_url).json()['data']

        self.post(record_url(onboarding), self.record_body())

        after = self.client.get(detail_url).json()['data']
        self.assertEqual(before['readiness'], after['readiness'])
        self.assertEqual(
            after['readiness']['blockers'], ['readiness_not_configured'],
        )
        self.assertEqual(before['needs_attention'], after['needs_attention'])

    def test_lifecycle_state_is_untouched_and_ungated(self):
        """
        EVERY lifecycle state, `offboarded` included.

        Offboarding is precisely when a tenant's terms most often need a closing
        correction, so refusing the write there would leave the record permanently
        wrong with no supported way to fix it. Soft-DELETED is the one refusal, and
        it is a different axis (see the 404 tests). The write never moves the
        lifecycle either — that is `transition_restaurant`'s exclusive job.
        """
        self.assertEqual(len(RESTAURANT_LIFECYCLE_STATES), 4)
        for status in RESTAURANT_LIFECYCLE_STATES:
            with self.subTest(status=status):
                restaurant = _make_restaurant(f'State {status}', status=status)
                response = self.post(record_url(restaurant), self.record_body())
                self.assertEqual(response.status_code, 200, response.content)
                restaurant.refresh_from_db()
                self.assertEqual(restaurant.status, status)
                self.assertEqual(
                    RestaurantSubscriptionTerms.objects.filter(
                        restaurant=restaurant, ended_at__isnull=True).count(),
                    1,
                )

    def test_a_test_tenant_has_no_special_semantics(self):
        tenant = _make_restaurant('Test Tenant', is_test=True)
        response = self.post(
            record_url(tenant), self.record_body(recurring_amount='0.00'),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.get(
                restaurant=tenant).recurring_amount,
            Decimal('0.00'),
        )


# --- §61 structural contract -----------------------------------------------------

class SubscriptionTermsContractTests(TestCase):
    """Structural facts about the adapter, not about one request."""

    VIEWS = None

    def setUp(self):
        super().setUp()
        self.VIEWS = (
            terms_endpoints.AdminRestaurantRecordSubscriptionTermsView,
            terms_endpoints.AdminRestaurantReplaceSubscriptionTermsView,
            terms_endpoints.AdminRestaurantEndSubscriptionTermsView,
        )

    def test_the_three_routes_resolve_to_the_three_distinct_views(self):
        from django.urls import resolve

        uid = '11111111-2222-3333-4444-555555555555'
        base = f'/admin/v1/restaurants/{uid}/commercial/subscription-terms/'
        with override_settings(ROOT_URLCONF='dinify_backend.urls_admin'):
            matches = [
                resolve(base).func.view_class,
                resolve(base + 'replace/').func.view_class,
                resolve(base + 'end/').func.view_class,
            ]
        self.assertEqual(matches, list(self.VIEWS))

    def test_there_is_no_parameterised_action_route(self):
        from django.urls import Resolver404, resolve

        uid = '11111111-2222-3333-4444-555555555555'
        with override_settings(ROOT_URLCONF='dinify_backend.urls_admin'):
            for path in ('archive/', 'delete/', 'anything/'):
                with self.subTest(path=path):
                    with self.assertRaises(Resolver404):
                        resolve(
                            f'/admin/v1/restaurants/{uid}'
                            f'/commercial/subscription-terms/{path}'
                        )

    def test_each_view_delegates_to_its_step_3c_writer(self):
        """
        An AST scan of the module: the adapter may READ terms for a failure
        before-state, but every MUTATION must name a domain writer.
        """
        import ast
        import inspect

        source = inspect.getsource(terms_endpoints)
        called = {
            node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for writer in ('record_subscription_terms', 'replace_subscription_terms',
                       'end_subscription_terms'):
            self.assertIn(writer, called, f'{writer} is never called')
        for forbidden in ('save', 'create', 'update_or_create', 'get_or_create',
                          'bulk_create', 'update', 'delete'):
            self.assertNotIn(
                forbidden, called,
                f'{forbidden}() is called in the terms adapter; terms rows must '
                f'move only through commercial_app.',
            )

    def test_the_shared_base_never_writes_a_model_either(self):
        """
        The two adapters delegate their mechanics to ``commercial_base``, so the
        "no direct write" property has to hold there too — otherwise moving a
        ``.save()`` one module down would silently satisfy both AST scans.
        """
        import ast
        import inspect

        from platform_admin_app.endpoints import commercial_base

        source = inspect.getsource(commercial_base)
        called = {
            node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in ('save', 'create', 'update_or_create', 'get_or_create',
                          'bulk_create', 'delete'):
            self.assertNotIn(
                forbidden, called,
                f'{forbidden}() is called in the shared commercial adapter base.',
            )

    def test_all_three_require_recent_elevation(self):
        from rest_framework.permissions import IsAuthenticated

        from platform_admin_app.permissions import IsRecentlyElevated

        for view in self.VIEWS:
            with self.subTest(view=view.__name__):
                self.assertEqual(
                    view.permission_classes, [IsAuthenticated, IsRecentlyElevated],
                )

    def test_the_serializers_are_plain_and_not_model_bound(self):
        from rest_framework import serializers as drf

        expected = {
            terms_endpoints.RecordSubscriptionTermsRequestSerializer: {
                'recurring_amount', 'currency', 'billing_interval_unit',
                'billing_interval_count', 'effective_from', 'reason',
            },
            terms_endpoints.ReplaceSubscriptionTermsRequestSerializer: {
                'expected_terms_id', 'recurring_amount', 'currency',
                'billing_interval_unit', 'billing_interval_count',
                'effective_from', 'reason',
            },
            terms_endpoints.EndSubscriptionTermsRequestSerializer: {
                'expected_terms_id', 'ended_at', 'reason',
            },
        }
        for cls, fields in expected.items():
            with self.subTest(serializer=cls.__name__):
                self.assertTrue(issubclass(cls, drf.Serializer))
                self.assertFalse(issubclass(cls, drf.ModelSerializer))
                self.assertEqual(set(cls().fields), fields)

    def test_record_does_not_accept_a_concurrency_token(self):
        """Structural: the field is simply not on the record contract."""
        self.assertNotIn(
            'expected_terms_id',
            terms_endpoints.RecordSubscriptionTermsRequestSerializer().fields,
        )

    def test_no_model_serializer_targets_the_terms_model(self):
        """
        The TENANT-STRUCT-00 ratchet reasons about DRF ``ModelSerializer``
        relations, so a ``ModelSerializer`` over the terms model would put a
        writable commercial relation on a surface that machinery inspects. There
        is none, and this walks the live class tree rather than a module list so
        one declared anywhere still counts.
        """
        from rest_framework.serializers import ModelSerializer

        seen, pending = set(), [ModelSerializer]
        while pending:
            cls = pending.pop()
            if cls in seen:
                continue
            seen.add(cls)
            pending.extend(cls.__subclasses__())
            model = getattr(getattr(cls, 'Meta', None), 'model', None)
            self.assertIsNot(
                model, RestaurantSubscriptionTerms,
                f'{cls.__name__} is a ModelSerializer over the terms model.',
            )
        self.assertGreater(len(seen), 1, 'the class walk found nothing to check')

    def test_the_audit_snapshot_is_narrow(self):
        terms = RestaurantSubscriptionTerms(
            recurring_amount=Decimal('150000.00'), currency=UGX,
            billing_interval_unit='month', billing_interval_count=1,
            effective_from=timezone.now(), ended_at=timezone.now(),
        )
        snapshot = terms_endpoints.terms_snapshot(terms)
        self.assertEqual(
            set(snapshot),
            {'id', 'recurring_amount', 'currency', 'billing_interval',
             'effective_from'},
        )
        self.assertIsNone(terms_endpoints.terms_snapshot(None))
        self.assertIsInstance(snapshot['recurring_amount'], str)
