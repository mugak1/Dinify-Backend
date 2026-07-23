"""
Tests for the PR-2a admin control-plane transport layer.

Covers (spec §2.8):
* AdminSession lifecycle — create / resolve / expire-absolute / expire-idle / revoke;
  touch throttling; the raw token is never persisted.
* AdminSessionAuthentication — rejects missing / garbage / expired / revoked cookie,
  a restaurant_user account, an inactive account, and a platform-staff account that
  holds an active membership (the PR-1 invariant, fail-closed); enforces CSRF on
  unsafe methods only.
* Cookie helpers — the ``__Host-`` attribute set.
* Middleware — request id is server-generated and ignores a client header; client ip
  is REMOTE_ADDR at trusted-proxy depth 0.
* Urlconf isolation — admin routes absent from the customer urlconf and vice versa.
* Prefix arithmetic — resolve of the /api-stripped path + reverse under SCRIPT_NAME=/api.
* Transport endpoints — health reachable unauthenticated; a stub AdminAPIView is
  unreachable without a session; CSRF reject / accept end-to-end.

These run under the base ``test_settings``; endpoint-level cases add the admin
ROOT_URLCONF / MIDDLEWARE / REST_FRAMEWORK via ``@override_settings`` and a local
``urlpatterns`` (below), so no second settings module is needed.
"""
from datetime import timedelta

from django.conf import settings as dj_settings
from django.http import HttpResponse
from django.test import (
    Client, RequestFactory, TestCase, override_settings,
)
from django.urls import (
    Resolver404, get_script_prefix, path, resolve, reverse, set_script_prefix,
)
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
)
from platform_admin_app import sessions
from platform_admin_app.authentication import AdminSessionAuthentication
from platform_admin_app.cookies import (
    clear_session_cookie, cookie_name, set_session_cookie,
)
from platform_admin_app.middleware import ClientIPMiddleware, RequestIDMiddleware
from platform_admin_app.models import AdminSession
from platform_admin_app.views import AdminAPIView, AdminHealthView
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

# Distinct phone range from platform_admin_app/tests.py to avoid any collision.
_PHONE = iter(f'2567020000{n:02d}' for n in range(1, 99))


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, is_active=True):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='T', last_name=phone[-3:], email=email,
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[], account_type=account_type,
        is_active=is_active,
    )


# --- Local urlconf + stub view for the transport endpoint tests --------------------

class _EchoAdminView(AdminAPIView):
    """A concrete authenticated admin view used only to exercise the base class."""

    def get(self, request):
        return Response({'ok': True, 'user': str(request.user.id)})

    def post(self, request):
        return Response({'ok': True})


urlpatterns = [
    path('admin/v1/health/', AdminHealthView.as_view(), name='t-admin-health'),
    path('admin/v1/echo/', _EchoAdminView.as_view(), name='t-admin-echo'),
]

_ADMIN_MIDDLEWARE = [
    'platform_admin_app.middleware.RequestIDMiddleware',
    'platform_admin_app.middleware.ClientIPMiddleware',
    *dj_settings.MIDDLEWARE,
]
_ADMIN_REST_FRAMEWORK = {
    **dj_settings.REST_FRAMEWORK,
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'platform_admin_app.authentication.AdminSessionAuthentication',
    ),
    'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
}
_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=_ADMIN_MIDDLEWARE,
    REST_FRAMEWORK=_ADMIN_REST_FRAMEWORK,
    CSRF_COOKIE_SAMESITE='Strict',
    CSRF_COOKIE_SECURE=True,
    CSRF_COOKIE_HTTPONLY=False,
    CSRF_TRUSTED_ORIGINS=['https://admin.dinifyapp.com'],
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)


# --- Session lifecycle -------------------------------------------------------------

class SessionServiceTests(TestCase):
    def test_create_stores_only_hash_and_resolves(self):
        user = _make_user('sess_create@t.com')
        raw, session = sessions.create_session(user, ip='1.2.3.4', user_agent='UA/1')
        self.assertEqual(len(session.token_hash), 64)
        self.assertNotEqual(session.token_hash, raw)
        # The raw token is never stored in any form other than its hash.
        self.assertFalse(AdminSession.objects.filter(token_hash=raw).exists())
        self.assertEqual(session.issued_ip, '1.2.3.4')
        self.assertEqual(session.issued_user_agent, 'UA/1')
        resolved = sessions.resolve_session(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, session.pk)

    def test_resolve_empty_or_unknown_returns_none(self):
        self.assertIsNone(sessions.resolve_session(''))
        self.assertIsNone(sessions.resolve_session(None))
        self.assertIsNone(sessions.resolve_session('not-a-real-token'))

    def test_resolve_none_past_absolute_expiry(self):
        user = _make_user('sess_abs@t.com')
        raw, session = sessions.create_session(user)
        session.absolute_expiry = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=['absolute_expiry'])
        self.assertIsNone(sessions.resolve_session(raw))

    def test_resolve_none_when_idle_timeout_exceeded(self):
        user = _make_user('sess_idle@t.com')
        raw, session = sessions.create_session(user)
        session.last_seen = timezone.now() - timedelta(minutes=31)
        session.save(update_fields=['last_seen'])
        self.assertIsNone(sessions.resolve_session(raw))

    def test_resolve_none_when_revoked(self):
        user = _make_user('sess_revoke@t.com')
        raw, session = sessions.create_session(user)
        sessions.revoke(session, 'manual')
        self.assertIsNone(sessions.resolve_session(raw))
        session.refresh_from_db()
        self.assertIsNotNone(session.revoked_at)
        self.assertEqual(session.revoked_reason, 'manual')

    def test_touch_is_throttled(self):
        user = _make_user('sess_touch@t.com')
        raw, session = sessions.create_session(user)
        # Fresh session: last_seen ~ now, so a touch is a no-op.
        first_seen = session.last_seen
        sessions.touch(session)
        session.refresh_from_db()
        self.assertEqual(session.last_seen, first_seen)
        # Push last_seen back beyond the throttle window: the next touch writes.
        stale = timezone.now() - timedelta(minutes=6)
        AdminSession.objects.filter(pk=session.pk).update(last_seen=stale)
        session.refresh_from_db()
        sessions.touch(session)
        session.refresh_from_db()
        self.assertGreater(session.last_seen, stale)

    def test_revoke_all_for_user(self):
        user = _make_user('sess_all@t.com')
        raw1, _ = sessions.create_session(user)
        raw2, _ = sessions.create_session(user)
        count = sessions.revoke_all_for_user(user, 'logout-everywhere')
        self.assertEqual(count, 2)
        self.assertIsNone(sessions.resolve_session(raw1))
        self.assertIsNone(sessions.resolve_session(raw2))


# --- Authentication class ----------------------------------------------------------

_factory = APIRequestFactory()


def _authenticate(raw_token):
    django_request = _factory.get('/admin/v1/echo/')
    if raw_token is not None:
        django_request.COOKIES[cookie_name()] = raw_token
    return AdminSessionAuthentication().authenticate(Request(django_request))


class AuthClassTests(TestCase):
    def test_no_cookie_returns_none(self):
        self.assertIsNone(_authenticate(None))

    def test_garbage_cookie_rejected(self):
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate('garbage-token')

    def test_valid_platform_staff_authenticates(self):
        user = _make_user('auth_ok@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, session = sessions.create_session(user)
        result = _authenticate(raw)
        self.assertIsNotNone(result)
        auth_user, auth_session = result
        self.assertEqual(auth_user.pk, user.pk)
        self.assertEqual(auth_session.pk, session.pk)

    def test_reject_restaurant_user(self):
        user = _make_user('auth_rest@t.com', account_type=ACCOUNT_TYPE_RESTAURANT_USER)
        raw, _ = sessions.create_session(user)
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate(raw)

    def test_reject_inactive_account(self):
        user = _make_user(
            'auth_inactive@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
            is_active=False,
        )
        raw, _ = sessions.create_session(user)
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate(raw)

    def test_reject_platform_staff_with_active_membership(self):
        owner = _make_user('auth_owner@t.com')
        restaurant = Restaurant.objects.create(
            name='M', location='loc', status='active', owner=owner,
        )
        staff = _make_user('auth_dual@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        # Dual-role fixture created out-of-band (mirrors a pre-flip standing row).
        RestaurantEmployee.objects.create(
            user=staff, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
        )
        raw, _ = sessions.create_session(staff)
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate(raw)

    def test_reject_expired_session(self):
        user = _make_user('auth_exp@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, session = sessions.create_session(user)
        session.absolute_expiry = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=['absolute_expiry'])
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate(raw)

    def test_reject_revoked_session(self):
        user = _make_user('auth_rev@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, session = sessions.create_session(user)
        sessions.revoke(session, 'revoked')
        with self.assertRaises(exceptions.AuthenticationFailed):
            _authenticate(raw)

    def test_authenticate_header_yields_401(self):
        # A non-None authenticate_header makes DRF answer 401 (not 403) for the
        # unauthenticated case, the SPA's re-login signal.
        self.assertIsNotNone(
            AdminSessionAuthentication().authenticate_header(_factory.get('/x'))
        )


# --- Cookie helpers ----------------------------------------------------------------

class CookieHelperTests(TestCase):
    def test_set_session_cookie_attributes(self):
        response = set_session_cookie(HttpResponse(), 'raw-token-value')
        morsel = response.cookies['__Host-dinify_admin_session']
        self.assertEqual(morsel.value, 'raw-token-value')
        self.assertTrue(morsel['httponly'])
        self.assertTrue(morsel['secure'])
        self.assertEqual(morsel['samesite'], 'Strict')
        self.assertEqual(morsel['path'], '/')
        self.assertEqual(morsel['domain'], '')  # host-only — no Domain attribute
        self.assertEqual(int(morsel['max-age']), int(timedelta(hours=8).total_seconds()))

    def test_clear_session_cookie(self):
        response = clear_session_cookie(HttpResponse())
        morsel = response.cookies['__Host-dinify_admin_session']
        # An expiry in the past (max-age 0) clears it.
        self.assertEqual(morsel['max-age'], 0)


# --- Middleware --------------------------------------------------------------------

class MiddlewareUnitTests(TestCase):
    def test_request_id_generated_and_ignores_client_header(self):
        captured = {}

        def get_response(request):
            captured['id'] = request.request_id
            return HttpResponse()

        mw = RequestIDMiddleware(get_response)
        request = RequestFactory().get('/x', HTTP_X_REQUEST_ID='client-forged')
        response = mw(request)
        self.assertNotEqual(captured['id'], 'client-forged')
        self.assertEqual(len(captured['id']), 32)  # uuid4().hex
        self.assertEqual(response['X-Request-ID'], captured['id'])

    def test_client_ip_is_remote_addr_at_depth_zero(self):
        captured = {}

        def get_response(request):
            captured['ip'] = request.client_ip
            return HttpResponse()

        mw = ClientIPMiddleware(get_response)
        request = RequestFactory().get(
            '/x', REMOTE_ADDR='9.9.9.9', HTTP_X_FORWARDED_FOR='1.2.3.4',
        )
        mw(request)
        # X-Forwarded-For is NOT trusted at depth 0.
        self.assertEqual(captured['ip'], '9.9.9.9')


# --- Urlconf isolation -------------------------------------------------------------

class UrlconfIsolationTests(TestCase):
    def test_admin_route_absent_from_customer_urlconf(self):
        with self.assertRaises(Resolver404):
            resolve('/admin/v1/health/', urlconf='dinify_backend.urls')

    def test_customer_routes_absent_from_admin_urlconf(self):
        with self.assertRaises(Resolver404):
            resolve('/api/v1/health/', urlconf='dinify_backend.urls_admin')
        with self.assertRaises(Resolver404):
            resolve('/api/v1/users/login/', urlconf='dinify_backend.urls_admin')

    def test_admin_health_resolves_in_admin_urlconf(self):
        match = resolve('/admin/v1/health/', urlconf='dinify_backend.urls_admin')
        self.assertEqual(match.view_name, 'admin-health')


# --- Prefix arithmetic under SCRIPT_NAME=/api --------------------------------------

class PrefixArithmeticTests(TestCase):
    def test_resolve_on_stripped_path(self):
        # Apache strips /api, so Django sees the bare path.
        match = resolve('/admin/v1/health/', urlconf='dinify_backend.urls_admin')
        self.assertEqual(match.view_name, 'admin-health')

    def test_reverse_includes_api_mount(self):
        # With SCRIPT_NAME=/api (the Apache mount), reverse yields the browser URL.
        original = get_script_prefix()
        try:
            set_script_prefix('/api/')
            self.assertEqual(
                reverse('admin-health', urlconf='dinify_backend.urls_admin'),
                '/api/admin/v1/health/',
            )
        finally:
            set_script_prefix(original)


# --- Transport endpoints (admin stack via override_settings) -----------------------

@override_settings(**_ADMIN_OVERRIDES)
class TransportEndpointTests(TestCase):
    def test_health_reachable_unauthenticated(self):
        response = self.client.get('/admin/v1/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_stub_view_requires_session(self):
        response = self.client.get('/admin/v1/echo/')
        self.assertIn(response.status_code, (401, 403))

    def test_stub_view_ok_with_session(self):
        user = _make_user('ep_ok@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, _ = sessions.create_session(user)
        self.client.cookies[cookie_name()] = raw
        response = self.client.get('/admin/v1/echo/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])

    def test_unsafe_method_without_csrf_rejected(self):
        user = _make_user('ep_csrf1@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, _ = sessions.create_session(user)
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = raw
        response = client.post(
            '/admin/v1/echo/', data='{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_unsafe_method_with_csrf_accepted(self):
        user = _make_user('ep_csrf2@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, _ = sessions.create_session(user)
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = raw
        token = 'a' * 32  # a valid unmasked CSRF secret (double-submit)
        client.cookies['csrftoken'] = token
        response = client.post(
            '/admin/v1/echo/', data='{}', content_type='application/json',
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)

    def test_request_id_header_present_and_ignores_client(self):
        response = self.client.get(
            '/admin/v1/health/', HTTP_X_REQUEST_ID='client-forged',
        )
        self.assertIn('X-Request-ID', response)
        self.assertNotEqual(response['X-Request-ID'], 'client-forged')
        self.assertEqual(len(response['X-Request-ID']), 32)
