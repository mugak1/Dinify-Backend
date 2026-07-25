"""
Tests for delegated sessions (PR-4b) — the customer-plane half of delegation.

This is the adversarial suite for the highest-risk change in the admin-portal
ladder: a second authentication path onto the plane the tenant-isolation closure
gate exists to protect. It is organised around the invariants, not the code:

* THE NO-OP PROPERTY — a request without ``X-Delegation-Session`` behaves exactly
  as it did before this feature existed, and the admin session cookie never
  authenticates anything here;
* EXCHANGE — single-use, race-safe, non-disclosing, and the raw token is readable
  exactly once;
* LIVENESS — revocation, expiry, ending and administrator ineligibility all take
  effect on the very next request, with no cache to wait out;
* SCOPE — ``view`` writes nothing, ``support`` writes exactly two things, and every
  other route is refused;
* THE AUTHORITY SEAM — a delegated principal is never a Dinify admin, never
  manage-level, and its restaurant-id resolvers never return the unrestricted
  ``None``;
* ATTRIBUTION — every delegated action is recorded against the ADMINISTRATOR, with
  the restaurant and the delegation attached, and no credential ever reaches a log.

Cross-tenant reachability through real endpoints is proved separately, in
``dinify_backend.tenancy.tests_tenant_isolation_closure``.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    MODULE_BILLING,
    MODULE_KITCHEN,
    MODULE_MENU,
    MODULE_REPORTS,
    MODULE_SUPPORT,
    MODULE_TEAM,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
    DINIFY_ADMIN,
)
from dinify_backend.tenancy.discovery import all_project_serializers
from platform_admin_app import delegated_sessions, delegation, sessions
from platform_admin_app.audit_actions import (
    ADMIN_DELEGATION_ACTION_DENIED,
    ADMIN_DELEGATION_ACTION_PERFORMED,
    ADMIN_DELEGATION_SESSION_ENDED,
    ADMIN_DELEGATION_SESSION_STARTED,
    ADMIN_DELEGATION_SESSION_START_DENIED,
)
from platform_admin_app.configs.delegation_scopes import (
    ALLOWED_ROUTES,
    SETUP_READABLE_RECORDS,
    scope_modules,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.delegated_auth import (
    PRINCIPAL_DELEGATION_ATTR,
    DelegatedSessionAuthentication,
)
from platform_admin_app.delegated_middleware import (
    ACTING_AS_HEADER,
    CODE_HEADER,
    SESSION_HEADER,
)

# Header names come from the module that defines them, never re-typed as
# literals — the WSGI META spellings below are the one unavoidable exception.
_CODE_META = 'HTTP_' + CODE_HEADER.upper().replace('-', '_')
_SESSION_META = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_SUCCESS,
    SCOPE_SUPPORT,
    SCOPE_VIEW,
    AdminAuditLog,
    DelegatedSession,
    DelegationGrant,
)
from platform_admin_app.sessions import hash_token
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import (
    MenuItem,
    MenuSection,
    Restaurant,
    RestaurantEmployee,
)
from users_app.controllers import permissions_check
from users_app.models import User

# Distinct phone range: tests.py …01…, tests_transport.py …02…, tests_audit.py
# …03…, tests_auth.py …04…, tests_delegation.py …05…, this module …06….
_PHONE = iter(f'2567060000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
REASON = 'Diagnosing a reported sold-out item on the live menu.'

EXCHANGE_URL = '/api/v1/delegation/exchange/'
SESSION_URL = '/api/v1/delegation/session/'
END_URL = '/api/v1/delegation/end/'
SETUP_URL = '/api/v1/restaurant-setup/menuitems/'
KITCHEN_ITEMS_URL = '/api/v1/kitchen/menu-items/'
SUPPORT_ISSUES_URL = '/api/v1/support/issues/'
PROFILE_URL = '/api/v1/users/user-profile/'
NOTIFICATIONS_URL = '/api/v1/notifications/'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True, roles=None):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=roles or [],
        account_type=account_type,
    )


def _make_admin(email='ds-admin@t.com', username='ds-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Java House', owner=None, deleted=False):
    owner = owner or _make_user(f'owner-{name}@t.com'.replace(' ', '-'))
    return Restaurant.objects.create(
        name=name, location=f'{name} loc', status=RestaurantStatus_Live,
        owner=owner, deleted=deleted,
    )


def _mint(admin, restaurant, scope=SCOPE_VIEW, ttl=None):
    """Mint a grant through the real PR-4a service and return ``(raw_code, grant)``."""
    return delegation.mint_grant(
        administrator=admin, admin_session=None, restaurant=restaurant,
        scope=scope, reason=REASON, session_ttl_seconds=ttl,
    )


def _session_for(admin, restaurant, scope=SCOPE_VIEW, ttl=None):
    """Mint and immediately redeem — returns ``(raw_token, context)``."""
    raw_code, _grant = _mint(admin, restaurant, scope=scope, ttl=ttl)
    return delegated_sessions.exchange_code(raw_code)


class _ThrottleIsolation:
    """
    Clear the throttle cache between tests.

    DRF throttles use the process-global LocMemCache, which the per-test
    transaction rollback does NOT reset — without this the exchange rate limit
    carries over and a later test gets a 429 instead of the status it asserts.
    """

    def setUp(self):
        super().setUp()
        cache.clear()


class _DelegatedClientMixin(_ThrottleIsolation):
    """Fixture: one administrator, two restaurants (A the target, B the victim)."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant('Alpha Grill')
        self.other = _make_restaurant('Beta Bistro')
        self.client = Client()

    def _headers(self, token):
        return {_SESSION_META: token}


# --- Exchange ---------------------------------------------------------------------

class ExchangeTests(_ThrottleIsolation, AuditAssertionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        self.client = Client()

    def _post(self, code):
        return self.client.post(EXCHANGE_URL, **{_CODE_META: code})

    def test_exchange_mints_a_session_and_returns_the_token_once(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        response = self._post(raw_code)

        self.assertEqual(response.status_code, 201)
        token = response.json()['data']['session_token']
        self.assertTrue(token)

        session = DelegatedSession.objects.get(grant=grant)
        # Only the hash is stored — the raw token cannot be recovered from the row.
        self.assertEqual(session.token_hash, hash_token(token))
        self.assertNotIn(token, str(session.__dict__))
        grant.refresh_from_db()
        self.assertIsNotNone(grant.redeemed_at)
        self.assertEqual(
            session.expires_at,
            grant.redeemed_at + timedelta(seconds=grant.session_ttl_seconds),
        )

    def test_exchange_response_carries_the_acting_as_context(self):
        raw_code, grant = _mint(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        acting = self._post(raw_code).json()['data']['acting_as']

        self.assertEqual(acting['delegation_id'], str(grant.id))
        self.assertEqual(acting['scope'], SCOPE_SUPPORT)
        self.assertEqual(acting['restaurant']['id'], str(self.restaurant.id))
        self.assertEqual(acting['restaurant']['name'], self.restaurant.name)
        self.assertTrue(acting['administrator']['name'])
        self.assertNotIn('session_token', acting)

    def test_code_is_single_use(self):
        raw_code, _ = _mint(self.admin, self.restaurant)
        self.assertEqual(self._post(raw_code).status_code, 201)
        replay = self._post(raw_code)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(DelegatedSession.objects.count(), 1)

    def test_expired_code_refused(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        DelegationGrant.objects.filter(id=grant.id).update(
            code_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self._post(raw_code).status_code, 400)
        self.assertFalse(DelegatedSession.objects.exists())

    def test_revoked_grant_cannot_be_redeemed(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        delegation.revoke_grant(grant, 'no longer needed')
        self.assertEqual(self._post(raw_code).status_code, 400)
        self.assertFalse(DelegatedSession.objects.exists())

    def test_unknown_and_missing_codes_share_one_message(self):
        _mint(self.admin, self.restaurant)
        unknown = self._post('not-a-real-code')
        absent = self.client.post(EXCHANGE_URL)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(absent.status_code, 400)
        # Non-disclosure: an unauthenticated prober learns nothing about which
        # guess was closer. The distinction lives in the audit log instead.
        self.assertEqual(unknown.json()['message'], absent.json()['message'])

    def test_deleted_restaurant_cannot_be_entered(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        Restaurant.objects.filter(id=self.restaurant.id).update(deleted=True)
        self.assertEqual(self._post(raw_code).status_code, 400)
        self.assertFalse(DelegatedSession.objects.exists())

    def test_administrator_who_lost_eligibility_cannot_redeem(self):
        raw_code, _ = _mint(self.admin, self.restaurant)
        User.objects.filter(id=self.admin.id).update(is_active=False)
        self.assertEqual(self._post(raw_code).status_code, 400)
        self.assertFalse(DelegatedSession.objects.exists())

    def test_code_in_a_query_string_or_body_does_not_work(self):
        raw_code, _ = _mint(self.admin, self.restaurant)
        # Header-only transport. A credential in a URL or body leaks into access
        # logs, Referer headers and browser history.
        self.assertEqual(
            self.client.post(f'{EXCHANGE_URL}?code={raw_code}').status_code, 400,
        )
        self.assertEqual(
            self.client.post(
                EXCHANGE_URL, data={'code': raw_code},
                content_type='application/json',
            ).status_code,
            400,
        )
        self.assertFalse(DelegatedSession.objects.exists())

    def test_exchange_ignores_an_ambient_admin_cookie(self):
        # The admin control plane's cookie must have no influence on the customer
        # plane — not even on the one endpoint that is deliberately unauthenticated.
        other_admin = _make_admin('other@t.com', username='other-admin')
        raw_session, _ = sessions.create_session(other_admin)
        self.client.cookies[cookie_name()] = raw_session
        raw_code, grant = _mint(self.admin, self.restaurant)

        acting = self._post(raw_code).json()['data']['acting_as']
        self.assertEqual(acting['delegation_id'], str(grant.id))
        session = DelegatedSession.objects.get()
        # The administrator comes from the STORED grant, never from the caller.
        self.assertEqual(session.grant.administrator_id, self.admin.id)

    # --- audit ---------------------------------------------------------------
    def test_successful_exchange_is_audited_against_the_administrator(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        self._post(raw_code)
        entry = self.assertAudited(
            ADMIN_DELEGATION_SESSION_STARTED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.delegation_id, grant.id)
        self.assertTrue(entry.request_id)

    def test_refused_exchange_is_audited_and_survives(self):
        raw_code, grant = _mint(self.admin, self.restaurant)
        delegation.revoke_grant(grant, 'withdrawn')
        self._post(raw_code)
        # The refusal happens inside a transaction that is rolled back on the
        # denial path; the entry must still be there afterwards.
        entry = self.assertAudited(
            ADMIN_DELEGATION_SESSION_START_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.error_code, 'code_revoked')
        self.assertEqual(entry.delegation_id, grant.id)

    def test_no_credential_ever_reaches_the_audit_log(self):
        raw_code, _ = _mint(self.admin, self.restaurant)
        token = self._post(raw_code).json()['data']['session_token']
        blob = '\n'.join(
            str(value)
            for row in AdminAuditLog.objects.values()
            for value in row.values()
        )
        self.assertNotIn(raw_code, blob)
        self.assertNotIn(token, blob)


class ExchangeConcurrencyTests(_ThrottleIsolation, TestCase):
    def test_a_second_redemption_of_a_locked_code_is_refused(self):
        """
        The single-use guarantee is enforced under the row lock, not by check-then-act.

        A true two-thread race needs a second DB connection (and Postgres); what is
        asserted here is the property that makes the lock sufficient — redemption
        re-reads ``is_code_live`` inside the locked transaction, so whichever call
        arrives second sees ``redeemed_at`` already set.
        """
        admin = _make_admin()
        restaurant = _make_restaurant()
        raw_code, grant = _mint(admin, restaurant)

        delegated_sessions.exchange_code(raw_code)
        with self.assertRaises(delegated_sessions.DelegatedSessionError) as caught:
            delegated_sessions.exchange_code(raw_code)

        self.assertEqual(caught.exception.code, 'code_already_redeemed')
        self.assertEqual(DelegatedSession.objects.filter(grant=grant).count(), 1)


# --- The no-op property -----------------------------------------------------------

class NoDelegationHeaderTests(_DelegatedClientMixin, TestCase):
    """Without the header, this plane is exactly what it was before PR-4b."""

    def test_anonymous_request_is_unauthenticated_as_before(self):
        self.assertEqual(self.client.get(SETUP_URL).status_code, 401)

    def test_admin_session_cookie_alone_authenticates_nothing(self):
        # The admin plane's session cookie is same-site with the customer API. It
        # must be an unread cookie here: AdminSessionAuthentication is never in the
        # customer plane's authenticator list.
        raw_session, _ = sessions.create_session(self.admin)
        self.client.cookies[cookie_name()] = raw_session
        for url in (SETUP_URL, KITCHEN_ITEMS_URL, PROFILE_URL):
            self.assertEqual(
                self.client.get(url).status_code, 401, msg=url,
            )

    def test_no_header_writes_no_audit_row(self):
        self.client.get(SETUP_URL)
        self.assertFalse(AdminAuditLog.objects.exists())

    def test_ordinary_staff_jwt_is_untouched_by_the_gate(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        staff = _make_user('staff@t.com')
        RestaurantEmployee.objects.create(
            user=staff, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True,
        )
        token = str(RefreshToken.for_user(staff).access_token)
        response = self.client.get(
            SETUP_URL, HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AdminAuditLog.objects.exists())


# --- Liveness ---------------------------------------------------------------------

class SessionLivenessTests(_DelegatedClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.token, self.context = _session_for(self.admin, self.restaurant)

    def _get(self):
        return self.client.get(SESSION_URL, **self._headers(self.token))

    def test_live_session_resolves(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['data']['restaurant']['id'], str(self.restaurant.id),
        )

    def test_revoking_the_grant_kills_the_next_request_immediately(self):
        self.assertEqual(self._get().status_code, 200)
        delegation.revoke_grant(self.context.grant, 'incident closed')
        # No cache, no TTL to wait out — the very next request is refused.
        self.assertEqual(self._get().status_code, 401)

    def test_expiry_kills_the_session(self):
        DelegatedSession.objects.filter(id=self.context.session.id).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self._get().status_code, 401)

    def test_deactivating_the_administrator_kills_the_session(self):
        User.objects.filter(id=self.admin.id).update(is_active=False)
        self.assertEqual(self._get().status_code, 401)

    def test_giving_the_administrator_a_membership_kills_the_session(self):
        # The platform-staff invariant, re-checked per request: a dual-role account
        # must not be able to keep working through a delegated session.
        RestaurantEmployee.objects.create(
            user=self.admin, restaurant=self.other, roles=[RESTAURANT_OWNER],
            active=True,
        )
        self.assertEqual(self._get().status_code, 401)

    def test_soft_deleting_the_restaurant_kills_the_session(self):
        Restaurant.objects.filter(id=self.restaurant.id).update(deleted=True)
        self.assertEqual(self._get().status_code, 401)

    def test_unknown_token_is_refused_without_falling_back(self):
        self.assertEqual(
            self.client.get(SESSION_URL, **{_SESSION_META: 'nope'}).status_code,
            401,
        )

    def test_ending_a_session_is_permanent_and_revokes_the_grant(self):
        self.assertEqual(
            self.client.post(END_URL, **self._headers(self.token)).status_code, 200,
        )
        self.assertEqual(self._get().status_code, 401)
        self.context.grant.refresh_from_db()
        self.assertIsNotNone(self.context.grant.revoked_at)
        self.assertEqual(self.context.grant.revoked_reason, 'session_ended')
        self.assertFalse(self.context.grant.is_session_live)

    def test_ending_is_audited(self):
        self.client.post(END_URL, **self._headers(self.token))
        entry = AdminAuditLog.objects.filter(
            action=ADMIN_DELEGATION_SESSION_ENDED,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.delegation_id, self.context.grant.id)


class CredentialAmbiguityTests(_DelegatedClientMixin, TestCase):
    def test_a_delegated_session_plus_a_bearer_token_is_refused(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        token, _ = _session_for(self.admin, self.restaurant)
        staff = _make_user('dual@t.com')
        RestaurantEmployee.objects.create(
            user=staff, restaurant=self.other, roles=[RESTAURANT_OWNER], active=True,
        )
        jwt = str(RefreshToken.for_user(staff).access_token)

        response = self.client.get(
            SESSION_URL,
            HTTP_AUTHORIZATION=f'Bearer {jwt}', **{_SESSION_META: token},
        )
        # Never pick one: an invalid delegated session must not fall back to staff
        # JWT, and a valid JWT must not be silently ignored.
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error_code'], 'ambiguous_credentials')


# --- Scope and the route allowlist -------------------------------------------------

class ScopeEnforcementTests(_DelegatedClientMixin, AuditAssertionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.section = MenuSection.objects.create(
            restaurant=self.restaurant, name='Mains',
        )
        self.item = MenuItem.objects.create(
            section=self.section, name='Rolex', primary_price=10000,
        )

    def test_view_scope_reads_the_menu(self):
        token, _ = _session_for(self.admin, self.restaurant)
        response = self.client.get(SETUP_URL, **self._headers(token))
        self.assertEqual(response.status_code, 200)

    def test_view_scope_cannot_write_anything(self):
        token, ctx = _session_for(self.admin, self.restaurant)
        response = self.client.put(
            f'{KITCHEN_ITEMS_URL}{self.item.id}/stock/',
            data={'in_stock': False}, content_type='application/json',
            **self._headers(token),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error_code'], 'scope_insufficient')
        self.item.refresh_from_db()
        self.assertTrue(self.item.in_stock)
        entry = self.assertAudited(
            ADMIN_DELEGATION_ACTION_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.delegation_id, ctx.grant.id)

    def test_support_scope_may_toggle_stock(self):
        token, ctx = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        response = self.client.put(
            f'{KITCHEN_ITEMS_URL}{self.item.id}/stock/',
            data={'in_stock': False}, content_type='application/json',
            **self._headers(token),
        )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)

        entry = self.assertAudited(
            ADMIN_DELEGATION_ACTION_PERFORMED, result=RESULT_SUCCESS,
        )
        # Attribution is never laundered into "the owner did it".
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertNotEqual(entry.actor_id, self.restaurant.owner_id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.delegation_id, ctx.grant.id)
        self.assertEqual(entry.after_state['method'], 'PUT')
        self.assertEqual(entry.after_state['status'], 200)

    def test_support_scope_may_raise_a_ticket(self):
        token, _ = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        response = self.client.post(
            SUPPORT_ISSUES_URL,
            data={
                'restaurant': str(self.restaurant.id),
                'category': 'menu',
                'impact': 'affecting_service',
                'title': 'Card reader offline',
                'description': 'Reported by the owner during a support call.',
            },
            content_type='application/json',
            **self._headers(token),
        )
        self.assertIn(response.status_code, (200, 201), msg=response.content)
        self.assertAudited(ADMIN_DELEGATION_ACTION_PERFORMED, result=RESULT_SUCCESS)

        from support_app.models import SupportIssue
        # The ticket is honestly attributed to the administrator who raised it.
        self.assertEqual(SupportIssue.objects.get().created_by_id, self.admin.id)

    def test_order_fulfilment_is_refused_even_at_support_scope(self):
        # Serving an order sets order_status='served', which makes it a SALE in
        # Reports. An administrator must never move a tenant's revenue.
        token, _ = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        response = self.client.put(
            f'/api/v1/kitchen/orders/{self.item.id}/fulfilment-status/',
            data={'fulfilment_status': 'served'}, content_type='application/json',
            **self._headers(token),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error_code'], 'not_permitted_for_delegation')

    def test_endpoints_without_a_restaurant_dimension_are_refused(self):
        # These act on request.user — a delegated principal here would read or
        # mutate the ADMINISTRATOR's own records, not the tenant's.
        token, _ = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        for url in (PROFILE_URL, NOTIFICATIONS_URL, '/api/v1/users/user-lookup/'):
            response = self.client.get(url, **self._headers(token))
            self.assertEqual(response.status_code, 403, msg=url)

    def test_cross_tenant_reports_and_admin_surfaces_are_refused(self):
        token, _ = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        for url in ('/api/v1/reports/dinify/dashboard/', '/api/v1/support/admin/issues/'):
            self.assertEqual(
                self.client.get(url, **self._headers(token)).status_code, 403, msg=url,
            )

    def test_billing_and_team_records_are_refused_within_an_allowed_route(self):
        token, _ = _session_for(self.admin, self.restaurant, scope=SCOPE_SUPPORT)
        for record in ('employees', 'subscription-details'):
            response = self.client.get(
                f'/api/v1/restaurant-setup/{record}/', **self._headers(token),
            )
            self.assertEqual(response.status_code, 403, msg=record)
            self.assertEqual(
                response.json()['error_code'], 'not_permitted_for_delegation',
            )

    def test_every_refusal_is_audited_against_the_administrator(self):
        token, ctx = _session_for(self.admin, self.restaurant)
        self.client.get(PROFILE_URL, **self._headers(token))
        entry = self.assertAudited(
            ADMIN_DELEGATION_ACTION_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.delegation_id, ctx.grant.id)

    def test_delegated_reads_are_not_audited_one_row_per_get(self):
        token, _ = _session_for(self.admin, self.restaurant)
        before = AdminAuditLog.objects.count()
        for _ in range(3):
            self.assertEqual(
                self.client.get(SETUP_URL, **self._headers(token)).status_code, 200,
            )
        # The session_started row is the record that an administrator entered the
        # tenant; a row per GET would drown it. (The log is append-only, so this
        # counts rather than truncating.)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_delegated_responses_are_never_cacheable(self):
        token, _ = _session_for(self.admin, self.restaurant)
        response = self.client.get(SETUP_URL, **self._headers(token))
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertIn(SESSION_HEADER, response['Vary'])
        self.assertEqual(response[ACTING_AS_HEADER], 'delegation')


class AllowlistIntegrityTests(TestCase):
    """The allowlist is the outer bound — it must not rot silently."""

    def test_every_allowlisted_route_resolves_to_a_real_url_pattern(self):
        from django.urls import get_resolver

        routes = set()

        def walk(patterns, prefix=''):
            for pattern in patterns:
                if hasattr(pattern, 'url_patterns'):
                    walk(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    routes.add(prefix + str(pattern.pattern))

        walk(get_resolver().url_patterns)
        for route, method in ALLOWED_ROUTES:
            self.assertIn(
                route, routes,
                msg=f'Allowlisted {method} {route} matches no URL pattern — a rename '
                    f'has stranded it, and the endpoint is now unreachable.',
            )

    def test_no_write_is_allowed_for_the_view_scope(self):
        from platform_admin_app.configs.delegation_scopes import SAFE_METHODS

        for (route, method), rule in ALLOWED_ROUTES.items():
            if method in SAFE_METHODS or route.startswith('api/v1/delegation/'):
                continue
            self.assertNotIn(
                SCOPE_VIEW, rule.scopes,
                msg=f'{method} {route} would let a read-only delegation write.',
            )

    def test_the_support_write_surface_is_exactly_two_endpoints(self):
        from platform_admin_app.configs.delegation_scopes import SAFE_METHODS

        writes = {
            (route, method)
            for (route, method), rule in ALLOWED_ROUTES.items()
            if method not in SAFE_METHODS and not route.startswith('api/v1/delegation/')
        }
        self.assertEqual(writes, {
            ('api/v1/support/issues/', 'POST'),
            ('api/v1/kitchen/menu-items/<str:pk>/stock/', 'PUT'),
        })

    def test_readable_setup_records_exclude_team_and_billing(self):
        self.assertNotIn('employees', SETUP_READABLE_RECORDS)
        self.assertNotIn('subscription-details', SETUP_READABLE_RECORDS)

    def test_an_unknown_scope_grants_nothing(self):
        self.assertEqual(scope_modules('root'), {})


# --- The authority seam ------------------------------------------------------------

class AuthoritySeamTests(TestCase):
    """
    A delegated principal must fail closed in every resolver, including the three
    that a Dinify admin passes through unrestricted.
    """

    def setUp(self):
        self.admin = _make_admin()
        self.restaurant = _make_restaurant('Alpha Grill')
        self.other = _make_restaurant('Beta Bistro')
        _token, self.context = _session_for(self.admin, self.restaurant)
        self.principal = self.context.administrator
        # Exactly what DelegatedSessionAuthentication does after the middleware has
        # validated the credential — asserted directly in PrincipalBindingTests.
        setattr(self.principal, PRINCIPAL_DELEGATION_ATTR, self.context)

    def test_never_a_dinify_admin(self):
        # Even if the account somehow carried a platform role, the delegation
        # branch closes the full-access map, the two `None` returns and
        # build_scoped_instance_queryset's `.all()` in one line.
        self.principal.roles = [DINIFY_ADMIN]
        self.assertFalse(permissions_check.is_dinify_admin(self.principal))
        self.assertFalse(permissions_check.is_dinify_superuser(self.principal))

    def test_never_an_owner_and_never_manage_level(self):
        self.assertFalse(
            permissions_check.is_restaurant_owner(self.principal, self.restaurant.id),
        )
        self.assertFalse(
            permissions_check.can_manage_restaurant(self.principal, self.restaurant.id),
        )

    def test_modules_resolve_only_at_the_granted_restaurant(self):
        for module in (MODULE_MENU, MODULE_KITCHEN, MODULE_REPORTS):
            self.assertTrue(
                permissions_check.can_user_access_module(
                    self.principal, self.restaurant.id, module,
                ),
                msg=module,
            )
            self.assertFalse(
                permissions_check.can_user_access_module(
                    self.principal, self.other.id, module,
                ),
                msg=module,
            )

    def test_billing_and_team_are_never_granted(self):
        for module in (MODULE_BILLING, MODULE_TEAM):
            self.assertFalse(
                permissions_check.can_user_access_module(
                    self.principal, self.restaurant.id, module,
                ),
                msg=module,
            )

    def test_the_ungated_support_module_is_still_scoped(self):
        # can_user_access_module returns True unconditionally for `support` for
        # ordinary principals; a delegation must not inherit that.
        self.assertFalse(
            permissions_check.can_user_access_module(
                self.principal, self.other.id, MODULE_SUPPORT,
            ),
        )

    def test_id_resolvers_never_return_the_unrestricted_none(self):
        for module in (MODULE_MENU, MODULE_SUPPORT, MODULE_TEAM, MODULE_BILLING):
            ids = permissions_check.get_module_restaurant_ids(self.principal, module)
            self.assertIsNotNone(ids, msg=module)
            self.assertTrue(ids <= {str(self.restaurant.id)}, msg=module)
        self.assertIsNotNone(
            permissions_check.get_employed_restaurant_ids(self.principal),
        )

    def test_view_scope_reaches_no_support_tickets(self):
        self.assertEqual(
            permissions_check.get_employed_restaurant_ids(self.principal), set(),
        )

    def test_support_scope_reaches_its_own_restaurant_only(self):
        _token, ctx = _session_for(self.admin, self.other, scope=SCOPE_SUPPORT)
        principal = ctx.administrator
        setattr(principal, PRINCIPAL_DELEGATION_ATTR, ctx)
        self.assertEqual(
            permissions_check.get_employed_restaurant_ids(principal),
            {str(self.other.id)},
        )

    def test_an_ordinary_principal_is_completely_unaffected(self):
        staff = _make_user('unaffected@t.com')
        RestaurantEmployee.objects.create(
            user=staff, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True,
        )
        self.assertTrue(
            permissions_check.can_user_access_module(
                staff, self.restaurant.id, MODULE_MENU,
            ),
        )
        self.assertTrue(
            permissions_check.can_manage_restaurant(staff, self.restaurant.id),
        )
        admin = _make_user('dinify@t.com', roles=[DINIFY_ADMIN])
        # The dinify-admin unrestricted path is untouched for a real admin.
        self.assertIsNone(
            permissions_check.get_module_restaurant_ids(admin, MODULE_MENU),
        )


class PrincipalBindingTests(TestCase):
    """The authenticator binds only what the middleware already validated."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()

    def test_returns_none_without_a_middleware_validated_context(self):
        request = self.factory.get('/api/v1/restaurant-setup/menuitems/')
        self.assertIsNone(DelegatedSessionAuthentication().authenticate(request))

    def test_ignores_a_raw_header_it_was_not_handed(self):
        token, _ = _session_for(self.admin, self.restaurant)
        request = self.factory.get(
            '/api/v1/restaurant-setup/menuitems/', **{_SESSION_META: token},
        )
        # The authenticator must never read the header itself: doing so would
        # bypass the allowlist for any view that opts out of middleware ordering.
        self.assertIsNone(DelegatedSessionAuthentication().authenticate(request))

    def test_binds_the_administrator_with_the_delegation_attached(self):
        token, context = _session_for(self.admin, self.restaurant)
        request = self.factory.get('/api/v1/restaurant-setup/menuitems/')
        setattr(request, 'delegation_context', context)

        user, auth = DelegatedSessionAuthentication().authenticate(request)
        self.assertEqual(user.id, self.admin.id)
        self.assertEqual(auth, context.session)
        self.assertIs(getattr(user, PRINCIPAL_DELEGATION_ATTR), context)

    def test_the_marker_is_in_memory_only(self):
        token, context = _session_for(self.admin, self.restaurant)
        request = self.factory.get('/api/v1/restaurant-setup/menuitems/')
        setattr(request, 'delegation_context', context)
        user, _ = DelegatedSessionAuthentication().authenticate(request)

        # Not a model field: it cannot be persisted, and a fresh read of the row
        # never carries it.
        field_names = {f.name for f in User._meta.get_fields()}
        self.assertNotIn('active_delegation', field_names)
        self.assertIsNone(
            getattr(User.objects.get(id=user.id), 'active_delegation', None),
        )


# --- Exposure guards ---------------------------------------------------------------

class ExposureGuardTests(TestCase):
    def test_no_serializer_targets_a_delegation_model(self):
        # Keeping DelegatedSession out of every ModelSerializer is also what keeps
        # the tenant-relation ratchet baseline from growing.
        for serializer in all_project_serializers():
            model = getattr(getattr(serializer, 'Meta', None), 'model', None)
            self.assertNotIn(
                model, (DelegatedSession, DelegationGrant),
                msg=f'{serializer.__name__} exposes a delegation model.',
            )

    def test_no_serializer_exposes_a_token_hash(self):
        for serializer in all_project_serializers():
            fields = getattr(getattr(serializer, 'Meta', None), 'fields', None)
            if isinstance(fields, (list, tuple)):
                self.assertNotIn('token_hash', fields, msg=serializer.__name__)
                self.assertNotIn(
                    'exchange_code_hash', fields, msg=serializer.__name__,
                )

    def test_the_session_endpoint_never_returns_a_hash(self):
        admin = _make_admin()
        restaurant = _make_restaurant()
        token, _ = _session_for(admin, restaurant)
        body = Client().get(
            SESSION_URL, **{_SESSION_META: token},
        ).content.decode()
        self.assertNotIn('token_hash', body)
        self.assertNotIn('exchange_code_hash', body)
