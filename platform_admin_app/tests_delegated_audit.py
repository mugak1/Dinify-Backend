"""
PR-D — the delegated write audit is transactional, so delegated tenant writes are
audit-atomic like every other privileged state change.

``DelegatedAccessMiddleware`` used to audit a delegated write from ``_finalize``, after
the view had returned and its transaction had committed. A failed audit had nothing
left to unwind, so it was swallowed into a log line and the change stood unrecorded —
the one place PR-3's contract did not hold, as the middleware's own docstring conceded.

Both tenant writes a delegation can reach now audit inside their own transaction, so a
failed audit rolls the write back. Before that change the stock stayed toggled and the
support issue stayed created; the two atomicity tests here are what pin it.

The third non-safe allowlisted route, ``POST api/v1/delegation/end/``, is deliberately
not covered by that mechanism — it writes admin-plane rows only and
``delegated_sessions.end_session`` already audits inside its own transaction. It is
asserted here so the exemption is deliberate rather than an oversight.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    RESTAURANT_OWNER,
)
from platform_admin_app import delegated_sessions, delegation
from platform_admin_app.audit_actions import ADMIN_DELEGATION_ACTION_PERFORMED
from platform_admin_app.models import (
    RESULT_SUCCESS,
    SCOPE_SUPPORT,
    AdminAuditLog,
)
from restaurants_app.models import MenuItem, MenuSection, Restaurant, RestaurantEmployee
from support_app.models import SupportIssue
from users_app.models import User

SUPPORT_ISSUES_URL = '/api/v1/support/issues/'


def _user(phone, **kwargs):
    return User.objects.create_user(
        first_name='Del', last_name='Egate', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[], **kwargs
    )


class DelegatedWriteAuditFixture(TestCase):
    """One restaurant, one administrator, one live support-scoped delegation."""

    def setUp(self):
        super().setUp()
        cache.clear()  # the exchange throttle is process-global (LocMemCache)
        self.owner = _user('256780000001')
        self.restaurant = Restaurant.objects.create(
            name='Delegated R', location='loc', owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=100,
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.administrator = User.objects.create_user(
            first_name='Plat', last_name='Admin', email='pd-admin@test.com',
            phone_number=None, username='pd-platform-admin', country='Uganda',
            password='password', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        raw_code, _grant = delegation.mint_grant(
            administrator=self.administrator, admin_session=None,
            restaurant=self.restaurant, scope=SCOPE_SUPPORT,
            reason='PR-D delegated write audit coverage.',
        )
        token, self.context = delegated_sessions.exchange_code(raw_code)
        self.headers = {'HTTP_X_DELEGATION_SESSION': token}
        self.client = Client()

    def _stock_url(self):
        return f'/api/v1/kitchen/menu-items/{self.item.id}/stock/'

    def _toggle_stock(self, in_stock=False):
        return self.client.put(
            self._stock_url(),
            data={'in_stock': in_stock},
            content_type='application/json',
            **self.headers,
        )

    def _raise_audit(self):
        """Patch the audit LEAF, so both record wrappers are covered by one patch."""
        return patch(
            'platform_admin_app.audit.record',
            side_effect=RuntimeError('audit down'),
        )


@override_settings(ROOT_URLCONF='dinify_backend.urls')
class DelegatedStockToggleAuditTests(DelegatedWriteAuditFixture):
    """Menu-item stock toggle — one of the two delegated tenant writes."""

    def test_the_write_is_audited_transactionally(self):
        response = self._toggle_stock(in_stock=False)
        self.assertEqual(response.status_code, 200)

        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)

        entry = AdminAuditLog.objects.get(action=ADMIN_DELEGATION_ACTION_PERFORMED)
        self.assertEqual(entry.result, RESULT_SUCCESS)
        self.assertEqual(entry.actor_id, self.administrator.id)
        self.assertNotEqual(entry.actor_id, self.owner.id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.delegation_id, self.context.grant.id)
        # Written from inside the service transaction, not by the middleware.
        self.assertTrue(entry.after_state.get('transactional'))

    def test_exactly_one_row_is_written(self):
        """The middleware must not add a second row on top of the transactional one."""
        self._toggle_stock(in_stock=False)
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_ACTION_PERFORMED).count(),
            1,
        )

    def test_a_failed_audit_rolls_the_toggle_back(self):
        """Audit-atomic: a failed audit unwinds the toggle rather than leaving it."""
        self.assertTrue(self.item.in_stock)

        with self._raise_audit():
            with self.assertRaises(RuntimeError):
                self._toggle_stock(in_stock=False)

        self.item.refresh_from_db()
        self.assertTrue(
            self.item.in_stock,
            'the stock toggle committed without its audit row',
        )
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_ACTION_PERFORMED).exists()
        )

    def test_an_ordinary_staff_toggle_writes_no_admin_row(self):
        """Non-regression: the helper is a no-op for every non-delegated principal."""
        from rest_framework_simplejwt.tokens import RefreshToken

        token = str(RefreshToken.for_user(self.owner).access_token)
        # Measured as a DELTA: the fixture's own grant mint and code exchange have
        # already written rows, and the log is append-only so it cannot be cleared.
        before = AdminAuditLog.objects.count()

        response = self.client.put(
            self._stock_url(),
            data={'in_stock': False},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)
        self.assertEqual(
            AdminAuditLog.objects.count(), before,
            'an ordinary staff write produced an admin audit row',
        )
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_ACTION_PERFORMED).exists()
        )


@override_settings(ROOT_URLCONF='dinify_backend.urls')
class DelegatedSupportIssueAuditTests(DelegatedWriteAuditFixture):
    """Support-issue creation — the other delegated tenant write."""

    def _raise_issue(self):
        return self.client.post(
            SUPPORT_ISSUES_URL,
            data={
                'restaurant': str(self.restaurant.id),
                'category': 'tables_qr',
                'impact': 'affecting_service',
                'title': 'Card reader offline',
                'description': 'The reader at table 4 will not pair.',
            },
            content_type='application/json',
            **self.headers,
        )

    def test_the_write_is_audited_transactionally(self):
        response = self._raise_issue()
        self.assertIn(response.status_code, (200, 201), response.content)
        self.assertEqual(SupportIssue.objects.count(), 1)

        entry = AdminAuditLog.objects.get(action=ADMIN_DELEGATION_ACTION_PERFORMED)
        self.assertEqual(entry.actor_id, self.administrator.id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.delegation_id, self.context.grant.id)
        self.assertTrue(entry.after_state.get('transactional'))

    def test_a_failed_audit_rolls_the_issue_back(self):
        """Audit-atomic: a failed audit unwinds the issue rather than leaving it."""
        with self._raise_audit():
            with self.assertRaises(RuntimeError):
                self._raise_issue()

        self.assertEqual(
            SupportIssue.objects.count(), 0,
            'the support issue committed without its audit row',
        )
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_ACTION_PERFORMED).exists()
        )

    def test_exactly_one_row_is_written(self):
        self._raise_issue()
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_ACTION_PERFORMED).count(),
            1,
        )


@override_settings(ROOT_URLCONF='dinify_backend.urls')
class DelegatedAuditCoverageTests(DelegatedWriteAuditFixture):
    """
    Which non-safe delegated routes are under transactional audit, and which are not.

    A guard against the allowlist growing a tenant write that quietly relies on the
    middleware's best-effort path again.
    """

    def test_the_allowlist_has_exactly_the_expected_non_safe_routes(self):
        from platform_admin_app.configs.delegation_scopes import (
            ALLOWED_ROUTES, SAFE_METHODS,
        )
        non_safe = {
            (route, method)
            for (route, method) in ALLOWED_ROUTES
            if method not in SAFE_METHODS
        }
        self.assertEqual(
            non_safe,
            {
                # Both under transactional audit as of PR-D.
                ('api/v1/support/issues/', 'POST'),
                ('api/v1/kitchen/menu-items/<str:pk>/stock/', 'PUT'),
                # Admin-plane only; audits its own session_ended transactionally.
                ('api/v1/delegation/end/', 'POST'),
            },
            'a non-safe delegated route was added — give it a transactional audit '
            'or exclude it from the allowlist, do not leave it half-covered',
        )
