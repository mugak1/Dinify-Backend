import json
import uuid
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken
from users_app.models import User
from users_app.tests import seed_user
from finance_app.models import DinifyTransaction
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    PaymentMode_MobileMoney,
    RestaurantStatus_Active,
    RESTAURANT_OWNER,
    DINIFY_ADMIN,
)
from orders_app.tests import seed_order
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME,
)
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction

TEST_MSISDN = '256700000000'


# Patch targets for external I/O — mirrors the pattern in users_app/tests.py.
# The payment aggregators (Yo momo / DPO) were retired; only the SMS gateway and
# OTP mocks remain.
_PATCH_YO_SMS = 'users_app.controllers.otp_manager.send_sms'
_PATCH_MESSENGER_EMAIL = 'notifications_app.controllers.messenger.Messenger.send_email'
_PATCH_MESSENGER_SMS = 'notifications_app.controllers.messenger.Messenger.send_sms'
# OTP mocks — resend_otp is mocked to avoid the user=None crash where
# resend_otp tries to access user.phone_number when identification='msisdn'
_PATCH_OTP_RESEND = 'users_app.controllers.otp_manager.OtpManager.resend_otp'
_PATCH_OTP_MAKE = 'users_app.controllers.otp_manager.OtpManager.make_otp'
_PATCH_OTP_VERIFY = 'users_app.controllers.otp_manager.OtpManager.verify_otp'


@patch(_PATCH_OTP_VERIFY, return_value={
    'status': 200, 'message': 'Valid OTP', 'data': {'valid': True}
})
@patch(_PATCH_OTP_RESEND, return_value={
    'status': 200, 'message': 'OTP sent successfully'
})
@patch(_PATCH_OTP_MAKE, return_value=True)
@patch(_PATCH_MESSENGER_SMS, return_value=True)
@patch(_PATCH_MESSENGER_EMAIL, return_value=True)
@patch(_PATCH_YO_SMS, return_value=True)
class FinanceAppTestFunctions(TestCase):
    """
    Test functions for the Finance app.
    The payment-initiation flows are aggregator-free stubs (8a): initiate
    records a pending DinifyTransaction and returns a pending response with no
    aggregator call. SMS/OTP and Messenger calls are mocked at class level.
    """
    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

    def test_subscription_payment(self, *mocks):
        """Subscription initiate now stubs to a pending transaction (no aggregator call)."""
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        restaurant.subscription_validity = False
        restaurant.save()

        # Per-order subscription — should reject direct payment
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 400)

        # Switch to monthly subscription with flat fee
        restaurant.preferred_subscription_method = 'monthly'
        restaurant.flat_fee = Decimal('50000')
        restaurant.save()

        # Monthly MoMo subscription payment — pending stub
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=restaurant.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn=TEST_MSISDN
        )
        self.assertEqual(result['status'], 200)
        self.assertIn('transaction_id', result['data'])

        txs = DinifyTransaction.objects.get(id=result['data']['transaction_id'])
        self.assertEqual(txs.processing_status, ProcessingStatus_Pending)

        # The transaction is recorded against the restaurant (record-only
        # DinifyTransaction; the dinify_revenue account was removed in 8a).
        revenue_txs = DinifyTransaction.objects.filter(
            restaurant=restaurant
        )
        self.assertTrue(revenue_txs.exists())


class RetiredOrderPaymentRouteTests(TestCase):
    """The anonymous ``initiate-order-payment`` write path (OrderPaymentsEndpoint
    + OrderPaymentTransaction) was retired. The route must no longer resolve — a
    POST returns 404 (no route), not 500/200.
    """

    def test_initiate_order_payment_route_returns_404(self):
        response = self.client.post(
            '/api/v1/finances/initiate-order-payment/', {},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


@patch(_PATCH_OTP_VERIFY, return_value={
    'status': 200, 'message': 'Valid OTP', 'data': {'valid': True}
})
@patch(_PATCH_OTP_RESEND, return_value={
    'status': 200, 'message': 'OTP sent successfully'
})
@patch(_PATCH_OTP_MAKE, return_value=True)
@patch(_PATCH_MESSENGER_SMS, return_value=True)
@patch(_PATCH_MESSENGER_EMAIL, return_value=True)
@patch(_PATCH_YO_SMS, return_value=True)
class SubscriptionTransactionTenancyTests(TestCase):
    """
    POST /api/v1/finances/transactions/ (subscription) must be writable for a
    restaurant only by a dinify-admin or an active owner/manager of THAT
    restaurant. The endpoint was authenticated-only and trusted the body
    ``restaurant_id``, so any authenticated principal — a diner employed
    nowhere, or an employee of a DIFFERENT restaurant — could write a
    DinifyTransaction under any tenant (created_by=attacker, msisdn=attacker,
    amount=victim.flat_fee), polluting the victim owner's Reports. The edge gate
    denies non-members with 404 (existence non-disclosure, mirroring
    RestaurantReportsEndpoint); the controller's DoesNotExist/ValidationError
    guard turns a bad id into that same 404 instead of a 500.

    The SMS/OTP class mocks mirror FinanceAppTestFunctions; the subscription
    initiate path dispatches no SMS/OTP today, so they are inert here but keep
    the suite network-free if that changes.
    """

    URL = '/api/v1/finances/transactions/'

    def setUp(self):
        # Restaurant A — the target tenant. Monthly method + flat_fee set so a
        # successful initiate creates a pending DinifyTransaction.
        self.owner_a = make_user('256700000310')
        self.restaurant_a = Restaurant.objects.create(
            name='Tenancy Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
            preferred_subscription_method='monthly', flat_fee=Decimal('50000'),
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_OWNER],
        )
        # Restaurant B — a genuinely separate tenant with its own owner.
        self.owner_b = make_user('256700000320')
        self.restaurant_b = Restaurant.objects.create(
            name='Tenancy Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
            preferred_subscription_method='monthly', flat_fee=Decimal('40000'),
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )
        # Role-less authenticated diner (valid JWT, employed nowhere).
        self.diner = make_user('256700000330')
        # Dinify admin — the platform-wide bypass.
        self.admin = make_user('256700000340', roles=[DINIFY_ADMIN])

    # --- request helpers ------------------------------------------------
    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def post_subscription(self, user, restaurant_id, msisdn='256700000399'):
        body = {
            'transaction_type': 'subscription',
            'transaction_platform': 'web',
            'payment_mode': PaymentMode_MobileMoney,
            'msisdn': msisdn,
        }
        if restaurant_id is not None:
            body['restaurant_id'] = str(restaurant_id)
        return self.client.post(
            self.URL, data=json.dumps(body),
            content_type='application/json', **self.auth(user),
        )

    def a_count(self):
        return DinifyTransaction.objects.filter(restaurant=self.restaurant_a).count()

    # --- 1. non-member denied, no write ---------------------------------
    def test_non_member_denied(self, *mocks):
        before = self.a_count()
        resp = self.post_subscription(self.diner, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(self.a_count(), before)

    # --- 2. cross-tenant employee denied, no write ----------------------
    def test_cross_tenant_employee_denied(self, *mocks):
        before = self.a_count()
        resp = self.post_subscription(self.owner_b, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(self.a_count(), before)

    # --- 3. owner allowed -> exactly one row, created_by == owner -------
    def test_owner_allowed(self, *mocks):
        resp = self.post_subscription(self.owner_a, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = DinifyTransaction.objects.filter(restaurant=self.restaurant_a)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.created_by, self.owner_a)
        self.assertEqual(row.processing_status, ProcessingStatus_Pending)

    # --- 4. dinify-admin allowed ----------------------------------------
    def test_admin_allowed(self, *mocks):
        resp = self.post_subscription(self.admin, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.a_count(), 1)

    # --- 5. unknown id -> 404 not 500, no row ---------------------------
    def test_unknown_restaurant_id_is_404_not_500(self, *mocks):
        resp = self.post_subscription(self.admin, uuid.uuid4())
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    # --- 6. null restaurant_id -> gate fails closed, no row -------------
    def test_null_restaurant_id_denied(self, *mocks):
        resp = self.post_subscription(self.diner, None)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    # --- 7. non-disclosure: non-member and unknown-id are identical 404s -
    def test_non_member_and_unknown_id_are_identical_404(self, *mocks):
        non_member = self.post_subscription(self.diner, self.restaurant_a.id)
        unknown = self.post_subscription(self.admin, uuid.uuid4())
        self.assertEqual(non_member.status_code, 404, non_member.content)
        self.assertEqual(unknown.status_code, 404, unknown.content)
        # Byte-for-byte identical body: a caller cannot distinguish
        # "not allowed" from "doesn't exist".
        self.assertEqual(non_member.content, unknown.content)

    # --- 8. malformed (non-UUID) id -> 404 not 500 (no id shape 500s) ---
    def test_malformed_restaurant_id_is_404_not_500(self, *mocks):
        resp = self.post_subscription(self.admin, 'not-a-uuid')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(DinifyTransaction.objects.count(), 0)
