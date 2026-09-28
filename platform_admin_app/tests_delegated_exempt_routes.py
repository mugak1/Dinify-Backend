"""
``DelegatedAccessMiddleware.EXEMPT_ROUTES`` — the routes the delegation gate never
evaluates (Codex P2 on #349).

The gate resolves an ``X-Delegation-Session`` header with a query on the request's own
database connection, then answers 401/403 and writes an audit row. For readiness that
meant a header decided whether, and how late, the route answered: against a frozen
database a request carrying one got no answer within 8 s, while the same request
without it answered 503 in 1.76 s. The exemption makes such a request an undelegated
one before the header is read.

Organised as REGRESSION (fails with the exemption removed) and CONTROL (must hold
either way). The controls are the other half of the contract: the match is EXACT and
on the URL PATTERN, and every other route — the liveness route beside it included —
is gated exactly as before.

The readiness view itself arrives with #349, so the exempt route is exercised here
through a URLconf that mounts a probe view the way ``dinify_backend.urls`` mounts
``misc_app.urls`` — ``api/v1/health/`` + ``ready/`` — which yields the same
``resolver_match.route``, the only thing the middleware reads. The controls against
real routes use the real URLconf.
"""
from unittest import mock

from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import include, path, resolve

from platform_admin_app import delegated_middleware
from platform_admin_app.delegated_middleware import (
    ACTING_AS_HEADER,
    EXEMPT_ROUTES,
    SESSION_HEADER,
    delegation_context,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from platform_admin_app.models import SCOPE_SUPPORT, SCOPE_VIEW, AdminAuditLog
from platform_admin_app.tests_delegated_session import (
    PROFILE_URL,
    SETUP_URL,
    _SESSION_META,
    _ThrottleIsolation,
    _make_admin,
    _session_for,
)
from restaurants_app.models import Restaurant
from users_app.models import User

READY_URL = '/api/v1/health/ready/'
LIVENESS_URL = '/api/v1/health/'
READY_ROUTE = 'api/v1/health/ready/'
JUNK = 'not-a-delegated-session-token'

# Distinct phone range from every other suite (…0990…). The restaurant builder is this
# module's own on purpose: ``tests_delegated_session._make_restaurant`` draws from THAT
# module's bounded phone iterator, and borrowing it exhausts the iterator when both
# modules run in one process. ``_make_admin`` creates no phone number, so it is shared.
_PHONE = iter(f'25670990{n:04d}' for n in range(1, 10000))


def _make_restaurant(name):
    phone = next(_PHONE)
    owner = User.objects.create_user(
        first_name='T', last_name='U', email=f'exempt-owner-{phone}@t.com',
        phone_number=phone, username=phone, country='Uganda',
        password='correct-horse-battery', roles=[],
    )
    return Restaurant.objects.create(
        name=name, location=f'{name} loc', status=RestaurantStatus_Live, owner=owner,
    )


def _probe(request, **kwargs):
    """Stands in for the view. Touches no database and reads no credential; it says
    only whether the gate bound a delegated context to the request."""
    return JsonResponse({'delegated': delegation_context(request) is not None})


class _MirrorUrls:
    """The production mount of ``misc_app.urls`` under ``api/v1/health/``, plus two
    lookalikes that must stay gated."""

    urlpatterns = [
        path('api/v1/health/', include([
            path('ready/', _probe),
            path('ready/<str:tail>/', _probe),
        ])),
        path('api/v1/health/readyz/', _probe),
    ]


class _PatternNotPathUrls:
    """``/api/v1/health/ready/`` reached through a DIFFERENT pattern."""

    urlpatterns = [path('api/v1/health/<str:probe>/', _probe)]


def _audit_count():
    return AdminAuditLog.objects.count()


@override_settings(ROOT_URLCONF=_MirrorUrls)
class ExemptRouteTests(_ThrottleIsolation, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def _assert_undelegated(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'delegated': False})
        self.assertNotIn(ACTING_AS_HEADER, response)
        self.assertNotIn(SESSION_HEADER, response.get('Vary', ''))

    # --- premises -----------------------------------------------------------------
    def test_PREMISE_the_mirror_mount_produces_the_exempt_route(self):
        self.assertEqual(resolve(READY_URL).route, READY_ROUTE)
        self.assertIn(READY_ROUTE, EXEMPT_ROUTES)

    def test_CONTRACT_the_exempt_set_is_exactly_the_readiness_route(self):
        """Adding an entry must be a deliberate edit to this test as well."""
        self.assertEqual(EXEMPT_ROUTES, frozenset({READY_ROUTE}))

    # --- regressions ----------------------------------------------------------------
    def test_REGRESSION_a_junk_session_header_never_reaches_the_gate(self):
        before = _audit_count()
        with self.assertNumQueries(0):
            response = self.client.get(READY_URL, **{_SESSION_META: JUNK})
        self._assert_undelegated(response)
        self.assertEqual(_audit_count(), before)

    def test_REGRESSION_a_valid_session_is_neither_honoured_nor_refused(self):
        admin = _make_admin()
        restaurant = _make_restaurant('Alpha Grill')
        for scope in (SCOPE_VIEW, SCOPE_SUPPORT):
            with self.subTest(scope=scope):
                token, _context = _session_for(admin, restaurant, scope=scope)
                before = _audit_count()
                with self.assertNumQueries(0):
                    response = self.client.get(READY_URL, **{_SESSION_META: token})
                self._assert_undelegated(response)
                self.assertEqual(_audit_count(), before)

    def test_REGRESSION_the_header_beside_an_access_token_is_not_refused(self):
        before = _audit_count()
        with self.assertNumQueries(0):
            response = self.client.get(
                READY_URL, HTTP_AUTHORIZATION='Bearer x', **{_SESSION_META: JUNK},
            )
        self._assert_undelegated(response)
        self.assertEqual(_audit_count(), before)

    def test_REGRESSION_the_header_is_never_read_and_no_session_resolved(self):
        """The route is checked BEFORE the header is read. The CONTROL half proves
        the two patches are live: the same request to a gated lookalike hits them."""
        with mock.patch.object(
            delegated_middleware, 'session_token_from_request',
            side_effect=AssertionError('header read'),
        ), mock.patch.object(
            delegated_middleware.delegated_sessions, 'resolve_session',
            side_effect=AssertionError('session resolved'),
        ):
            self._assert_undelegated(
                self.client.get(READY_URL, **{_SESSION_META: JUNK}),
            )
            with self.assertLogs('django.request', 'ERROR'), \
                    self.assertRaisesMessage(AssertionError, 'header read'):
                self.client.get('/api/v1/health/readyz/', **{_SESSION_META: JUNK})

    def test_REGRESSION_the_exemption_is_per_route_not_per_method(self):
        """The view decides which methods it answers (readiness: GET/HEAD, else
        405); the gate does not re-open for a different method."""
        before = _audit_count()
        for method in ('head', 'post', 'put', 'patch', 'delete', 'options'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    READY_URL, **{_SESSION_META: JUNK},
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(ACTING_AS_HEADER, response)
        self.assertEqual(_audit_count(), before)

    # --- controls -------------------------------------------------------------------
    def test_CONTROL_no_header_is_unchanged(self):
        with self.assertNumQueries(0):
            self._assert_undelegated(self.client.get(READY_URL))

    def test_CONTROL_a_longer_route_sharing_the_prefix_is_still_gated(self):
        """Exact, never a prefix: ``api/v1/health/ready/<str:tail>/``."""
        before = _audit_count()
        response = self.client.get(f'{READY_URL}extra/', **{_SESSION_META: JUNK})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error_code'], 'session_invalid')
        self.assertEqual(_audit_count(), before + 1)

    def test_CONTROL_a_route_sharing_the_characters_is_still_gated(self):
        before = _audit_count()
        response = self.client.get('/api/v1/health/readyz/', **{_SESSION_META: JUNK})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(_audit_count(), before + 1)


@override_settings(ROOT_URLCONF=_PatternNotPathUrls)
class ExemptionMatchesThePatternTests(_ThrottleIsolation, TestCase):
    def test_CONTROL_the_same_path_through_another_pattern_is_still_gated(self):
        """The match is on ``resolver_match.route``, never the request path, so a
        record id or a catch-all can never make a route exempt."""
        self.assertEqual(resolve(READY_URL).route, 'api/v1/health/<str:probe>/')
        before = _audit_count()
        response = Client().get(READY_URL, **{_SESSION_META: JUNK})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(_audit_count(), before + 1)


class RealRoutesStillGatedTests(_ThrottleIsolation, TestCase):
    """The real URLconf: nothing outside ``EXEMPT_ROUTES`` moved."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant('Alpha Grill')

    def test_CONTROL_liveness_beside_it_is_still_gated(self):
        before = _audit_count()
        response = self.client.get(LIVENESS_URL, **{_SESSION_META: JUNK})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error_code'], 'session_invalid')
        self.assertEqual(_audit_count(), before + 1)

    def test_CONTROL_an_allowlisted_read_is_still_delegated(self):
        token, _context = _session_for(self.admin, self.restaurant)
        response = self.client.get(SETUP_URL, **{_SESSION_META: token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[ACTING_AS_HEADER], 'delegation')

    def test_CONTROL_a_route_off_the_allowlist_is_still_refused(self):
        token, _context = _session_for(self.admin, self.restaurant)
        before = _audit_count()
        response = self.client.get(PROFILE_URL, **{_SESSION_META: token})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()['error_code'], 'not_permitted_for_delegation',
        )
        self.assertEqual(_audit_count(), before + 1)

    def test_CONTROL_both_credentials_are_still_refused_elsewhere(self):
        response = self.client.get(
            SETUP_URL, HTTP_AUTHORIZATION='Bearer x', **{_SESSION_META: JUNK},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error_code'], 'ambiguous_credentials')
