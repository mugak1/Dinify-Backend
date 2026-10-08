"""
Step 2F.1 — the owner-claim challenge.

WHAT IS BEING PROVED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Three things, and they pull in different directions, which is why they are separate
classes:

  1. EVERY unclaimable state refuses IDENTICALLY and sends no OTP. The endpoint is
     ``AllowAny``, so a distinguishable refusal is an oracle about a restaurant the
     caller has no relationship with.
  2. The two claimable states succeed and report the right
     ``credential_setup_required``, while leaving the invitation, the access state,
     the password and owner control exactly as they were.
  3. NOTHING durable is written, and no lock or transaction spans OTP delivery.

THE ONE THAT IS EASY TO GET WRONG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

(3). PR #306's Codex P2 found a ``Restaurant`` row held across synchronous MongoDB
notification I/O, where a lifecycle transition waiting on that row holds the EXCLUSIVE
admission advisory lock and every diner order queues behind it. This endpoint delivers
an SMS, which is strictly worse to hold a lock across. ``NoLockSpansDeliveryTests``
pins the transaction shape rather than timing it — a benchmark would be flaky and
would not say WHY it regressed.

ENV IS ``dev`` UNDER TEST SETTINGS, so ``make_otp`` hardcodes the code and returns
``True`` without real delivery. Delivery FAILURE is therefore exercised by patching
``make_otp`` to return ``False``, which is the same falsy value a real gateway refusal
produces outside dev.
"""
import ast
import pathlib
import re
from unittest import mock

from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
)
from misc_app.controllers.msisdn import normalise_msisdn
from platform_admin_app import onboarding_adoption, onboarding_creation, owner_claim
from platform_admin_app.endpoints.owner_claim import (
    CLAIM_TOKEN_HEADER, OWNER_CLAIM_OTP_PURPOSE, REFUSAL_MESSAGE,
)
from platform_admin_app.models import OwnerInvitation
from platform_admin_app.onboarding_creation import ExistingOwner, NewOwner
from platform_admin_app.onboarding_reads import onboarding_summary
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User, UserOtp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CHALLENGE_PATH = '/api/v1/users/owner-claim/challenge/'

# A phone range distinct from every other suite in this app.
_PHONE = iter(f'0772{n:06d}' for n in range(851000, 859999))


def claim_rate(value):
    """
    Set the challenge throttle rate for a test or class.

    NOT ``override_settings(REST_FRAMEWORK=...)``: DRF binds
    ``SimpleRateThrottle.THROTTLE_RATES`` as a CLASS attribute at import time, so it
    keeps pointing at the original dict however the setting is overridden, and a test
    written the obvious way silently exercises the real rate instead. Patching the
    attribute is what actually takes effect.
    """
    from platform_admin_app.endpoints.owner_claim import OwnerClaimChallengeThrottle
    return mock.patch.object(
        OwnerClaimChallengeThrottle, 'THROTTLE_RATES',
        {**OwnerClaimChallengeThrottle.THROTTLE_RATES,
         'owner_claim_challenge': value},
    )


# The rest of the suite runs wide open — a rate low enough to fire would make every
# other test order-dependent on how many requests its neighbours made.
_UNTHROTTLED = claim_rate('1000/min')


def _staff():
    return User.objects.create_user(
        first_name='Ada', last_name='Min', email=f'oc-admin-{next(_PHONE)}@t.com',
        username=f'oc-admin-{next(_PHONE)}', country='UG', password='x', roles=[],
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


def _restaurant_user(label):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='Own', last_name='Er', email=f'oc-{label}-{phone}@t.com',
        phone_number=f'256{phone[1:]}', username=f'256{phone[1:]}',
        country='UG', password='x', roles=[],
    )


class _ClaimTestCase(TestCase):
    """
    One admin-created restaurant with a BRAND-NEW owner and a live claim token.

    ``mode=new``, so the owner is ``pending_initial_claim`` with an unusable password
    — the state this whole flow exists for.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.admin = _staff()
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Claim Cafe', location='Bugolobi', is_test=False,
            owner=NewOwner('Owen', 'Ner', next(_PHONE), None),
            actor=self.admin, reason='Creating the owner-claim fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.invitation = self.creation.invitation
        self.owner = self.creation.owner
        self.token = self.creation.claim_token

    # --- helpers ---

    def challenge(self, token=..., **extra):
        headers = dict(extra)
        if token is not ...:
            headers[CLAIM_TOKEN_HEADER] = token
        return self.client.post(CHALLENGE_PATH, {}, format='json', headers=headers)

    def otps(self):
        return UserOtp.objects.filter(user=self.owner)

    def assertRefused(self, response):
        """The ONE public failure: same status, same sentence, and no OTP."""
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['status'], 400)
        self.assertEqual(response.data['message'], REFUSAL_MESSAGE)
        # Exactly two keys — a refusal must not grow a `code`, `reason` or `data`.
        self.assertEqual(set(response.data), {'status', 'message'})
        self.assertFalse(
            UserOtp.objects.exists(),
            'a refused challenge must never generate an OTP',
        )

    def assertInvitationUntouched(self):
        stamps = OwnerInvitation.objects.filter(pk=self.invitation.pk).values(
            'consumed_at', 'cancelled_at', 'superseded_at', 'expires_at',
            'token_hash', 'issued_at',
        ).first()
        self.assertEqual(stamps['consumed_at'], None)
        self.assertEqual(stamps['cancelled_at'], None)
        self.assertEqual(stamps['superseded_at'], None)
        self.assertEqual(stamps['expires_at'], self.invitation.expires_at)
        self.assertEqual(stamps['token_hash'], self.invitation.token_hash)
        self.assertEqual(stamps['issued_at'], self.invitation.issued_at)


# ═══════════════════════════════════════════════════════════════════════════════
# Refusals — every one of them identical
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class UnclaimableStateTests(_ClaimTestCase):
    """
    Seventeen ways to be unclaimable, one public answer.

    Each test drives the REAL endpoint rather than the domain function, because the
    property under test is what an anonymous caller can observe — and that is a
    property of the response, not of the exception.
    """

    # -- the token itself ----------------------------------------------------

    def test_a_missing_header_is_refused(self):
        self.assertRefused(self.challenge())

    def test_a_blank_header_is_refused(self):
        self.assertRefused(self.challenge(token='   '))

    def test_a_malformed_token_is_refused(self):
        self.assertRefused(self.challenge(token='not-a-token'))

    def test_an_unknown_token_is_refused(self):
        # Well-formed and the right shape — it simply hashes to nothing stored.
        self.assertRefused(self.challenge(token='A' * 64))

    def test_a_token_differing_by_one_character_is_refused(self):
        tampered = ('B' if self.token[0] != 'B' else 'C') + self.token[1:]
        self.assertRefused(self.challenge(token=tampered))

    # -- invitation lifecycle ------------------------------------------------

    def test_an_expired_invitation_is_refused(self):
        # Aged BACKWARDS, both stamps together: `owner_invitation_expires_after_issue`
        # forbids `expires_at <= issued_at`, so an expired row is one that was issued
        # long ago — which is also the only way it happens in life.
        from datetime import timedelta
        from django.utils import timezone
        issued = timezone.now() - timedelta(days=30)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=issued, expires_at=issued + timedelta(days=7),
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_cancelled_invitation_is_refused(self):
        from django.utils import timezone
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            cancelled_at=timezone.now(), cancelled_by=self.admin,
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_superseded_invitation_is_refused(self):
        from django.utils import timezone
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            superseded_at=timezone.now(),
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_consumed_invitation_is_refused(self):
        # Nothing writes `consumed_at` yet — Step 2F.2 will be the first — so this is
        # set directly. It is the state a replayed claim token would be in.
        from django.utils import timezone
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_superseded_token_is_refused_after_reissue(self):
        """
        The realistic replay: an operator reissued, so the token the claimant holds is
        no longer the head. Driven through the REAL Step-2E writer rather than by
        stamping the row, so the two surfaces are proved to agree about which
        credential is current.
        """
        from platform_admin_app import onboarding_invitations
        result = onboarding_invitations.reissue_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=self.invitation.id,
            actor=self.admin, reason='Rotating before the claimant redeems.',
        )
        self.assertRefused(self.challenge(token=self.token))
        # ...and the NEW token works, which is what makes this a rotation rather
        # than a lockout.
        self.assertEqual(
            self.challenge(token=result.claim_token).status_code, 200,
        )

    # -- tenant state --------------------------------------------------------

    def test_a_legacy_adopted_onboarding_is_refused(self):
        """
        A legacy tenant never entered Dinify through a claim flow. It cannot hold an
        invitation at all, so the fixture is built the only way the state is
        reachable: adopt a separate restaurant, then point an invitation at it.
        """
        owner = _restaurant_user('legacy')
        legacy = Restaurant.objects.create(
            name='Legacy Claim', location='Ntinda', owner=owner,
        )
        RestaurantEmployee.objects.create(
            user=owner, restaurant=legacy, roles=[RESTAURANT_OWNER], active=True,
        )
        adopted = onboarding_adoption.adopt_existing_restaurant(
            restaurant_id=legacy.id, actor=self.admin,
            reason='Adopting for the legacy-claim case.',
        ).onboarding
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            onboarding=adopted, invited_user=owner,
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_soft_deleted_restaurant_is_refused(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        self.assertRefused(self.challenge(token=self.token))

    # -- owner binding -------------------------------------------------------

    def test_an_invited_user_who_is_no_longer_the_owner_is_refused(self):
        """
        A CLEAN ownership transfer, not a drift: the FK and the owner membership move
        TOGETHER to the replacement, so `assert_owner_consistency` passes and the only
        thing wrong is that the invitation names somebody who no longer owns this
        restaurant.

        Written this way deliberately. Moving only the FK would leave the tenant
        inconsistent, and the consistency check would refuse first — the binding check
        would then be untested, which is exactly what a red-team mutation caught.
        """
        replacement = _restaurant_user('replacement')
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=replacement)
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(user=replacement)
        # The tenant is CONSISTENT — the refusal below is about the binding alone.
        from platform_admin_app.onboarding import assert_owner_consistency
        assert_owner_consistency(Restaurant.objects.get(pk=self.restaurant.pk))

        self.assertRefused(self.challenge(token=self.token))

    def test_a_missing_owner_membership_is_refused(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(roles=[RESTAURANT_MANAGER])
        self.assertRefused(self.challenge(token=self.token))

    def test_multiple_owner_memberships_are_refused(self):
        RestaurantEmployee.objects.create(
            user=_restaurant_user('second'), restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_mismatched_owner_membership_is_refused(self):
        """
        The FK and the authority disagree: the sole owner-role membership belongs to
        somebody other than ``Restaurant.owner``. The invitation still names the owner
        of record, so this is caught by the consistency assertion and by nothing else.
        """
        other = _restaurant_user('mismatch')
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(user=other)
        self.assertRefused(self.challenge(token=self.token))

    # -- invited account eligibility ----------------------------------------

    def test_an_inactive_invited_user_is_refused(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertRefused(self.challenge(token=self.token))

    def test_a_platform_staff_invited_user_is_refused(self):
        User.objects.filter(pk=self.owner.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_an_invited_user_without_a_phone_is_refused(self):
        # The second factor is delivered by SMS; with no MSISDN there is no factor,
        # so this refuses rather than quietly becoming a single-factor claim.
        User.objects.filter(pk=self.owner.pk).update(phone_number=None)
        self.assertRefused(self.challenge(token=self.token))

    def test_a_blank_phone_is_refused(self):
        User.objects.filter(pk=self.owner.pk).update(phone_number='   ')
        self.assertRefused(self.challenge(token=self.token))

    def test_a_non_canonical_stored_phone_is_refused(self):
        """
        NON-BLANK IS NOT ENOUGH (Codex P2 on this PR).

        ``make_otp(user=...)`` with no ``msisdn`` argument canonicalises only a msisdn
        it was PASSED; it then falls back to ``user.phone_number`` VERBATIM as the SMS
        destination. So a stored ``+256…`` parses fine and would still be handed to
        the gateway with the ``+``.

        Reachable, not theoretical: the ``users_app/0008`` backfill deliberately skips
        invalid / unsupported / diverged / colliding rows, and ``mode=existing``
        attaches such an account without modifying it.
        """
        User.objects.filter(pk=self.owner.pk).update(
            phone_number=f'+{self.owner.phone_number}',
        )
        self.assertRefused(self.challenge(token=self.token))

    def test_a_malformed_stored_phone_is_refused(self):
        User.objects.filter(pk=self.owner.pk).update(phone_number='not-a-number')
        self.assertRefused(self.challenge(token=self.token))

    def test_an_unsupported_country_stored_phone_is_refused(self):
        # Kenya. `normalise_msisdn` is Uganda-only and raises UnsupportedCountry,
        # which must land on the same uniform refusal rather than escaping as a 500.
        User.objects.filter(pk=self.owner.pk).update(phone_number='254712345678')
        self.assertRefused(self.challenge(token=self.token))

    def test_a_locally_formatted_stored_phone_is_refused(self):
        # `0772…` is what a human types and what the backfill converts. An account it
        # skipped still holds it, and it is NOT what the canonical column should say.
        local = f'0{self.owner.phone_number[3:]}'
        User.objects.filter(pk=self.owner.pk).update(phone_number=local)
        self.assertRefused(self.challenge(token=self.token))

    def test_a_canonical_phone_is_accepted(self):
        # The negative control for the four above: the fixture's own canonical phone
        # must still pass, or the check would be refusing everybody.
        self.assertEqual(
            self.owner.phone_number,
            normalise_msisdn(self.owner.phone_number),
        )
        self.assertEqual(self.challenge(token=self.token).status_code, 200)

    # -- the shape itself ----------------------------------------------------

    def test_a_token_that_is_not_the_head_invitation_is_refused(self):
        """
        THE DEFENSIVE BRANCH, tested as one.

        `one_unresolved_owner_invitation_per_onboarding` makes this state unreachable
        through the database: an unresolved invitation IS the head, because the head
        rule takes the single unresolved row first. So the branch is exercised at the
        domain level with the selector patched, rather than by constructing a row the
        schema forbids.

        It stays in the code because "an invariant is enforced somewhere else" is not
        a reason for a credential path to skip a check — and a red-team mutation
        showed this guard was otherwise carried entirely by the resolved/expired
        checks above it.
        """
        from platform_admin_app.onboarding_reads import HeadInvitation
        other = OwnerInvitation.objects.create(
            onboarding=self.onboarding, invited_user=self.owner,
            issued_by=self.admin, token_hash='0' * 64,
            issued_at=self.invitation.issued_at,
            expires_at=self.invitation.expires_at,
            superseded_at=self.invitation.issued_at,
        )
        with mock.patch(
            'platform_admin_app.owner_claim.select_head_invitation',
            return_value=HeadInvitation(other, 'superseded', None),
        ):
            with self.assertRaises(owner_claim.ClaimRefused) as caught:
                owner_claim.resolve_claim_token(self.token)
        self.assertEqual(caught.exception.code, owner_claim.NOT_THE_HEAD_INVITATION)
        self.assertFalse(UserOtp.objects.exists())

    def test_every_refusal_is_byte_identical(self):
        """
        The anti-oracle property stated once, over the whole set: no caller can tell
        which refusal they hit. A test per case proves each is 400; this proves they
        are the SAME 400.
        """
        from django.utils import timezone
        bodies = set()

        def record(response):
            bodies.add((response.status_code, response.data['message']))

        record(self.challenge())
        record(self.challenge(token='unknown'))
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        record(self.challenge(token=self.token))
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=False)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            cancelled_at=timezone.now(), cancelled_by=self.admin,
        )
        record(self.challenge(token=self.token))

        self.assertEqual(len(bodies), 1, bodies)


# ═══════════════════════════════════════════════════════════════════════════════
# The two claimable states
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class PendingOwnerChallengeTests(_ClaimTestCase):
    """Case A — a brand-new owner who has never had customer access."""

    def test_the_challenge_succeeds_and_asks_for_credential_setup(self):
        response = self.challenge(token=self.token)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 200)
        self.assertEqual(response.data['message'], 'Verification code sent.')
        self.assertEqual(
            response.data['data'], {'credential_setup_required': True},
        )

    def test_the_issued_otp_carries_the_owner_claim_purpose(self):
        self.challenge(token=self.token)
        otp = self.otps().get()
        self.assertEqual(otp.purpose, OWNER_CLAIM_OTP_PURPOSE)
        self.assertEqual(otp.purpose, 'owner-claim')

    def test_the_response_carries_no_owner_or_invitation_identity(self):
        response = self.challenge(token=self.token)
        rendered = str(response.data)
        for secret in (
            self.token, self.invitation.token_hash, str(self.invitation.id),
            str(self.owner.id), str(self.restaurant.id), self.restaurant.name,
            self.owner.email, self.owner.phone_number,
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(str(secret), rendered)
        self.assertEqual(set(response.data['data']), {'credential_setup_required'})

    def test_nothing_durable_moves(self):
        before_password = User.objects.values_list('password', flat=True).get(
            pk=self.owner.pk,
        )

        self.assertEqual(self.challenge(token=self.token).status_code, 200)

        self.assertInvitationUntouched()
        owner = User.objects.get(pk=self.owner.pk)
        self.assertEqual(
            owner.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertEqual(owner.password, before_password)
        self.assertFalse(owner.has_usable_password())
        self.assertTrue(owner.is_active)

    def test_owner_control_is_still_not_established(self):
        """
        A challenge is not a claim. The Admin read must still say the credential is
        outstanding and control unproven — the two facts Step 2F.2 will move.
        """
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        summary = onboarding_summary(Restaurant.objects.get(pk=self.restaurant.pk))
        self.assertEqual(summary['owner_control']['status'], 'not_established')
        self.assertEqual(summary['invitation']['status'], 'pending')
        self.assertEqual(summary['invitation']['id'], str(self.invitation.id))

    def test_a_repeated_challenge_is_allowed_and_still_changes_nothing(self):
        # Re-requesting a code is an ordinary thing to do; it must not burn the
        # credential. `make_otp` replaces the row, so exactly one stays live.
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self.assertEqual(self.otps().count(), 1)
        self.assertInvitationUntouched()

    def test_delivery_failure_reports_failure_and_writes_nothing(self):
        with mock.patch(
            'platform_admin_app.endpoints.owner_claim.OtpManager.make_otp',
            return_value=False,
        ):
            response = self.challenge(token=self.token)

        self.assertEqual(response.status_code, 500)
        self.assertIn("couldn't send", response.data['message'])
        self.assertInvitationUntouched()
        self.assertEqual(
            User.objects.values_list('customer_access_state', flat=True).get(
                pk=self.owner.pk,
            ),
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )


@_UNTHROTTLED
class EstablishedOwnerChallengeTests(TestCase):
    """
    Case B — an ESTABLISHED owner invited to claim an ADDITIONAL restaurant.

    This is the multi-tenant false positive Step 2D.1 was built to avoid, seen from
    the other side: they already hold customer access for restaurant A and must not be
    asked to set a credential again for B.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.admin = _staff()
        self.owner = _restaurant_user('established')
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Second Site', location='Kabalagala', is_test=False,
            owner=ExistingOwner(self.owner.id),
            actor=self.admin, reason='A second restaurant for an existing owner.',
        )
        self.restaurant = self.creation.restaurant
        self.invitation = self.creation.invitation
        self.token = self.creation.claim_token

    def challenge(self):
        return self.client.post(
            CHALLENGE_PATH, {}, format='json',
            headers={CLAIM_TOKEN_HEADER: self.token},
        )

    def test_the_owner_is_established_to_begin_with(self):
        # The fixture's whole point; if `mode=existing` ever started writing the
        # access state, this class would silently stop testing case B.
        self.assertEqual(
            User.objects.values_list('customer_access_state', flat=True).get(
                pk=self.owner.pk,
            ),
            CUSTOMER_ACCESS_ESTABLISHED,
        )

    def test_the_challenge_succeeds_without_asking_for_credential_setup(self):
        response = self.challenge()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data['data'], {'credential_setup_required': False},
        )

    def test_the_issued_otp_carries_the_owner_claim_purpose(self):
        self.challenge()
        self.assertEqual(
            UserOtp.objects.get(user=self.owner).purpose, OWNER_CLAIM_OTP_PURPOSE,
        )

    def test_nothing_durable_moves(self):
        before = User.objects.values('password', 'customer_access_state').get(
            pk=self.owner.pk,
        )
        self.assertEqual(self.challenge().status_code, 200)
        after = User.objects.values('password', 'customer_access_state').get(
            pk=self.owner.pk,
        )
        self.assertEqual(before, after)
        invitation = OwnerInvitation.objects.get(pk=self.invitation.pk)
        self.assertFalse(invitation.is_resolved)

    def test_owner_control_is_still_not_established(self):
        self.assertEqual(self.challenge().status_code, 200)
        summary = onboarding_summary(Restaurant.objects.get(pk=self.restaurant.pk))
        self.assertEqual(summary['owner_control']['status'], 'not_established')
        self.assertEqual(summary['invitation']['status'], 'pending')

    def test_credential_setup_follows_the_access_axis_not_the_password(self):
        """
        THE DISCRIMINATING CASE, and the only one that separates the explicit axis
        from the inference §12 forbids.

        In the ordinary fixtures the two agree — a brand-new owner is pending AND has
        an unusable password; an existing owner is established AND has a usable one —
        so `not has_usable_password()` returns the right answer for the wrong reason,
        and a red-team mutation to exactly that survived the suite until this test
        existed.

        Here they DISAGREE: an established owner whose password happens to be
        unusable (an account that has never set one, which `create_user` permits).
        `customer_access_state` is what decides, so the answer must stay False —
        Step 2F.2 will not ask this owner to choose a credential, because they
        already hold customer access for their other restaurant.
        """
        self.owner.set_unusable_password()
        self.owner.save(update_fields=['password'])
        self.assertFalse(
            User.objects.get(pk=self.owner.pk).has_usable_password()
        )
        self.assertEqual(
            User.objects.values_list('customer_access_state', flat=True).get(
                pk=self.owner.pk,
            ),
            CUSTOMER_ACCESS_ESTABLISHED,
        )

        response = self.challenge()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIs(response.data['data']['credential_setup_required'], False)


# ═══════════════════════════════════════════════════════════════════════════════
# The purpose boundary
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class OwnerClaimPurposeTests(_ClaimTestCase):
    """
    ``owner-claim`` reaches a pending identity; the two customer-auth purposes still
    do not; and a verified owner-claim code mints nothing.
    """

    def test_the_purpose_is_not_a_customer_auth_purpose(self):
        from users_app.controllers.otp_manager import CUSTOMER_AUTH_OTP_PURPOSES
        self.assertNotIn(OWNER_CLAIM_OTP_PURPOSE, CUSTOMER_AUTH_OTP_PURPOSES)
        # Pinned exactly: if a future change widened this set, `owner-claim` would
        # stop reaching the one identity it exists for.
        self.assertEqual(CUSTOMER_AUTH_OTP_PURPOSES, {'login', 'reset-password'})

    def test_a_pending_owner_still_cannot_receive_a_login_otp(self):
        from users_app.controllers.otp_manager import OtpManager
        self.assertFalse(OtpManager().make_otp(user=self.owner, purpose='login'))
        self.assertFalse(self.otps().exists())

    def test_a_pending_owner_still_cannot_receive_a_reset_otp(self):
        from users_app.controllers.otp_manager import OtpManager
        self.assertFalse(
            OtpManager().make_otp(user=self.owner, purpose='reset-password')
        )
        self.assertFalse(self.otps().exists())

    def test_verifying_the_owner_claim_otp_mints_no_customer_token(self):
        """
        THE LOAD-BEARING ONE. ``verify_otp`` mints a customer session only for
        ``purpose == 'login'``. An owner-claim code verifies as valid — it is EVIDENCE
        Step 2F.2 will consume — and yields no token, no refresh, and no change to the
        identity's access state.
        """
        from users_app.controllers.otp_manager import OtpManager
        self.assertEqual(self.challenge(token=self.token).status_code, 200)

        result = OtpManager().verify_otp(user_id=str(self.owner.id), otp='1234')

        self.assertTrue(result['data']['valid'])
        self.assertNotIn('token', result['data'])
        self.assertNotIn('refresh', result['data'])
        self.assertEqual(
            User.objects.values_list('customer_access_state', flat=True).get(
                pk=self.owner.pk,
            ),
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_verify_otp_now_binds_the_purpose_it_is_asked_for(self):
        """
        THE GAP THIS TEST USED TO RECORD IS CLOSED — and its own instruction was to
        become this assertion once it was.

        Through Step 2F.1 ``verify_otp`` took no expected purpose: it selected the most
        recent live challenge for the identity and read ``purpose`` OFF THE ROW, so "the
        code was correct" did not mean "the code was issued for what I am authorising".
        Step 2F.2 added ``expected_purpose`` and ``expected_msisdn``, which NARROW the
        locked query rather than being checked after it — so a challenge issued for
        something else is never selected and is left entirely untouched.
        """
        from users_app.controllers.otp_manager import OtpManager
        import inspect

        parameters = inspect.signature(OtpManager.verify_otp).parameters
        self.assertIn('expected_purpose', parameters)
        self.assertIn('expected_msisdn', parameters)
        # BOTH DEFAULT TO None, so the four pre-existing callers are unchanged.
        self.assertIsNone(parameters['expected_purpose'].default)
        self.assertIsNone(parameters['expected_msisdn'].default)

    def test_an_unbound_verify_still_behaves_exactly_as_before(self):
        """
        Backward compatibility, exercised rather than asserted from the signature: the
        four existing callers pass neither argument and must keep selecting the most
        recent live challenge for the identity whatever it was issued for.
        """
        from users_app.controllers.otp_manager import OtpManager
        self.assertEqual(self.challenge(token=self.token).status_code, 200)

        result = OtpManager().verify_otp(user_id=str(self.owner.id), otp='1234')

        self.assertTrue(result['data']['valid'])
        self.assertIsNotNone(self.otps().get().consumed_at)


@_UNTHROTTLED
class CrossPurposeOtpReplacementTests(_ClaimTestCase):
    """
    OTP REPLACEMENT SEMANTICS — RESTATED TRUTHFULLY FOR STEP 2F.2.

    ━━ WHAT THESE TESTS USED TO SAY, AND WHY IT CHANGED ━━━━━━━━━━━━━━━━━━━━━━━━━━

    Through Step 2F.1 this endpoint called ``make_otp(user=…, purpose='owner-claim')``
    with NO ``msisdn``, so its row stored ``msisdn=NULL``. ``make_otp``'s replacement
    delete is ``filter(user=user, msisdn=msisdn).delete()`` — purpose-blind — and both
    generic callers (``login.py``, ``reset_password.py``) also pass no ``msisdn``. All
    three therefore shared the ``msisdn IS NULL`` bucket and destroyed each other's
    challenges. That was documented here rather than fixed, because purpose-scoping the
    delete would change what a login OTP does to a reset OTP.

    Step 2F.2 requires the challenge to RECORD ITS DESTINATION (``UserOtp.msisdn``), so
    redemption can bind the factor to the phone the code actually went to. Passing an
    explicit ``msisdn`` moves the owner-claim row into its own bucket — which changes the
    collision behaviour in BOTH directions, and improves it in both:

      * an owner-claim challenge NO LONGER destroys a live login or reset code;
      * a login or reset attempt NO LONGER destroys a live owner-claim challenge, which
        is load-bearing: otherwise requesting a login code would silently kill the claim
        challenge the owner is in the middle of using.

    Step 2F.2 left the delete itself purpose-blind, so ``login`` and ``reset-password``
    went on sharing the NULL bucket and replacing one another, and a newer owner-claim
    challenge still shadowed an older login code at the purpose-blind generic verify
    endpoint.

    ━━ D11 E-R2: THE DELETE IS PURPOSE-SCOPED, AND THE LOGIN ROUTE IS BOUND ━━━━━━━━

    ``make_otp`` now replaces within ``(user, msisdn, purpose)`` only, so ``login`` and
    ``reset-password`` coexist in the NULL bucket instead of deleting each other, and an
    account-resolving resend to the owner's phone no longer deletes a live owner-claim
    challenge (whose correct code used to be refused and charged to the invitation). The
    ``verify-otp`` endpoint now passes ``expected_purpose='login'``, so a coexisting
    owner-claim challenge is never selected, charged or consumed there. Redemption is
    unchanged: it binds purpose AND destination, so it selects its own row or none.
    """

    def _established_owner(self):
        User.objects.filter(pk=self.owner.pk).update(
            customer_access_state=CUSTOMER_ACCESS_ESTABLISHED,
        )
        self.owner.refresh_from_db()

    def _make(self, purpose):
        from users_app.controllers.otp_manager import OtpManager
        self.assertTrue(OtpManager().make_otp(user=self.owner, purpose=purpose))

    def test_the_challenge_records_its_delivery_destination(self):
        """The premise of everything below, and of Step 2F.2's factor binding."""
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        row = self.otps().get()
        self.assertEqual(row.msisdn, self.owner.phone_number)
        self.assertEqual(row.purpose, OWNER_CLAIM_OTP_PURPOSE)

    def test_an_owner_claim_challenge_no_longer_destroys_a_live_login_otp(self):
        """CHANGED BEHAVIOUR, and an improvement: it used to delete the login row."""
        self._established_owner()
        self._make('login')
        self.assertEqual(self.otps().get().purpose, 'login')

        self.assertEqual(self.challenge(token=self.token).status_code, 200)

        purposes = set(self.otps().values_list('purpose', flat=True))
        self.assertEqual(purposes, {'login', OWNER_CLAIM_OTP_PURPOSE})

    def test_a_login_otp_no_longer_destroys_a_live_owner_claim_challenge(self):
        """
        CHANGED BEHAVIOUR, and the load-bearing direction: an owner mid-claim who
        requests a login code must not lose the challenge they are using.
        """
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        claim_row = self.otps().get(purpose=OWNER_CLAIM_OTP_PURPOSE).pk

        self._established_owner()
        self._make('login')

        self.assertTrue(self.otps().filter(pk=claim_row).exists())
        self.assertEqual(
            set(self.otps().values_list('purpose', flat=True)),
            {'login', OWNER_CLAIM_OTP_PURPOSE},
        )

    def test_a_reset_otp_no_longer_destroys_a_live_owner_claim_challenge(self):
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self._established_owner()
        self._make('reset-password')
        self.assertEqual(
            set(self.otps().values_list('purpose', flat=True)),
            {'reset-password', OWNER_CLAIM_OTP_PURPOSE},
        )

    def test_login_and_reset_no_longer_replace_each_other(self):
        """
        CHANGED BY D11 E-R2. Both still pass no `msisdn`, so both still occupy the NULL
        bucket, but replacement is now scoped by purpose too: an anonymous reset request
        no longer deletes a login in progress, nor a login a reset in progress.
        """
        self._established_owner()
        self._make('login')
        self._make('reset-password')
        self.assertEqual(
            sorted(self.otps().values_list('purpose', 'msisdn', 'attempts', 'consumed_at')),
            [('login', None, 0, None), ('reset-password', None, 0, None)],
        )

    def test_a_second_challenge_still_replaces_the_first(self):
        """
        The one direction that is unambiguously desirable, and it still holds: both
        owner-claim rows share the same (user, msisdn) bucket, so a re-request supersedes
        the previous code rather than leaving two live.
        """
        self.challenge(token=self.token)
        first = self.otps().get().pk
        self.challenge(token=self.token)
        self.assertEqual(self.otps().count(), 1)
        self.assertNotEqual(self.otps().get().pk, first)

    def test_the_delete_is_purpose_scoped(self):
        """
        Stated structurally, so a future change to the replacement key is a DELIBERATE
        change that updates this test rather than a quiet one that slips past the tests
        above. D11 E-R2 made it purpose-scoped.
        """
        import inspect
        from users_app.controllers import otp_manager

        source = inspect.getsource(otp_manager.OtpManager.make_otp)
        self.assertIn(
            'UserOtp.objects.filter(user=user, msisdn=msisdn, purpose=purpose).delete()',
            source,
            'the replacement delete changed shape — re-derive which rows each caller '
            'now collides with before updating these expectations',
        )


# ═══════════════════════════════════════════════════════════════════════════════
# No lock, no transaction, across delivery
# ═══════════════════════════════════════════════════════════════════════════════

class NoLockSpansDeliveryTests(TransactionTestCase):
    """
    The PR #306 lesson, applied before it can become a finding.

    ``TransactionTestCase``, not ``TestCase``: the latter wraps every test in a
    transaction, so ``in_atomic_block`` would be ``True`` throughout and the central
    assertion would pass vacuously.

    WHAT CHANGED WITH D11 B2-C, AND WHAT DID NOT. Issuing the challenge now writes the
    replacement delete, the new challenge and its accounting row in ONE short
    transaction, which takes the owner's ``users`` row ``FOR KEY SHARE`` first (the
    order that keeps it from deadlocking a redemption). That is the ONLY locking read on
    this path, and it is confined to that transaction, which commits before delivery.
    The claim PREFLIGHT still takes no lock and opens no transaction, and nothing is
    held when a sender is entered — which is the property these tests exist for, now
    asserted at the sender itself rather than at ``make_otp``'s door.
    """

    reset_sequences = False

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.admin = _staff()
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Lock Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Lock', 'Owner', next(_PHONE), None),
            actor=self.admin, reason='Creating the lock-shape fixture.',
        )
        self.token = self.creation.claim_token
        self.restaurant = self.creation.restaurant

    def challenge(self):
        return self.client.post(
            CHALLENGE_PATH, {}, format='json',
            headers={CLAIM_TOKEN_HEADER: self.token},
        )

    @_UNTHROTTLED
    def test_no_transaction_is_open_when_the_otp_is_sent(self):
        """
        The complete proof, and it is complete for a simple reason: a
        ``select_for_update`` outside a transaction is released by the statement that
        took it. No open transaction therefore means no held row lock — there is
        nothing left to check.
        """
        observed = []

        def spy(_self, **kwargs):
            observed.append(transaction.get_connection().in_atomic_block)
            return True

        with mock.patch(
            'platform_admin_app.endpoints.owner_claim.OtpManager.make_otp', spy,
        ):
            self.assertEqual(self.challenge().status_code, 200)

        self.assertEqual(observed, [False], 'OTP delivery ran inside a transaction')

    @_UNTHROTTLED
    def test_nothing_is_held_when_the_sender_is_entered(self):
        """
        At the sender itself: no transaction open, and no transaction id held by this
        backend — so no row lock of any kind survives into delivery. ``ENV=test`` so the
        SMS goes out synchronously on this very connection; ``dev`` would hand it to a
        thread and prove nothing about this one.
        """
        if connection.vendor != 'postgresql':
            self.skipTest('pg_locks is PostgreSQL-specific.')
        observed = []

        def sms_spy(message, msisdn, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND locktype = 'transactionid'",
                )
                held = cursor.fetchone()[0]
            observed.append((transaction.get_connection().in_atomic_block, held))
            return True

        def env(key, **kwargs):
            return 'test' if key == 'ENV' else kwargs.get('default')

        with mock.patch('users_app.controllers.otp_manager.config', side_effect=env), \
                mock.patch('users_app.controllers.otp_manager.send_sms', sms_spy), \
                mock.patch('users_app.controllers.otp_manager.Messenger'):
            self.assertEqual(self.challenge().status_code, 200)

        self.assertEqual(observed, [(False, 0)], 'delivery ran with something held')

    @_UNTHROTTLED
    def test_the_only_locking_read_is_the_issuance_key_share_inside_its_transaction(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock clauses are PostgreSQL-specific.')
        statements = []

        def record(execute, sql, params, many, context):
            statements.append((sql, connection.in_atomic_block))
            return execute(sql, params, many, context)

        with mock.patch('users_app.controllers.otp_manager.send_sms', return_value=True), \
                connection.execute_wrapper(record):
            self.assertEqual(self.challenge().status_code, 200)

        locking = [
            (sql, atomic) for sql, atomic in statements
            if re.search(r'\bFOR (UPDATE|NO KEY UPDATE|SHARE|KEY SHARE)\b', sql)
        ]
        self.assertEqual(len(locking), 1, locking)
        sql, atomic = locking[0]
        self.assertIn('FOR KEY SHARE', sql)
        self.assertIn('"users"', sql)
        self.assertTrue(atomic, 'the key share must sit inside the issuance transaction')

        # The preflight — everything before the issuance transaction opens — runs in
        # autocommit and locks nothing.
        first_atomic = next(i for i, (_, atomic) in enumerate(statements) if atomic)
        self.assertTrue(first_atomic > 0)
        self.assertFalse(any(atomic for _, atomic in statements[:first_atomic]))
        # And it had closed before the send was concluded: the ledger's finalization,
        # which runs after delivery, runs in autocommit.
        finalize = [atomic for sql, atomic in statements
                    if sql.startswith('UPDATE "otp_issuances"')]
        self.assertEqual(finalize, [False])

    @_UNTHROTTLED
    def test_the_membership_barrier_is_never_acquired(self):
        """
        The challenge mutates no membership, so it has no business taking the
        parent-Restaurant barrier PR #306 installed — and taking it would put SMS
        delivery underneath the lock that serializes ownership decisions.
        """
        from restaurants_app.controllers import employee_membership_lock
        with mock.patch.object(
            employee_membership_lock, 'lock_restaurant_for_membership_mutation',
        ) as barrier:
            self.assertEqual(self.challenge().status_code, 200)
        barrier.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Ambient authentication must not matter
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class NoAmbientAuthorityTests(_ClaimTestCase):
    """The stored invitation decides whose claim this is. Nothing about the caller."""

    def test_a_customer_jwt_does_not_substitute_for_the_token(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        other = _restaurant_user('bearer')
        raw = str(RefreshToken.for_user(other).access_token)
        response = self.client.post(
            CHALLENGE_PATH, {}, format='json',
            headers={'Authorization': f'Bearer {raw}'},
        )
        self.assertRefused(response)

    def test_a_customer_jwt_does_not_redirect_the_claim_to_the_bearer(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        other = _restaurant_user('bearer2')
        raw = str(RefreshToken.for_user(other).access_token)
        response = self.challenge(
            token=self.token, Authorization=f'Bearer {raw}',
        )
        self.assertEqual(response.status_code, 200)
        # The OTP went to the INVITED user, not to whoever happened to be signed in.
        self.assertEqual(UserOtp.objects.get().user_id, self.owner.pk)

    def test_the_token_is_not_accepted_from_the_body(self):
        response = self.client.post(
            CHALLENGE_PATH, {'token': self.token, 'claim_token': self.token},
            format='json',
        )
        self.assertRefused(response)

    def test_the_token_is_not_accepted_from_the_query_string(self):
        response = self.client.post(
            f'{CHALLENGE_PATH}?token={self.token}&claim_token={self.token}',
            {}, format='json',
        )
        self.assertRefused(response)

    def test_the_token_is_not_accepted_from_a_cookie(self):
        self.client.cookies['owner_claim_token'] = self.token
        self.assertRefused(self.client.post(CHALLENGE_PATH, {}, format='json'))

    def test_the_view_declares_no_authenticator(self):
        from platform_admin_app.endpoints.owner_claim import OwnerClaimChallengeView
        self.assertEqual(OwnerClaimChallengeView.authentication_classes, [])


# ═══════════════════════════════════════════════════════════════════════════════
# Response hygiene
# ═══════════════════════════════════════════════════════════════════════════════

@_UNTHROTTLED
class ResponseHygieneTests(_ClaimTestCase):

    def test_success_is_no_store(self):
        self._assert_no_store(self.challenge(token=self.token))

    def test_refusal_is_no_store(self):
        self._assert_no_store(self.challenge(token='unknown'))

    def _assert_no_store(self, response):
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['Pragma'], 'no-cache')
        self.assertEqual(response['Expires'], '0')

    def test_no_cookie_and_no_location_are_set(self):
        response = self.challenge(token=self.token)
        self.assertEqual(len(response.cookies), 0)
        self.assertNotIn('Location', response)


# ═══════════════════════════════════════════════════════════════════════════════
# Throttling
# ═══════════════════════════════════════════════════════════════════════════════

class ThrottleTests(_ClaimTestCase):
    """
    The throttle is defence against request and SMS abuse, not the security boundary.
    Pinned anyway: an unthrottled endpoint that can send an SMS per request is a bill.
    """

    @claim_rate('2/min')
    def test_the_endpoint_is_throttled(self):
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        self.assertEqual(self.challenge(token=self.token).status_code, 200)
        throttled = self.challenge(token=self.token)
        self.assertEqual(throttled.status_code, 429)

    @claim_rate('1/min')
    def test_a_throttled_response_leaks_no_invitation_state(self):
        self.challenge(token=self.token)
        throttled = self.challenge(token=self.token)
        self.assertEqual(throttled.status_code, 429)
        rendered = str(throttled.data)
        for secret in (self.token, str(self.invitation.id), str(self.owner.id)):
            self.assertNotIn(str(secret), rendered)

    @claim_rate('1/min')
    def test_the_throttle_refuses_before_an_otp_is_sent(self):
        self.challenge(token=self.token)
        UserOtp.objects.all().delete()
        self.assertEqual(self.challenge(token=self.token).status_code, 429)
        self.assertFalse(UserOtp.objects.exists())

    def test_the_throttle_key_is_the_client_ip_and_never_the_token(self):
        """
        DRF throttle cache keys surface in diagnostics, so a raw credential must not
        be one. ``AnonRateThrottle`` keys on the IP; this pins that the view did not
        subclass it into something token-aware.
        """
        from platform_admin_app.endpoints.owner_claim import (
            OwnerClaimChallengeThrottle,
        )
        from rest_framework.throttling import AnonRateThrottle
        self.assertTrue(issubclass(OwnerClaimChallengeThrottle, AnonRateThrottle))
        self.assertEqual(OwnerClaimChallengeThrottle.scope, 'owner_claim_challenge')

        request = mock.Mock()
        request.user = None
        request.META = {'REMOTE_ADDR': '203.0.113.9'}
        key = OwnerClaimChallengeThrottle().get_cache_key(request, view=None)
        self.assertIn('203.0.113.9', key)
        self.assertNotIn(self.token, key)


# ═══════════════════════════════════════════════════════════════════════════════
# Structural secret-safety ratchet
# ═══════════════════════════════════════════════════════════════════════════════

class ClaimSecretSafetyTests(TestCase):
    """
    An AST scan over the two claim modules (OWNER-CLAIM-SAFE-00).

    The behavioural tests above prove today's code does not leak a credential or
    mutate durable state. This proves the NEXT edit cannot do so quietly: the things
    forbidden here are forbidden by shape, not by anyone remembering.
    """

    MODULES = (
        'platform_admin_app/owner_claim.py',
        'platform_admin_app/endpoints/owner_claim.py',
    )

    # Names that must never be CALLED from the claim modules.
    FORBIDDEN_CALLS = frozenset({
        'issue_customer_tokens',   # Step 2F.2's job, under its own transaction
        'set_password',
        'set_unusable_password',
        'for_user',                # RefreshToken.for_user — the mint
        'save',                    # no durable write of any kind lives here
        'create',
        'update',
        'delete',
        'get_or_create',
        'update_or_create',
        'select_for_update',       # a preflight takes no lock (§8)
        'atomic',                  # ...and opens no transaction
    })

    # Attributes that must never be ASSIGNED.
    FORBIDDEN_ASSIGNMENTS = frozenset({
        'customer_access_state', 'password', 'prompt_password_change',
        'last_login', 'is_active', 'account_type',
        'consumed_at', 'cancelled_at', 'superseded_at', 'expires_at', 'token_hash',
        'owner', 'owner_id', 'roles', 'active', 'deleted',
    })

    def _trees(self):
        for relative in self.MODULES:
            path = REPO_ROOT / relative
            yield relative, ast.parse(path.read_text(encoding='utf-8'))

    def test_no_forbidden_call_appears(self):
        for relative, tree in self._trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = None
                if isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    name = node.func.id
                with self.subTest(module=relative, call=name):
                    self.assertNotIn(
                        name, self.FORBIDDEN_CALLS,
                        f'{relative} calls {name!r}. The challenge writes nothing '
                        f'durable and takes no lock — Step 2F.2 owns all of that.',
                    )

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

    def test_the_token_is_never_logged(self):
        """
        No logging call may take the raw token or the hash as an argument. Checked by
        NAME, so ``logger.info('...', raw_token)`` fails even though the format string
        looks innocent.
        """
        leaky = {'raw_token', 'token', 'token_hash', 'claim_token'}
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

    # The ONLY keys this module may read out of a request body. The claim token is not
    # among them and never will be — it has exactly one entry point, the header.
    #
    # NARROWED FROM A BLANKET BAN ON `request.data` (Step 2F.2). Redemption legitimately
    # reads a body: it carries the OTP and, for a brand-new owner, the chosen password.
    # Banning the attribute outright would have meant either giving up the scan or moving
    # redemption somewhere it is not scanned — so the rule now says what it actually
    # means: a body key must be on this list.
    BODY_KEYS = frozenset({'otp', 'new_password'})

    def test_the_claim_token_is_read_from_the_header_and_nowhere_else(self):
        source = (REPO_ROOT / 'platform_admin_app/endpoints/owner_claim.py').read_text()
        # These have NO legitimate use here — a credential in a query string lands in
        # access logs and browser history, and a cookie is ambient authority by another
        # name.
        for forbidden in ('query_params', 'COOKIES', 'GET.get', 'request.POST'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn('request.headers.get', source)

        # ...and every literal key read out of a mapping must be an expected body field.
        # `request.headers.get(CLAIM_TOKEN_HEADER)` passes a NAME, not a literal, so the
        # header read is not caught by this and the body reads are.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            key = node.args[0].value
            with self.subTest(key=key):
                self.assertIn(
                    key, self.BODY_KEYS,
                    f'{key!r} is read out of a mapping in the claim endpoints. If it '
                    f'is a body field it belongs in BODY_KEYS; if it is the claim '
                    f'token it belongs in the header and nowhere else.',
                )

    def test_the_preflight_result_carries_no_credential(self):
        """
        Pinned as an EXACT set, so a new field is a deliberate decision.

        ``canonical_phone`` (Step 2F.2) is the delivery DESTINATION, not a credential:
        it is the account's own already-stored phone number, it is what the endpoint
        hands `make_otp` so the `UserOtp` row records where the code went, and it never
        appears in a response. A claim token, its hash and an OTP are the things that
        must never be reachable through this object.
        """
        fields = set(owner_claim.ClaimPreflight.__dataclass_fields__)
        self.assertEqual(
            fields,
            {
                'invitation', 'onboarding', 'restaurant', 'invited_user',
                'credential_setup_required', 'canonical_phone',
            },
        )
        for forbidden in ('token', 'raw_token', 'token_hash', 'claim_token'):
            self.assertNotIn(forbidden, fields)

    def test_the_refusal_exception_carries_no_details(self):
        refusal = owner_claim.ClaimRefused(owner_claim.UNKNOWN_TOKEN)
        self.assertFalse(hasattr(refusal, 'details'))
        self.assertEqual(refusal.code, 'unknown_token')
