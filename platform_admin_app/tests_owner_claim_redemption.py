"""
Owner-claim redemption — Step 2F.2, the authority transaction.

WHAT THIS SUITE IS DEFENDING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

One route turns a bearer claim token plus an OTP into restaurant authority and a
customer session. Almost every test below exists because a specific plausible
implementation would be wrong in a specific way:

  * accept any correct-looking OTP -> a login code redeems a restaurant;
  * bind the OTP to the user but not the destination -> a code sent to a replaced
    phone still proves "current control";
  * count guesses on the OTP row -> re-request the code, get five more guesses;
  * raise on a wrong code -> the attempt counter rolls back with the exception;
  * raise late on a right code -> a legitimate claimant loses a valid factor;
  * `objects.get(token_hash=…)` -> a superseded or cancelled credential still redeems;
  * "helpfully" set a password for an established owner -> a restaurant claim silently
    rewrites somebody's account credential.

The concurrency proofs live in ``tests_owner_claim_redemption_concurrency`` because they
need ``TransactionTestCase`` and real PostgreSQL row locks.
"""
import ast
import inspect
import pathlib
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
)
from platform_admin_app import (
    onboarding_creation, onboarding_invitations, owner_claim_redemption,
)
from platform_admin_app.endpoints.owner_claim import (
    OWNER_CLAIM_OTP_PURPOSE,
    OwnerClaimRedeemThrottle,
    PASSWORD_NOT_REQUIRED_MESSAGE,
    PASSWORD_REQUIRED_MESSAGE,
    REDEEM_REFUSAL_MESSAGE,
    REDEEM_SUCCESS_MESSAGE,
)
from platform_admin_app.models import (
    OWNER_CLAIM_MAX_FAILED_ATTEMPTS, OwnerInvitation,
)
from platform_admin_app.onboarding_creation import ExistingOwner, NewOwner
from platform_admin_app.tests_owner_claim import claim_rate
from platform_admin_app.onboarding_reads import onboarding_summary
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CHALLENGE_PATH = '/api/v1/users/owner-claim/challenge/'
REDEEM_PATH = '/api/v1/users/owner-claim/redeem/'
CLAIM_TOKEN_HEADER = 'X-Owner-Claim-Token'

# ENV=dev hardcodes every OTP to this. That is precisely why the purpose and destination
# bindings have to decide which row is selected — the digits cannot.
DEV_OTP = '1234'
WRONG_OTP = '9999'

# A password that satisfies all four configured validators. Long, not numeric, not in
# the common list, and not similar to any owner attribute this suite creates.
GOOD_PASSWORD = 'Kabalagala-Sunrise-7'

# A phone range distinct from every other suite in this app.
_PHONE = iter(f'0772{n:06d}' for n in range(861000, 869999))


def imported_and_called(relative):
    """
    Every NAME this module imports or calls, as a set.

    AST rather than a substring scan, and that distinction is load-bearing here: these
    modules explain at length in prose WHY they do not use `change_password`,
    `Notification`, `AdminAuditLog` or the admission advisory lock, so a substring match
    would fire on the very comments that document the decision. What matters is whether
    the name is REACHED, not whether it is mentioned.
    """
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split('.'))
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or '').split('.'))
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
                if isinstance(node.func.value, ast.Name):
                    names.add(node.func.value.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def redeem_rate(value):
    """
    Set the redemption throttle rate for a test or class.

    NOT ``override_settings(REST_FRAMEWORK=…)``: DRF binds
    ``SimpleRateThrottle.THROTTLE_RATES`` as a CLASS attribute at import time, so it
    keeps pointing at the original dict however the setting is overridden and a test
    written the obvious way silently exercises the real rate.
    """
    return mock.patch.object(
        OwnerClaimRedeemThrottle, 'THROTTLE_RATES',
        {**OwnerClaimRedeemThrottle.THROTTLE_RATES, 'owner_claim_redeem': value},
    )


# Most of the suite runs wide open: a rate low enough to fire would make every test
# order-dependent on how many requests its neighbours made.
_UNTHROTTLED = redeem_rate('1000/min')

# The attempt-budget suite drives the CHALLENGE route five or six times per test, which
# is above its own 5/min rate. That throttle is not what those tests are about.
_CHALLENGE_UNTHROTTLED = claim_rate('1000/min')


def staff():
    return User.objects.create_user(
        first_name='Ada', last_name='Min', email=f'ocr-admin-{next(_PHONE)}@t.com',
        username=f'ocr-admin-{next(_PHONE)}', country='UG', password='x', roles=[],
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


class RedemptionTestCase(TestCase):
    """
    One admin-created restaurant with a BRAND-NEW owner and a live claim credential.

    ``mode=new``, so the owner is ``pending_initial_claim`` with an unusable password —
    the state the whole flow exists for. Established-owner cases build their own fixture.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.admin = staff()
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Redeem Cafe', location='Ntinda', is_test=False,
            owner=NewOwner('Owen', 'Ner', next(_PHONE), None),
            actor=self.admin, reason='Creating the redemption fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.invitation = self.creation.invitation
        self.owner = self.creation.owner
        self.token = self.creation.claim_token

    # --- helpers ---

    def challenge(self, token=...):
        headers = {} if token is ... else {CLAIM_TOKEN_HEADER: token}
        return self.client.post(CHALLENGE_PATH, {}, format='json', headers=headers)

    def redeem(self, token=..., **body):
        headers = {} if token is ... else {CLAIM_TOKEN_HEADER: token}
        return self.client.post(REDEEM_PATH, body, format='json', headers=headers)

    def claim(self, token=None, otp=DEV_OTP, password=GOOD_PASSWORD):
        """Challenge then redeem — the whole two-factor flow."""
        self.assertEqual(self.challenge(token=token or self.token).status_code, 200)
        body = {'otp': otp}
        if password is not None:
            body['new_password'] = password
        return self.redeem(token=token or self.token, **body)

    def fresh_invitation(self):
        return OwnerInvitation.objects.get(pk=self.invitation.pk)

    def fresh_owner(self):
        return User.objects.get(pk=self.owner.pk)

    def assertRefused(self, response):
        """The ONE public failure: same status, same sentence, exactly two keys."""
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['status'], 400)
        self.assertEqual(response.data['message'], REDEEM_REFUSAL_MESSAGE)
        self.assertEqual(
            set(response.data), {'status', 'message'},
            'a refusal must not grow a code, reason, errors or data key',
        )

    def assertNothingHappened(self):
        """No consume, no access transition, no password, no session."""
        invitation = self.fresh_invitation()
        self.assertIsNone(invitation.consumed_at)
        self.assertIsNone(invitation.cancelled_at)
        self.assertIsNone(invitation.superseded_at)
        owner = self.fresh_owner()
        self.assertEqual(
            owner.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(self.outstanding_tokens(), 0)

    def outstanding_tokens(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        return OutstandingToken.objects.filter(user=self.owner).count()


# ═══════════════════════════════════════════════════════════════════════════════
# §39 — the new-owner end-to-end domain test
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class NewOwnerEndToEndTests(RedemptionTestCase):
    """
    Admin creates -> owner challenges -> owner redeems -> owner has authority.

    The one test that would notice if any single link in the chain were replaced with
    something that merely looked right.
    """

    def test_the_starting_state_is_what_step_2d_leaves_behind(self):
        self.assertEqual(
            self.owner.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertFalse(self.owner.has_usable_password())
        self.assertEqual(self.restaurant.status, 'onboarding')
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(summary['invitation']['status'], 'pending')
        self.assertEqual(summary['owner_control']['status'], 'not_established')

    def test_the_full_claim_succeeds(self):
        response = self.claim()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 200)
        self.assertEqual(response.data['message'], REDEEM_SUCCESS_MESSAGE)
        self.assertEqual(
            set(response.data['data']), {'token', 'refresh', 'restaurant_id'},
        )
        self.assertEqual(
            response.data['data']['restaurant_id'], str(self.restaurant.pk),
        )

    def test_the_identity_transition_is_complete(self):
        self.claim()
        owner = self.fresh_owner()
        self.assertEqual(owner.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)
        self.assertTrue(owner.check_password(GOOD_PASSWORD))
        self.assertFalse(owner.prompt_password_change)

    def test_the_invitation_is_consumed_exactly_once(self):
        self.claim()
        invitation = self.fresh_invitation()
        self.assertIsNotNone(invitation.consumed_at)
        self.assertIsNone(invitation.cancelled_at)
        self.assertIsNone(invitation.superseded_at)
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_the_admin_read_now_shows_owner_control(self):
        """§29. The consumed invitation IS the evidence — nothing else was written."""
        self.claim()
        invitation = self.fresh_invitation()
        summary = onboarding_summary(self.restaurant)

        self.assertEqual(summary['owner_relationship']['status'], 'consistent')
        self.assertEqual(summary['owner_control']['status'], 'invitation_redeemed')
        self.assertEqual(summary['owner_control']['evidence'], 'invitation_redeemed')
        self.assertEqual(
            summary['owner_control']['evidence_at'], invitation.consumed_at.isoformat(),
        )
        self.assertEqual(summary['invitation']['status'], 'consumed')

    def test_the_owner_holds_ordinary_owner_authority(self):
        """The membership Step 2D created is now exercisable by a real principal."""
        from users_app.controllers.permissions_check import (
            can_user_access_module, get_module_restaurant_ids,
        )
        self.claim()
        owner = self.fresh_owner()
        self.assertTrue(can_user_access_module(owner, str(self.restaurant.pk), 'menu'))
        self.assertIn(
            str(self.restaurant.pk), get_module_restaurant_ids(owner, 'menu'),
        )

    def test_lifecycle_and_commercial_state_are_untouched(self):
        """§39. Claiming is not going live, and it configures nothing."""
        from commercial_app.models import (
            RestaurantServiceConfiguration, RestaurantSubscriptionTerms,
        )
        from restaurants_app.controllers.lifecycle import check_go_live_readiness

        self.claim()
        restaurant = Restaurant.objects.get(pk=self.restaurant.pk)
        self.assertEqual(restaurant.status, 'onboarding')
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())
        readiness = check_go_live_readiness(restaurant)
        self.assertFalse(readiness.ready)
        self.assertIn('readiness_not_configured', readiness.blockers)

    def test_the_onboarding_provenance_is_untouched(self):
        self.claim()
        self.onboarding.refresh_from_db()
        self.assertEqual(self.onboarding.source, 'admin_created')
        self.assertIsNone(self.onboarding.owner_control_attested_at)
        self.assertIsNone(self.onboarding.owner_control_attested_user_id)
        self.assertIsNone(self.onboarding.owner_control_attested_by_id)

    def test_the_owner_of_record_and_membership_are_untouched(self):
        before = list(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant)
            .values('user_id', 'roles', 'active', 'deleted').order_by('user_id')
        )
        self.claim()
        self.assertEqual(
            Restaurant.objects.values_list('owner_id', flat=True).get(
                pk=self.restaurant.pk,
            ),
            self.owner.pk,
        )
        self.assertEqual(
            list(
                RestaurantEmployee.objects.filter(restaurant=self.restaurant)
                .values('user_id', 'roles', 'active', 'deleted').order_by('user_id')
            ),
            before,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §40 — token presentation
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class TokenPresentationTests(RedemptionTestCase):
    """
    §40. The transition is OPERATIONAL, not merely a word in a column.

    Before redemption a fabricated token for this identity is refused by the Step-2D.1
    gate; after it, the token redemption actually returned authenticates.
    """

    # An authenticated customer-plane GET any active employee of the restaurant may
    # reach (`support` is the ungated module), so a 200 proves the token both
    # AUTHENTICATES and resolves to this tenant's staff.
    SUPPORT = '/api/v1/support/issues/'
    # ...and one only an OWNER may reach (`team` is off-grid owner-only), so a 200 here
    # proves the claim produced real owner authority rather than merely a session.
    ROLE_GRID = '/api/v1/restaurant-setup/role-permissions/'
    REFRESH = '/api/v1/users/auth/token/refresh/'

    def test_a_fabricated_token_is_refused_before_redemption(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        raw = str(RefreshToken.for_user(self.owner).access_token)
        response = self.client.get(self.SUPPORT, HTTP_AUTHORIZATION=f'Bearer {raw}')
        self.assertIn(response.status_code, (401, 403), response.content)

    def test_the_returned_access_token_authenticates_after_redemption(self):
        token = self.claim().data['data']['token']
        response = self.client.get(self.SUPPORT, HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_returned_token_carries_owner_authority(self):
        token = self.claim().data['data']['token']
        response = self.client.get(
            f'{self.ROLE_GRID}?restaurant={self.restaurant.pk}',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_returned_refresh_token_passes_the_gated_refresh_endpoint(self):
        refresh = self.claim().data['data']['refresh']
        response = self.client.post(
            self.REFRESH, {'refresh': refresh}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('access', response.data)

    def test_an_outstanding_token_row_is_persisted(self):
        """Which is what makes the mint participate in the transaction at all."""
        self.claim()
        self.assertEqual(self.outstanding_tokens(), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# §38 — the existing-owner multi-restaurant case
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class EstablishedOwnerTests(TestCase):
    """
    §38. THE LOAD-BEARING FALSE-POSITIVE TEST.

    An established owner of restaurant A is named owner of a new restaurant B and claims
    it. Restaurant-scoped owner control must not be reinterpreted as global account
    onboarding: B's credential is consumed and a session is minted, and the IDENTITY is
    not touched in any way.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.admin = staff()
        phone = next(_PHONE)
        self.owner = User.objects.create_user(
            first_name='Est', last_name='Ablished',
            email=f'ocr-est-{phone}@t.com',
            phone_number=f'256{phone[1:]}', username=f'256{phone[1:]}',
            country='UG', password='Original-Passw0rd!', roles=[],
        )
        self.owner.prompt_password_change = True
        self.owner.save(update_fields=['prompt_password_change'])
        # Restaurant A, owned the ordinary way.
        self.restaurant_a = onboarding_creation.create_admin_restaurant(
            name='Alpha Grill', location='Kololo', is_test=False,
            owner=ExistingOwner(str(self.owner.pk)),
            actor=self.admin, reason='The first restaurant fixture.',
        ).restaurant
        # Restaurant B — the one being claimed.
        self.creation_b = onboarding_creation.create_admin_restaurant(
            name='Beta Grill', location='Naguru', is_test=False,
            owner=ExistingOwner(str(self.owner.pk)),
            actor=self.admin, reason='The second restaurant fixture.',
        )
        self.token = self.creation_b.claim_token
        self.password_hash = User.objects.values_list('password', flat=True).get(
            pk=self.owner.pk,
        )

    def claim(self, **overrides):
        self.assertEqual(
            self.client.post(
                CHALLENGE_PATH, {}, format='json',
                headers={CLAIM_TOKEN_HEADER: self.token},
            ).status_code, 200,
        )
        body = {'otp': DEV_OTP}
        body.update(overrides)
        return self.client.post(
            REDEEM_PATH, body, format='json',
            headers={CLAIM_TOKEN_HEADER: self.token},
        )

    def test_creation_left_this_owner_established(self):
        """Step 2D's `mode=existing` never touches the access state — the premise."""
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)

    def test_no_password_is_required(self):
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_supplied_password_is_refused_not_ignored(self):
        response = self.claim(new_password='Another-Passw0rd!')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['message'], PASSWORD_NOT_REQUIRED_MESSAGE)
        # ...and refusing means nothing moved at all.
        self.owner.refresh_from_db()
        self.assertEqual(
            User.objects.values_list('password', flat=True).get(pk=self.owner.pk),
            self.password_hash,
        )
        self.assertIsNone(
            OwnerInvitation.objects.get(pk=self.creation_b.invitation.pk).consumed_at,
        )

    def test_the_identity_is_byte_for_byte_unchanged(self):
        before = User.objects.values(
            'password', 'customer_access_state', 'prompt_password_change',
            'email', 'phone_number', 'username', 'account_type', 'is_active', 'roles',
        ).get(pk=self.owner.pk)

        self.assertEqual(self.claim().status_code, 200)

        after = User.objects.values(*before.keys()).get(pk=self.owner.pk)
        self.assertEqual(after, before)

    def test_the_original_password_still_works(self):
        self.claim()
        self.assertTrue(self.fresh().check_password('Original-Passw0rd!'))

    def test_prompt_password_change_stays_true(self):
        """It was True before; a restaurant claim is not a password event."""
        self.claim()
        self.assertTrue(self.fresh().prompt_password_change)

    def test_bs_invitation_is_consumed_and_a_session_is_minted(self):
        response = self.claim()
        self.assertIsNotNone(
            OwnerInvitation.objects.get(pk=self.creation_b.invitation.pk).consumed_at,
        )
        self.assertIn('token', response.data['data'])
        self.assertEqual(
            response.data['data']['restaurant_id'], str(self.creation_b.restaurant.pk),
        )

    def test_restaurant_a_is_completely_unaffected(self):
        a_invitation = OwnerInvitation.objects.get(
            onboarding__restaurant=self.restaurant_a,
        )
        self.claim()
        a_invitation.refresh_from_db()
        self.assertIsNone(a_invitation.consumed_at)
        summary = onboarding_summary(self.restaurant_a)
        self.assertEqual(summary['invitation']['status'], 'pending')
        self.assertEqual(summary['owner_control']['status'], 'not_established')

    def test_owner_control_moves_for_b_only(self):
        self.claim()
        self.assertEqual(
            onboarding_summary(self.creation_b.restaurant)['owner_control']['status'],
            'invitation_redeemed',
        )
        self.assertEqual(
            onboarding_summary(self.restaurant_a)['owner_control']['status'],
            'not_established',
        )

    def test_an_established_owner_with_an_unusable_password_is_not_repaired(self):
        """§21. That is an account-recovery question, not a restaurant-claim one."""
        User.objects.filter(pk=self.owner.pk).update(password='!unusable')
        self.assertEqual(self.claim().status_code, 200)
        self.assertFalse(self.fresh().has_usable_password())

    def fresh(self):
        return User.objects.get(pk=self.owner.pk)


# ═══════════════════════════════════════════════════════════════════════════════
# §36 — purpose binding
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class PurposeBindingTests(RedemptionTestCase):
    """
    §36. A correct code issued for something else must not redeem a restaurant.

    ENV=dev hardcodes every OTP to ``1234``, so the DIGITS cannot possibly decide. Only
    the row's ``purpose`` and ``msisdn`` can — which is exactly the condition production
    would reach the day two challenges happened to collide.
    """

    def _established(self):
        """Login and reset OTPs are refused to a pending identity, so establish it."""
        User.objects.filter(pk=self.owner.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            password=make_password('Some-Other-Passw0rd!'),
        )
        self.owner.refresh_from_db()

    def test_a_valid_owner_claim_otp_succeeds(self):
        self.assertEqual(self.claim().status_code, 200)

    def test_a_login_otp_with_the_identical_code_is_refused(self):
        self._established()
        self.assertTrue(OtpManager().make_otp(user=self.owner, purpose='login'))
        self.assertEqual(UserOtp.objects.get().purpose, 'login')

        self.assertRefused(self.redeem(token=self.token, otp=DEV_OTP))

    def test_a_reset_password_otp_with_the_identical_code_is_refused(self):
        self._established()
        self.assertTrue(
            OtpManager().make_otp(user=self.owner, purpose='reset-password')
        )
        self.assertRefused(self.redeem(token=self.token, otp=DEV_OTP))

    def test_a_purpose_mismatch_does_not_consume_the_other_otp(self):
        """The binding NARROWS the query, so the other row is never even selected."""
        self._established()
        OtpManager().make_otp(user=self.owner, purpose='login')
        login_otp = UserOtp.objects.get()

        self.redeem(token=self.token, otp=DEV_OTP)

        login_otp.refresh_from_db()
        self.assertIsNone(login_otp.consumed_at)
        self.assertEqual(login_otp.attempts, 0)

    def test_a_purpose_mismatch_mints_nothing_and_consumes_nothing(self):
        self._established()
        OtpManager().make_otp(user=self.owner, purpose='login')

        self.assertRefused(self.redeem(token=self.token, otp=DEV_OTP))

        self.assertIsNone(self.fresh_invitation().consumed_at)
        self.assertEqual(self.outstanding_tokens(), 0)

    def test_the_service_binds_the_exact_purpose(self):
        """Pinned by identity, not by hoping the string is right somewhere."""
        self.assertEqual(
            owner_claim_redemption.OWNER_CLAIM_OTP_PURPOSE, 'owner-claim',
        )
        self.assertIs(
            OWNER_CLAIM_OTP_PURPOSE, owner_claim_redemption.OWNER_CLAIM_OTP_PURPOSE,
        )

    def test_the_purpose_is_still_not_a_customer_auth_purpose(self):
        from users_app.controllers.otp_manager import CUSTOMER_AUTH_OTP_PURPOSES
        self.assertNotIn(OWNER_CLAIM_OTP_PURPOSE, CUSTOMER_AUTH_OTP_PURPOSES)
        self.assertEqual(CUSTOMER_AUTH_OTP_PURPOSES, {'login', 'reset-password'})

    def test_redemption_passes_the_binding_to_verify_otp(self):
        """
        The structural half: a future refactor that dropped the kwargs would still pass
        the behavioural tests above IF the OTP rows happened not to collide. This one
        fails immediately.
        """
        with mock.patch.object(
            OtpManager, 'verify_otp', wraps=OtpManager().verify_otp,
        ) as spy:
            self.claim()
        _, kwargs = spy.call_args
        self.assertEqual(kwargs['expected_purpose'], OWNER_CLAIM_OTP_PURPOSE)
        self.assertEqual(kwargs['expected_msisdn'], self.owner.phone_number)


# ═══════════════════════════════════════════════════════════════════════════════
# §8 / §17 / §34 — destination binding
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class DestinationBindingTests(RedemptionTestCase):
    """
    §17. An OTP proves control of the number it was DELIVERED TO.

    NOTE ON THE WRITER (§34): **no production path mutates an existing
    ``User.phone_number``.** ``self_update_user_profile`` explicitly refuses a change
    (400, "Phone number cannot be changed here"); every other site is a CREATE
    (``self_register``, ``determine-customers``, ``onboarding_creation``). So the race is
    pinned with a direct ``UPDATE``, which is the only way a phone can move today —
    operator SQL or a future writer. The point of the binding is that it holds whatever
    that writer turns out to be, because it compares against the row this transaction
    LOCKS rather than against anything remembered from the challenge.
    """

    def test_the_challenge_now_records_the_destination(self):
        """§8. Without this the row cannot be compared to anything afterwards."""
        self.challenge(token=self.token)
        otp = UserOtp.objects.get()
        self.assertEqual(otp.msisdn, self.owner.phone_number)
        self.assertEqual(otp.purpose, OWNER_CLAIM_OTP_PURPOSE)
        self.assertEqual(otp.user_id, self.owner.pk)

    def test_the_recorded_destination_is_canonical(self):
        self.challenge(token=self.token)
        self.assertRegex(UserOtp.objects.get().msisdn, r'^256\d{9}$')

    def test_a_code_for_the_old_phone_cannot_redeem_after_a_change(self):
        """THE RACE, pinned. Challenge to A, phone becomes B, A's code is refused."""
        self.challenge(token=self.token)
        old = UserOtp.objects.get().msisdn
        new_phone = f'256{next(_PHONE)[1:]}'
        self.assertNotEqual(new_phone, old)
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=new_phone, username=new_phone,
        )

        self.assertRefused(
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )
        self.assertNothingHappened()

    def test_the_stale_code_is_not_even_consumed(self):
        self.challenge(token=self.token)
        stale = UserOtp.objects.get()
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=f'256{next(_PHONE)[1:]}',
        )

        self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD)

        stale.refresh_from_db()
        self.assertIsNone(stale.consumed_at)
        self.assertEqual(stale.attempts, 0)

    def test_a_new_challenge_to_the_new_phone_can_redeem(self):
        """The other half: the owner is not locked out, they re-challenge."""
        new_phone = f'256{next(_PHONE)[1:]}'
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=new_phone, username=new_phone,
        )
        self.owner.refresh_from_db()

        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self.assertEqual(UserOtp.objects.get().msisdn, new_phone)
        response = self.redeem(
            token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_non_canonical_stored_phone_is_refused_at_redemption(self):
        self.challenge(token=self.token)
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=f'+{self.owner.phone_number}',
        )
        self.assertRefused(
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §37 / §19 — the invitation-level attempt budget
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
@_CHALLENGE_UNTHROTTLED
class InvitationAttemptBudgetTests(RedemptionTestCase):
    """
    §19. The proof that repeated OTP issuance cannot reset the guessing budget.

    ``UserOtp.attempts`` resets on every new challenge — by design, since ``make_otp``
    deletes and re-inserts. The counter that matters therefore lives on the CREDENTIAL.
    """

    def wrong(self):
        return self.redeem(
            token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD,
        )

    def attempts(self):
        return self.fresh_invitation().claim_failed_attempts

    def test_a_fresh_invitation_starts_at_zero(self):
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'], 'pending',
        )

    def test_one_wrong_attempt_increments_exactly_once(self):
        self.challenge(token=self.token)
        self.assertRefused(self.wrong())
        self.assertEqual(self.attempts(), 1)

    def test_a_new_challenge_does_not_reset_the_budget(self):
        """THE WHOLE POINT. The OTP row resets; the credential's counter does not."""
        self.challenge(token=self.token)
        self.wrong()
        self.assertEqual(self.attempts(), 1)

        self.challenge(token=self.token)
        self.assertEqual(UserOtp.objects.get().attempts, 0, 'premise: the row reset')
        self.assertEqual(self.attempts(), 1, 'the credential budget must NOT reset')

        self.wrong()
        self.assertEqual(self.attempts(), 2)

    def test_five_failures_lock_verification(self):
        for expected in range(1, OWNER_CLAIM_MAX_FAILED_ATTEMPTS + 1):
            self.challenge(token=self.token)
            self.assertRefused(self.wrong())
            self.assertEqual(self.attempts(), expected)

        invitation = self.fresh_invitation()
        self.assertTrue(invitation.is_verification_locked)
        # STILL UNRESOLVED — it holds the slot, and it is reissuable and cancellable.
        self.assertFalse(invitation.is_resolved)
        self.assertFalse(invitation.is_claimable)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'],
            'verification_locked',
        )

    def test_a_locked_invitation_cannot_be_challenged(self):
        """§11. No OTP is sent, and the caller learns nothing."""
        self._exhaust()
        UserOtp.objects.all().delete()

        response = self.challenge(token=self.token)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(UserOtp.objects.exists(), 'a locked claim must send no OTP')

    def test_a_locked_invitation_refuses_before_verification(self):
        self._exhaust()
        # A CORRECT code, and it still cannot be spent.
        UserOtp.objects.all().delete()
        OtpManager().make_otp(
            user=self.owner, msisdn=self.owner.phone_number,
            purpose=OWNER_CLAIM_OTP_PURPOSE,
        )
        otp_row = UserOtp.objects.get()

        self.assertRefused(
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )

        otp_row.refresh_from_db()
        self.assertIsNone(
            otp_row.consumed_at, 'a locked claim must not even test the code',
        )
        self.assertNothingHappened()

    def test_a_sixth_request_never_reaches_verification(self):
        """
        RENAMED, because the original name claimed something this does not prove.

        Once the budget is spent the invitation reads ``verification_locked`` and
        redemption refuses BEFORE the OTP is looked at — so the counter does not move
        because the increment is never reached, NOT because it is capped. Discovered by
        red-teaming: removing the ``min()`` cap left this passing, which is exactly the
        false positive it would have shipped.

        The cap itself is a BACKSTOP and is tested where it is actually reachable, in
        ``test_the_increment_is_capped_at_the_policy`` below.
        """
        self._exhaust()
        self.challenge(token=self.token)
        self.assertRefused(self.wrong())
        self.assertEqual(self.attempts(), OWNER_CLAIM_MAX_FAILED_ATTEMPTS)

    def test_the_increment_is_capped_at_the_policy(self):
        """
        THE CAP, exercised directly — the only way to reach it.

        ``_record_failed_attempt`` is unreachable at the cap through HTTP (the
        verification lock refuses first), so this drives the primitive. It is not
        ceremony: without the cap the counter would exceed the policy the moment any
        future change let a request past the lock check, and
        ``owner_invitation_claim_attempts_bounded`` would turn that into an
        ``IntegrityError`` — a 500 on the one route whose whole job is to refuse
        cleanly.
        """
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            claim_failed_attempts=OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
        )
        invitation = self.fresh_invitation()

        result = owner_claim_redemption._record_failed_attempt(invitation)

        self.assertEqual(
            self.attempts(), OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
            'the increment exceeded the policy the database constraint enforces',
        )
        self.assertEqual(result.remaining, 0)

    def test_the_cap_keeps_the_counter_inside_the_database_constraint(self):
        """
        The consequence spelled out: an uncapped increment would violate
        ``owner_invitation_claim_attempts_bounded`` and raise instead of refusing.
        """
        invitation = self.fresh_invitation()
        for _ in range(OWNER_CLAIM_MAX_FAILED_ATTEMPTS + 3):
            owner_claim_redemption._record_failed_attempt(invitation)
        self.assertEqual(self.attempts(), OWNER_CLAIM_MAX_FAILED_ATTEMPTS)

    def test_owner_control_never_moved(self):
        self._exhaust()
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(summary['owner_control']['status'], 'not_established')
        self.assertNothingHappened()

    def test_reissue_mints_a_fresh_budget(self):
        """§19. The ONLY reset is an elevated, reasoned Admin decision."""
        self._exhaust()
        old_token = self.token

        result = onboarding_invitations.reissue_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin,
            reason='Rotating after a verification lockout.',
        )

        # The locked row is superseded, keeping its history.
        old = self.fresh_invitation()
        self.assertIsNotNone(old.superseded_at)
        self.assertEqual(
            old.claim_failed_attempts, OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
            'history is preserved, not rewritten',
        )
        # The replacement starts clean.
        self.assertEqual(result.invitation.claim_failed_attempts, 0)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'], 'pending',
        )

        # The old token is dead...
        self.assertEqual(self.challenge(token=old_token).status_code, 400)
        # ...and the new one works, with its own five attempts.
        self.token = result.claim_token
        self.invitation = result.invitation
        self.assertEqual(self.claim().status_code, 200)

    def test_a_successful_redemption_preserves_the_failure_history(self):
        for _ in range(2):
            self.challenge(token=self.token)
            self.wrong()
        self.assertEqual(self.claim().status_code, 200)
        invitation = self.fresh_invitation()
        self.assertEqual(invitation.claim_failed_attempts, 2)
        self.assertIsNotNone(invitation.consumed_at)

    def test_a_locked_invitation_can_still_be_cancelled(self):
        self._exhaust()
        onboarding_invitations.cancel_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin,
            reason='Abandoning this onboarding after a lockout.',
        )
        self.assertIsNotNone(self.fresh_invitation().cancelled_at)

    def _exhaust(self):
        for _ in range(OWNER_CLAIM_MAX_FAILED_ATTEMPTS):
            self.challenge(token=self.token)
            self.wrong()
        self.assertTrue(self.fresh_invitation().is_verification_locked)


# ═══════════════════════════════════════════════════════════════════════════════
# §18 — wrong-attempt COMMIT semantics
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class WrongAttemptCommitTests(RedemptionTestCase):
    """
    §18. A failed security attempt must not be able to erase its own evidence.

    The service RETURNS ``RedemptionAttemptFailed`` rather than raising, precisely so the
    transaction commits. These tests fail the moment somebody "tidies up" that asymmetry
    into a single exception type.
    """

    def test_the_service_returns_rather_than_raising(self):
        self.challenge(token=self.token)
        outcome = owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=WRONG_OTP,
            encoded_password=make_password(GOOD_PASSWORD),
        )
        self.assertIsInstance(
            outcome, owner_claim_redemption.RedemptionAttemptFailed,
        )
        self.assertEqual(outcome.remaining, OWNER_CLAIM_MAX_FAILED_ATTEMPTS - 1)

    def test_the_increment_survives_the_service_call(self):
        """
        Discriminating: under ``TestCase`` the outer atomic is a savepoint, so an
        exception escaping it would roll the increment back and this would read 0.
        """
        self.challenge(token=self.token)
        owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=WRONG_OTP,
            encoded_password=make_password(GOOD_PASSWORD),
        )
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 1)

    def test_the_otp_rows_own_counter_also_moves(self):
        self.challenge(token=self.token)
        self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
        self.assertEqual(UserOtp.objects.get().attempts, 1)

    def test_a_wrong_attempt_changes_nothing_else(self):
        self.challenge(token=self.token)
        self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
        self.assertNothingHappened()

    def test_the_refusal_is_the_generic_one(self):
        self.challenge(token=self.token)
        self.assertRefused(
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §35 — password policy
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class PasswordPolicyTests(RedemptionTestCase):
    """
    §35. Django's CONFIGURED validators are the policy — there is no second one.

    ``AUTH_PASSWORD_VALIDATORS`` is not weakened for these tests: the failures below are
    the real validators refusing real passwords.
    """

    def setUp(self):
        super().setUp()
        self.challenge(token=self.token)

    def attempt(self, password):
        body = {'otp': DEV_OTP}
        if password is not None:
            body['new_password'] = password
        return self.redeem(token=self.token, **body)

    def assertPasswordRejected(self, response):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['message'], PASSWORD_REQUIRED_MESSAGE)
        self.assertIn('new_password', response.data.get('errors', {}))
        # And NOTHING was spent: not the OTP, not the credential budget.
        otp = UserOtp.objects.get()
        self.assertIsNone(otp.consumed_at)
        self.assertEqual(otp.attempts, 0)
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 0)
        self.assertNothingHappened()

    def test_a_missing_password_is_a_controlled_400(self):
        response = self.attempt(None)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['message'], PASSWORD_REQUIRED_MESSAGE)
        self.assertNothingHappened()

    def test_a_short_password_is_rejected(self):
        self.assertPasswordRejected(self.attempt('ab3!'))

    def test_a_numeric_password_is_rejected(self):
        self.assertPasswordRejected(self.attempt('83619204750'))

    def test_a_common_password_is_rejected(self):
        self.assertPasswordRejected(self.attempt('password123'))

    def test_a_password_similar_to_the_owner_is_rejected(self):
        self.assertPasswordRejected(self.attempt(self.owner.username))

    def test_a_valid_password_succeeds(self):
        self.assertEqual(self.attempt(GOOD_PASSWORD).status_code, 200)

    def test_a_password_with_spaces_is_not_trimmed(self):
        """Whitespace can be part of a password; trimming would store another one."""
        spaced = f'  {GOOD_PASSWORD}  '
        self.assertEqual(self.attempt(spaced).status_code, 200)
        owner = self.fresh_owner()
        self.assertTrue(owner.check_password(spaced))
        self.assertFalse(owner.check_password(GOOD_PASSWORD))

    def test_the_configured_validators_are_the_ones_in_force(self):
        from django.conf import settings
        names = {v['NAME'].rsplit('.', 1)[-1] for v in settings.AUTH_PASSWORD_VALIDATORS}
        self.assertEqual(names, {
            'UserAttributeSimilarityValidator', 'MinimumLengthValidator',
            'CommonPasswordValidator', 'NumericPasswordValidator',
        })

    def test_the_hashing_path_bypasses_no_live_hook(self):
        """
        §6. Redemption assigns a PRE-COMPUTED hash instead of calling ``set_password``,
        so ``AbstractBaseUser.save``'s ``password_changed`` dispatch never fires.

        That is safe only while no configured validator implements it. If one ever does
        — a password-history validator, say — this fails, and the redemption path has to
        be revisited rather than silently skipping it.
        """
        from django.contrib.auth.password_validation import (
            get_default_password_validators,
        )
        implementers = [
            type(v).__name__ for v in get_default_password_validators()
            if hasattr(v, 'password_changed')
        ]
        self.assertEqual(
            implementers, [],
            'a configured validator now implements password_changed; redemption '
            'assigns a pre-computed hash and would skip it',
        )

    def test_the_stored_hash_is_a_real_django_hash(self):
        self.attempt(GOOD_PASSWORD)
        stored = User.objects.values_list('password', flat=True).get(pk=self.owner.pk)
        self.assertTrue(stored)
        self.assertNotEqual(stored, GOOD_PASSWORD)
        self.assertIn('$', stored)


# ═══════════════════════════════════════════════════════════════════════════════
# §15 / §26 — claim-state refusals, all identical
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class UnclaimableStateTests(RedemptionTestCase):
    """
    §26. Every claim-state failure renders ONE sentence with ONE status.

    A caller who could tell "unknown token" from "right token, wrong tenant state" would
    learn which guess was closest and would learn facts about a restaurant they have no
    relationship with.
    """

    def setUp(self):
        super().setUp()
        self.challenge(token=self.token)

    def attempt(self, token=None):
        return self.redeem(
            token=token or self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
        )

    def test_no_token_at_all(self):
        self.assertRefused(
            self.client.post(
                REDEEM_PATH, {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD},
                format='json',
            ),
        )

    def test_an_unknown_token(self):
        self.assertRefused(self.attempt(token='not-a-real-claim-token'))

    def test_a_blank_token(self):
        self.assertRefused(self.attempt(token='   '))

    def test_a_consumed_invitation_cannot_be_replayed(self):
        """§27. After success the token is dead."""
        self.assertEqual(self.attempt().status_code, 200)
        consumed_at = self.fresh_invitation().consumed_at
        tokens_after_success = self.outstanding_tokens()

        self.challenge(token=self.token)  # refused, sends nothing
        self.assertRefused(self.attempt())

        self.assertEqual(
            self.fresh_invitation().consumed_at, consumed_at,
            'the consume timestamp must not move on a replay',
        )
        self.assertEqual(
            self.outstanding_tokens(), tokens_after_success,
            'a replay must not mint a second session',
        )

    def test_a_cancelled_invitation(self):
        onboarding_invitations.cancel_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin, reason='Cancelling before redemption.',
        )
        self.assertRefused(self.attempt())
        self.assertIsNone(self.fresh_invitation().consumed_at)

    def test_a_superseded_invitation(self):
        """The token a reissue replaced can never be consumed afterwards."""
        onboarding_invitations.reissue_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin, reason='Rotating this credential now.',
        )
        self.assertRefused(self.attempt())
        old = self.fresh_invitation()
        self.assertIsNotNone(old.superseded_at)
        self.assertIsNone(old.consumed_at)

    def test_an_expired_invitation(self):
        now = timezone.now()
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=now - timezone.timedelta(days=10),
            expires_at=now - timezone.timedelta(days=3),
        )
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_a_soft_deleted_restaurant(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_an_owner_who_is_no_longer_the_owner(self):
        replacement = User.objects.create_user(
            first_name='New', last_name='Owner',
            email=f'ocr-new-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=replacement)
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_a_drifted_owner_relationship(self):
        """The membership is deactivated: owner of record no longer has authority."""
        RestaurantEmployee.objects.filter(restaurant=self.restaurant).update(
            active=False,
        )
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_a_second_live_owner_membership(self):
        other = User.objects.create_user(
            first_name='Second', last_name='Owner',
            email=f'ocr-2nd-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        RestaurantEmployee.objects.create(
            restaurant=self.restaurant, user=other, roles=['owner'],
            active=True, deleted=False,
        )
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_a_deactivated_owner_account(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertRefused(self.attempt())

    def test_an_owner_promoted_to_platform_staff(self):
        User.objects.filter(pk=self.owner.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_a_legacy_adopted_tenant_has_no_claim_flow(self):
        from platform_admin_app.models import ONBOARDING_SOURCE_LEGACY_ADOPTED
        from platform_admin_app.models import RestaurantOnboarding
        RestaurantOnboarding.objects.filter(pk=self.onboarding.pk).update(
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED, created_by=None,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        self.assertRefused(self.attempt())
        self.assertNothingHappened()

    def test_every_refusal_is_byte_identical(self):
        """The anti-oracle property, asserted across the whole set at once."""
        bodies = set()

        bodies.add(self.attempt(token='wrong-token').content)
        onboarding_invitations.cancel_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin, reason='Cancelling for the oracle test.',
        )
        bodies.add(self.attempt().content)
        bodies.add(self.redeem(token=self.token, otp=WRONG_OTP).content)

        self.assertEqual(
            len(bodies), 1,
            f'refusals must be indistinguishable, saw: {bodies}',
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §4 — the request contract
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class RequestContractTests(RedemptionTestCase):

    def setUp(self):
        super().setUp()
        self.challenge(token=self.token)

    def test_a_missing_otp_is_a_controlled_400(self):
        response = self.redeem(token=self.token, new_password=GOOD_PASSWORD)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingHappened()

    def test_a_numeric_otp_is_refused_rather_than_coerced(self):
        """
        A JSON number is not a code. ``int('0123')`` would silently become a different
        one, and the stored hash is over exact digits.
        """
        response = self.redeem(
            token=self.token, otp=1234, new_password=GOOD_PASSWORD,
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingHappened()

    def test_the_token_is_never_accepted_from_the_body(self):
        response = self.client.post(
            REDEEM_PATH,
            {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD, 'token': self.token,
             'claim_token': self.token},
            format='json',
        )
        self.assertRefused(response)
        self.assertNothingHappened()

    def test_the_token_is_never_accepted_from_the_query_string(self):
        response = self.client.post(
            f'{REDEEM_PATH}?token={self.token}&claim_token={self.token}',
            {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD}, format='json',
        )
        self.assertRefused(response)

    def test_the_token_is_never_accepted_from_a_cookie(self):
        self.client.cookies['X-Owner-Claim-Token'] = self.token
        self.client.cookies['claim_token'] = self.token
        self.addCleanup(self.client.cookies.clear)
        response = self.client.post(
            REDEEM_PATH, {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD},
            format='json',
        )
        self.assertRefused(response)

    def test_server_facts_in_the_body_are_ignored(self):
        """
        §4. Owner, restaurant, invitation, phone, email, state, roles and account type
        are authoritative server facts. A body naming other values changes nothing.
        """
        victim = User.objects.create_user(
            first_name='Vic', last_name='Tim', email=f'ocr-vic-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        before = User.objects.values(
            'customer_access_state', 'account_type', 'roles', 'email', 'phone_number',
        ).get(pk=victim.pk)

        response = self.redeem(
            token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
            user_id=str(victim.pk), owner_id=str(victim.pk),
            restaurant_id=str(self.restaurant.pk),
            invitation_id=str(self.invitation.pk),
            phone_number='256700000000', email='attacker@t.com',
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            roles=['owner'], account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )

        self.assertEqual(response.status_code, 200, response.data)
        # The real owner was established; the victim was not touched.
        self.assertEqual(
            self.fresh_owner().customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
        )
        self.assertEqual(
            User.objects.values(*before.keys()).get(pk=victim.pk), before,
        )
        self.assertEqual(self.fresh_owner().account_type, ACCOUNT_TYPE_RESTAURANT_USER)
        self.assertEqual(self.fresh_owner().roles, [])

    def test_a_non_dict_body_does_not_crash(self):
        response = self.client.post(
            REDEEM_PATH, [1, 2, 3], format='json',
            headers={CLAIM_TOKEN_HEADER: self.token},
        )
        self.assertEqual(response.status_code, 400, response.content)


# ═══════════════════════════════════════════════════════════════════════════════
# §2 — no ambient authority
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class NoAmbientAuthorityTests(RedemptionTestCase):
    """
    §2. A customer JWT, a delegated session and an admin cookie must have ZERO influence.

    The claim token names the invitation; the invitation names the identity.
    """

    def test_the_view_has_no_authenticator_at_all(self):
        from platform_admin_app.endpoints.owner_claim import OwnerClaimRedeemView
        self.assertEqual(OwnerClaimRedeemView.authentication_classes, [])

    def test_a_customer_jwt_for_someone_else_changes_nothing(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        other = User.objects.create_user(
            first_name='Oth', last_name='Er', email=f'ocr-oth-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        raw = str(RefreshToken.for_user(other).access_token)
        self.challenge(token=self.token)

        response = self.client.post(
            REDEEM_PATH, {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD},
            format='json',
            headers={CLAIM_TOKEN_HEADER: self.token},
            HTTP_AUTHORIZATION=f'Bearer {raw}',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data['data']['restaurant_id'], str(self.restaurant.pk),
        )
        # The bearer of that JWT gained nothing.
        other.refresh_from_db()
        self.assertEqual(other.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)
        self.assertFalse(
            OwnerInvitation.objects.filter(invited_user=other).exists(),
        )

    def test_a_jwt_alone_redeems_nothing(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        raw = str(RefreshToken.for_user(self.admin).access_token)
        self.challenge(token=self.token)
        response = self.client.post(
            REDEEM_PATH, {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD},
            format='json', HTTP_AUTHORIZATION=f'Bearer {raw}',
        )
        self.assertRefused(response)
        self.assertNothingHappened()

    def test_an_admin_session_cookie_redeems_nothing(self):
        self.client.cookies['__Host-admin-session'] = 'anything-at-all'
        self.addCleanup(self.client.cookies.clear)
        self.challenge(token=self.token)
        response = self.client.post(
            REDEEM_PATH, {'otp': DEV_OTP, 'new_password': GOOD_PASSWORD},
            format='json',
        )
        self.assertRefused(response)


# ═══════════════════════════════════════════════════════════════════════════════
# §25 — response and cache hygiene
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class ResponseHygieneTests(RedemptionTestCase):
    """§25. Especially success — it carries customer tokens."""

    def assertNoStore(self, response):
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['Pragma'], 'no-cache')
        self.assertEqual(response['Expires'], '0')
        self.assertEqual(len(response.cookies), 0)
        self.assertNotIn('Location', response)

    def test_success(self):
        self.assertNoStore(self.claim())

    def test_an_invalid_claim(self):
        self.assertNoStore(
            self.redeem(token='nope', otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )

    def test_an_invalid_otp(self):
        self.challenge(token=self.token)
        self.assertNoStore(
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD),
        )

    def test_a_password_validation_response(self):
        self.challenge(token=self.token)
        self.assertNoStore(
            self.redeem(token=self.token, otp=DEV_OTP, new_password='abc'),
        )

    def test_a_throttled_response(self):
        with redeem_rate('1/min'):
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD)
            throttled = self.redeem(
                token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
            )
        self.assertEqual(throttled.status_code, 429)
        self.assertNoStore(throttled)

    def test_the_success_body_carries_no_credential_material(self):
        response = self.claim()
        blob = response.content.decode()
        self.assertNotIn(self.token, blob)
        self.assertNotIn(self.invitation.token_hash, blob)
        self.assertNotIn(DEV_OTP, blob)
        self.assertNotIn(GOOD_PASSWORD, blob)
        self.assertNotIn(str(self.invitation.pk), blob)
        self.assertNotIn(str(self.owner.pk), blob)
        self.assertNotIn(self.owner.phone_number, blob)
        self.assertNotIn(str(self.admin.pk), blob)

    def test_the_success_body_has_exactly_the_documented_keys(self):
        response = self.claim()
        self.assertEqual(set(response.data), {'status', 'message', 'data'})
        self.assertEqual(
            set(response.data['data']), {'token', 'refresh', 'restaurant_id'},
        )
        self.assertNotIn('require_otp', response.data['data'])
        self.assertNotIn('profile', response.data['data'])


# ═══════════════════════════════════════════════════════════════════════════════
# §12 — throttle
# ═══════════════════════════════════════════════════════════════════════════════

class ThrottleTests(RedemptionTestCase):

    def test_the_throttle_fires(self):
        with redeem_rate('2/min'):
            self.challenge(token=self.token)
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
            third = self.redeem(
                token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD,
            )
        self.assertEqual(third.status_code, 429)

    def test_a_throttled_request_costs_no_attempt_budget(self):
        with redeem_rate('1/min'):
            self.challenge(token=self.token)
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
            self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 1)

    def test_the_throttle_is_keyed_on_the_ip_not_the_token(self):
        throttle = OwnerClaimRedeemThrottle()
        request = mock.Mock()
        request.user = None
        request.META = {'REMOTE_ADDR': '203.0.113.7'}
        with mock.patch.object(
            OwnerClaimRedeemThrottle, 'get_ident', return_value='203.0.113.7',
        ):
            key = throttle.get_cache_key(request, None)
        self.assertIn('203.0.113.7', key)
        self.assertNotIn(self.token, key)

    def test_the_configured_rate(self):
        from django.conf import settings
        self.assertEqual(
            settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['owner_claim_redeem'],
            '5/min',
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §22 / §41 — fault injection: a correct OTP must not be lost
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class FaultInjectionTests(RedemptionTestCase):
    """
    §41. Break each stage AFTER a CORRECT code and prove everything rolls back.

    THE ASYMMETRY THIS DEFENDS. A wrong code's attempt counters must COMMIT (see
    ``WrongAttemptCommitTests``); a failure after a right code must roll back
    EVERYTHING, the OTP's consumption included — otherwise a legitimate claimant loses
    a valid second factor to a server-side database error and has to ask an
    administrator to reissue.

    Compensating deletes are not used and must not be: database transactionality is the
    proof, and a hand-written unwind is a second thing to get wrong.
    """

    def setUp(self):
        super().setUp()
        self.challenge(token=self.token)
        self.otp_row = UserOtp.objects.get()

    def assertFullyRolledBack(self):
        self.assertNothingHappened()
        self.otp_row.refresh_from_db()
        self.assertIsNone(
            self.otp_row.consumed_at,
            'a valid second factor must survive a later server-side failure',
        )
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 0)

    def attempt(self):
        return owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=DEV_OTP,
            encoded_password=make_password(GOOD_PASSWORD),
        )

    def test_a_failure_right_after_otp_verification(self):
        """A. Injected at the invitation save, the first write after verification."""
        with mock.patch.object(
            OwnerInvitation, 'save', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.attempt()
        self.assertFullyRolledBack()

    def test_a_failure_at_the_identity_save(self):
        """C. The invitation HAS been consumed in-transaction by this point."""
        original = User.save
        state = {}

        def exploding_save(self_user, *args, **kwargs):
            if self_user.pk == self.owner.pk and 'password' in (
                kwargs.get('update_fields') or ()
            ):
                # GUARD: prove the invitation really was consumed before we blow up, so
                # a rollback that "passes" because nothing had happened yet cannot.
                state['invitation_consumed'] = OwnerInvitation.objects.filter(
                    pk=self.invitation.pk, consumed_at__isnull=False,
                ).exists()
                raise RuntimeError('boom')
            return original(self_user, *args, **kwargs)

        with mock.patch.object(User, 'save', exploding_save):
            with self.assertRaises(RuntimeError):
                self.attempt()

        self.assertTrue(
            state.get('invitation_consumed'),
            'the fault fired before the stage it is meant to test',
        )
        self.assertFullyRolledBack()

    def test_a_failure_at_the_token_mint(self):
        """D. Everything authoritative has happened by the time this fires."""
        state = {}

        def exploding_mint(user):
            state['access_established'] = User.objects.filter(
                pk=self.owner.pk, customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            ).exists()
            state['invitation_consumed'] = OwnerInvitation.objects.filter(
                pk=self.invitation.pk, consumed_at__isnull=False,
            ).exists()
            raise RuntimeError('boom')

        with mock.patch.object(
            owner_claim_redemption.customer_access, 'issue_customer_tokens',
            exploding_mint,
        ):
            with self.assertRaises(RuntimeError):
                self.attempt()

        self.assertTrue(state.get('access_established'))
        self.assertTrue(state.get('invitation_consumed'))
        self.assertFullyRolledBack()

    def test_an_outstanding_token_persistence_failure(self):
        """
        D, through the real mint. ``RefreshToken.for_user`` INSERTs an
        ``OutstandingToken``, which is what makes token minting participate in this
        transaction at all — so a persistence failure there must unwind the claim.
        """
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        with mock.patch.object(
            OutstandingToken.objects, 'create', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.attempt()
        self.assertFullyRolledBack()

    def test_the_same_code_still_works_after_a_rolled_back_attempt(self):
        """The point of all of the above: the claimant simply tries again."""
        with mock.patch.object(
            OwnerInvitation, 'save', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.attempt()

        result = self.attempt()
        self.assertIsInstance(result, owner_claim_redemption.RedemptionResult)
        self.assertIsNotNone(self.fresh_invitation().consumed_at)
        self.assertEqual(
            self.fresh_owner().customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
        )

    def test_no_outstanding_token_survives_a_failed_redemption(self):
        with mock.patch.object(
            User, 'save', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.attempt()
        self.assertEqual(self.outstanding_tokens(), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# §42 — no external I/O, and no admin audit
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class NoExternalIoTests(RedemptionTestCase):
    """
    §42. Patch each channel to EXPLODE, and redemption must still succeed.

    The transaction holds the ``Restaurant`` row, and PR #306 measured what holding it
    across unreachable-MongoDB I/O costs: a lifecycle transition waiting on that row
    holds the exclusive order-admission advisory lock while it waits, so every diner
    order at the restaurant queues behind it.
    """

    def test_no_sms_is_sent(self):
        with mock.patch(
            'notifications_app.controllers.sms.send_sms',
            side_effect=AssertionError('redemption sent an SMS'),
        ):
            self.challenge(token=self.token)  # the CHALLENGE may send; patch after
        with mock.patch(
            'notifications_app.controllers.sms.send_sms',
            side_effect=AssertionError('redemption sent an SMS'),
        ):
            response = self.redeem(
                token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
            )
        self.assertEqual(response.status_code, 200, response.data)

    def test_no_email_is_sent(self):
        self.challenge(token=self.token)
        with mock.patch(
            'notifications_app.controllers.messenger.Messenger.send_email',
            side_effect=AssertionError('redemption sent an email'),
        ):
            self.assertEqual(
                self.redeem(
                    token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
                ).status_code, 200,
            )

    def test_no_mongodb_notification_is_created(self):
        self.challenge(token=self.token)
        with mock.patch(
            'misc_app.controllers.notifications.notification.Notification'
            '.create_notification',
            side_effect=AssertionError('redemption created a Notification'),
        ):
            self.assertEqual(
                self.redeem(
                    token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
                ).status_code, 200,
            )

    def test_no_legacy_action_log_is_written(self):
        self.challenge(token=self.token)
        with mock.patch(
            'misc_app.controllers.save_action_log.save_action',
            side_effect=AssertionError('redemption wrote an action log'),
        ):
            self.assertEqual(
                self.redeem(
                    token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
                ).status_code, 200,
            )

    def test_no_admin_audit_row_is_written(self):
        """
        §42. NOT AN OVERSIGHT. ``AdminAuditLog`` records PLATFORM-STAFF decisions and its
        actor is a platform staff member; a row naming a restaurant owner as the actor of
        an admin action would corrupt what that log means and would drop a
        tenant-initiated event into the operator's activity strip. The consumed
        invitation, with its timestamp, is the durable record.
        """
        from platform_admin_app.models import AdminAuditLog
        before = AdminAuditLog.objects.count()
        self.assertEqual(self.claim().status_code, 200)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_redemption_does_not_reuse_the_io_heavy_password_controllers(self):
        """
        Structural. ``change_password`` and ``reset_password`` both perform synchronous
        MongoDB I/O (``save_action``, ``Notification``), which is exactly why redemption
        establishes the credential itself rather than delegating to either.
        """
        reached = imported_and_called(
            'platform_admin_app/owner_claim_redemption.py',
        )
        for forbidden in ('change_password', 'reset_password', 'save_action',
                          'Notification', 'send_sms', 'Messenger', 'AdminAuditLog',
                          'create_notification'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, reached)


# ═══════════════════════════════════════════════════════════════════════════════
# §43 — the structural ratchets
# ═══════════════════════════════════════════════════════════════════════════════

class RedemptionSecretSafetyTests(TestCase):
    """
    An AST scan over the redemption modules (OWNER-CLAIM-REDEEM-SAFE-00).

    The behavioural tests prove today's code is right; this proves the NEXT edit cannot
    quietly widen it. Note this scan is the MIRROR of the Step-2F.1 one: the challenge is
    forbidden to write, lock or mint, and redemption is REQUIRED to do all three — so the
    rules here are about what must not LEAK and what must not be REACHED FOR, not about
    abstinence.
    """

    SERVICE = 'platform_admin_app/owner_claim_redemption.py'
    ENDPOINT = 'platform_admin_app/endpoints/owner_claim.py'
    MODULES = (SERVICE, ENDPOINT)

    # Attributes redemption must NEVER assign. `password`, `customer_access_state` and
    # `prompt_password_change` are absent because the service legitimately writes all
    # three — they are covered by the dedicated writer scan below instead.
    FORBIDDEN_ASSIGNMENTS = frozenset({
        # identity facts that are not this operation's to change
        'account_type', 'is_active', 'email', 'phone_number', 'username', 'roles',
        'last_login',
        # ownership and membership — redemption RECORDS control, it never grants it
        'owner', 'owner_id', 'active', 'deleted',
        # invitation stamps other than the one consume
        'cancelled_at', 'cancelled_by', 'superseded_at', 'expires_at', 'token_hash',
        'issued_at', 'issued_by', 'invited_user', 'invited_user_id',
        # provenance and attestation
        'source', 'owner_control_attested_at', 'owner_control_attested_user',
        'owner_control_attested_by',
        # lifecycle, classification and commercial state
        'status', 'is_test', 'payment_timing', 'payment_collection_mode',
    })

    def _trees(self):
        for relative in self.MODULES:
            path = REPO_ROOT / relative
            yield relative, ast.parse(path.read_text(encoding='utf-8'))

    def test_no_forbidden_assignment_appears(self):
        for relative, tree in self._trees():
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AugAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Attribute):
                        continue
                    with self.subTest(module=relative, attribute=target.attr):
                        self.assertNotIn(target.attr, self.FORBIDDEN_ASSIGNMENTS)

    def test_the_service_never_mints_directly(self):
        """§23. The chokepoint, or nothing. Also covered repo-wide by the AST scan in
        ``users_app.tests_customer_access_gate``; asserted here too so this module's own
        suite fails first and says why."""
        _, tree = next(t for t in self._trees() if t[0] == self.SERVICE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(
                    node.func.attr, 'for_user',
                    'redemption must mint through customer_access.issue_customer_tokens',
                )
        source = (REPO_ROOT / self.SERVICE).read_text()
        self.assertIn('issue_customer_tokens', source)

    def test_no_credential_material_is_logged(self):
        """
        Checked by NAME, so ``logger.info('…', raw_token)`` fails even though the format
        string looks innocent.
        """
        leaky = {
            'raw_token', 'token', 'token_hash', 'claim_token', 'otp', 'password',
            'new_password', 'encoded_password',
        }
        for relative, tree in self._trees():
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'logger'
                ):
                    continue
                for argument in node.args + [kw.value for kw in node.keywords]:
                    if isinstance(argument, ast.Name):
                        with self.subTest(module=relative, arg=argument.id):
                            self.assertNotIn(argument.id, leaky)
                    if isinstance(argument, ast.Attribute):
                        with self.subTest(module=relative, arg=argument.attr):
                            self.assertNotIn(argument.attr, leaky)

    def test_the_service_takes_no_admission_advisory_lock(self):
        """
        The lifecycle transition takes ``advisory -> Restaurant``. Acquiring the advisory
        lock AFTER the row lock here would invert that order and reintroduce exactly the
        cycle ``restaurants_app.controllers.admission_lock`` documents.
        """
        reached = imported_and_called(self.SERVICE)
        for forbidden in ('lock_admission', 'lock_admission_shared',
                          'lock_admission_exclusive', 'admission_lock',
                          'pg_advisory_xact_lock'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, reached)

    def test_the_result_objects_carry_no_credential(self):
        for cls in (
            owner_claim_redemption.RedemptionResult,
            owner_claim_redemption.RedemptionAttemptFailed,
        ):
            fields = set(cls.__dataclass_fields__)
            for forbidden in (
                'token_hash', 'claim_token', 'raw_token', 'otp', 'password',
            ):
                with self.subTest(cls=cls.__name__, field=forbidden):
                    self.assertNotIn(forbidden, fields)

    def test_the_refusal_exception_carries_no_details(self):
        refusal = owner_claim_redemption.RedemptionRefused(
            owner_claim_redemption.UNKNOWN_TOKEN,
        )
        self.assertFalse(hasattr(refusal, 'details'))
        self.assertEqual(refusal.code, 'unknown_token')

    def test_the_service_never_touches_membership_or_ownership(self):
        reached = imported_and_called(self.SERVICE)
        for forbidden in ('RestaurantEmployee', 'ensure_role_permissions',
                          'RestaurantRolePermission', 'transition_restaurant',
                          'mark_restaurant_test',
                          'lock_restaurant_for_membership_mutation'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, reached)


class CustomerAccessWriterRatchetTests(TestCase):
    """
    §43. EXACTLY TWO production modules may assign ``customer_access_state``.

    ``onboarding_creation`` writes ``pending_initial_claim`` when Admin provisions a new
    owner; ``owner_claim_redemption`` writes ``established`` when that owner proves both
    claim factors. Nothing else, ever — and in particular there is still no general
    ``establish_customer_access(user)`` helper, because a caller could invoke one without
    claim evidence (pinned in ``users_app.tests_customer_access_gate``).

    This is the narrow sanction the new writer needed. It is deliberately an inventory
    rather than a count, so ADDING a writer is a deliberate edit to this list.
    """

    SANCTIONED_WRITERS = {
        'platform_admin_app/onboarding_creation.py',
        'platform_admin_app/owner_claim_redemption.py',
    }

    PRUNE_DIRS = frozenset({
        'migrations', '__pycache__', 'node_modules', 'site-packages',
        'venv', 'env', 'staticfiles', 'media',
    })

    def _production_modules(self):
        import os
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.PRUNE_DIRS and not d.startswith('.')
            ]
            for filename in sorted(filenames):
                if not filename.endswith('.py') or filename.startswith('test'):
                    continue
                if '/tests' in dirpath:
                    continue
                path = pathlib.Path(dirpath) / filename
                yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(
                    encoding='utf-8', errors='replace',
                )

    @staticmethod
    def _assigns_access_state(source):
        """
        Whether ``source`` ASSIGNS the field — as an attribute, as a keyword argument to
        a constructor or ORM call, or through ``update()``.

        An AST walk rather than a substring match, so the many modules that DISCUSS the
        field at length in prose (this repo does so in half a dozen docstrings) are not
        false positives.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - defensive
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == 'customer_access_state'
                    ):
                        return True
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == 'customer_access_state':
                        return True
        return False

    def test_only_the_sanctioned_writers_assign_the_access_state(self):
        writers = {
            relative for relative, source in self._production_modules()
            if self._assigns_access_state(source)
        }
        self.assertEqual(
            writers, self.SANCTIONED_WRITERS,
            'the set of production modules that write customer_access_state changed. '
            'Establishing customer access is the redemption transaction\'s job and '
            'provisioning a pending identity is creation\'s; a third writer needs a '
            'deliberate decision, not an addition to this list.',
        )

    def test_the_redemption_writer_establishes_and_the_creator_pends(self):
        """Guards the inventory above from passing because neither module writes."""
        creation = (REPO_ROOT / 'platform_admin_app/onboarding_creation.py').read_text()
        redemption = (
            REPO_ROOT / 'platform_admin_app/owner_claim_redemption.py'
        ).read_text()
        self.assertIn('CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM', creation)
        self.assertIn('CUSTOMER_ACCESS_ESTABLISHED', redemption)

    def test_there_is_still_no_general_establish_helper(self):
        from users_app import customer_access
        self.assertFalse(hasattr(customer_access, 'establish_customer_access'))
        self.assertFalse(hasattr(customer_access, 'establish'))


class AttemptBudgetPolicyTests(RedemptionTestCase):
    """
    The module constant and the database constraint must agree.

    Extends the fixture case because two of these need a real invitation row: the
    constraint has to be violated against one, and the raw-SQL default probe has to
    insert one.
    """

    def test_the_policy_is_five(self):
        self.assertEqual(OWNER_CLAIM_MAX_FAILED_ATTEMPTS, 5)

    def test_the_database_constraint_matches_the_constant(self):
        """
        The migration writes the bound as a LITERAL so it cannot change meaning when a
        module constant moves. That makes THIS the test that couples them: raising the
        policy without a migration would turn the next increment into an
        ``IntegrityError`` in production rather than a refusal.
        """
        constraint = next(
            c for c in OwnerInvitation._meta.constraints
            if c.name == 'owner_invitation_claim_attempts_bounded'
        )
        children = dict(constraint.condition.deconstruct()[1])
        self.assertEqual(
            children['claim_failed_attempts__lte'], OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
        )

    def test_the_database_enforces_the_bound(self):
        from django.db.utils import IntegrityError
        invitation = self.invitation
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OwnerInvitation.objects.filter(pk=invitation.pk).update(
                    claim_failed_attempts=OWNER_CLAIM_MAX_FAILED_ATTEMPTS + 1,
                )

    def test_the_column_carries_a_database_default(self):
        """
        §44. Rollback compatibility: old code INSERTs an ``owner_invitation`` without
        naming this column, so the DATABASE must supply the default — Django's
        ``AddField`` default is applied in Python and then DROPPED, leaving a NOT NULL
        column with no database default.

        Asserted by INSERTing through raw SQL without naming the column, which is exactly
        what a rolled-back ``mint_owner_invitation`` does. Anything less — asserting the
        Python default, or inspecting the field — would pass against the very schema this
        is meant to rule out.

        The fixture's own invitation occupies the one-unresolved slot, so the row is
        inserted against its own fresh onboarding.
        """
        second = onboarding_creation.create_admin_restaurant(
            name='Default Probe', location='Kabalagala', is_test=True,
            owner=NewOwner('Def', 'Ault', next(_PHONE), None),
            actor=self.admin, reason='Fixture for the db-default probe.',
        )
        # Free the slot the way a reissue would, so the raw INSERT is legal.
        OwnerInvitation.objects.filter(pk=second.invitation.pk).update(
            superseded_at=timezone.now(),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO owner_invitation '
                '(id, onboarding_id, invited_user_id, issued_by_id, token_hash, '
                ' issued_at, expires_at) '
                "VALUES (gen_random_uuid(), %s, %s, %s, %s, now(), "
                "now() + interval '7 days') RETURNING claim_failed_attempts",
                [
                    second.onboarding.pk, second.owner.pk, self.admin.pk,
                    'f' * 64,
                ],
            )
            self.assertEqual(cursor.fetchone()[0], 0)


# ═══════════════════════════════════════════════════════════════════════════════
# §17 — a stale factor costs no budget
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class StaleDestinationTests(RedemptionTestCase):
    """
    §17. "Refusal, invitation untouched" — INCLUDING the attempt budget.

    The destination binding alone would already make a code sent to a replaced number
    unusable: it narrows the locked query, so the stale row is never selected. But
    routing that through the wrong-code path would spend one of five guesses on a
    situation the claimant did not cause and cannot see, and five of them would lock a
    perfectly good credential because somebody else changed a phone number.

    NOT AN ORACLE: the response is byte-identical either way, and the condition is
    decided entirely from server state — an attacker cannot create a live owner-claim
    row bound to a number the owner no longer has, because issuing one always binds to
    the CURRENT canonical phone.
    """

    def move_the_phone(self):
        new_phone = f'256{next(_PHONE)[1:]}'
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=new_phone, username=new_phone,
        )
        return new_phone

    def test_a_stale_factor_is_refused_without_spending_budget(self):
        self.challenge(token=self.token)
        self.move_the_phone()

        self.assertRefused(
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )

        self.assertEqual(
            self.fresh_invitation().claim_failed_attempts, 0,
            'a phone change the claimant did not make must not cost a guess',
        )
        self.assertNothingHappened()

    def test_it_is_its_own_domain_code(self):
        self.challenge(token=self.token)
        self.move_the_phone()
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            owner_claim_redemption.redeem_owner_claim(
                raw_token=self.token, otp=DEV_OTP,
                encoded_password=make_password(GOOD_PASSWORD),
            )
        self.assertEqual(
            caught.exception.code, owner_claim_redemption.OTP_DESTINATION_STALE,
        )

    def test_five_stale_attempts_do_not_lock_the_credential(self):
        """The whole reason the refusal is separated from the wrong-code path."""
        self.challenge(token=self.token)
        self.move_the_phone()
        for _ in range(OWNER_CLAIM_MAX_FAILED_ATTEMPTS + 1):
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD)

        invitation = self.fresh_invitation()
        self.assertEqual(invitation.claim_failed_attempts, 0)
        self.assertFalse(invitation.is_verification_locked)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'], 'pending',
        )

    def test_a_wrong_code_to_the_CURRENT_phone_still_costs_budget(self):
        """The discriminating half: this is not a blanket amnesty."""
        self.challenge(token=self.token)
        self.redeem(token=self.token, otp=WRONG_OTP, new_password=GOOD_PASSWORD)
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 1)

    def test_no_outstanding_factor_at_all_still_costs_budget(self):
        """
        Also discriminating. "Nothing was ever requested" is a wrong-code situation, not
        a stale-destination one — the refusal is narrow by design.
        """
        UserOtp.objects.all().delete()
        self.assertRefused(
            self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD),
        )
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 1)

    def test_a_fresh_challenge_beside_a_stale_row_is_not_refused(self):
        """
        THE 'AND NO CURRENT ONE' CLAUSE, pinned. ``make_otp`` deletes by
        ``(user, msisdn)``, so re-challenging to a NEW number leaves the old row live
        beside the new one. Refusing merely because a stale row exists would strand an
        owner who had already done the right thing.
        """
        self.challenge(token=self.token)
        new_phone = self.move_the_phone()
        self.owner.refresh_from_db()
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self.assertEqual(UserOtp.objects.filter(user=self.owner).count(), 2)

        response = self.redeem(
            token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            UserOtp.objects.get(
                user=self.owner, msisdn=new_phone,
            ).consumed_at is not None, True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §27 / §28 — replay and the lost success response
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class ReplayAndLostResponseTests(RedemptionTestCase):
    """
    §27. After success the credential is DEAD.
    §28. A lost 200 does not roll authority back, and is recovered by ordinary login.
    """

    def test_an_identical_replay_is_refused(self):
        first = self.claim()
        self.assertEqual(first.status_code, 200)
        consumed_at = self.fresh_invitation().consumed_at

        # Same token, same code, same password.
        second = self.redeem(
            token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD,
        )

        self.assertRefused(second)
        self.assertEqual(self.fresh_invitation().consumed_at, consumed_at)

    def test_a_replay_mints_no_second_session(self):
        self.claim()
        before = self.outstanding_tokens()
        self.redeem(token=self.token, otp=DEV_OTP, new_password=GOOD_PASSWORD)
        self.assertEqual(self.outstanding_tokens(), before)

    def test_a_replay_does_not_re_run_the_access_transition(self):
        self.claim()
        owner = User.objects.values(
            'customer_access_state', 'password', 'prompt_password_change',
        ).get(pk=self.owner.pk)

        self.redeem(token=self.token, otp=DEV_OTP, new_password='Another-Passw0rd-9')

        self.assertEqual(
            User.objects.values(*owner.keys()).get(pk=self.owner.pk), owner,
        )

    def test_a_replay_cannot_rewrite_the_password(self):
        """The important half of the one above, said on its own."""
        self.claim()
        self.redeem(token=self.token, otp=DEV_OTP, new_password='Another-Passw0rd-9')
        owner = self.fresh_owner()
        self.assertTrue(owner.check_password(GOOD_PASSWORD))
        self.assertFalse(owner.check_password('Another-Passw0rd-9'))

    def test_a_challenge_after_success_is_refused_and_sends_nothing(self):
        self.claim()
        UserOtp.objects.all().delete()
        self.assertEqual(self.challenge(token=self.token).status_code, 400)
        self.assertFalse(UserOtp.objects.exists())

    def test_a_lost_response_leaves_the_claim_durably_succeeded(self):
        """
        §28. Simulated at the seam that matters: the transaction COMMITS and the
        response never reaches the caller.

        Authority is NOT rolled back because an HTTP response disappeared, and no
        plaintext JWT is stored for replay. The recovery is ordinary login.
        """
        self.challenge(token=self.token)
        result = owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=DEV_OTP,
            encoded_password=make_password(GOOD_PASSWORD),
        )
        # Asserted rather than merely discarded: `del result` alone would let a silent
        # RedemptionAttemptFailed pass for a lost response.
        self.assertIsInstance(result, owner_claim_redemption.RedemptionResult)
        del result  # ...and NOW the "response" is lost

        owner = self.fresh_owner()
        self.assertEqual(owner.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED)
        self.assertTrue(owner.check_password(GOOD_PASSWORD))
        self.assertIsNotNone(self.fresh_invitation().consumed_at)
        self.assertEqual(
            onboarding_summary(self.restaurant)['owner_control']['status'],
            'invitation_redeemed',
        )

    def test_the_recovery_is_ordinary_login(self):
        """A NEW owner now has established access and a password they chose."""
        self.claim()
        response = self.client.post(
            '/api/v1/users/auth/login/',
            {'username': self.owner.username, 'password': GOOD_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        # An owner is a privileged role, so login escalates to OTP rather than handing
        # out a token directly — which is the ordinary contract, not a claim concern.
        self.assertTrue(response.data['data'].get('require_otp'))

    def test_no_plaintext_jwt_is_persisted_anywhere(self):
        """
        ``OutstandingToken`` stores the refresh token SimpleJWT itself needs for
        blacklisting; nothing in this domain stores a token for replay.
        """
        response = self.claim()
        access = response.data['data']['token']
        for model_source in (
            owner_claim_redemption, OwnerInvitation,
        ):
            del model_source
        stored = set(
            OwnerInvitation.objects.values_list('token_hash', flat=True)
        )
        self.assertNotIn(access, stored)
        self.assertFalse(
            OwnerInvitation.objects.filter(token_hash=access).exists(),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §16 — the SERVICE's own authoritative checks, not the preflight's
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class ServiceRechecksEverythingTests(RedemptionTestCase):
    """
    THE ENDPOINT TESTS DO NOT PROVE THESE, and red-teaming is how that surfaced.

    ``UnclaimableStateTests`` drives HTTP, and the endpoint runs
    ``owner_claim.resolve_claim_token`` as a preflight first — which performs its OWN
    owner-consistency, ownership, eligibility and invitation-state checks. So an
    endpoint-level drift test is satisfied by the PREFLIGHT and says nothing about
    whether the service re-checks anything under the lock.

    That distinction is the entire point of the preflight/authoritative split: the
    preflight's answer is a snapshot with no lock behind it, and the service must not
    trust a single fact from it. Removing ``assert_owner_consistency`` from the service
    left every endpoint test green (only the PostgreSQL concurrency suite caught it) —
    so these call ``redeem_owner_claim`` DIRECTLY, with the drift already committed,
    and pin each authoritative check in the fast suite as well.
    """

    def setUp(self):
        super().setUp()
        self.challenge(token=self.token)

    def redeem_directly(self, password=GOOD_PASSWORD):
        return owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=DEV_OTP,
            encoded_password=make_password(password) if password else None,
        )

    def assertServiceRefuses(self, code):
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly()
        self.assertEqual(caught.exception.code, code)
        self.assertNothingHappened()
        # ...and the second factor was never spent on a claim-state refusal.
        otp = UserOtp.objects.get()
        self.assertIsNone(otp.consumed_at)
        self.assertEqual(otp.attempts, 0)
        self.assertEqual(self.fresh_invitation().claim_failed_attempts, 0)

    def test_the_service_refuses_a_deactivated_owner_membership(self):
        RestaurantEmployee.objects.filter(restaurant=self.restaurant).update(
            active=False,
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
        )

    def test_the_service_refuses_a_soft_deleted_owner_membership(self):
        RestaurantEmployee.objects.filter(restaurant=self.restaurant).update(
            deleted=True,
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
        )

    def test_the_service_refuses_a_removed_owner_role(self):
        RestaurantEmployee.objects.filter(restaurant=self.restaurant).update(
            roles=['manager'],
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
        )

    def test_the_service_refuses_a_second_live_owner_membership(self):
        other = User.objects.create_user(
            first_name='Two', last_name='Owner',
            email=f'ocr-svc-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        RestaurantEmployee.objects.create(
            restaurant=self.restaurant, user=other, roles=['owner'],
            active=True, deleted=False,
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
        )

    def test_the_service_refuses_when_ownership_has_moved(self):
        replacement = User.objects.create_user(
            first_name='New', last_name='Owner',
            email=f'ocr-svcnew-{next(_PHONE)}@t.com',
            phone_number=f'256{next(_PHONE)[1:]}',
            username=f'256{next(_PHONE)[1:]}', country='UG', password='x', roles=[],
        )
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=replacement)
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly()
        self.assertIn(
            caught.exception.code,
            {
                owner_claim_redemption.INVITED_USER_NOT_OWNER,
                owner_claim_redemption.OWNER_RELATIONSHIP_INCONSISTENT,
            },
        )
        self.assertIsNone(self.fresh_invitation().consumed_at)

    def test_the_service_refuses_a_deactivated_owner_account(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertServiceRefuses(owner_claim_redemption.OWNER_INACTIVE)

    def test_the_service_refuses_a_promoted_owner_account(self):
        User.objects.filter(pk=self.owner.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_NOT_RESTAURANT_USER,
        )

    def test_the_service_refuses_a_non_canonical_stored_phone(self):
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=f'+{self.owner.phone_number}',
        )
        self.assertServiceRefuses(
            owner_claim_redemption.OWNER_PHONE_NOT_CANONICAL,
        )

    def test_the_service_refuses_a_soft_deleted_restaurant(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        self.assertServiceRefuses(owner_claim_redemption.RESTAURANT_GONE)

    def test_the_service_refuses_a_cancelled_invitation(self):
        """
        NOTE WHICH GUARD FIRES, because it differs from the superseded case below and
        the difference is the head selector working correctly.

        Cancellation leaves NOTHING unresolved, so the head falls through to "the latest
        resolved row" — which is this invitation. The token therefore DOES match the
        head, and the next check refuses it as resolved. A superseded invitation, by
        contrast, has a live successor that becomes the head, so the token fails the
        identity check first.
        """
        onboarding_invitations.cancel_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin, reason='Cancelling for the service-level test.',
        )
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly()
        self.assertEqual(
            caught.exception.code, owner_claim_redemption.INVITATION_RESOLVED,
        )
        invitation = self.fresh_invitation()
        self.assertIsNotNone(invitation.cancelled_at)
        self.assertIsNone(invitation.consumed_at)

    def test_the_service_refuses_a_superseded_invitation(self):
        onboarding_invitations.reissue_owner_invitation(
            restaurant_id=str(self.restaurant.pk),
            expected_invitation_id=str(self.invitation.pk),
            actor=self.admin, reason='Rotating for the service-level test.',
        )
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly()
        self.assertEqual(
            caught.exception.code,
            owner_claim_redemption.TOKEN_DOES_NOT_MATCH_HEAD,
        )
        old = self.fresh_invitation()
        self.assertIsNone(old.consumed_at)
        self.assertIsNotNone(old.superseded_at)

    def test_the_service_refuses_an_expired_invitation(self):
        now = timezone.now()
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=now - timezone.timedelta(days=10),
            expires_at=now - timezone.timedelta(days=3),
        )
        self.assertServiceRefuses(owner_claim_redemption.INVITATION_EXPIRED)

    def test_the_service_refuses_a_verification_locked_invitation(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            claim_failed_attempts=OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
        )
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly()
        self.assertEqual(
            caught.exception.code,
            owner_claim_redemption.INVITATION_VERIFICATION_LOCKED,
        )
        self.assertIsNone(UserOtp.objects.get().consumed_at)

    def test_the_service_refuses_a_legacy_adopted_tenant(self):
        from platform_admin_app.models import (
            ONBOARDING_SOURCE_LEGACY_ADOPTED, RestaurantOnboarding,
        )
        RestaurantOnboarding.objects.filter(pk=self.onboarding.pk).update(
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED, created_by=None,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        self.assertServiceRefuses(owner_claim_redemption.NOT_ADMIN_CREATED)

    def test_the_service_refuses_an_unknown_token_without_locking(self):
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            owner_claim_redemption.redeem_owner_claim(
                raw_token='not-a-real-token', otp=DEV_OTP,
                encoded_password=make_password(GOOD_PASSWORD),
            )
        self.assertEqual(caught.exception.code, owner_claim_redemption.UNKNOWN_TOKEN)

    def test_the_service_refuses_a_credential_requirement_disagreement(self):
        """
        §8 of the transaction. The request was shaped against the pre-lock snapshot; if
        the LOCKED identity disagrees, the service refuses rather than guessing —
        supplying a password for an established owner would rewrite a credential this
        operation has no business touching.
        """
        User.objects.filter(pk=self.owner.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
            password=make_password('Pre-Existing-Passw0rd!'),
        )
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly(password=GOOD_PASSWORD)
        self.assertEqual(
            caught.exception.code,
            owner_claim_redemption.CREDENTIAL_REQUIREMENT_CHANGED,
        )
        self.assertTrue(
            self.fresh_owner().check_password('Pre-Existing-Passw0rd!'),
        )

    def test_the_service_refuses_a_missing_password_for_a_pending_owner(self):
        with self.assertRaises(owner_claim_redemption.RedemptionRefused) as caught:
            self.redeem_directly(password=None)
        self.assertEqual(
            caught.exception.code,
            owner_claim_redemption.CREDENTIAL_REQUIREMENT_CHANGED,
        )
        self.assertNothingHappened()
