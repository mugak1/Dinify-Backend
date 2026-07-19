"""
Final adversarial tenant-isolation closure gate (TENANT-ISO-PR6A).

This module is the consolidated, readable *attack matrix* that proves the
launch-critical restaurant/diner boundary is closed at the real endpoint/service
chokepoints — the diner capability channel, QR rotation/revocation, diner
resource scoping, the canonical menu-publication + checkout policy, the modifier
order-integrity re-check (this PR's A0 fix), staff tenant isolation through the
RestaurantSetupEndpoint + Secretary, server-owned field mass-assignment, the
anonymous public-directory / PII posture, order idempotency + daily-counter
scoping, the subscription writer, and the get_detail dynamic dispatch.

It is INTENTIONALLY a high-value cross-cutting matrix, NOT a re-derivation of
every domain test. The closure CI gate (scripts/verify.sh + ci.yml) runs this
module ALONGSIDE the deep per-surface suites it builds on and does not replace:

    restaurants_app.tests_diner_capability            (capability depth)
    restaurants_app.tests_menu_relationship_integrity (relationship integrity)
    restaurants_app.tests_menu_relationships_concurrency (deterministic races)
    restaurants_app.tests_write_surface_tenancy       (write-surface / read_only)

Every class is tagged ``@tag('tenant_closure')`` so the gate can select it.

Threat actors exercised (task §3): unauthenticated caller; raw-UUID holder;
QR-credential holder; table-session holder; tampered/expired/rotated capability
holder; staff of A only; staff of A+B; module-scoped staff; owner; Dinify admin;
client submitting foreign nested ids / forbidden fields; idempotency replayer.

This is NOT a certification and NOT a penetration test — see
dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md for the assurance boundary.
"""
import json
import logging
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.test import TestCase, SimpleTestCase, override_settings, tag
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, Table, DiningArea,
    MenuSection, SectionGroup, MenuItem, UpsellConfig, UpsellItem,
)
from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from finance_app.models import DinifyTransaction
from reviews_app.models import Review
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.con_orders import ConOrder
from restaurants_app.controllers.diner_capability import (
    issue_qr_credential, issue_table_session,
    resolve_qr_credential, resolve_table_session,
    DinerCapabilityDenied, DinerCapabilityError,
    QR_SALT, SESSION_SALT, CAPABILITY_VERSION,
    CREDENTIAL_HEADER, SESSION_HEADER,
)
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RestaurantStatus_Pending, RestaurantStatus_Blocked,
    RESTAURANT_OWNER, RESTAURANT_STAFF, RESTAURANT_KITCHEN,
    DINIFY_ADMIN,
    OrderStatus_Served, OrderStatus_Pending, OrderStatus_Initiated,
    TransactionType_OrderPayment, TransactionStatus_Success,
    TransactionPlatform_Web, PaymentMode_MobileMoney,
)


# --- production routes the closure gate exercises (contract-parity source) -----
SCAN_URL = '/api/v1/orders/journey/table-scan/'
SHOW_MENU_URL = '/api/v1/orders/journey/show-menu/'
ORDER_DETAILS_URL = '/api/v1/orders/journey/order-details/'
PAYMENT_DETAILS_URL = '/api/v1/orders/journey/payment-details/'
INITIATE_URL = '/api/v2/orders/initiate/'
SUBMIT_URL = '/api/v1/orders/submit/'
REVIEW_SUBMIT_URL = '/api/v1/reviews/submit/'
REGENERATE_QR_URL = '/api/v1/restaurant-setup/table-actions/regenerate-qr/'
DETAIL_URL = '/api/v1/restaurant-setup/details/'
MISC_PUBLIC_RESTAURANTS_URL = '/api/v1/restaurant-setup/misc-public/restaurants/'
MISC_PUBLIC_TABLES_URL = '/api/v1/restaurant-setup/misc-public/tables/'
TRANSACTIONS_URL = '/api/v1/finances/transactions/'
PROFILE_URL = '/api/v1/users/user-profile/'


def _setup_url(config_detail):
    return f'/api/v1/restaurant-setup/{config_detail}/'


def _header_kw(header_name, value):
    """Django test-client kwarg for a custom request header."""
    return {'HTTP_' + header_name.upper().replace('-', '_'): value}


@tag('tenant_closure')
class ClosureFixtureBase(TestCase):
    """
    Two active restaurants with independent floors, menu graphs, staff, orders and
    transactions — the adversarial substrate every closure class reuses.

    Restaurant A: owner, tables-only staff, kitchen-only staff, a published
    section+group with a plain item, a modifier item, an extra + a parent that
    lists it, a hidden section, a hidden group and a hidden item, and two tables.
    Restaurant B: owner, published section+item+extra, and one table — the
    cross-tenant target every "A cannot reach B" assertion aims at.
    """

    @classmethod
    def setUpTestData(cls):
        # --- Restaurant A -----------------------------------------------------
        cls.owner_a = cls._user('256700010001')
        cls.restaurant_a = Restaurant.objects.create(
            name='Closure A', location='loc-a', status=RestaurantStatus_Active,
            owner=cls.owner_a, accepting_orders=True,
            preferred_subscription_method='monthly', flat_fee=Decimal('50000.00'),
        )
        RestaurantEmployee.objects.create(
            user=cls.owner_a, restaurant=cls.restaurant_a, roles=[RESTAURANT_OWNER],
        )
        cls.staff_a = cls._user('256700010002')           # tables-only by default
        RestaurantEmployee.objects.create(
            user=cls.staff_a, restaurant=cls.restaurant_a, roles=[RESTAURANT_STAFF],
        )
        cls.kitchen_a = cls._user('256700010003')          # kitchen-only
        RestaurantEmployee.objects.create(
            user=cls.kitchen_a, restaurant=cls.restaurant_a, roles=[RESTAURANT_KITCHEN],
        )

        cls.area_a = DiningArea.objects.create(name='A Hall', restaurant=cls.restaurant_a)
        cls.section_a = MenuSection.objects.create(
            name='A Section', restaurant=cls.restaurant_a,
            approved=True, enabled=True, available=True, availability='always',
        )
        cls.group_a = SectionGroup.objects.create(
            name='A Group', section=cls.section_a, approved=True, enabled=True,
        )
        cls.item_a = MenuItem.objects.create(
            name='A Item', section=cls.section_a, section_group=cls.group_a,
            primary_price=Decimal('1000'), approved=True, enabled=True,
            available=True, in_stock=True,
        )
        # Modifier item: one required single-choice group + one optional
        # multi-choice group (drives the A0 modifier tests).
        cls.item_mod = MenuItem.objects.create(
            name='A Modifier Item', section=cls.section_a,
            primary_price=Decimal('1000'), approved=True, enabled=True,
            available=True, in_stock=True,
            options={
                'hasModifiers': True,
                'groups': [
                    {'id': 'g-req', 'name': 'Base',
                     'minSelections': 1, 'maxSelections': 1,
                     'choices': [
                         {'id': 'c1', 'name': 'Plain', 'additionalCost': 0},
                         {'id': 'c2', 'name': 'Deluxe', 'additionalCost': 200},
                     ]},
                    {'id': 'g-multi', 'name': 'Add-ons',
                     'minSelections': 0, 'maxSelections': 2,
                     'choices': [
                         {'id': 'm1', 'name': 'Cheese', 'additionalCost': 500},
                         {'id': 'm2', 'name': 'Bacon', 'additionalCost': 300},
                     ]},
                ],
            },
        )
        # Extra + parent that lists it (drives extra-applicability tests).
        cls.extra_a = MenuItem.objects.create(
            name='A Extra', section=cls.section_a, primary_price=Decimal('500'),
            approved=True, enabled=True, available=True, in_stock=True, is_extra=True,
        )
        cls.plain_a = MenuItem.objects.create(       # same-restaurant, NOT an extra
            name='A Plain (not extra)', section=cls.section_a,
            primary_price=Decimal('700'), approved=True, enabled=True,
            available=True, in_stock=True,
        )
        cls.parent_a = MenuItem.objects.create(
            name='A Parent', section=cls.section_a, primary_price=Decimal('1500'),
            approved=True, enabled=True, available=True, in_stock=True,
            has_extras=True, extras_min_selections=0, extras_max_selections=2,
            extras_applicable=[str(cls.extra_a.id)],
        )
        # Unpublished graph inside A.
        cls.hidden_section = MenuSection.objects.create(
            name='A Hidden Section', restaurant=cls.restaurant_a,
            approved=False, enabled=True, available=True,
        )
        cls.item_in_hidden_section = MenuItem.objects.create(
            name='A Item In Hidden Section', section=cls.hidden_section,
            primary_price=Decimal('1000'), approved=True, enabled=True, available=True,
        )
        cls.hidden_group = SectionGroup.objects.create(
            name='A Hidden Group', section=cls.section_a, approved=False, enabled=True,
        )
        cls.item_in_hidden_group = MenuItem.objects.create(
            name='A Item In Hidden Group', section=cls.section_a,
            section_group=cls.hidden_group, primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True,
        )
        cls.hidden_item = MenuItem.objects.create(
            name='A Hidden Item', section=cls.section_a, primary_price=Decimal('1000'),
            approved=False, enabled=True, available=True,
        )
        cls.soldout_item = MenuItem.objects.create(
            name='A Sold Out Item', section=cls.section_a, primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True, in_stock=False,
        )

        cls.table_a = Table.objects.create(
            number=1, str_number='1', restaurant=cls.restaurant_a, dining_area=cls.area_a,
        )
        cls.table_a2 = Table.objects.create(
            number=2, str_number='2', restaurant=cls.restaurant_a, dining_area=cls.area_a,
        )

        # --- Restaurant B (cross-tenant target) -------------------------------
        cls.owner_b = cls._user('256700010010')
        cls.restaurant_b = Restaurant.objects.create(
            name='Closure B', location='loc-b', status=RestaurantStatus_Active,
            owner=cls.owner_b, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=cls.owner_b, restaurant=cls.restaurant_b, roles=[RESTAURANT_OWNER],
        )
        cls.area_b = DiningArea.objects.create(name='B Hall', restaurant=cls.restaurant_b)
        cls.section_b = MenuSection.objects.create(
            name='B Section', restaurant=cls.restaurant_b,
            approved=True, enabled=True, available=True, availability='always',
        )
        cls.item_b = MenuItem.objects.create(
            name='B Item', section=cls.section_b, primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        cls.extra_b = MenuItem.objects.create(
            name='B Extra', section=cls.section_b, primary_price=Decimal('500'),
            approved=True, enabled=True, available=True, in_stock=True, is_extra=True,
        )
        cls.table_b = Table.objects.create(
            number=1, str_number='1', restaurant=cls.restaurant_b, dining_area=cls.area_b,
        )

        # --- Dinify platform admin -------------------------------------------
        cls.admin = cls._user('256700010099', roles=[DINIFY_ADMIN])

    # --- fixture / request helpers -------------------------------------------
    @classmethod
    def _user(cls, phone, roles=None):
        return User.objects.create_user(
            first_name='Clos', last_name='User', email=f'{phone}@test.com',
            phone_number=phone, username=phone, country='Uganda',
            password='password', roles=roles or [],
        )

    def setUp(self):
        self.client = APIClient()

    def _jwt(self, user):
        return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}

    def _cred(self, table):
        table.refresh_from_db()
        return issue_qr_credential(table.restaurant_id, table.id, table.qr_version)

    def _sess(self, table):
        table.refresh_from_db()
        return issue_table_session(table)

    def _make_order(self, restaurant, table, status=OrderStatus_Served):
        return Order.objects.create(
            restaurant=restaurant, table=table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            order_status=status,
        )

    def _make_txn(self, restaurant, order=None):
        return DinifyTransaction.objects.create(
            restaurant=restaurant, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_platform=TransactionPlatform_Web, transaction_amount=1000,
        )

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

    def _setup_post(self, user, config_detail, body):
        return self.client.post(
            _setup_url(config_detail), data=body, format='json', **self._jwt(user),
        )

    def _setup_put(self, user, config_detail, body):
        return self.client.put(
            _setup_url(config_detail), data=body, format='json', **self._jwt(user),
        )

    def _setup_delete(self, user, config_detail, body):
        return self.client.delete(
            _setup_url(config_detail), data=json.dumps(body),
            content_type='application/json', **self._jwt(user),
        )

    def _setup_get(self, user, config_detail, query=''):
        return self.client.get(_setup_url(config_detail) + query, **self._jwt(user))

    def _detail(self, user, record, id):
        return self.client.get(
            f'{DETAIL_URL}?record={record}&id={id}', **self._jwt(user),
        )


# =============================================================================
# A. Capability-only anonymous entry (task §6.A). Depth lives in
#    tests_diner_capability; here is the high-signal cross-cutting smoke.
# =============================================================================
@tag('tenant_closure')
class AnonymousEntryClosureTests(ClosureFixtureBase):

    def test_raw_table_uuid_in_query_mints_no_session(self):
        before = Order.objects.count()
        resp = self.client.get(f'{SCAN_URL}?table={self.table_a.id}')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('session_token', json.dumps(resp.json()))
        self.assertEqual(Order.objects.count(), before)

    def test_raw_table_uuid_in_body_mints_no_session(self):
        resp = self.client.generic(
            'GET', SCAN_URL, data=json.dumps({'table': str(self.table_a.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertNotIn('session_token', json.dumps(resp.json()))

    def test_credential_in_query_or_body_is_rejected(self):
        cred = self._cred(self.table_a)
        self.assertEqual(self.client.get(f'{SCAN_URL}?credential={cred}').status_code, 400)
        resp = self.client.generic(
            'GET', SCAN_URL, data=json.dumps({'credential': cred}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_credential_header_only_mints_session(self):
        resp = self.client.get(
            SCAN_URL, **_header_kw(CREDENTIAL_HEADER, self._cred(self.table_a)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        token = resp.json()['data']['session_token']
        self.assertEqual(resolve_table_session(token).id, self.table_a.id)

    def test_session_in_query_is_rejected_downstream(self):
        session = self._sess(self.table_a)
        before = Order.objects.count()
        resp = self.client.post(
            f'{INITIATE_URL}?session={session}',
            data=json.dumps({'items': [{'item': str(self.item_a.id), 'quantity': 1}]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_credential_and_session_are_not_interchangeable(self):
        with self.assertRaises(DinerCapabilityError):
            resolve_table_session(self._cred(self.table_a))   # QR cred as session
        with self.assertRaises(DinerCapabilityError):
            resolve_qr_credential(self._sess(self.table_a))   # session as QR cred

    def test_tampered_and_expired_are_rejected(self):
        cred = self._cred(self.table_a)
        tampered = cred[:-3] + ('AAA' if not cred.endswith('AAA') else 'BBB')
        with self.assertRaises(DinerCapabilityError):
            resolve_qr_credential(tampered)
        with self.assertRaises(DinerCapabilityError):
            resolve_table_session(self._sess(self.table_a), max_age=-1)

    def test_unknown_and_foreign_are_non_disclosing_404(self):
        unknown = issue_qr_credential(self.restaurant_a.id, uuid4(), 1)
        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(unknown)
        # A credential whose restaurant is swapped to B (table id kept) fails the
        # (id, restaurant_id) filter -> denied, indistinguishable from unknown.
        forged = issue_qr_credential(self.restaurant_b.id, self.table_a.id, 1)
        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(forged)

    def test_invalid_session_does_not_fall_back_to_staff_jwt(self):
        order = self._make_order(self.restaurant_a, self.table_a, status=OrderStatus_Initiated)
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

    def test_capability_responses_are_no_store(self):
        resp = self.client.get(
            SCAN_URL, **_header_kw(CREDENTIAL_HEADER, self._cred(self.table_a)),
        )
        self.assertEqual(resp['Cache-Control'], 'no-store, private')
        self.assertIn('X-Diner-Session', resp.get('Vary', ''))

    def test_key_and_tokens_are_not_logged(self):
        session = self._sess(self.table_a)
        order = self._make_order(self.restaurant_a, self.table_a)
        captured = []

        class _Cap(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = _Cap(level=logging.DEBUG)
        watched = [logging.getLogger()] + [
            logging.getLogger(n) for n in ('restaurants_app', 'orders_app')
        ]
        prev = [(lg, lg.level) for lg in watched]
        for lg in watched:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            self.client.get(
                f'{ORDER_DETAILS_URL}?order={order.id}',
                **_header_kw(SESSION_HEADER, session),
            )
        finally:
            for lg in watched:
                lg.removeHandler(handler)
            for lg, level in prev:
                lg.setLevel(level)
        blob = '\n'.join(captured)
        self.assertNotIn(session, blob)
        self.assertNotIn(settings.DINER_CAP_KEY, blob)


# =============================================================================
# B. QR rotation revokes old capability + live sessions (task §6.B).
# =============================================================================
@tag('tenant_closure')
class QrRotationClosureTests(ClosureFixtureBase):

    def _regenerate(self, table, user):
        return self.client.post(
            REGENERATE_QR_URL, data=json.dumps({'table_id': str(table.id)}),
            content_type='application/json', **self._jwt(user),
        )

    def test_rotation_revokes_old_credential_and_session_new_works(self):
        old_cred = self._cred(self.table_a)
        old_session = self._sess(self.table_a)
        other_cred = self._cred(self.table_a2)   # a different table's credential

        resp = self._regenerate(self.table_a, self.owner_a)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertEqual(data['qr_version'], 2)   # incremented exactly once

        with self.assertRaises(DinerCapabilityDenied):
            resolve_qr_credential(old_cred)
        with self.assertRaises(DinerCapabilityDenied):
            resolve_table_session(old_session)
        # The fresh credential works; a DIFFERENT table's credential is untouched.
        self.assertEqual(resolve_qr_credential(data['qr_credential']).id, self.table_a.id)
        self.assertEqual(resolve_qr_credential(other_cred).id, self.table_a2.id)

    def test_ordinary_put_cannot_forge_qr_version_or_regenerated_at(self):
        resp = self._setup_put(self.owner_a, 'tables', {
            'id': str(self.table_a.id), 'display_name': 'Renamed',
            'qr_version': 99, 'qr_regenerated_at': '2020-01-01T00:00:00Z',
        })
        self.assertEqual(resp.json().get('status'), 200)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 1)          # unchanged
        self.assertIsNone(self.table_a.qr_regenerated_at)
        self.assertEqual(self.table_a.display_name, 'Renamed')  # real edit applied

    def test_caller_without_tables_module_cannot_rotate(self):
        # kitchen-only staff lacks MODULE_TABLES.
        resp = self._regenerate(self.table_a, self.kitchen_a)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 1)

    def test_foreign_table_cannot_be_rotated(self):
        resp = self._regenerate(self.table_a, self.owner_b)   # B's owner, A's table
        self.assertEqual(resp.status_code, 403, resp.content)
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.qr_version, 1)

    def test_rotation_response_never_exposes_the_cap_key(self):
        resp = self._regenerate(self.table_a, self.owner_a)
        self.assertNotIn(settings.DINER_CAP_KEY, resp.content.decode())


# =============================================================================
# C. Diner resource scoping — a session for A cannot reach B (task §6.C).
# =============================================================================
@tag('tenant_closure')
class DinerResourceScopingClosureTests(ClosureFixtureBase):

    def test_session_a_cannot_initiate_for_b_via_body_ids(self):
        before = Order.objects.count()
        resp = self._initiate(
            {'restaurant': str(self.restaurant_b.id), 'table': str(self.table_b.id),
             'items': [{'item': str(self.item_b.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)   # no state change

    def test_session_a_cannot_read_or_submit_b_order(self):
        b_order = self._make_order(self.restaurant_b, self.table_b, status=OrderStatus_Initiated)
        session = self._sess(self.table_a)
        details = self.client.get(
            f'{ORDER_DETAILS_URL}?order={b_order.id}', **_header_kw(SESSION_HEADER, session),
        )
        self.assertEqual(details.status_code, 404, details.content)
        submit = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(b_order.id)}),
            content_type='application/json', **_header_kw(SESSION_HEADER, session),
        )
        self.assertEqual(submit.status_code, 404, submit.content)
        self.assertEqual(
            Order.objects.get(id=b_order.id).order_status, OrderStatus_Initiated,
        )

    def test_session_a_cannot_read_b_payment(self):
        b_order = self._make_order(self.restaurant_b, self.table_b)
        txn = self._make_txn(self.restaurant_b, order=b_order)
        resp = self.client.get(
            f'{PAYMENT_DETAILS_URL}?transaction={txn.id}',
            **_header_kw(SESSION_HEADER, self._sess(self.table_a)),
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_session_a_cannot_review_b_order(self):
        b_order = self._make_order(self.restaurant_b, self.table_b)
        resp = self.client.post(
            REVIEW_SUBMIT_URL,
            data=json.dumps({'order': str(b_order.id), 'overall_rating': 5}),
            content_type='application/json',
            **_header_kw(SESSION_HEADER, self._sess(self.table_a)),
        )
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertFalse(Review.objects.filter(order_id=b_order.id).exists())

    def test_foreign_unknown_and_malformed_order_are_indistinguishable(self):
        session = self._sess(self.table_a)
        foreign = self._make_order(self.restaurant_a, self.table_a2)  # A, other table
        for order_id in (str(foreign.id), str(uuid4()), 'not-a-uuid'):
            resp = self.client.get(
                f'{ORDER_DETAILS_URL}?order={order_id}',
                **_header_kw(SESSION_HEADER, session),
            )
            self.assertEqual(resp.status_code, 404, f'{order_id}: {resp.content}')

    def test_diner_source_flag_cannot_enter_staff_branch(self):
        # A body source='admin' from a pure diner (only a session, no JWT) selects
        # the staff branch, which REQUIRES authentication — so the diner is bounced
        # (401) and gains no staff authority and creates no order. The staff-only
        # source flag can never be leveraged from the anonymous channel.
        before = Order.objects.count()
        resp = self._initiate(
            {'source': 'admin', 'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertEqual(Order.objects.count(), before)


# =============================================================================
# D. Canonical menu publication + checkout (task §6.D). The read side is proven
#    in tests_menu_publication_boundary; here is the CHECKOUT side + lifecycle.
# =============================================================================
@tag('tenant_closure')
class MenuPublicationCheckoutClosureTests(ClosureFixtureBase):

    def _order_one(self, item, session=None, **item_kw):
        body = {'items': [{'item': str(item.id), 'quantity': 1, **item_kw}]}
        return self._initiate(body, session=session or self._sess(self.table_a))

    def test_active_paused_restaurant_still_shows_menu(self):
        self.restaurant_a.accepting_orders = False
        self.restaurant_a.save(update_fields=['accepting_orders'])
        resp = self.client.get(f'{SHOW_MENU_URL}?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 200, resp.content)
        item_ids = {i['id'] for s in resp.json()['data'] for i in s['items']}
        self.assertIn(str(self.item_a.id), item_ids)

    def test_pending_restaurant_serves_no_anonymous_menu(self):
        self.restaurant_a.status = RestaurantStatus_Pending
        self.restaurant_a.save(update_fields=['status'])
        resp = self.client.get(f'{SHOW_MENU_URL}?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_hidden_section_group_item_cannot_be_ordered(self):
        session = self._sess(self.table_a)
        before = Order.objects.count()
        for item in (self.item_in_hidden_section, self.item_in_hidden_group, self.hidden_item):
            resp = self._order_one(item, session=session)
            self.assertEqual(resp.status_code, 400, f'{item.name}: {resp.content}')
        self.assertEqual(Order.objects.count(), before)   # nothing created

    def test_foreign_item_cannot_be_ordered_by_session(self):
        before = Order.objects.count()
        resp = self._order_one(self.item_b, session=self._sess(self.table_a))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_foreign_and_non_allowlisted_extras_are_rejected(self):
        session = self._sess(self.table_a)
        before = Order.objects.count()
        # a cross-tenant extra
        r1 = self._order_one(self.parent_a, session=session, extras=[str(self.extra_b.id)])
        # a same-restaurant item that is not in the parent's allowlist / not an extra
        r2 = self._order_one(self.parent_a, session=session, extras=[str(self.plain_a.id)])
        self.assertEqual(r1.status_code, 400, r1.content)
        self.assertEqual(r2.status_code, 400, r2.content)
        self.assertEqual(Order.objects.count(), before)

    def test_allowlisted_extra_is_accepted(self):
        resp = self._order_one(
            self.parent_a, session=self._sess(self.table_a), extras=[str(self.extra_a.id)],
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_staff_bypass_keeps_tenant_integrity(self):
        # Staff orders bypass diner publication but never tenant integrity:
        # staff A cannot order restaurant B's item onto an A table.
        before = Order.objects.count()
        resp = self._initiate(
            {'source': 'admin', 'restaurant': str(self.restaurant_a.id),
             'table': str(self.table_a.id),
             'items': [{'item': str(self.item_b.id), 'quantity': 1}]},
            jwt=self._jwt(self.owner_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_unpublish_after_read_blocks_new_checkout(self):
        # A genuinely new request after the item is unpublished is rejected in
        # the load-bearing in-transaction re-validation.
        session = self._sess(self.table_a)
        self.item_a.approved = False
        self.item_a.save(update_fields=['approved'])
        before = Order.objects.count()
        resp = self._order_one(self.item_a, session=session)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_sold_out_item_zero_and_flag_reconciliation(self):
        # An out-of-stock line is NOT hard-rejected: the order is created but the
        # line is zeroed + flagged unavailable (never prepared/charged).
        resp = self._order_one(self.soldout_item, session=self._sess(self.table_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        line = OrderItem.objects.get(order=order, item=self.soldout_item)
        self.assertFalse(line.available)
        self.assertEqual(line.actual_cost, 0)
        self.assertEqual(order.actual_cost, 0)


# =============================================================================
# A0. Modifier order-integrity — this PR's fix (task §7 modifier focus).
#     Group/choice validity + cost are re-derived in-transaction; the new fix
#     adds min/max completeness in-transaction and de-dupes duplicate choices.
# =============================================================================
@tag('tenant_closure')
class ModifierIntegrityClosureTests(ClosureFixtureBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A group requiring TWO distinct choices — proves a duplicate id cannot
        # satisfy a higher minimum (task canonicalization proof 4).
        cls.item_min2 = MenuItem.objects.create(
            name='A Min2 Modifier Item', section=cls.section_a,
            primary_price=Decimal('1000'), approved=True, enabled=True,
            available=True, in_stock=True,
            options={
                'hasModifiers': True,
                'groups': [
                    {'id': 'g2', 'name': 'Pick two',
                     'minSelections': 2, 'maxSelections': 3,
                     'choices': [
                         {'id': 'x1', 'name': 'Alpha', 'additionalCost': 0},
                         {'id': 'x2', 'name': 'Bravo', 'additionalCost': 0},
                         {'id': 'x3', 'name': 'Delta', 'additionalCost': 0},
                     ]},
                ],
            },
        )
        # A sold-out item that ALSO has a modifier group — proves the zero-and-flag
        # reconciliation is unchanged when canonical modifiers are present (proof 24).
        cls.item_soldout_mod = MenuItem.objects.create(
            name='A Sold Out Modifier Item', section=cls.section_a,
            primary_price=Decimal('1000'), approved=True, enabled=True,
            available=True, in_stock=False,
            options={
                'hasModifiers': True,
                'groups': [
                    {'id': 'g-req', 'name': 'Base',
                     'minSelections': 1, 'maxSelections': 1,
                     'choices': [
                         {'id': 'c1', 'name': 'Plain', 'additionalCost': 0},
                         {'id': 'c2', 'name': 'Deluxe', 'additionalCost': 200},
                     ]},
                ],
            },
        )

    def _service_create(self, selected_modifiers):
        """Call the internal service DIRECTLY (bypassing the endpoint preflight)
        to prove the in-transaction modifier re-check is load-bearing."""
        return _create_order(
            restaurant=self.restaurant_a, table=self.table_a,
            items=[{'item': str(self.item_mod.id), 'quantity': 1,
                    'selected_modifiers': selected_modifiers}],
            customer=None, created_by=None,
        )

    def _line(self, item, quantity=1, **extra):
        """Build one request line for a given MenuItem."""
        line = {'item': str(item.id), 'quantity': quantity}
        line.update(extra)
        return line

    def _create(self, items, created_by=None, client_order_id=None,
                restaurant=None, table=None):
        """Direct internal-service order create with an explicit items list — the
        load-bearing path with the endpoint preflight bypassed."""
        return _create_order(
            restaurant=restaurant or self.restaurant_a,
            table=table or self.table_a,
            items=items, customer=None, created_by=created_by,
            client_order_id=client_order_id,
        )

    def test_in_tx_min_violation_creates_no_order(self):
        before = Order.objects.count()
        result = self._service_create({'g-multi': ['m1']})   # required g-req omitted
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_in_tx_max_violation_creates_no_order(self):
        before = Order.objects.count()
        result = self._service_create({'g-req': ['c1', 'c2']})  # 2 > max 1
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_foreign_modifier_group_or_choice_is_rejected(self):
        before = Order.objects.count()
        self.assertNotEqual(self._service_create({'g-bogus': ['x'], 'g-req': ['c1']}).get('status'), 200)
        self.assertNotEqual(self._service_create({'g-req': ['bogus']}).get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_duplicate_choice_charged_once_and_does_not_trip_max(self):
        # g-multi max=2, cost(m1)=500. Three duplicates de-dupe to one selection:
        # accepted (count 1 <= 2) and charged exactly once (base 1000 + 500).
        resp = self._initiate(
            {'items': [{'item': str(self.item_mod.id), 'quantity': 1,
                        'selected_modifiers': {'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm1']}}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        self.assertEqual(order.actual_cost, Decimal('1500'))   # 1000 + 500 once

    def test_valid_modifiers_priced_from_server_side_options(self):
        resp = self._initiate(
            {'items': [{'item': str(self.item_mod.id), 'quantity': 1,
                        'selected_modifiers': {'g-req': ['c2'], 'g-multi': ['m1', 'm2']}}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        # 1000 base + 200 (c2) + 500 (m1) + 300 (m2)
        self.assertEqual(order.actual_cost, Decimal('2000'))

    def test_modifier_snapshot_derives_from_validated_parent(self):
        resp = self._initiate(
            {'items': [{'item': str(self.item_mod.id), 'quantity': 1,
                        'selected_modifiers': {'g-req': ['c2']}}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=resp.json()['data']['order_details']['id'])
        line = OrderItem.objects.get(order=order, item=self.item_mod)
        self.assertEqual(line.item_name_snapshot, self.item_mod.name)
        self.assertTrue(any('Deluxe' in s for s in (line.modifiers_snapshot or [])))

    # -- canonical persistence (proofs 1-3) --------------------------------
    def test_duplicate_choice_persisted_once(self):
        result = self._create([self._line(
            self.item_mod,
            selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1']})

    def test_duplicate_choice_charged_once(self):
        result = self._create([self._line(
            self.item_mod,
            selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.discounted_price, Decimal('1500'))   # 1000 + m1 500 once
        self.assertEqual(line.cost_of_options, Decimal('500'))

    def test_duplicate_choice_name_once_in_snapshot(self):
        result = self._create([self._line(
            self.item_mod,
            selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        addon = [s for s in line.modifiers_snapshot if s.startswith('Add-ons')]
        self.assertEqual(addon, ['Add-ons: Cheese'])   # not 'Cheese, Cheese'

    # -- duplicates cannot game min/max (proofs 4-5) -----------------------
    def test_duplicate_cannot_satisfy_higher_min(self):
        before = Order.objects.count()
        result = self._create([self._line(
            self.item_min2, selected_modifiers={'g2': ['x1', 'x1']})])  # dedupes to 1 < 2
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_duplicate_cannot_breach_max(self):
        # g-multi max 2: three raw ids but two DISTINCT stays within max, stored deduped.
        result = self._create([self._line(
            self.item_mod,
            selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm2']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1', 'm2']})

    # -- controlled 400s (proofs 6-10) -------------------------------------
    def test_unknown_group_endpoint_400(self):
        before = Order.objects.count()
        resp = self._initiate(
            {'items': [self._line(self.item_mod,
                                  selected_modifiers={'g-x': ['c1'], 'g-req': ['c1']})]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_unknown_choice_endpoint_400(self):
        before = Order.objects.count()
        resp = self._initiate(
            {'items': [self._line(self.item_mod, selected_modifiers={'g-req': ['nope']})]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_non_dict_selected_modifiers_400(self):
        before = Order.objects.count()
        result = self._create([self._line(self.item_mod, selected_modifiers='not-a-dict')])
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_non_list_group_selection_400(self):
        before = Order.objects.count()
        result = self._create([self._line(self.item_mod, selected_modifiers={'g-req': 'c1'})])
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    def test_modifiers_on_non_modifier_item_rejected(self):
        before = Order.objects.count()
        result = self._create([self._line(self.item_a, selected_modifiers={'g-x': ['y']})])
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    # -- empty / omitted (proof 11) ----------------------------------------
    def test_omitted_modifiers_on_plain_item_ok(self):
        result = self._create([self._line(self.item_a)])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_a)
        self.assertEqual(line.selected_modifiers, {})

    # -- ordering determinism (proofs 12-13) -------------------------------
    def test_group_key_ordering_does_not_change_persisted_state(self):
        result = self._create([self._line(
            self.item_mod, selected_modifiers={'g-multi': ['m1'], 'g-req': ['c1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1']})

    def test_choice_ordering_normalized_to_menu_definition(self):
        # Deterministic ordering = MenuItem.options definition order (m1 before m2),
        # regardless of the order the client submitted them in.
        result = self._create([self._line(
            self.item_mod, selected_modifiers={'g-req': ['c1'], 'g-multi': ['m2', 'm1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers['g-multi'], ['m1', 'm2'])

    # -- line identity (proofs 14-16) --------------------------------------
    def test_duplicate_and_canonical_merge_to_one_line(self):
        # Explicit end-to-end regression: same item submitted twice — once with
        # duplicate ids, once canonical with reordered keys — merges into ONE line.
        result = self._create([
            self._line(self.item_mod,
                       selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm1']}),
            self._line(self.item_mod,
                       selected_modifiers={'g-multi': ['m1'], 'g-req': ['c1']}),
        ])
        self.assertEqual(result.get('status'), 200, result)
        lines = OrderItem.objects.filter(order=result['order'], parent_item__isnull=True)
        self.assertEqual(lines.count(), 1)
        line = lines.first()
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1']})
        self.assertEqual(line.discounted_price, Decimal('1500'))   # 1000 + m1 500
        self.assertEqual(line.modifiers_snapshot.count('Add-ons: Cheese'), 1)

    def test_different_selections_stay_separate_lines(self):
        result = self._create([
            self._line(self.item_mod, selected_modifiers={'g-req': ['c1']}),
            self._line(self.item_mod, selected_modifiers={'g-req': ['c2']}),
        ])
        self.assertEqual(result.get('status'), 200, result)
        lines = OrderItem.objects.filter(order=result['order'], parent_item__isnull=True)
        self.assertEqual(lines.count(), 2)

    def test_legacy_duplicate_representation_tolerated(self):
        # A pre-canonical row (duplicate ids) still MERGES with a canonical incoming
        # selection — the tolerance that lets us ship WITHOUT a data migration.
        order = self._make_order(self.restaurant_a, self.table_a,
                                 status=OrderStatus_Initiated)
        legacy = OrderItem.objects.create(
            order=order, item=self.item_mod, quantity=1,
            unit_price=Decimal('1000'), discounted_price=Decimal('1500'),
            cost_of_options=Decimal('500'), unit_cost_of_options=Decimal('500'),
            total_cost=Decimal('1000'), discounted_cost=Decimal('1500'),
            savings=Decimal('0'), actual_cost=Decimal('1500'),
            selected_modifiers={'g-req': ['c1', 'c1'], 'g-multi': ['m1', 'm1']},
        )
        merged = ConOrder.add_order_item(
            item=self._line(self.item_mod,
                            selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1']}),
            order_id=str(order.id),
        )
        self.assertEqual(merged.get('status'), 200, merged)
        self.assertEqual(
            OrderItem.objects.filter(order=order, item=self.item_mod).count(), 1)
        legacy.refresh_from_db()
        self.assertEqual(legacy.quantity, 2)

    # -- service-level enforcement + rejection unwinds (proofs 17-20) ------
    def test_direct_service_call_normalizes(self):
        result = self._service_create({'g-req': ['c1'], 'g-multi': ['m1', 'm1', 'm1']})
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1']})

    def test_rejected_selection_leaves_no_order_item_or_counter(self):
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        counters_before = RestaurantDailyOrderCounter.objects.count()
        result = self._create([self._line(
            self.item_mod, selected_modifiers={'g-req': ['c1', 'c2']})])  # 2 > max 1
        self.assertNotEqual(result.get('status'), 200)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)
        self.assertEqual(RestaurantDailyOrderCounter.objects.count(), counters_before)

    # -- staff parity (proof 21) -------------------------------------------
    def test_staff_order_normalized_and_validated(self):
        ok = self._create(
            [self._line(self.item_mod,
                        selected_modifiers={'g-req': ['c1'], 'g-multi': ['m1', 'm1']})],
            created_by=self.owner_a,
        )
        self.assertEqual(ok.get('status'), 200, ok)
        line = OrderItem.objects.get(order=ok['order'], item=self.item_mod)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1'], 'g-multi': ['m1']})
        before = Order.objects.count()
        bad = self._create(
            [self._line(self.item_mod, selected_modifiers={'g-req': ['nope']})],
            created_by=self.owner_a, table=self.table_a2,
        )
        self.assertNotEqual(bad.get('status'), 200)
        self.assertEqual(Order.objects.count(), before)

    # -- idempotency first (proof 22) --------------------------------------
    def test_idempotent_replay_skips_modifier_revalidation(self):
        coid = str(uuid4())
        first = self._create(
            [self._line(self.item_mod, selected_modifiers={'g-req': ['c1']})],
            client_order_id=coid,
        )
        self.assertEqual(first.get('status'), 200, first)
        self.assertFalse(first.get('idempotent'))
        # Replay with a now-INVALID selection: must still return the original order
        # WITHOUT re-validating current modifier config.
        replay = self._create(
            [self._line(self.item_mod, selected_modifiers={'g-bogus': ['x']})],
            client_order_id=coid,
        )
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay.get('idempotent'))
        self.assertEqual(replay['order'].id, first['order'].id)

    # -- sold-out + extras unchanged (proofs 24-25) ------------------------
    def test_soldout_reconciliation_unchanged_with_modifiers(self):
        result = self._create([self._line(
            self.item_soldout_mod, selected_modifiers={'g-req': ['c1']})])
        self.assertEqual(result.get('status'), 200, result)
        line = OrderItem.objects.get(order=result['order'], item=self.item_soldout_mod)
        self.assertEqual(line.quantity, 0)
        self.assertEqual(line.status, 'unavailable')
        self.assertFalse(line.available)
        self.assertEqual(line.selected_modifiers, {'g-req': ['c1']})

    def test_extras_unchanged_alongside_modifiers(self):
        result = self._create([self._line(self.parent_a, extras=[str(self.extra_a.id)])])
        self.assertEqual(result.get('status'), 200, result)
        parent = OrderItem.objects.get(
            order=result['order'], item=self.parent_a, parent_item__isnull=True)
        children = OrderItem.objects.filter(order=result['order'], parent_item=parent)
        self.assertEqual(children.count(), 1)
        self.assertEqual(children.first().item_id, self.extra_a.id)
        self.assertEqual(parent.selected_modifiers, {})


# =============================================================================
# F. Staff tenant isolation through RestaurantSetupEndpoint + Secretary (§6.F).
# =============================================================================
@tag('tenant_closure')
class StaffTenantIsolationClosureTests(ClosureFixtureBase):

    def test_owner_a_lists_only_a_menu_items(self):
        resp = self._setup_get(self.owner_a, 'menuitems')
        ids = {i['id'] for i in resp.json()['data']['records']}
        self.assertIn(str(self.item_a.id), ids)
        self.assertNotIn(str(self.item_b.id), ids)

    def test_cross_tenant_detail_read_is_404(self):
        resp = self._detail(self.owner_a, 'menuitems', str(self.item_b.id))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_cross_tenant_update_by_id_is_denied(self):
        resp = self._setup_put(self.owner_a, 'menuitems', {
            'id': str(self.item_b.id), 'name': 'Hacked B Item',
        })
        self.assertEqual(resp.status_code, 403, resp.content)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.name, 'B Item')

    def test_cross_tenant_update_via_spoofed_restaurant_body_is_denied(self):
        # A create that names a foreign parent section while claiming A's restaurant
        # is resolved SERVER-SIDE from the section (B) and gated on B -> denied;
        # the spoofed restaurant=A in the body cannot smuggle access.
        resp = self._setup_post(self.owner_a, 'menuitems', {
            'restaurant': str(self.restaurant_a.id),   # spoof
            'section': str(self.section_b.id),          # actually B's section
            'name': 'Smuggled', 'primary_price': 100,
        })
        self.assertNotEqual(resp.json().get('status'), 200)
        self.assertFalse(MenuItem.objects.filter(name='Smuggled').exists())

    def test_cross_tenant_delete_is_denied(self):
        resp = self._setup_delete(self.owner_a, 'menuitems', {
            'id': str(self.item_b.id), 'deletion_reason': 'x',
        })
        self.assertNotIn(resp.status_code, (200,))
        self.item_b.refresh_from_db()
        self.assertFalse(self.item_b.deleted)

    def test_foreign_nested_fk_is_rejected(self):
        # A table create in A that references B's dining area is rejected.
        resp = self._setup_post(self.owner_a, 'tables', {
            'restaurant': str(self.restaurant_a.id), 'dining_area': str(self.area_b.id),
            'number': 99, 'min_capacity': 2, 'max_capacity': 4, 'shape': 'square',
        })
        self.assertEqual(resp.json().get('status'), 400)
        self.assertFalse(Table.objects.filter(dining_area=self.area_b, number='99').exists())

    def test_module_scoped_staff_cannot_write_outside_module(self):
        # tables-only staff can update an A table but not an A menu item.
        ok = self._setup_put(self.staff_a, 'tables', {
            'id': str(self.table_a.id), 'display_name': 'Staff-renamed',
        })
        self.assertEqual(ok.json().get('status'), 200)
        denied = self._setup_put(self.staff_a, 'menuitems', {
            'id': str(self.item_a.id), 'name': 'Staff hack',
        })
        self.assertEqual(denied.status_code, 403, denied.content)

    def test_admin_unrestricted_scope_reads_both_tenants(self):
        resp = self._setup_get(self.admin, 'menuitems')
        ids = {i['id'] for i in resp.json()['data']['records']}
        self.assertIn(str(self.item_a.id), ids)
        self.assertIn(str(self.item_b.id), ids)


# =============================================================================
# Bulk creation binds rows to the authorized restaurant (task §7 bulk-creation).
# =============================================================================
@tag('tenant_closure')
class BulkCreationTenantClosureTests(ClosureFixtureBase):

    def test_dining_area_bulk_tables_bound_to_authorized_restaurant(self):
        resp = self._setup_post(self.owner_a, 'diningareas', {
            'restaurant': str(self.restaurant_a.id), 'name': 'Bulk Area A',
            'smoking_zone': False, 'outdoor_seating': False,
            'create_tables': True, 'consideration': 'count', 'no_tables': 3,
        })
        self.assertEqual(resp.json().get('status'), 200, resp.content)
        area = DiningArea.objects.get(name='Bulk Area A', restaurant=self.restaurant_a)
        tables = Table.objects.filter(dining_area=area)
        self.assertGreaterEqual(tables.count(), 1)
        # Every bulk-created row is bound server-side to the authorized restaurant.
        self.assertTrue(all(t.restaurant_id == self.restaurant_a.id for t in tables))

    def test_cross_tenant_bulk_dining_area_is_denied(self):
        before_areas = DiningArea.objects.filter(restaurant=self.restaurant_b).count()
        before_tables = Table.objects.filter(restaurant=self.restaurant_b).count()
        resp = self._setup_post(self.owner_a, 'diningareas', {
            'restaurant': str(self.restaurant_b.id), 'name': 'Smuggled Area',
            'create_tables': True, 'consideration': 'count', 'no_tables': 3,
        })
        self.assertEqual(resp.status_code, 403, resp.content)
        # No area or table was created under B by A's owner.
        self.assertEqual(DiningArea.objects.filter(restaurant=self.restaurant_b).count(), before_areas)
        self.assertEqual(Table.objects.filter(restaurant=self.restaurant_b).count(), before_tables)
        self.assertFalse(DiningArea.objects.filter(name='Smuggled Area').exists())

    def test_section_group_bulk_bound_to_created_section(self):
        resp = self._setup_post(self.owner_a, 'menusections', {
            'restaurant': str(self.restaurant_a.id), 'name': 'Bulk Section A',
            'groups': "['Grp One', 'Grp Two']",
        })
        self.assertEqual(resp.json().get('status'), 200, resp.content)
        section = MenuSection.objects.get(id=resp.json()['data']['id'])
        self.assertEqual(section.restaurant_id, self.restaurant_a.id)
        groups = SectionGroup.objects.filter(section=section)
        self.assertEqual(groups.count(), 2)
        # Every bulk-created group is bound to the just-created, authorized section.
        self.assertTrue(all(g.section_id == section.id for g in groups))


# =============================================================================
# G. Mass assignment — server-owned / privilege fields never take effect (§6.G).
# =============================================================================
@tag('tenant_closure')
class MassAssignmentClosureTests(ClosureFixtureBase):

    def test_user_privilege_fields_are_not_mass_assignable(self):
        resp = self.client.put(
            PROFILE_URL,
            data=json.dumps({
                'first_name': 'Legit',
                'is_staff': True, 'is_superuser': True,
                'password': 'attacker-chosen', 'groups': [1], 'user_permissions': [1],
            }),
            content_type='application/json', **self._jwt(self.staff_a),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.staff_a.refresh_from_db()
        self.assertFalse(self.staff_a.is_staff)
        self.assertFalse(self.staff_a.is_superuser)
        self.assertTrue(self.staff_a.check_password('password'))   # unchanged
        self.assertEqual(self.staff_a.first_name, 'Legit')          # legit edit applied

    def test_restaurant_owner_and_audit_fields_are_not_writable(self):
        resp = self._setup_put(self.owner_a, 'restaurants', {
            'id': str(self.restaurant_a.id), 'name': 'Renamed A',
            'owner': str(self.owner_b.id), 'created_by': str(self.owner_b.id),
        })
        self.assertEqual(resp.json().get('status'), 200)
        self.restaurant_a.refresh_from_db()
        self.assertEqual(self.restaurant_a.owner_id, self.owner_a.id)   # unchanged
        self.assertEqual(self.restaurant_a.name, 'Renamed A')

    def test_platform_status_and_flat_fee_stripped_for_non_admin(self):
        resp = self._setup_put(self.owner_a, 'restaurants', {
            'id': str(self.restaurant_a.id), 'name': 'Renamed Again',
            'status': RestaurantStatus_Blocked, 'flat_fee': '0.00',
        })
        self.assertEqual(resp.json().get('status'), 200)
        self.restaurant_a.refresh_from_db()
        self.assertEqual(self.restaurant_a.status, RestaurantStatus_Active)   # not blocked
        self.assertEqual(self.restaurant_a.flat_fee, Decimal('50000.00'))     # not zeroed


# =============================================================================
# H. Public directory & PII posture (task §6.H).
# =============================================================================
@tag('tenant_closure')
class PublicDirectoryPiiClosureTests(ClosureFixtureBase):

    def test_soft_deleted_restaurant_hidden_regardless_of_deleted_param(self):
        deleted_restaurant = Restaurant.objects.create(
            name='Deleted Dir R', location='del-dir', status=RestaurantStatus_Active,
            owner=self.owner_a, deleted=True,
        )
        resp = self.client.get(f'{MISC_PUBLIC_RESTAURANTS_URL}?deleted=true')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.content.decode()
        self.assertNotIn(str(deleted_restaurant.id), body)

    def test_anonymous_table_directory_is_retired(self):
        resp = self.client.get(MISC_PUBLIC_TABLES_URL)
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_public_directory_exposes_no_owner_pii(self):
        resp = self.client.get(MISC_PUBLIC_RESTAURANTS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.content.decode()
        self.assertNotIn(self.owner_a.email, body)
        self.assertNotIn(self.owner_a.phone_number, body)

    def test_scan_payload_carries_only_safe_fields(self):
        resp = self.client.get(
            SCAN_URL, **_header_kw(CREDENTIAL_HEADER, self._cred(self.table_a)),
        )
        data = resp.json()['data']
        for internal in ('is_active', 'enabled', 'has_qr', 'qr_regenerated_at',
                         'floor_x', 'floor_y'):
            self.assertNotIn(internal, data)
        self.assertNotIn('owner', data['restaurant'])

    def test_public_menu_exposes_no_internal_fields(self):
        resp = self.client.get(f'{SHOW_MENU_URL}?restaurant={self.restaurant_a.id}')
        items = [i for s in resp.json()['data'] for i in s['items']]
        self.assertTrue(items)
        for item in items:
            for internal in ('created_by', 'deleted', 'deleted_by',
                             'time_deleted', 'deletion_reason'):
                self.assertNotIn(internal, item)


# =============================================================================
# Idempotency + daily-counter scoping (task §7 #16/#17).
# =============================================================================
@tag('tenant_closure')
class IdempotencyCounterScopingClosureTests(ClosureFixtureBase):

    def test_same_client_order_id_is_independent_across_tenants(self):
        shared_key = str(uuid4())
        # A order under the shared key (via session A).
        a = self._initiate(
            {'client_order_id': shared_key,
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(a.status_code, 200, a.content)
        a_id = a.json()['data']['order_details']['id']
        # B order under the SAME key (via session B) — a distinct order, not a replay.
        b = self._initiate(
            {'client_order_id': shared_key,
             'items': [{'item': str(self.item_b.id), 'quantity': 1}]},
            session=self._sess(self.table_b),
        )
        self.assertEqual(b.status_code, 200, b.content)
        b_id = b.json()['data']['order_details']['id']
        self.assertNotEqual(a_id, b_id)
        self.assertEqual(Order.objects.get(id=a_id).restaurant_id, self.restaurant_a.id)
        self.assertEqual(Order.objects.get(id=b_id).restaurant_id, self.restaurant_b.id)

    def test_replay_returns_own_tenant_order_only(self):
        shared_key = str(uuid4())
        first = self._initiate(
            {'client_order_id': shared_key,
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        replay = self._initiate(
            {'client_order_id': shared_key,
             'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        self.assertEqual(
            first.json()['data']['order_details']['id'],
            replay.json()['data']['order_details']['id'],
        )

    def test_daily_counters_are_per_restaurant(self):
        self._initiate(
            {'items': [{'item': str(self.item_a.id), 'quantity': 1}]},
            session=self._sess(self.table_a),
        )
        self._initiate(
            {'items': [{'item': str(self.item_b.id), 'quantity': 1}]},
            session=self._sess(self.table_b),
        )
        counters = RestaurantDailyOrderCounter.objects.filter(
            restaurant__in=[self.restaurant_a, self.restaurant_b],
        )
        self.assertEqual(counters.count(), 2)   # a distinct counter row per tenant


# =============================================================================
# Subscription writer (task §7 #9/#11).
# =============================================================================
@tag('tenant_closure')
class SubscriptionTransactionClosureTests(ClosureFixtureBase):

    def _subscribe(self, user, restaurant_id, **over):
        body = {'transaction_type': 'subscription', 'restaurant_id': str(restaurant_id),
                'transaction_platform': TransactionPlatform_Web,
                'payment_mode': PaymentMode_MobileMoney}
        body.update(over)
        return self.client.post(
            TRANSACTIONS_URL, data=json.dumps(body),
            content_type='application/json', **self._jwt(user),
        )

    def test_endpoint_gates_on_manage_and_amount_is_server_derived(self):
        resp = self._subscribe(self.owner_a, self.restaurant_a.id, transaction_amount='1')
        self.assertEqual(resp.status_code, 200, resp.content)
        txn = DinifyTransaction.objects.get(id=resp.json()['data']['transaction_id'])
        # Amount comes from restaurant.flat_fee, never the client body's '1'.
        self.assertEqual(txn.transaction_amount, Decimal('50000.00'))
        self.assertEqual(txn.restaurant_id, self.restaurant_a.id)

    def test_non_manager_cannot_bill_a_restaurant(self):
        # owner_b holds no role at A; kitchen staff at A is not a manager.
        self.assertEqual(self._subscribe(self.owner_b, self.restaurant_a.id).status_code, 404)
        self.assertEqual(self._subscribe(self.kitchen_a, self.restaurant_a.id).status_code, 404)

    def test_cross_tenant_restaurant_id_is_not_billable(self):
        before = DinifyTransaction.objects.filter(restaurant=self.restaurant_b).count()
        resp = self._subscribe(self.owner_a, self.restaurant_b.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(
            DinifyTransaction.objects.filter(restaurant=self.restaurant_b).count(), before,
        )


# =============================================================================
# get_detail dynamic dispatch (task §7 #15).
# =============================================================================
@tag('tenant_closure')
class GetDetailDispatchClosureTests(ClosureFixtureBase):

    def test_own_record_detail_succeeds(self):
        resp = self._detail(self.owner_a, 'menuitems', str(self.item_a.id))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['id'], str(self.item_a.id))

    def test_cross_tenant_and_unknown_are_indistinguishable_404(self):
        cross = self._detail(self.owner_a, 'menuitems', str(self.item_b.id))
        unknown = self._detail(self.owner_a, 'menuitems', str(uuid4()))
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(unknown.status_code, 404)

    def test_unknown_record_type_is_not_dispatched(self):
        resp = self._detail(self.owner_a, 'orders', str(uuid4()))
        # 'orders' is not in the get_detail serializer allowlist -> 404, never a 500.
        self.assertEqual(resp.status_code, 404, resp.content)


# =============================================================================
# §14 cross-repository contract parity — the constants/routes this repo OWNS.
# The frontend asserts the identical values in its own closure spec (no runtime
# cross-repo import). If either side drifts, one of the two gates fails.
# =============================================================================
@tag('tenant_closure')
class ContractParityClosureTests(SimpleTestCase):

    def test_capability_header_names(self):
        self.assertEqual(CREDENTIAL_HEADER, 'X-Diner-Credential')
        self.assertEqual(SESSION_HEADER, 'X-Diner-Session')

    def test_capability_salts_are_separated(self):
        self.assertNotEqual(QR_SALT, SESSION_SALT)
        self.assertEqual(CAPABILITY_VERSION, 1)

    def test_scan_and_session_gated_routes(self):
        self.assertEqual(SCAN_URL, '/api/v1/orders/journey/table-scan/')
        for route in (ORDER_DETAILS_URL, PAYMENT_DETAILS_URL, INITIATE_URL,
                      SUBMIT_URL, REVIEW_SUBMIT_URL):
            self.assertTrue(route.startswith('/api/'))

    def test_credential_and_session_are_never_encrypted(self):
        # The tokens are signed, not encrypted — a parity check that the design
        # binds r/t/generation in a readable payload (no secret embedded).
        self.assertTrue(QR_SALT.startswith('dinify.diner'))
        self.assertTrue(SESSION_SALT.startswith('dinify.diner'))
