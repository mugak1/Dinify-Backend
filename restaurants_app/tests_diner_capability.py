"""
Tests for the opaque, expiring diner table-session capability (PR 7A).

The capability replaces raw table/order-UUID authority for every anonymous diner
operation. Two signed tiers — a long-lived QR *credential* (revoked by a
``qr_version`` bump) and a short-lived table *session* (expiring + re-checked
against live table state) — are verified by
``restaurants_app.controllers.diner_capability``, a channel completely separate
from staff JWT (``request.user``).

Coverage (the 20 required scenarios + two extras), grouped by surface:

* ``DinerCapabilityModuleTests``    — issuance/verification unit behaviour:
  malformed (3), unknown/unavailable parity (4), regeneration invalidation (5),
  expiry (7), session↛table (8) / session↛restaurant (9), live-state
  invalidation (17), salt cross-use, and signed-not-encrypted.
* ``DinerTableScanCapabilityTests`` — scan endpoint: credential→session (1, 6),
  raw-UUID / query / body rejected — no legacy path (1, 2, 3), malformed/unknown
  4xx (3, 4), no PII (20).
* ``DinerHeaderOnlyTransportTests`` — credential + session are header-only; the
  same token in a query string or request body is rejected everywhere.
* ``DinerResponseCacheTests``       — capability responses are no-store/private.
* ``DinerCapKeyConfigTests``        — DINER_CAP_KEY fails closed in production.
* ``DinerCapabilityLoggingTests``   — session tokens never reach the logs.
* ``DinerInitiateCapabilityTests``  — v2 initiate: derives r/t (10), body/session
  mismatch (11), staff admin path intact (12), idempotency scoped (18),
  table-lock/serialization intact (19).
* ``DinerOrderDetailsCapabilityTests`` — order-details + submit: session required
  (13), foreign order 404 (14), capability governs over a staff JWT.
* ``DinerReviewCapabilityTests``    — review needs an eligible order + session (15).
* ``DinerPaymentDetailsCapabilityTests`` — payment-details session rule + null
  order (16).
* ``RegenerateQrEndpointTests``     — the JWT-gated regenerate-qr action.
"""
import json
import logging
from uuid import uuid4

from django.test import TestCase, SimpleTestCase, override_settings
from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.diner_cap_config import resolve_diner_cap_key

from users_app.models import User
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, Table, MenuSection, MenuItem,
)
from orders_app.models import Order
from reviews_app.models import Review
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RESTAURANT_OWNER, RESTAURANT_STAFF,
    OrderStatus_Served, OrderStatus_Pending, OrderStatus_Initiated,
    TransactionType_OrderPayment, TransactionStatus_Success,
    TransactionPlatform_Web,
)
from restaurants_app.controllers.diner_capability import (
    issue_qr_credential, issue_table_session,
    resolve_qr_credential, resolve_table_session, require_table_session,
    DinerCapabilityError, DinerCapabilityDenied,
    QR_SALT, SESSION_SALT, CAPABILITY_VERSION,
    CREDENTIAL_HEADER, SESSION_HEADER,
)


SCAN_URL = '/api/v1/orders/journey/table-scan/'
ORDER_DETAILS_URL = '/api/v1/orders/journey/order-details/'
PAYMENT_DETAILS_URL = '/api/v1/orders/journey/payment-details/'
INITIATE_URL = '/api/v2/orders/initiate/'
SUBMIT_URL = '/api/v1/orders/submit/'
REVIEW_SUBMIT_URL = '/api/v1/reviews/submit/'
REGENERATE_QR_URL = '/api/v1/restaurant-setup/table-actions/regenerate-qr/'


def _header_kw(header_name, value):
    """Django test-client kwarg for a custom request header (X-A-B -> HTTP_X_A_B)."""
    return {'HTTP_' + header_name.upper().replace('-', '_'): value}


class DinerCapabilityTestBase(TestCase):
    """
    Two active restaurants. Restaurant A carries an owner, a tables-role staff, a
    published section+item and two tables; restaurant B carries an owner and one
    table (the cross-tenant target).
    """

    def setUp(self):
        # Restaurant A.
        self.owner_a = self._user('256700009001')
        self.restaurant_a = Restaurant.objects.create(
            name='Cap Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
            accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_OWNER],
        )
        # Staff with the tables-only role (grants MODULE_TABLES) — the staff
        # order-initiation path.
        self.staff_a = self._user('256700009002')
        RestaurantEmployee.objects.create(
            user=self.staff_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_STAFF],
        )
        self.section_a = MenuSection.objects.create(
            name='Cap Section A', restaurant=self.restaurant_a,
            approved=True, enabled=True,
        )
        self.item_a = MenuItem.objects.create(
            name='Cap Item A', section=self.section_a, primary_price=1000,
            approved=True, enabled=True,
        )
        self.table_a = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_a,
        )
        self.table_a2 = Table.objects.create(
            number=2, str_number='2', restaurant=self.restaurant_a,
        )

        # Restaurant B (cross-tenant).
        self.owner_b = self._user('256700009010')
        self.restaurant_b = Restaurant.objects.create(
            name='Cap Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
            accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )
        self.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant_b,
        )

    # --- fixtures / helpers ---------------------------------------------
    def _user(self, phone, roles=None):
        return User.objects.create_user(
            first_name='Cap', last_name='User', email=f'{phone}@test.com',
            phone_number=phone, username=phone, country='Uganda',
            password='password', roles=roles or [],
        )

    def _credential(self, table):
        return issue_qr_credential(table.restaurant_id, table.id, table.qr_version)

    def _session(self, table):
        return issue_table_session(table)

    def _jwt(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _make_order(self, restaurant, table, status=OrderStatus_Served):
        return Order.objects.create(
            restaurant=restaurant, table=table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            order_status=status,
        )


class DinerCapabilityModuleTests(DinerCapabilityTestBase):
    """Unit behaviour of the verifier module (no HTTP)."""

    def test_valid_credential_and_session_resolve_to_the_table(self):
        # Baseline: both tiers round-trip to the bound table.
        self.assertEqual(
            resolve_qr_credential(self._credential(self.table_a)).id,
            self.table_a.id,
        )
        self.assertEqual(
            resolve_table_session(self._session(self.table_a)).id,
            self.table_a.id,
        )

    # (3) malformed credential -> clean DinerCapabilityError (400), never a 500.
    def test_malformed_token_raises_capability_error(self):
        for junk in ('', '   ', 'not-a-token', 'a.b.c', None):
            with self.assertRaises(DinerCapabilityError) as ctx:
                resolve_qr_credential(junk)
            self.assertEqual(ctx.exception.status, 400)

    # (4) unknown table id is non-disclosing 404 — parity with an unavailable
    # table, so an attacker cannot tell "no such table" from "table off".
    def test_unknown_and_unavailable_are_both_denied_404(self):
        unknown = issue_qr_credential(self.restaurant_a.id, uuid4(), 1)
        with self.assertRaises(DinerCapabilityDenied) as unknown_ctx:
            resolve_qr_credential(unknown)
        self.assertEqual(unknown_ctx.exception.status, 404)

        self.table_a.enabled = False
        self.table_a.save(update_fields=['enabled'])
        with self.assertRaises(DinerCapabilityDenied) as off_ctx:
            resolve_qr_credential(self._credential(self.table_a))
        self.assertEqual(off_ctx.exception.status, 404)
        # Identical, non-disclosing message for both.
        self.assertEqual(unknown_ctx.exception.message, off_ctx.exception.message)

    # (5) a qr_version bump (regeneration) invalidates BOTH an old credential and
    # an old live session, while a freshly issued credential still resolves.
    def test_regeneration_invalidates_old_credential_and_session(self):
        old_credential = self._credential(self.table_a)
        old_session = self._session(self.table_a)
        # Sanity: both valid before the bump.
        self.assertEqual(resolve_qr_credential(old_credential).id, self.table_a.id)
        self.assertEqual(resolve_table_session(old_session).id, self.table_a.id)

        # Regenerate (bump the generation).
        self.table_a.qr_version = 2
        self.table_a.save(update_fields=['qr_version'])

        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(old_credential)
        with self.assertRaises(DinerCapabilityDenied):
            resolve_table_session(old_session)
        # The new-generation credential resolves.
        self.table_a.refresh_from_db()
        self.assertEqual(
            resolve_qr_credential(self._credential(self.table_a)).id,
            self.table_a.id,
        )

    # (7) an expired session is rejected (SignatureExpired -> friendly re-scan).
    def test_expired_session_is_rejected(self):
        token = self._session(self.table_a)
        with self.assertRaises(DinerCapabilityError) as ctx:
            # A negative max_age forces expiry deterministically (no clock mocking).
            resolve_table_session(token, max_age=-1)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn('expired', ctx.exception.message.lower())

    # (8) a session bound to table A does not resolve to table A2.
    def test_session_does_not_cross_to_another_table(self):
        resolved = resolve_table_session(self._session(self.table_a))
        self.assertEqual(resolved.id, self.table_a.id)
        self.assertNotEqual(resolved.id, self.table_a2.id)
        # A2's own session resolves to A2 — the binding is per-table.
        self.assertEqual(
            resolve_table_session(self._session(self.table_a2)).id,
            self.table_a2.id,
        )

    # (9) a session bound to a table in restaurant A never resolves into
    # restaurant B — the payload carries both r and t and both must match.
    def test_session_does_not_cross_to_another_restaurant(self):
        resolved = resolve_table_session(self._session(self.table_a))
        self.assertEqual(str(resolved.restaurant_id), str(self.restaurant_a.id))
        self.assertNotEqual(str(resolved.restaurant_id), str(self.restaurant_b.id))
        # A credential whose restaurant id is swapped to B (table id kept) fails
        # the (id, restaurant_id) filter -> denied.
        forged = issue_qr_credential(self.restaurant_b.id, self.table_a.id, 1)
        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(forged)

    # (17) a table deactivated/deleted/out-of-service AFTER a session is minted
    # invalidates that live session on the next use (re-checked, not cached).
    def test_live_state_change_invalidates_an_existing_session(self):
        for i, (field, value) in enumerate((
            ('deleted', True), ('enabled', False),
            ('is_active', False), ('status', 'out_of_service'),
        )):
            table = Table.objects.create(
                number=200 + i, str_number=str(200 + i),
                restaurant=self.restaurant_a,
            )
            token = self._session(table)
            self.assertEqual(resolve_table_session(token).id, table.id)  # live
            setattr(table, field, value)
            table.save(update_fields=[field])
            with self.assertRaises(DinerCapabilityDenied):
                resolve_table_session(token)

    # A QR credential replayed on the session channel (and vice-versa) fails on
    # the salt — the two tiers are not interchangeable.
    def test_salt_cross_use_is_rejected(self):
        qr_token = self._credential(self.table_a)
        session_token = self._session(self.table_a)
        # QR credential presented as a session -> BadSignature -> error.
        with self.assertRaises(DinerCapabilityError):
            resolve_table_session(qr_token)
        # Session presented as a QR credential -> BadSignature -> error.
        with self.assertRaises(DinerCapabilityError):
            resolve_qr_credential(session_token)

    def test_tokens_are_signed_not_encrypted_and_carry_no_secret(self):
        # The payload is readable (signed, not encrypted) — it binds r/t/generation
        # and must never contain a secret. Confirms the documented design.
        token = self._credential(self.table_a)
        payload = signing.loads(token, key=settings.DINER_CAP_KEY, salt=QR_SALT)
        self.assertEqual(payload['v'], CAPABILITY_VERSION)
        self.assertEqual(str(payload['r']), str(self.restaurant_a.id))
        self.assertEqual(str(payload['t']), str(self.table_a.id))
        self.assertEqual(payload['g'], 1)

    def test_tampered_payload_is_rejected(self):
        # Flipping a character breaks the signature.
        token = self._credential(self.table_a)
        tampered = token[:-3] + ('AAA' if not token.endswith('AAA') else 'BBB')
        with self.assertRaises(DinerCapabilityError):
            resolve_qr_credential(tampered)


class DinerTableScanCapabilityTests(DinerCapabilityTestBase):
    """The scan endpoint: credential in, session out."""

    def _scan(self, credential=None, table=None):
        extra = {}
        if credential is not None:
            extra = _header_kw(CREDENTIAL_HEADER, credential)
        query = f'?table={table}' if table is not None else ''
        return self.client.get(SCAN_URL + query, **extra)

    # (1)+(6) a valid QR credential scans and the response carries a session token
    # that itself resolves back to the same table.
    def test_valid_credential_scan_returns_session(self):
        resp = self._scan(credential=self._credential(self.table_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertIn('session_token', data)
        self.assertEqual(
            resolve_table_session(data['session_token']).id, self.table_a.id,
        )

    # (1, 2) a raw table UUID is NOT authority. Under the DEFAULT configuration —
    # there is no legacy grace flag any more — a raw ?table=<uuid>, even a real
    # one, mints no session. Only the signed credential is accepted.
    def test_raw_table_uuid_is_rejected(self):
        before = Order.objects.count()
        resp = self._scan(table=str(self.table_a.id))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())
        self.assertNotIn('session_token', json.dumps(resp.json()))
        self.assertEqual(Order.objects.count(), before)
        # The signed credential still works — the independent, only path.
        ok = self._scan(credential=self._credential(self.table_a))
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertIn('session_token', ok.json()['data'])

    def test_malformed_raw_table_is_rejected(self):
        resp = self._scan(table='not-a-uuid')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    def test_raw_table_in_body_is_rejected(self):
        # A raw table id in the request body is ignored (no body transport).
        resp = self.client.generic(
            'GET', SCAN_URL, data=json.dumps({'table': str(self.table_a.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    def test_missing_credential_returns_clean_400(self):
        resp = self._scan()  # no credential, no table
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    def test_no_legacy_scan_setting_exists(self):
        # There must be no feature flag that re-enables raw-table scanning.
        self.assertFalse(hasattr(settings, 'DINER_ALLOW_LEGACY_TABLE_SCAN'))

    # (3) malformed credential -> clean 400 (never 500).
    def test_malformed_credential_returns_400(self):
        resp = self._scan(credential='not-a-real-token')
        self.assertEqual(resp.status_code, 400, resp.content)

    # (4) a well-formed credential for an unknown table -> non-disclosing 404.
    def test_unknown_table_credential_returns_404(self):
        resp = self._scan(
            credential=issue_qr_credential(self.restaurant_a.id, uuid4(), 1),
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_stale_generation_credential_returns_404(self):
        # A credential minted at an old generation is rejected after a bump.
        credential = self._credential(self.table_a)
        self.table_a.qr_version = 5
        self.table_a.save(update_fields=['qr_version'])
        resp = self._scan(credential=credential)
        self.assertEqual(resp.status_code, 404, resp.content)

    # (20) the public scan payload leaks no PII (owner phone/email) and omits the
    # internal ops fields the diner never needs.
    def test_scan_response_exposes_no_pii(self):
        resp = self._scan(credential=self._credential(self.table_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.content.decode()
        self.assertNotIn(self.owner_a.phone_number, body)
        self.assertNotIn(self.owner_a.email, body)
        data = resp.json()['data']
        for leaked in ('is_active', 'enabled', 'has_qr', 'qr_regenerated_at',
                       'floor_x', 'floor_y'):
            self.assertNotIn(leaked, data)
        # The restaurant blob carries only diner-facing branding, never owner PII.
        self.assertNotIn('owner', data['restaurant'])
        self.assertNotIn('contact_phone', data['restaurant'])


class DinerInitiateCapabilityTests(DinerCapabilityTestBase):
    """v2 order initiation under the capability model."""

    def _initiate(self, body, session=None, jwt=None):
        extra = {}
        if session is not None:
            extra.update(_header_kw(SESSION_HEADER, session))
        if jwt is not None:
            extra.update(jwt)
        return self.client.post(
            INITIATE_URL, data=json.dumps(body),
            content_type='application/json', **extra,
        )

    # (10) the anonymous path derives restaurant+table from the SESSION — the
    # body need not (and here does not) carry them.
    def test_anonymous_initiate_derives_r_and_t_from_session(self):
        resp = self._initiate(
            {'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._session(self.table_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        self.assertEqual(str(order.restaurant_id), str(self.restaurant_a.id))
        self.assertEqual(str(order.table_id), str(self.table_a.id))

    def test_anonymous_initiate_requires_a_session(self):
        before = Order.objects.count()
        resp = self._initiate(
            {'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    # (11) a body restaurant/table that disagrees with the session is rejected —
    # the body can never override the session.
    def test_body_restaurant_mismatch_is_rejected(self):
        resp = self._initiate(
            {'restaurant': str(self.restaurant_b.id),
             'table': str(self.table_a.id),
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._session(self.table_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('restaurant', resp.json()['message'].lower())

    def test_body_table_mismatch_is_rejected(self):
        resp = self._initiate(
            {'restaurant': str(self.restaurant_a.id),
             'table': str(self.table_a2.id),
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._session(self.table_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('table', resp.json()['message'].lower())

    def test_matching_body_is_accepted(self):
        # The transitional shim allows a body that MATCHES the session.
        resp = self._initiate(
            {'restaurant': str(self.restaurant_a.id),
             'table': str(self.table_a.id),
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._session(self.table_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    # (12) the staff source='admin' path is unchanged: JWT + tables module, body
    # r/t, NO diner session.
    def test_staff_admin_source_initiation_intact(self):
        resp = self._initiate(
            {'source': 'admin', 'restaurant': str(self.restaurant_a.id),
             'table': str(self.table_a.id),
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            jwt=self._jwt(self.staff_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        self.assertEqual(order.created_by_id, self.staff_a.id)
        self.assertIsNone(order.customer_id)

    # (18) idempotency stays scoped to (restaurant, client_order_id): a same-key
    # retry returns the SAME order, not a duplicate.
    def test_idempotent_retry_same_client_order_id(self):
        body = {
            'client_order_id': str(uuid4()),
            'items': [{'item': str(self.item_a.id), 'quantity': 1}],
        }
        first = self._initiate(body, session=self._session(self.table_a))
        self.assertEqual(first.status_code, 200, first.content)
        before = Order.objects.count()
        second = self._initiate(body, session=self._session(self.table_a))
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            first.json()['data']['order_details']['id'],
            second.json()['data']['order_details']['id'],
        )
        self.assertEqual(Order.objects.count(), before)  # no duplicate row

    # (19) the table-claim serialization (PR #210) is intact under the session
    # path: two drafts on one table, the first submit claims it, the second 400s.
    def test_table_lock_serializes_two_submits(self):
        def initiate():
            resp = self._initiate(
                {'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
                session=self._session(self.table_a),
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            return resp.json()['data']['order_details']['id']

        order1, order2 = initiate(), initiate()

        def submit(order_id):
            return self.client.put(
                SUBMIT_URL, data=json.dumps({'order': order_id}),
                content_type='application/json',
                **_header_kw(SESSION_HEADER, self._session(self.table_a)),
            )

        first = submit(order1)
        self.assertEqual(first.status_code, 200, first.content)
        second = submit(order2)
        self.assertEqual(second.status_code, 400, second.content)
        # First claimed the table; the second draft stayed a draft.
        self.assertEqual(
            Order.objects.get(id=order1).order_status, OrderStatus_Pending,
        )
        self.assertEqual(
            Order.objects.get(id=order2).order_status, OrderStatus_Initiated,
        )


class DinerOrderDetailsCapabilityTests(DinerCapabilityTestBase):
    """order-details + submit BOLA fix: bound to the session's table."""

    def _order_details(self, order_id, session=None, jwt=None):
        extra = {}
        if session is not None:
            extra.update(_header_kw(SESSION_HEADER, session))
        if jwt is not None:
            extra.update(jwt)
        return self.client.get(f'{ORDER_DETAILS_URL}?order={order_id}', **extra)

    # (13) order-details requires a session, and the order must be on that
    # session's table.
    def test_order_details_requires_session(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        # No session at all -> 400 capability required.
        self.assertEqual(self._order_details(str(order.id)).status_code, 400)
        # Valid session bound to the order -> 200.
        ok = self._order_details(str(order.id), session=self._session(self.table_a))
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()['data']['id'], str(order.id))

    # (14) a foreign order (another table / restaurant) is a non-disclosing 404
    # even with a valid session — order-UUID knowledge is not authority.
    def test_foreign_order_returns_404(self):
        other_table_order = self._make_order(self.restaurant_a, self.table_a2)
        other_restaurant_order = self._make_order(self.restaurant_b, self.table_b)
        session = self._session(self.table_a)
        self.assertEqual(
            self._order_details(str(other_table_order.id), session=session).status_code,
            404,
        )
        self.assertEqual(
            self._order_details(str(other_restaurant_order.id), session=session).status_code,
            404,
        )

    # A staff JWT does NOT substitute for a diner session on this session-only
    # read — the capability channel governs, independent of request.user.
    def test_staff_jwt_without_session_is_not_authority_for_order_details(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        resp = self._order_details(str(order.id), jwt=self._jwt(self.owner_a))
        self.assertEqual(resp.status_code, 400, resp.content)

    # (7, endpoint) an expired session is rejected at the endpoint too.
    @override_settings(DINER_SESSION_TTL_SECONDS=-1)
    def test_expired_session_rejected_at_endpoint(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        resp = self._order_details(str(order.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_submit_requires_session_or_staff(self):
        # An initiated draft on table A.
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Initiated,
        )
        # Session bound to the order -> submit succeeds (initiated -> pending).
        resp = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(order.id)}),
            content_type='application/json',
            **_header_kw(SESSION_HEADER, self._session(self.table_a)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Order.objects.get(id=order.id).order_status, OrderStatus_Pending,
        )

    def test_submit_with_foreign_session_cannot_transition_order(self):
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Initiated,
        )
        # A session for a DIFFERENT table cannot drive this order's submit.
        resp = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(order.id)}),
            content_type='application/json',
            **_header_kw(SESSION_HEADER, self._session(self.table_a2)),
        )
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(
            Order.objects.get(id=order.id).order_status, OrderStatus_Initiated,
        )

    # (27) a valid staff JWT can submit WITHOUT a diner session — the separate,
    # explicit staff path stays functional.
    def test_staff_jwt_can_submit_without_a_session(self):
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Initiated,
        )
        resp = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(order.id)}),
            content_type='application/json', **self._jwt(self.staff_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Order.objects.get(id=order.id).order_status, OrderStatus_Pending,
        )

    # (28) an INVALID diner session present alongside a valid staff JWT is NOT
    # silently downgraded to staff auth — the session governs and its failure is
    # returned. The order must not transition.
    def test_invalid_session_does_not_fall_back_to_staff_on_submit(self):
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Initiated,
        )
        resp = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(order.id)}),
            content_type='application/json',
            **{**_header_kw(SESSION_HEADER, 'garbage-not-a-token'),
               **self._jwt(self.owner_a)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            Order.objects.get(id=order.id).order_status, OrderStatus_Initiated,
        )


class DinerReviewCapabilityTests(DinerCapabilityTestBase):
    """(15) a review needs an eligible (served/paid) order AND a bound session."""

    def _submit_review(self, order_id, session=None, rating=5):
        extra = {}
        if session is not None:
            extra.update(_header_kw(SESSION_HEADER, session))
        return self.client.post(
            REVIEW_SUBMIT_URL,
            data=json.dumps({'order': order_id, 'overall_rating': rating}),
            content_type='application/json', **extra,
        )

    def test_review_requires_a_session(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        resp = self._submit_review(str(order.id))  # no session
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Review.objects.filter(order_id=order.id).exists())

    def test_review_with_session_on_served_order_succeeds(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        resp = self._submit_review(str(order.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_review_ineligible_order_rejected_even_with_session(self):
        # A pending (not served/paid) order is not reviewable — the SALE_STATUSES
        # gate is preserved under the capability model.
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Pending,
        )
        resp = self._submit_review(str(order.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_review_of_foreign_order_is_404(self):
        # A served order on another table, reviewed with table A's session -> the
        # scoped lookup misses -> non-disclosing 404.
        foreign = self._make_order(self.restaurant_a, self.table_a2)
        resp = self._submit_review(str(foreign.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 404, resp.content)


class DinerPaymentDetailsCapabilityTests(DinerCapabilityTestBase):
    """(16) payment-details follows the same session rule; null-order -> 404."""

    def _make_txn(self, order=None, restaurant=None):
        return DinifyTransaction.objects.create(
            restaurant=restaurant or self.restaurant_a, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_platform=TransactionPlatform_Web,
            transaction_amount=1000,
        )

    def _payment_details(self, txn_id, session=None):
        extra = {}
        if session is not None:
            extra.update(_header_kw(SESSION_HEADER, session))
        return self.client.get(f'{PAYMENT_DETAILS_URL}?transaction={txn_id}', **extra)

    def test_payment_details_requires_session(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        txn = self._make_txn(order=order)
        self.assertEqual(self._payment_details(str(txn.id)).status_code, 400)

    def test_payment_details_for_own_table_order_succeeds(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        txn = self._make_txn(order=order)
        resp = self._payment_details(str(txn.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(str(resp.json()['data']['id']), str(txn.id))

    def test_payment_details_null_order_is_404(self):
        # A transaction with no order is not diner-scoped.
        txn = self._make_txn(order=None)
        resp = self._payment_details(str(txn.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_payment_details_foreign_order_is_404(self):
        foreign_order = self._make_order(self.restaurant_b, self.table_b)
        txn = self._make_txn(order=foreign_order, restaurant=self.restaurant_b)
        resp = self._payment_details(str(txn.id), session=self._session(self.table_a))
        self.assertEqual(resp.status_code, 404, resp.content)


class RegenerateQrEndpointTests(DinerCapabilityTestBase):
    """The JWT-gated regenerate-qr table action (owner/manager, tables module)."""

    def _regenerate(self, table, jwt=None):
        extra = jwt or {}
        return self.client.post(
            REGENERATE_QR_URL, data=json.dumps({'table_id': str(table.id)}),
            content_type='application/json', **extra,
        )

    def test_regenerate_bumps_version_and_returns_fresh_credential(self):
        old_credential = self._credential(self.table_a)
        resp = self._regenerate(self.table_a, jwt=self._jwt(self.owner_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertEqual(data['qr_version'], 2)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 2)
        self.assertIsNotNone(self.table_a.qr_regenerated_at)

        # The returned credential resolves; the pre-regen one no longer does.
        self.assertEqual(
            resolve_qr_credential(data['qr_credential']).id, self.table_a.id,
        )
        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(old_credential)

    def test_regenerate_invalidates_live_sessions(self):
        session = self._session(self.table_a)
        self.assertEqual(resolve_table_session(session).id, self.table_a.id)
        self._regenerate(self.table_a, jwt=self._jwt(self.owner_a))
        with self.assertRaises(DinerCapabilityDenied):
            resolve_table_session(session)

    def test_regenerate_unauthenticated_is_401(self):
        resp = self._regenerate(self.table_a)
        self.assertEqual(resp.status_code, 401, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 1)  # untouched

    def test_regenerate_cross_tenant_is_forbidden(self):
        # owner_b holds no role at restaurant A.
        resp = self._regenerate(self.table_a, jwt=self._jwt(self.owner_b))
        self.assertEqual(resp.status_code, 403, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 1)  # untouched

    def test_regenerate_unknown_table_is_404(self):
        resp = self.client.post(
            REGENERATE_QR_URL, data=json.dumps({'table_id': str(uuid4())}),
            content_type='application/json', **self._jwt(self.owner_a),
        )
        self.assertEqual(resp.status_code, 404, resp.content)


class DinerHeaderOnlyTransportTests(DinerCapabilityTestBase):
    """
    Bearer capabilities are honoured ONLY from their dedicated headers. The same
    token in a query string or request body must never grant authority — those
    channels leak into access logs, ``Referer`` headers and shared caches. The
    query/body fallbacks that once existed were removed.
    """

    def _make_txn(self, order):
        return DinifyTransaction.objects.create(
            restaurant=self.restaurant_a, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_platform=TransactionPlatform_Web, transaction_amount=1000,
        )

    # --- QR credential (scan) -------------------------------------------
    def test_credential_in_query_is_rejected(self):
        cred = self._credential(self.table_a)
        resp = self.client.get(f'{SCAN_URL}?credential={cred}')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    def test_credential_in_body_is_rejected(self):
        cred = self._credential(self.table_a)
        resp = self.client.generic(
            'GET', SCAN_URL, data=json.dumps({'credential': cred}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('data', resp.json())

    # --- session token (initiate: POST) ---------------------------------
    def test_session_in_query_is_rejected_on_initiate(self):
        session = self._session(self.table_a)
        before = Order.objects.count()
        resp = self.client.post(
            f'{INITIATE_URL}?session={session}',
            data=json.dumps({'items': [{'item': str(self.item_a.id), 'quantity': 1}]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)  # no order minted

    def test_session_in_body_is_rejected_on_initiate(self):
        session = self._session(self.table_a)
        before = Order.objects.count()
        resp = self.client.post(
            INITIATE_URL,
            data=json.dumps({'session': session,
                             'items': [{'item': str(self.item_a.id), 'quantity': 1}]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    # --- session token (order-details: GET) -----------------------------
    def test_session_in_query_is_rejected_on_order_details(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        session = self._session(self.table_a)
        resp = self.client.get(f'{ORDER_DETAILS_URL}?order={order.id}&session={session}')
        self.assertEqual(resp.status_code, 400, resp.content)

    # --- session token (payment-details: GET) ---------------------------
    def test_session_in_query_is_rejected_on_payment_details(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        txn = self._make_txn(order)
        session = self._session(self.table_a)
        resp = self.client.get(
            f'{PAYMENT_DETAILS_URL}?transaction={txn.id}&session={session}'
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # --- session token (review: POST body) ------------------------------
    def test_session_in_body_is_rejected_on_review(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        session = self._session(self.table_a)
        resp = self.client.post(
            REVIEW_SUBMIT_URL,
            data=json.dumps({'order': str(order.id), 'overall_rating': 5,
                             'session': session}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Review.objects.filter(order_id=order.id).exists())

    # --- session token (submit): no header -> staff fallback -> 400 ------
    def test_session_in_body_is_rejected_on_submit(self):
        order = self._make_order(
            self.restaurant_a, self.table_a, status=OrderStatus_Initiated,
        )
        session = self._session(self.table_a)
        resp = self.client.put(
            SUBMIT_URL,
            data=json.dumps({'order': str(order.id), 'session': session}),
            content_type='application/json',
        )
        # No session header + no staff JWT -> the "session required" 400.
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            Order.objects.get(id=order.id).order_status, OrderStatus_Initiated,
        )


class DinerResponseCacheTests(DinerCapabilityTestBase):
    """
    Capability-scoped responses must be non-cacheable (``no-store, private``) and
    Vary on the diner capability headers (#40-42). ``show-menu`` — public and
    session-free — is deliberately NOT stamped.
    """

    def _assert_no_store(self, resp):
        self.assertEqual(resp['Cache-Control'], 'no-store, private')
        self.assertEqual(resp['Pragma'], 'no-cache')
        self.assertEqual(resp['Expires'], '0')
        vary = resp.get('Vary', '')
        self.assertIn('X-Diner-Session', vary)
        self.assertIn('X-Diner-Credential', vary)

    def test_table_scan_response_is_no_store(self):
        resp = self.client.get(
            SCAN_URL,
            **_header_kw(CREDENTIAL_HEADER, self._credential(self.table_a)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_no_store(resp)

    def test_order_details_response_is_no_store(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        resp = self.client.get(
            f'{ORDER_DETAILS_URL}?order={order.id}',
            **_header_kw(SESSION_HEADER, self._session(self.table_a)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_no_store(resp)

    def test_payment_details_response_is_no_store(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        txn = DinifyTransaction.objects.create(
            restaurant=self.restaurant_a, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_platform=TransactionPlatform_Web, transaction_amount=1000,
        )
        resp = self.client.get(
            f'{PAYMENT_DETAILS_URL}?transaction={txn.id}',
            **_header_kw(SESSION_HEADER, self._session(self.table_a)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self._assert_no_store(resp)

    def test_scan_error_response_is_also_no_store(self):
        # The rejection path (a missing credential) is capability-scoped too.
        resp = self.client.get(SCAN_URL)
        self.assertEqual(resp.status_code, 400, resp.content)
        self._assert_no_store(resp)

    def test_show_menu_is_not_no_store(self):
        resp = self.client.get(
            f'/api/v1/orders/journey/show-menu/?restaurant={self.restaurant_a.id}'
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn('no-store', resp.get('Cache-Control', '') or '')


class DinerCapKeyConfigTests(SimpleTestCase):
    """
    Fail-closed resolution of DINER_CAP_KEY (dinify_backend.diner_cap_config).
    Pure-function tests — no DB, no settings re-import (#34-39).
    """

    def test_missing_key_in_production_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key(None, 'x' * 40, debug=False)
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key('', 'x' * 40, debug=False)
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key('   ', 'x' * 40, debug=False)

    def test_too_short_key_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key('short-key-under-32', 'x' * 40, debug=False)

    def test_key_equal_to_secret_fails_closed(self):
        secret = 'S' * 50
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key(secret, secret, debug=False)

    def test_placeholder_key_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key('changeme', 'x' * 40, debug=False)
        with self.assertRaises(ImproperlyConfigured):
            resolve_diner_cap_key('SECRET', 'x' * 40, debug=False)

    def test_valid_explicit_key_is_returned(self):
        key = 'a-strong-diner-cap-key-0123456789abcdef'
        self.assertEqual(resolve_diner_cap_key(key, 'x' * 40, debug=False), key)
        # Whitespace is trimmed.
        self.assertEqual(resolve_diner_cap_key(f'  {key}  ', 'x' * 40, debug=False), key)

    def test_debug_fallback_derives_key_and_warns(self):
        with self.assertWarns(UserWarning):
            derived = resolve_diner_cap_key(None, 'x' * 40, debug=True)
        # HMAC-SHA256 hexdigest.
        self.assertEqual(len(derived), 64)
        # Deterministic for a given secret.
        with self.assertWarns(UserWarning):
            self.assertEqual(derived, resolve_diner_cap_key(None, 'x' * 40, debug=True))

    def test_error_message_never_contains_the_key(self):
        secret = 'super-secret-value-that-must-not-leak-0123456789'
        with self.assertRaises(ImproperlyConfigured) as ctx:
            resolve_diner_cap_key(secret, secret, debug=False)
        self.assertNotIn(secret, str(ctx.exception))

    def test_settings_use_an_explicit_test_only_key(self):
        # test_settings.py supplies an explicit, non-derived key (not the DEBUG
        # fallback), unmistakably non-production.
        self.assertEqual(
            settings.DINER_CAP_KEY,
            'diner-cap-test-key-0123456789abcdef0123456789abcdef',
        )


class DinerCapabilityLoggingTests(DinerCapabilityTestBase):
    """Diner capability tokens must never be written to the logs (#43)."""

    def test_session_values_are_not_logged(self):
        order = self._make_order(self.restaurant_a, self.table_a)
        valid_session = self._session(self.table_a)
        marker = 'diner-token-marker-should-not-be-logged'

        # Capture on the root AND the app loggers directly, so a record is caught
        # whether or not the app loggers propagate to root (they do not).
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                try:
                    captured.append(record.getMessage())
                except Exception:  # pragma: no cover - defensive
                    captured.append(str(record.msg))

        handler = _Capture(level=logging.DEBUG)
        watched = [logging.getLogger()] + [
            logging.getLogger(name) for name in
            ('restaurants_app', 'orders_app', 'reviews_app', 'misc_app')
        ]
        prev_levels = [(lg, lg.level) for lg in watched]
        for lg in watched:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            # A successful request carrying a real session in the header.
            self.client.get(
                f'{ORDER_DETAILS_URL}?order={order.id}',
                **_header_kw(SESSION_HEADER, valid_session),
            )
            # A rejected request carrying a distinctive invalid token.
            self.client.get(
                f'{ORDER_DETAILS_URL}?order={order.id}',
                **_header_kw(SESSION_HEADER, marker),
            )
        finally:
            for lg in watched:
                lg.removeHandler(handler)
            for lg, level in prev_levels:
                lg.setLevel(level)

        blob = '\n'.join(captured)
        self.assertNotIn(valid_session, blob)
        self.assertNotIn(marker, blob)
