"""
The admin-plane lifecycle transition endpoint (PR-5).

Concerned with the HTTP layer only — authentication, elevation, routing, response
shape and the denial audit that DRF's permission layer would otherwise skip. The
matrix, reason validation, preconditions and the transition audit row are the
service's, and are covered in ``restaurants_app.tests_lifecycle``.
"""
from datetime import timedelta

from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from platform_admin_app import sessions
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_LIFECYCLE_TRANSITION,
    ADMIN_RESTAURANT_TRANSITION_DENIED,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import RESULT_DENIED, RESULT_SUCCESS, AdminAuditLog
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant
from users_app.models import User

# Distinct phone range from the other admin suites (…01…–…05…).
_PHONE = iter(f'2567060000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
REASON = 'Confirmed non-payment after the third notice.'

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


def _make_admin(email='life-admin@t.com', username='life-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Java House', status=RestaurantStatus_Live, deleted=False):
    return Restaurant.objects.create(
        name=name, location=f'{name} loc', status=status,
        owner=_make_user(f'owner-{name}@t.com'.replace(' ', '-')), deleted=deleted,
    )


@override_settings(**_ADMIN_OVERRIDES)
class LifecycleTransitionEndpointTests(AuditAssertionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def _url(self, restaurant=None):
        target = restaurant if restaurant is not None else self.restaurant
        return f'/admin/v1/restaurants/{target.id}/transition/'

    def _post(self, url=None, **body):
        payload = dict(to_state=RestaurantStatus_Suspended, reason=REASON)
        payload.update(body)
        return self.client.post(
            url or self._url(), data=payload, content_type='application/json',
        )

    # --- the happy path ----------------------------------------------------
    def test_transition_applies_and_returns_the_resulting_state(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(data['status'], RestaurantStatus_Suspended)
        self.assertEqual(data['id'], str(self.restaurant.id))
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Suspended)

    def test_response_names_the_next_legal_moves(self):
        data = self._post().json()['data']
        self.assertEqual(sorted(data['allowed_transitions']), ['live', 'offboarded'])

    def test_transition_is_audited(self):
        self._post()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_LIFECYCLE_TRANSITION, result=RESULT_SUCCESS,
        )
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.before_state, {'status': RestaurantStatus_Live})
        self.assertEqual(entry.after_state, {'status': RestaurantStatus_Suspended})

    def test_audit_entry_carries_the_request_context(self):
        self._post()
        entry = AdminAuditLog.objects.get(
            action=ADMIN_RESTAURANT_LIFECYCLE_TRANSITION)
        # Server-generated, never a client header.
        self.assertTrue(entry.request_id)
        self.assertEqual(entry.session_id, self.session.id)

    # --- authentication ----------------------------------------------------
    def test_anonymous_transition_rejected(self):
        anon = Client()
        response = anon.post(
            self._url(),
            data={'to_state': RestaurantStatus_Suspended, 'reason': REASON},
            content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)
        # An unauthenticated call is about identity, not a lifecycle decision — it
        # must not manufacture a denial entry for a caller we cannot name.
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_RESTAURANT_TRANSITION_DENIED).exists()
        )

    def test_revoked_session_rejected(self):
        sessions.revoke(self.session, reason='test')
        response = self._post()
        self.assertIn(response.status_code, (401, 403))
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    # --- elevation ---------------------------------------------------------
    def test_stale_elevation_denied_and_audited(self):
        self.session.elevated_at = timezone.now() - timedelta(minutes=30)
        self.session.save(update_fields=['elevated_at'])

        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_TRANSITION_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.error_code, 'elevation_required')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))

    def test_never_elevated_session_denied(self):
        self.session.elevated_at = None
        self.session.save(update_fields=['elevated_at'])
        self.assertEqual(self._post().status_code, 403)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    # --- refusals from the service ----------------------------------------
    def test_disallowed_transition_returns_400_with_field_errors(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Offboarded)
        response = self._post(to_state=RestaurantStatus_Live)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['code'], 'transition_not_allowed')
        self.assertIn('to_state', body['errors'])
        self.assertAudited(ADMIN_RESTAURANT_TRANSITION_DENIED, result=RESULT_DENIED)

    def test_missing_reason_returns_400(self):
        response = self._post(reason='')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'reason_required')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    def test_short_reason_returns_400(self):
        response = self._post(reason='nope')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'reason_too_short')

    def test_unknown_state_returns_400(self):
        response = self._post(to_state='archived')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'unknown_state')

    # --- target resolution -------------------------------------------------
    def test_unknown_restaurant_returns_404(self):
        response = self._post(
            url='/admin/v1/restaurants/11111111-1111-4111-8111-111111111111/transition/',
        )
        self.assertEqual(response.status_code, 404)

    def test_soft_deleted_restaurant_returns_404(self):
        deleted = _make_restaurant(name='Gone', deleted=True)
        response = self._post(url=self._url(deleted))
        self.assertEqual(response.status_code, 404)
        deleted.refresh_from_db()
        self.assertEqual(deleted.status, RestaurantStatus_Live)

    def test_malformed_restaurant_id_does_not_resolve(self):
        response = self.client.post(
            '/admin/v1/restaurants/not-a-uuid/transition/',
            data={'to_state': RestaurantStatus_Suspended, 'reason': REASON},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    # --- method surface ----------------------------------------------------
    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    def test_go_live_is_refused_while_readiness_is_unconfigured(self):
        """
        THE FAIL-CLOSED SEAM, through the endpoint (PR-D).

        Readiness returns not-ready until Phase 1 wires the real checklist, so an
        operator calling this route today gets an explicit, machine-readable refusal
        naming the blocker — not a generic error and not a silent success.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        response = self._post(
            to_state=RestaurantStatus_Live, reason='Readiness confirmed on site.')

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['code'], 'not_ready_for_go_live')
        self.assertEqual(
            body['errors']['blockers'],
            [lifecycle.BLOCKER_READINESS_NOT_CONFIGURED],
        )
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Onboarding)

    def test_full_onboarding_to_live_journey(self):
        """
        The go-live path an operator actually walks, once readiness passes.

        Readiness is supplied here because this test's subject is the ENDPOINT
        journey; the seam's own fail-closed behaviour is asserted directly above.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        with patch.object(
            lifecycle, 'check_go_live_readiness',
            return_value=lifecycle.ReadinessResult(True, []),
        ):
            response = self._post(
                to_state=RestaurantStatus_Live,
                reason='Readiness confirmed on site.',
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)
