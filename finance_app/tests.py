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
    # ProcessingStatus_Pending is no longer imported: it existed only for the
    # fake-success assertions D07 replaced. The model default is untouched.
    PaymentMode_MobileMoney,
    RestaurantStatus_Live,
    RESTAURANT_OWNER,
)
from orders_app.tests import seed_order
from platform_admin_app.testing import give_legacy_platform_role
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME,
)
from finance_app.controllers.tx_subscription import (
    SubscriptionPaymentTransaction,
    REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
    MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
)

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

    def test_subscription_collection_is_refused_whatever_the_legacy_plan_says(self, *mocks):
        """D07 REGRESSION (was a fake-success characterization).

        This test used to assert the two shapes D07 removed: a ``per_order``
        BUSINESS refusal, and a 200 booking a Pending row for a monthly plan.
        Both are gone. The plan column no longer decides anything, because there
        is no 200 to gate — offering "switch to monthly" as the way past a
        refusal would point at machinery that does not exist.
        """
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        restaurant.subscription_validity = False
        restaurant.save()

        def refuse(expected_plan):
            result = SubscriptionPaymentTransaction().initiate(
                restaurant_id=restaurant.id,
                transaction_platform='web',
                payment_mode=PaymentMode_MobileMoney,
                user=None,
                msisdn=TEST_MSISDN,
            )
            self.assertEqual(
                result['status'], 501,
                f'plan={expected_plan}: expected the unimplemented-collection '
                f'refusal, got {result}',
            )
            self.assertEqual(
                result['reason'], REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE)
            self.assertEqual(
                result['message'], MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE)
            # It answers about the request, not about a transaction.
            self.assertNotIn('data', result)
            return result

        # The DEFAULT plan every supported creation path produces.
        self.assertEqual(restaurant.preferred_subscription_method, 'per_order')
        refuse('per_order')

        # And the plan that used to unlock the fake success. Written directly
        # because no supported path on this plane can set it (restaurant_setup
        # strips it from every PUT) — which is exactly why it is worth pinning.
        restaurant.preferred_subscription_method = 'monthly'
        restaurant.flat_fee = Decimal('50000')
        restaurant.save()
        refuse('monthly')

        restaurant.preferred_subscription_method = 'yearly'
        restaurant.save()
        refuse('yearly')

        # Nothing was recorded by any of the three.
        self.assertFalse(
            DinifyTransaction.objects.filter(restaurant=restaurant).exists())


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
    restaurant only by an active owner/manager of THAT restaurant. The endpoint
    was authenticated-only and trusted the body
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
            status=RestaurantStatus_Live, owner=self.owner_a,
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
            status=RestaurantStatus_Live, owner=self.owner_b,
            preferred_subscription_method='monthly', flat_fee=Decimal('40000'),
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )
        # Role-less authenticated diner (valid JWT, employed nowhere).
        self.diner = make_user('256700000330')
        # An account carrying the LEGACY platform role string. It used to be the
        # platform-wide bypass here (can_manage_restaurant returned True for it at
        # every restaurant); it now resolves like any other stranger. Written
        # through the ORM because the write paths refuse the string outright.
        self.legacy_role_holder = give_legacy_platform_role(
            make_user('256700000340'))

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

    # --- 3. owner reaches the capability answer, and books nothing -------
    def test_owner_is_authorized_and_still_gets_the_unavailable_answer(self, *mocks):
        """D07 REGRESSION (was ``test_owner_allowed``, a fake-success characterization).

        The AUTHORIZATION half is unchanged and still pinned: an owner of THIS
        restaurant clears the gate, where the four denials below do not. What
        changed is what lies past the gate — the collector is unimplemented, so
        the authorized caller gets 501 and no row is written.
        """
        resp = self.post_subscription(self.owner_a, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 501, resp.content)
        body = resp.json()
        self.assertEqual(body['status'], 501)
        self.assertEqual(
            body['reason'], REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE)
        self.assertEqual(
            body['message'], MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE)
        # The owner got PAST the gate — that is what separates this from the
        # denial tests, and asserting only "no row" would not show it.
        self.assertNotEqual(resp.status_code, 404)
        self.assertEqual(
            DinifyTransaction.objects.filter(restaurant=self.restaurant_a).count(), 0)

    def test_the_refusal_is_not_cacheable(self, *mocks):
        resp = self.post_subscription(self.owner_a, self.restaurant_a.id)
        self.assertEqual(resp['Cache-Control'], 'no-store, private')
        self.assertEqual(resp['Pragma'], 'no-cache')
        self.assertIn('Authorization', resp['Vary'])

    def test_no_retry_affordance_is_offered(self, *mocks):
        """A Retry-After would say waiting implements a collector. Nothing waits."""
        resp = self.post_subscription(self.owner_a, self.restaurant_a.id)
        self.assertNotIn('Retry-After', resp)
        body = resp.json()
        for word in ('retry', 'try again', 'later', 'temporarily'):
            self.assertNotIn(word, body['message'].lower(), body['message'])

    def test_repetition_stays_side_effect_free(self, *mocks):
        """No refusal ledger, no dedup machinery needed — just nothing, five times."""
        for _ in range(5):
            resp = self.post_subscription(self.owner_a, self.restaurant_a.id)
            self.assertEqual(resp.status_code, 501, resp.content)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_no_tender_string_reaches_a_successful_response(self, *mocks):
        """The old path validated no tender at all and persisted whatever it got."""
        for mode in ('momo', 'card', 'cash', 'bitcoin', '', None):
            body = {
                'transaction_type': 'subscription',
                'transaction_platform': 'web',
                'payment_mode': mode,
                'restaurant_id': str(self.restaurant_a.id),
                'msisdn': '256700000399',
            }
            resp = self.client.post(
                self.URL, data=json.dumps(body),
                content_type='application/json', **self.auth(self.owner_a),
            )
            self.assertEqual(resp.status_code, 501, f'{mode!r}: {resp.content}')
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_an_msisdn_is_never_stored(self, *mocks):
        self.post_subscription(
            self.owner_a, self.restaurant_a.id, msisdn='256700000777')
        self.assertFalse(
            DinifyTransaction.objects.filter(msisdn='256700000777').exists())
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_the_direct_in_process_call_is_refused_too(self, *mocks):
        """A UI-only or endpoint-only disable would leave this open.

        ``user=None`` is the exact shape that used to persist a row with no
        attribution at all.
        """
        result = SubscriptionPaymentTransaction().initiate(
            restaurant_id=self.restaurant_a.id,
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=None,
            msisdn='256700000399',
            otp='1234',
        )
        self.assertEqual(result['status'], 501)
        self.assertEqual(
            result['reason'], REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_a_malformed_or_unknown_id_still_404s_from_the_service_itself(self, *mocks):
        """The service self-guards; it does not rely on its caller's gate.

        Both answers must stay the endpoint gate's opaque 404 — not a 501, which
        would tell an unauthorized caller that the restaurant exists.
        """
        for bad in ('not-a-uuid', str(uuid.uuid4())):
            result = SubscriptionPaymentTransaction().initiate(
                restaurant_id=bad, transaction_platform='web',
                payment_mode=PaymentMode_MobileMoney, user=None,
            )
            self.assertEqual(result['status'], 404, f'{bad!r}: {result}')
            self.assertNotIn('reason', result)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_the_refusal_sends_no_sms_email_or_otp(self, *mocks):
        """The class mocks are asserted on, not merely installed.

        Decorator order is bottom-up, so *mocks arrives as
        (yo_sms, messenger_email, messenger_sms, otp_make, otp_resend, otp_verify).
        """
        self.post_subscription(self.owner_a, self.restaurant_a.id)
        for m in mocks:
            self.assertEqual(
                m.call_count, 0,
                f'{getattr(m, "_mock_name", m)} was called by a refused request')

    # --- 4. the legacy platform role grants nothing ---------------------
    def test_legacy_platform_role_denied(self, *mocks):
        # Was `test_admin_allowed`: a dinify_admin role string used to write a
        # subscription transaction under ANY tenant. It is now denied exactly like
        # the role-less diner above, and writes nothing.
        before = self.a_count()
        resp = self.post_subscription(self.legacy_role_holder, self.restaurant_a.id)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(self.a_count(), before)

    def test_legacy_platform_role_is_indistinguishable_from_a_stranger(self, *mocks):
        holder = self.post_subscription(self.legacy_role_holder, self.restaurant_a.id)
        stranger = self.post_subscription(self.diner, self.restaurant_a.id)
        self.assertEqual(holder.status_code, stranger.status_code)
        self.assertEqual(holder.content, stranger.content)

    # --- 5. unknown id -> 404 not 500, no row ---------------------------
    def test_unknown_restaurant_id_is_404_not_500(self, *mocks):
        # An owner (a principal the gate would otherwise admit) naming a
        # restaurant that does not exist. can_manage_restaurant fails closed on an
        # unresolvable id, so this 404s at the gate rather than reaching the
        # controller — the client-visible outcome is unchanged, and still not 500.
        resp = self.post_subscription(self.owner_a, uuid.uuid4())
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
        unknown = self.post_subscription(self.owner_a, uuid.uuid4())
        self.assertEqual(non_member.status_code, 404, non_member.content)
        self.assertEqual(unknown.status_code, 404, unknown.content)
        # Byte-for-byte identical body: a caller cannot distinguish
        # "not allowed" from "doesn't exist".
        self.assertEqual(non_member.content, unknown.content)

    # --- 8. malformed (non-UUID) id -> 404 not 500 (no id shape 500s) ---
    def test_malformed_restaurant_id_is_404_not_500(self, *mocks):
        resp = self.post_subscription(self.owner_a, 'not-a-uuid')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(DinifyTransaction.objects.count(), 0)


class TheDisclosureAndTheBoundaryAgreeTests(TestCase):
    """
    ONE capability fact, published in two places, pinned so they cannot drift.

    ``finance_app.subscription_capability.IN_APP_COLLECTION_SUPPORTED`` is what the
    restaurant's own billing read publishes; ``tx_subscription.initiate`` is what
    actually happens when somebody tries to pay. If those two ever disagree the
    portal offers a Pay button the server refuses — which is a smaller version of
    exactly the defect D07 closed, reached from the other side.

    THE PAIRING IS PINNED RATHER THAN ENFORCED, and that is deliberate. ``initiate``
    does NOT branch on the flag: it removed its insertion branch outright, because a
    switch is a working fake collection one boolean away from returning. So the
    invariant cannot be "the code consults the constant" — it has to be "while the
    constant says unsupported, the boundary refuses", which is what the first test
    below asserts and what makes flipping the constant alone insufficient.
    """

    #: Module-level names that may ASSIGN the capability. One, by construction.
    CAPABILITY_HOME = 'finance_app/subscription_capability.py'

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Cap', last_name='Owner', email='cap-owner@test.com',
            phone_number='256775000401', username='256775000401',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Capability Ltd', location='loc-cap',
            status=RestaurantStatus_Live, owner=self.owner,
        )

    def _initiate(self):
        return SubscriptionPaymentTransaction().initiate(
            restaurant_id=str(self.restaurant.pk),
            transaction_platform='web',
            payment_mode=PaymentMode_MobileMoney,
            user=self.owner,
            msisdn=TEST_MSISDN,
            otp='1234',
        )

    # -- the pairing ----------------------------------------------------------

    def test_while_the_disclosure_says_unsupported_the_boundary_refuses(self):
        # REVERTING EITHER HALF FAILS HERE: make `initiate` succeed and the status
        # assertion breaks; flip the constant and the premise assertion does.
        from finance_app import subscription_capability

        self.assertIs(
            subscription_capability.IN_APP_COLLECTION_SUPPORTED, False,
            'the capability constant no longer states what this build does',
        )
        result = self._initiate()
        self.assertEqual(result['status'], 501)
        self.assertEqual(
            result['reason'],
            subscription_capability.REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
        )
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_the_boundary_re_exports_the_shared_code_and_sentence(self):
        # Re-exported, not re-spelled: an importer of either module gets the same
        # two strings.
        from finance_app import subscription_capability

        self.assertEqual(
            REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
            subscription_capability.REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
        )
        self.assertEqual(
            MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
            subscription_capability.MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
        )

    # -- structural: one writer, and never a switch ---------------------------

    def _assignments_of(self, relative_path, name):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse((root / relative_path).read_text())
        found = []
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(node.lineno)
        return found

    def test_only_one_module_assigns_the_capability(self):
        # A second assignment is a second source of truth, and the two would
        # disagree the first time one of them was edited.
        name = 'IN_APP_COLLECTION_SUPPORTED'
        self.assertTrue(self._assignments_of(self.CAPABILITY_HOME, name))
        for consumer in (
            'finance_app/controllers/tx_subscription.py',
            'restaurants_app/controllers/subscriptions.py',
        ):
            self.assertEqual(
                self._assignments_of(consumer, name), [],
                f'{consumer} restates the capability instead of importing it',
            )

    def test_the_collection_boundary_never_reads_the_flag_at_runtime(self):
        # It imports it to be PINNED by the test above, never to branch on it.
        # A `if IN_APP_COLLECTION_SUPPORTED:` inside `initiate` would turn a
        # removed capability back into a switch — a working fake collection one
        # boolean away from returning.
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse(
            (root / 'finance_app/controllers/tx_subscription.py').read_text()
        )
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Name)
                        and inner.id == 'IN_APP_COLLECTION_SUPPORTED'):
                    offenders.append((node.name, inner.lineno))
        self.assertEqual(
            offenders, [],
            'tx_subscription consults the capability flag at runtime; it must '
            'refuse unconditionally instead',
        )
