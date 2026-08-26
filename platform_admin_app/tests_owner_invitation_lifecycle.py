"""
``platform_admin_app.onboarding_invitations`` — the Step-2E credential lifecycle.

The DOMAIN suite: the services called directly, with no HTTP, no session and no
client. Authority, the audit row and the response envelope belong to the adapter and
are covered by ``tests_owner_invitation_endpoint``; what is pinned here is what the
two operations MEAN — which states they are legitimate from, what they bind to, what
they must never touch, and how the concurrency token behaves.
"""
import ast
import inspect
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
)
from platform_admin_app import onboarding_creation, onboarding_invitations, sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    MISSING_OWNER_MEMBERSHIP,
    MULTIPLE_OWNER_MEMBERSHIPS,
    OWNER_MEMBERSHIP_MISMATCH,
    OwnerConsistencyError,
)
from platform_admin_app.onboarding_creation import NewOwner
from platform_admin_app.onboarding_reads import (
    onboarding_summary,
    select_head_invitation,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

REASON = 'Rotating the claim credential after the operator lost the response.'

# A sentinel, so a test can pass `expected=None` and actually exercise the missing-
# token path. A plain `None` default would silently substitute the real id and make
# that case untestable — which is exactly the sort of helper that turns a suite
# green without proving anything.
_DEFAULT = object()

# Distinct phone range from every other admin suite. Yielded in the LOCAL form the
# creation service accepts; `normalise_msisdn` canonicalises it to 256772XXXXXX.
_PHONE = iter(f'0772{n:06d}' for n in range(810000, 819999))

# Every external I/O path a delivery regression would reach for.
_DELIVERY_PATCHES = (
    'notifications_app.controllers.sms.send_sms',
    'misc_app.controllers.notifications.notification.Notification.create_notification',
    'misc_app.controllers.save_action_log.save_action',
)


def _make_admin(email='oi-admin@t.com', username='oi-admin'):
    return User.objects.create_user(
        first_name='Ada', last_name='Min', email=email, username=username,
        country='UG', password='x', roles=[],
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


def _make_restaurant_user(email, **kwargs):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='Rest', last_name='User', email=email, phone_number=phone,
        username=phone, country='UG', password='x', roles=[],
        account_type=ACCOUNT_TYPE_RESTAURANT_USER, **kwargs,
    )


class _LifecycleTestCase(TestCase):
    """One admin-created restaurant with its initial pending invitation."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Kampala Bistro', location='Kololo', is_test=False,
            owner=NewOwner('Jane', 'Doe', next(_PHONE), None),
            actor=self.admin, reason='Creating after the signed agreement.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.owner = self.creation.owner
        self.invitation = self.creation.invitation

    # --- helpers ---

    def reissue(self, expected=_DEFAULT, actor=None, reason=REASON):
        return onboarding_invitations.reissue_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=(
                self.invitation.id if expected is _DEFAULT else expected
            ),
            actor=actor or self.admin,
            reason=reason,
        )

    def cancel(self, expected=_DEFAULT, actor=None, reason=REASON):
        return onboarding_invitations.cancel_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=(
                self.invitation.id if expected is _DEFAULT else expected
            ),
            actor=actor or self.admin,
            reason=reason,
        )

    def expire(self, invitation):
        """Push an invitation's window into the past. It stays UNRESOLVED."""
        past = timezone.now() - timedelta(days=3)
        OwnerInvitation.objects.filter(pk=invitation.pk).update(
            issued_at=past - timedelta(days=1), expires_at=past,
        )
        invitation.refresh_from_db()
        return invitation

    def assertRefused(self, callable_, code):
        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as ctx:
            callable_()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def unresolved_count(self):
        return OwnerInvitation.objects.filter(
            onboarding=self.onboarding,
            consumed_at__isnull=True,
            cancelled_at__isnull=True,
            superseded_at__isnull=True,
        ).count()


# --- §34 reissue: the four legitimate states ---------------------------------

class ReissueFromPendingTests(_LifecycleTestCase):
    """The ordinary case: a live credential is rotated."""

    def setUp(self):
        super().setUp()
        self.result = self.reissue()
        self.invitation.refresh_from_db()

    def test_the_old_invitation_is_superseded(self):
        self.assertIsNotNone(self.invitation.superseded_at)

    def test_a_new_unresolved_invitation_exists(self):
        new = self.result.invitation
        self.assertIsNone(new.consumed_at)
        self.assertIsNone(new.cancelled_at)
        self.assertIsNone(new.superseded_at)
        self.assertNotEqual(new.id, self.invitation.id)

    def test_exactly_one_invitation_is_unresolved(self):
        """The database invariant, asserted as an outcome rather than assumed."""
        self.assertEqual(self.unresolved_count(), 1)

    def test_history_is_retained_not_replaced(self):
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 2,
        )

    def test_the_result_reports_what_it_found_and_what_it_did(self):
        self.assertEqual(self.result.head, self.invitation)
        self.assertEqual(self.result.head_status, 'pending')
        self.assertTrue(self.result.superseded)
        self.assertTrue(self.result.changed)

    def test_the_old_row_keeps_its_own_token_hash_and_expiry(self):
        """
        Superseding stamps ONE column. A rotation that rewrote the old row's expiry or
        hash would destroy the evidence of what the dead credential actually was.
        """
        original = OwnerInvitation.objects.get(pk=self.invitation.pk)
        self.assertEqual(original.token_hash, self.creation.invitation.token_hash)
        self.assertEqual(original.expires_at, self.creation.invitation.expires_at)
        self.assertIsNone(original.cancelled_at)
        self.assertIsNone(original.consumed_at)

    def test_the_read_now_reports_the_new_invitation_as_the_head(self):
        block = onboarding_summary(self.restaurant)['invitation']
        self.assertEqual(block['id'], str(self.result.invitation.id))
        self.assertEqual(block['status'], 'pending')


class ReissueFromExpiredTests(_LifecycleTestCase):
    """
    An expired invitation is STILL UNRESOLVED and still holds the per-onboarding slot,
    so it must be superseded before a replacement can be inserted.
    """

    def setUp(self):
        super().setUp()
        self.expire(self.invitation)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'], 'expired',
        )
        self.result = self.reissue()
        self.invitation.refresh_from_db()

    def test_the_expired_invitation_is_superseded(self):
        self.assertIsNotNone(self.invitation.superseded_at)

    def test_no_persisted_expired_stamp_or_status_is_invented(self):
        """
        Expiry stays DERIVED. There is no ``status`` column, no ``expired_at``, and the
        row is stamped ``superseded_at`` — the thing that actually happened.
        """
        self.assertFalse(hasattr(self.invitation, 'status'))
        self.assertFalse(hasattr(self.invitation, 'expired_at'))
        self.assertIsNone(self.invitation.cancelled_at)
        self.assertIsNone(self.invitation.consumed_at)

    def test_the_replacement_gets_a_fresh_window(self):
        self.assertGreater(self.result.invitation.expires_at, timezone.now())

    def test_the_head_status_recorded_is_expired_not_pending(self):
        """
        The operational distinction between "I killed a live link" and "I replaced a
        dead one", captured before the stamp made it unrecoverable.
        """
        self.assertEqual(self.result.head_status, 'expired')

    def test_exactly_one_invitation_is_unresolved(self):
        self.assertEqual(self.unresolved_count(), 1)


class ReissueAfterCancellationTests(_LifecycleTestCase):
    """
    §31: reopening a deliberately-closed onboarding is a REQUIRED workflow.

    There is no "uncancel". The cancelled row stays cancelled forever as historical
    evidence and a NEW credential is minted beside it.
    """

    def setUp(self):
        super().setUp()
        self.cancel()
        self.invitation.refresh_from_db()
        self.cancelled_at = self.invitation.cancelled_at
        self.result = self.reissue()
        self.invitation.refresh_from_db()

    def test_the_cancelled_invitation_is_untouched(self):
        self.assertEqual(self.invitation.cancelled_at, self.cancelled_at)
        self.assertIsNone(self.invitation.superseded_at)

    def test_a_new_pending_invitation_exists(self):
        self.assertIsNone(self.result.invitation.cancelled_at)
        self.assertEqual(
            onboarding_summary(self.restaurant)['invitation']['status'], 'pending',
        )

    def test_nothing_was_superseded(self):
        """A resolved head occupies no slot, so there was nothing to free."""
        self.assertFalse(self.result.superseded)
        self.assertEqual(self.result.head_status, 'cancelled')

    def test_exactly_one_invitation_is_unresolved(self):
        self.assertEqual(self.unresolved_count(), 1)


class ReissueAfterAPreviousOwnerConsumedTests(_LifecycleTestCase):
    """
    §32: a historical invitation consumed by a PREVIOUS owner must not permanently
    block a fresh credential for the CURRENT one.

    The question reissue asks is *has the CURRENT owner's control been established?*,
    never *has any invitation in history ever been consumed?*
    """

    def setUp(self):
        super().setUp()
        # The original owner claims the restaurant.
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self.invitation.refresh_from_db()

        # Ownership then moves to somebody else, consistently.
        self.new_owner = _make_restaurant_user('successor@t.com')
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        RestaurantEmployee.objects.create(
            user=self.new_owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            owner=self.new_owner,
        )
        self.restaurant.refresh_from_db()

    def test_the_current_owner_has_no_control_evidence(self):
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(summary['owner_control']['status'], 'not_established')
        self.assertEqual(summary['invitation']['status'], 'consumed')

    def test_a_fresh_invitation_is_issued_to_the_current_owner(self):
        result = self.reissue()
        self.assertEqual(result.invitation.invited_user_id, self.new_owner.id)
        self.assertNotEqual(result.invitation.invited_user_id, self.owner.id)

    def test_the_historical_consumed_row_is_not_mutated(self):
        before = OwnerInvitation.objects.get(pk=self.invitation.pk)
        self.reissue()
        after = OwnerInvitation.objects.get(pk=self.invitation.pk)
        self.assertEqual(after.consumed_at, before.consumed_at)
        self.assertEqual(after.invited_user_id, self.owner.id)
        self.assertIsNone(after.superseded_at)
        self.assertIsNone(after.cancelled_at)

    def test_evidence_is_not_transferred_to_the_new_owner(self):
        self.reissue()
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(summary['owner_control']['status'], 'not_established')


class ALiveCredentialIsAlwaysRevocableTests(_LifecycleTestCase):
    """
    THE REGRESSION FROM CODEX'S P2 REVIEW, reproduced end to end.

    An outstanding claim credential must always be nameable by the read and killable
    by the API. The first version of this PR could produce one that was neither.

    The sequence: owner A consumes an invitation; ownership moves to B; a credential
    is issued to B; ownership moves BACK to A. The selector then answered "what is
    this onboarding's invitation?" with A's CONSUMED row, because owner-control
    evidence came first in one short-circuiting chain. So the read published a
    resolved id, cancellation refused that id (already resolved) and refused B's id
    (stale), and reissue refused outright because A's control was established.
    **B's live credential could not be revoked through the API at all** — the one
    outcome a credential-lifecycle surface must never produce.

    Fixed by ordering the UNRESOLVED row first and computing owner control as its own
    independent lookup. Both axes are now simultaneously true, which is the point of
    their being two axes.
    """

    def setUp(self):
        super().setUp()
        # 1. The original owner claims the restaurant.
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )

        # 2. Ownership moves to a successor, consistently.
        self.successor = _make_restaurant_user('revocable-successor@t.com')
        self._reseat(self.owner, self.successor)

        # 3. A credential is issued to the successor.
        self.live = self.reissue(expected=self.invitation.id).invitation

        # 4. Ownership moves BACK to the original owner.
        self._reseat(self.successor, self.owner)

    def _reseat(self, outgoing, incoming):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=outgoing,
        ).update(active=False)
        RestaurantEmployee.objects.update_or_create(
            restaurant=self.restaurant, user=incoming,
            defaults={'roles': [RESTAURANT_OWNER], 'active': True},
        )
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=incoming)
        self.restaurant.refresh_from_db()

    def test_the_read_names_the_live_credential_not_the_consumed_one(self):
        block = onboarding_summary(self.restaurant)['invitation']
        self.assertEqual(block['id'], str(self.live.id))
        self.assertEqual(block['status'], 'pending')

    def test_owner_control_is_still_reported_from_the_consumed_row(self):
        """Both axes true at once — the live credential does not erase the evidence."""
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(
            summary['owner_control']['status'], 'invitation_redeemed',
        )
        self.assertEqual(
            summary['owner_control']['evidence_at'],
            OwnerInvitation.objects.get(pk=self.invitation.pk)
            .consumed_at.isoformat(),
        )

    def test_the_live_credential_can_be_cancelled(self):
        """The whole point: it is revocable."""
        result = self.cancel(expected=self.live.id)
        self.assertTrue(result.changed)
        self.live.refresh_from_db()
        self.assertIsNotNone(self.live.cancelled_at)

    def test_reissue_is_still_refused_because_control_is_established(self):
        """
        Correct, and not a contradiction with the above. There is nothing to issue a
        NEW credential for — the current owner has claimed — but the outstanding one
        still has to be killable, and cancellation is what kills it.
        """
        self.assertRefused(
            lambda: self.reissue(expected=self.live.id),
            onboarding_invitations.OWNER_CONTROL_ALREADY_ESTABLISHED,
        )

    def test_cancelling_it_leaves_the_control_evidence_intact(self):
        """
        And the head then falls back to the current owner's redemption — ``consumed``,
        not ``cancelled``.

        That is the ordering doing its job rather than an oversight: with nothing
        outstanding, the meaningful terminal fact about this onboarding's credential is
        that the CURRENT owner claimed one, which is exactly what branch 2 is for. The
        cancelled row belongs to a credential issued to somebody who is no longer the
        owner, and it stays in the history where the audit log can find it.
        """
        self.cancel(expected=self.live.id)
        summary = onboarding_summary(self.restaurant)
        self.assertEqual(
            summary['owner_control']['status'], 'invitation_redeemed',
        )
        self.assertEqual(summary['invitation']['status'], 'consumed')
        self.assertEqual(summary['invitation']['id'], str(self.invitation.id))
        # The cancellation really happened; it is simply no longer the headline.
        self.live.refresh_from_db()
        self.assertIsNotNone(self.live.cancelled_at)


class HeadSelectionOrderingTests(_LifecycleTestCase):
    """
    The ordering rule itself, stated directly: an UNRESOLVED row is always the head.
    """

    def test_an_unresolved_row_outranks_a_current_owner_redemption(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        live = OwnerInvitation.objects.create(
            onboarding=self.onboarding, invited_user=self.owner,
            issued_by=self.admin, token_hash='f' * 64,
            issued_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        head = select_head_invitation(self.onboarding, self.restaurant)
        self.assertEqual(head.invitation.id, live.id)
        self.assertEqual(head.status, 'pending')

    def test_control_evidence_is_a_separate_row_from_the_head(self):
        consumed_id = self.invitation.id
        OwnerInvitation.objects.filter(pk=consumed_id).update(
            consumed_at=timezone.now(),
        )
        live = OwnerInvitation.objects.create(
            onboarding=self.onboarding, invited_user=self.owner,
            issued_by=self.admin, token_hash='e' * 64,
            issued_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        head = select_head_invitation(self.onboarding, self.restaurant)
        self.assertEqual(head.invitation.id, live.id)
        self.assertEqual(head.control_evidence.id, consumed_id)
        self.assertTrue(head.establishes_current_owner_control)

    def test_they_are_the_same_row_on_a_settled_restaurant(self):
        """The ordinary case: one invitation, claimed by the owner, nothing outstanding."""
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        head = select_head_invitation(self.onboarding, self.restaurant)
        self.assertEqual(head.invitation.id, self.invitation.id)
        self.assertEqual(head.control_evidence.id, self.invitation.id)
        self.assertEqual(head.status, 'consumed')

    def test_a_previous_owners_redemption_is_not_control_evidence(self):
        """Unchanged by the reordering — evidence is still per-owner."""
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        successor = _make_restaurant_user('ordering-successor@t.com')
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=successor)
        self.restaurant.refresh_from_db()
        head = select_head_invitation(self.onboarding, self.restaurant)
        self.assertIsNone(head.control_evidence)
        self.assertFalse(head.establishes_current_owner_control)
        self.assertEqual(head.status, 'consumed')


# --- §34 reissue: the refusals ------------------------------------------------

class ReissueRefusalTests(_LifecycleTestCase):

    def test_current_owner_already_redeemed_is_refused(self):
        """
        §11 case 5. Control is established; this is no longer an invitation problem,
        and a second live credential would have nothing to do but be stolen.
        """
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self.assertEqual(
            onboarding_summary(self.restaurant)['owner_control']['status'],
            'invitation_redeemed',
        )
        self.assertRefused(
            self.reissue,
            onboarding_invitations.OWNER_CONTROL_ALREADY_ESTABLISHED,
        )
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_a_legacy_adopted_restaurant_is_refused(self):
        other = Restaurant.objects.create(
            name='Legacy Ltd', location='Ntinda', country='UG', owner=self.owner,
        )
        RestaurantOnboarding.objects.create(
            restaurant=other, source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        self.assertRefused(
            lambda: onboarding_invitations.reissue_owner_invitation(
                restaurant_id=other.id, expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            ),
            onboarding_invitations.OWNER_INVITATION_NOT_APPLICABLE,
        )

    def test_legacy_provenance_is_never_converted(self):
        other = Restaurant.objects.create(
            name='Legacy Two', location='Ntinda', country='UG', owner=self.owner,
        )
        row = RestaurantOnboarding.objects.create(
            restaurant=other, source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        with self.assertRaises(onboarding_invitations.OwnerInvitationError):
            onboarding_invitations.reissue_owner_invitation(
                restaurant_id=other.id, expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            )
        row.refresh_from_db()
        self.assertEqual(row.source, ONBOARDING_SOURCE_LEGACY_ADOPTED)

    def test_an_untracked_restaurant_is_refused_and_no_row_is_manufactured(self):
        other = Restaurant.objects.create(
            name='Untracked Ltd', location='Bugolobi', country='UG',
            owner=self.owner,
        )
        self.assertRefused(
            lambda: onboarding_invitations.reissue_owner_invitation(
                restaurant_id=other.id, expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            ),
            onboarding_invitations.ONBOARDING_NOT_TRACKED,
        )
        self.assertFalse(
            RestaurantOnboarding.objects.filter(restaurant=other).exists(),
        )

    def test_a_missing_restaurant_is_refused(self):
        import uuid
        self.assertRefused(
            lambda: onboarding_invitations.reissue_owner_invitation(
                restaurant_id=uuid.uuid4(),
                expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            ),
            onboarding_invitations.RESTAURANT_NOT_FOUND,
        )

    def test_a_soft_deleted_restaurant_is_refused_the_same_way(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        self.assertRefused(
            self.reissue, onboarding_invitations.RESTAURANT_NOT_FOUND,
        )

    def test_a_stale_expected_id_is_refused(self):
        fresh = self.reissue()
        self.assertRefused(
            lambda: self.reissue(expected=self.invitation.id),
            onboarding_invitations.STALE_OWNER_INVITATION,
        )
        # And the newer credential was NOT touched by the stale request.
        fresh.invitation.refresh_from_db()
        self.assertIsNone(fresh.invitation.superseded_at)

    def test_an_invitation_from_another_restaurant_is_stale_not_accepted(self):
        other = onboarding_creation.create_admin_restaurant(
            name='Other Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Sam', 'Kato', next(_PHONE), None),
            actor=self.admin, reason='Creating the second tenant.',
        )
        self.assertRefused(
            lambda: self.reissue(expected=other.invitation.id),
            onboarding_invitations.STALE_OWNER_INVITATION,
        )

    def test_conflict_details_carry_no_token_material(self):
        self.reissue()
        exc = self.assertRefused(
            lambda: self.reissue(expected=self.invitation.id),
            onboarding_invitations.STALE_OWNER_INVITATION,
        )
        blob = repr(exc.details) + str(exc)
        self.assertNotIn(self.invitation.token_hash, blob)
        self.assertNotIn(self.owner.email or '@', blob)
        self.assertNotIn(self.owner.phone_number, blob)

    def test_an_inactive_owner_is_refused(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertRefused(
            self.reissue, onboarding_invitations.OWNER_ACCOUNT_INACTIVE,
        )

    def test_the_inactive_owner_is_not_reactivated(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(onboarding_invitations.OwnerInvitationError):
            self.reissue()
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)

    def test_a_platform_staff_owner_can_never_be_invited(self):
        """
        The owner FK could be repointed at a platform-staff account by some future
        path; a claim credential must never be minted for one.
        """
        User.objects.filter(pk=self.owner.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertRefused(
            self.reissue,
            onboarding_invitations.OWNER_ACCOUNT_NOT_RESTAURANT_USER,
        )

    def test_the_no_owner_guard_refuses_rather_than_minting(self):
        """
        ``Restaurant.owner`` is a NOT NULL column, so this state is unreachable through
        the database — the guard is defence in depth, mirroring the one
        ``onboarding_reads._redeemed_by_current_owner`` already carries. Exercised as a
        unit test of the branch rather than by forcing a row the schema forbids, which
        is the honest way to cover a guard that exists because "unreachable" is a
        property of today's schema.
        """
        detached = Restaurant(id=self.restaurant.id, name='X', location='Y')
        detached.owner_id = None
        self.assertRefused(
            lambda: onboarding_invitations._resolve_current_owner(detached),
            onboarding_invitations.OWNER_ACCOUNT_NOT_FOUND,
        )


class ReissueRequiresOwnerConsistencyTests(_LifecycleTestCase):
    """
    §12: minting authority needs a sound owner target, and the check VALIDATES rather
    than repairs.
    """

    def test_a_missing_owner_membership_refuses_the_reissue(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        with self.assertRaises(OwnerConsistencyError) as ctx:
            self.reissue()
        self.assertEqual(ctx.exception.code, MISSING_OWNER_MEMBERSHIP)

    def test_two_live_owner_memberships_refuse_the_reissue(self):
        RestaurantEmployee.objects.create(
            user=_make_restaurant_user('second-owner@t.com'),
            restaurant=self.restaurant, roles=[RESTAURANT_OWNER], active=True,
        )
        with self.assertRaises(OwnerConsistencyError) as ctx:
            self.reissue()
        self.assertEqual(ctx.exception.code, MULTIPLE_OWNER_MEMBERSHIPS)

    def test_a_mismatched_owner_refuses_the_reissue(self):
        other = _make_restaurant_user('mismatch@t.com')
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(user=other)
        with self.assertRaises(OwnerConsistencyError) as ctx:
            self.reissue()
        self.assertEqual(ctx.exception.code, OWNER_MEMBERSHIP_MISMATCH)

    def test_nothing_is_written_when_consistency_fails(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        with self.assertRaises(OwnerConsistencyError):
            self.reissue()
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.superseded_at)
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_ownership_is_never_repaired(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        with self.assertRaises(OwnerConsistencyError):
            self.reissue()
        self.assertFalse(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True,
            ).exists(),
        )
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.owner_id, self.owner.id)

    def test_a_manager_membership_never_satisfies_the_invariant(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(roles=[RESTAURANT_MANAGER])
        with self.assertRaises(OwnerConsistencyError):
            self.reissue()


# --- §12 current-owner binding ------------------------------------------------

class ReissueBindsToTheCurrentOwnerTests(_LifecycleTestCase):

    def test_the_new_invitation_names_restaurant_owner(self):
        result = self.reissue()
        self.restaurant.refresh_from_db()
        self.assertEqual(
            result.invitation.invited_user_id, self.restaurant.owner_id,
        )

    def test_it_does_not_copy_the_previous_invitations_invited_user(self):
        """
        The previous row's ``invited_user`` may name a FORMER owner. Binding to it
        would issue a fresh live credential to somebody who no longer runs the
        business.
        """
        stranger = _make_restaurant_user('stranger@t.com')
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            invited_user=stranger,
        )
        result = self.reissue()
        self.assertEqual(result.invitation.invited_user_id, self.owner.id)
        self.assertNotEqual(result.invitation.invited_user_id, stranger.id)

    def test_it_does_not_bind_to_the_onboarding_creator(self):
        """``created_by`` is the platform staff member who created the tenant."""
        result = self.reissue()
        self.assertNotEqual(
            result.invitation.invited_user_id, self.onboarding.created_by_id,
        )

    def test_the_service_takes_no_owner_argument_at_all(self):
        """
        The strongest form of "the request carries no owner identity": there is no
        parameter through which one could be supplied.
        """
        signature = inspect.signature(
            onboarding_invitations.reissue_owner_invitation,
        )
        self.assertEqual(
            set(signature.parameters),
            {'restaurant_id', 'expected_invitation_id', 'actor', 'reason'},
        )

    def test_issued_by_is_the_actor_not_the_owner(self):
        result = self.reissue()
        self.assertEqual(result.invitation.issued_by_id, self.admin.id)


# --- §14 / §27 the credential ------------------------------------------------

class ReissueCredentialTests(_LifecycleTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.reissue()

    def test_only_the_hash_is_persisted(self):
        stored = OwnerInvitation.objects.get(pk=self.result.invitation.pk)
        self.assertEqual(
            stored.token_hash, sessions.hash_token(self.result.claim_token),
        )
        self.assertNotEqual(stored.token_hash, self.result.claim_token)

    def test_the_raw_token_appears_in_no_column(self):
        raw = self.result.claim_token
        for row in OwnerInvitation.objects.all():
            for value in (row.token_hash, str(row.id)):
                self.assertNotIn(raw, value)

    def test_the_token_is_high_entropy_and_urlsafe(self):
        token = self.result.claim_token
        self.assertGreaterEqual(len(token), 43)
        self.assertTrue(
            all(c.isalnum() or c in '-_' for c in token), token,
        )

    def test_tokens_are_unique_across_reissues(self):
        tokens = {self.result.claim_token}
        current = self.result.invitation
        for _ in range(3):
            current_result = onboarding_invitations.reissue_owner_invitation(
                restaurant_id=self.restaurant.id,
                expected_invitation_id=current.id,
                actor=self.admin, reason=REASON,
            )
            tokens.add(current_result.claim_token)
            current = current_result.invitation
        self.assertEqual(len(tokens), 4)

    def test_hashes_are_unique_across_reissues(self):
        hashes = set(
            OwnerInvitation.objects.values_list('token_hash', flat=True),
        )
        self.assertEqual(hashes.__len__(), OwnerInvitation.objects.count())

    @override_settings(ADMIN_OWNER_INVITATION_TTL=timedelta(hours=6))
    def test_the_ttl_is_the_configured_one_and_is_fresh(self):
        result = self.reissue(expected=self.result.invitation.id)
        self.assertEqual(
            result.invitation.expires_at - result.invitation.issued_at,
            timedelta(hours=6),
        )

    def test_the_new_expiry_does_not_inherit_the_old_one(self):
        self.assertNotEqual(
            self.result.invitation.expires_at, self.invitation.expires_at,
        )
        self.assertGreater(
            self.result.invitation.expires_at, self.invitation.expires_at,
        )

    def test_issued_at_and_expires_at_come_from_one_captured_instant(self):
        """
        Exactly the configured TTL apart. Two ``timezone.now()`` calls would put a
        sub-millisecond drift between them and make the window something else.
        """
        self.assertEqual(
            self.result.invitation.expires_at - self.result.invitation.issued_at,
            onboarding_invitations.owner_invitation_ttl(),
        )

    def test_creation_and_reissue_share_one_mint_primitive(self):
        """
        §6: not "they happen to agree", but "there is one function". Asserted by
        identity so a second copy cannot pass by producing similar output.
        """
        self.assertIs(
            onboarding_creation.mint_owner_invitation,
            onboarding_invitations.mint_owner_invitation,
        )
        self.assertIs(
            onboarding_creation.owner_invitation_ttl,
            onboarding_invitations.owner_invitation_ttl,
        )

    def test_reissued_and_initial_credentials_are_indistinguishable(self):
        initial, reissued = self.creation.claim_token, self.result.claim_token
        self.assertEqual(len(initial), len(reissued))
        self.assertEqual(
            self.creation.invitation.expires_at
            - self.creation.invitation.issued_at,
            self.result.invitation.expires_at - self.result.invitation.issued_at,
        )


# --- §16-§18 cancellation ----------------------------------------------------

class CancelPendingTests(_LifecycleTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.cancel()
        self.invitation.refresh_from_db()

    def test_it_is_cancelled_and_attributed(self):
        self.assertTrue(self.result.changed)
        self.assertIsNotNone(self.invitation.cancelled_at)
        self.assertEqual(self.invitation.cancelled_by_id, self.admin.id)

    def test_no_other_terminal_stamp_is_written(self):
        self.assertIsNone(self.invitation.superseded_at)
        self.assertIsNone(self.invitation.consumed_at)

    def test_the_row_is_not_deleted(self):
        self.assertTrue(
            OwnerInvitation.objects.filter(pk=self.invitation.pk).exists(),
        )

    def test_expiry_and_token_hash_are_untouched(self):
        self.assertEqual(
            self.invitation.expires_at, self.creation.invitation.expires_at,
        )
        self.assertEqual(
            self.invitation.token_hash, self.creation.invitation.token_hash,
        )

    def test_no_replacement_invitation_is_created(self):
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )
        self.assertEqual(self.unresolved_count(), 0)

    def test_the_read_reports_cancelled_with_the_same_id(self):
        block = onboarding_summary(self.restaurant)['invitation']
        self.assertEqual(block['status'], 'cancelled')
        self.assertEqual(block['id'], str(self.invitation.id))


class CancelExpiredTests(_LifecycleTestCase):
    """An expired invitation is unresolved, holds the slot, and is cancellable."""

    def test_an_expired_invitation_can_be_cancelled(self):
        self.expire(self.invitation)
        result = self.cancel()
        self.assertTrue(result.changed)
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.cancelled_at)

    def test_expiry_is_not_rewritten_by_the_cancellation(self):
        self.expire(self.invitation)
        before = self.invitation.expires_at
        self.cancel()
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.expires_at, before)


class CancelExactRetryTests(_LifecycleTestCase):
    """
    §18: cancellation returns no credential, so an identical retry is safe — and must
    be a no-op rather than a second terminal event.
    """

    def setUp(self):
        super().setUp()
        self.cancel()
        self.invitation.refresh_from_db()
        self.original_at = self.invitation.cancelled_at
        self.original_by = self.invitation.cancelled_by_id

    def test_the_retry_succeeds_with_changed_false(self):
        result = self.cancel()
        self.assertFalse(result.changed)

    def test_the_timestamp_does_not_move(self):
        self.cancel()
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.cancelled_at, self.original_at)

    def test_the_actor_is_not_replaced(self):
        other = _make_admin(email='oi-admin2@t.com', username='oi-admin2')
        self.cancel(actor=other)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.cancelled_by_id, self.original_by)

    def test_the_retry_creates_no_second_invitation(self):
        self.cancel()
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_an_old_token_can_never_cancel_a_newer_invitation(self):
        """
        The failure the concurrency token exists for. After a reissue the head has
        moved, so a request naming the old id is stale — it must NOT fall through to
        the retry branch and must NOT touch the new credential.
        """
        fresh = self.reissue(expected=self.invitation.id)
        self.assertRefused(
            lambda: self.cancel(expected=self.invitation.id),
            onboarding_invitations.STALE_OWNER_INVITATION,
        )
        fresh.invitation.refresh_from_db()
        self.assertIsNone(fresh.invitation.cancelled_at)


class CancelRefusalTests(_LifecycleTestCase):

    def test_a_consumed_invitation_cannot_be_cancelled(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self.assertRefused(
            self.cancel,
            onboarding_invitations.OWNER_INVITATION_ALREADY_RESOLVED,
        )
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.cancelled_at)

    def test_a_superseded_invitation_cannot_be_cancelled(self):
        """
        Reached by naming a superseded row directly. It is stale as well as resolved,
        and either refusal is correct — what must never happen is a cancellation stamp
        landing on a row that already ended another way.
        """
        self.reissue()
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.superseded_at)
        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as ctx:
            self.cancel(expected=self.invitation.id)
        self.assertIn(
            ctx.exception.code,
            {
                onboarding_invitations.STALE_OWNER_INVITATION,
                onboarding_invitations.OWNER_INVITATION_ALREADY_RESOLVED,
            },
        )
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.cancelled_at)

    def test_a_legacy_adopted_restaurant_is_refused(self):
        other = Restaurant.objects.create(
            name='Legacy Cancel', location='Ntinda', country='UG', owner=self.owner,
        )
        RestaurantOnboarding.objects.create(
            restaurant=other, source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        self.assertRefused(
            lambda: onboarding_invitations.cancel_owner_invitation(
                restaurant_id=other.id, expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            ),
            onboarding_invitations.OWNER_INVITATION_NOT_APPLICABLE,
        )

    def test_an_untracked_restaurant_is_refused(self):
        other = Restaurant.objects.create(
            name='Untracked Cancel', location='Bugolobi', country='UG',
            owner=self.owner,
        )
        self.assertRefused(
            lambda: onboarding_invitations.cancel_owner_invitation(
                restaurant_id=other.id, expected_invitation_id=self.invitation.id,
                actor=self.admin, reason=REASON,
            ),
            onboarding_invitations.ONBOARDING_NOT_TRACKED,
        )

    def test_a_stale_expected_id_is_refused(self):
        self.reissue()
        self.assertRefused(
            lambda: self.cancel(expected=self.invitation.id),
            onboarding_invitations.STALE_OWNER_INVITATION,
        )


class CancellationSurvivesOwnerDriftTests(_LifecycleTestCase):
    """
    §17 — THE DELIBERATE ASYMMETRY.

    Reissue mints authority and needs a sound owner target. Cancellation REMOVES
    authority, and a drifted tenant with a live credential outstanding is exactly when
    an administrator most needs to be able to kill it.
    """

    def _break_ownership(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)

    def test_cancellation_succeeds_while_reissue_is_refused(self):
        self._break_ownership()
        with self.assertRaises(OwnerConsistencyError):
            self.reissue()
        result = self.cancel()
        self.assertTrue(result.changed)

    def test_it_works_with_two_live_owner_memberships(self):
        RestaurantEmployee.objects.create(
            user=_make_restaurant_user('drift-two@t.com'),
            restaurant=self.restaurant, roles=[RESTAURANT_OWNER], active=True,
        )
        self.assertTrue(self.cancel().changed)

    def test_it_works_with_a_deactivated_owner_account(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertTrue(self.cancel().changed)

    def test_cancellation_repairs_nothing(self):
        self._break_ownership()
        self.cancel()
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.owner_id, self.owner.id)
        self.assertFalse(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, active=True,
            ).exists(),
        )


# --- §13 / §19 customer access ------------------------------------------------

class CustomerAccessIsNeverTouchedTests(_LifecycleTestCase):
    """
    Both operations, both owner classes. A restaurant-scoped credential is not an
    identity claim in either direction.
    """

    def _existing_owner_restaurant(self):
        """A second tenant whose owner is an ESTABLISHED account."""
        established = _make_restaurant_user('established@t.com')
        self.assertEqual(
            established.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
        )
        creation = onboarding_creation.create_admin_restaurant(
            name='Second Cafe', location='Muyenga', is_test=False,
            owner=onboarding_creation.ExistingOwner(user_id=established.id),
            actor=self.admin, reason='Attaching an existing owner account.',
        )
        return creation, established

    def _snapshot(self, user):
        user.refresh_from_db()
        return (
            user.customer_access_state, user.password, user.is_active,
            user.prompt_password_change, user.last_login,
        )

    def test_a_new_owner_stays_pending_through_reissue(self):
        self.assertEqual(
            self._snapshot(self.owner)[0], CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        before = self._snapshot(self.owner)
        self.reissue()
        self.assertEqual(self._snapshot(self.owner), before)

    def test_a_new_owner_stays_pending_through_cancellation(self):
        before = self._snapshot(self.owner)
        self.cancel()
        self.assertEqual(self._snapshot(self.owner), before)

    def test_an_established_owner_stays_established_through_reissue(self):
        creation, established = self._existing_owner_restaurant()
        before = self._snapshot(established)
        onboarding_invitations.reissue_owner_invitation(
            restaurant_id=creation.restaurant.id,
            expected_invitation_id=creation.invitation.id,
            actor=self.admin, reason=REASON,
        )
        self.assertEqual(self._snapshot(established), before)
        self.assertEqual(
            established.customer_access_state, CUSTOMER_ACCESS_ESTABLISHED,
        )

    def test_an_established_owner_stays_established_through_cancellation(self):
        creation, established = self._existing_owner_restaurant()
        before = self._snapshot(established)
        onboarding_invitations.cancel_owner_invitation(
            restaurant_id=creation.restaurant.id,
            expected_invitation_id=creation.invitation.id,
            actor=self.admin, reason=REASON,
        )
        self.assertEqual(self._snapshot(established), before)

    def test_cancellation_does_not_deactivate_or_unseat_the_owner(self):
        self.cancel()
        self.owner.refresh_from_db()
        self.restaurant.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(self.restaurant.owner_id, self.owner.id)
        self.assertTrue(
            RestaurantEmployee.objects.filter(
                restaurant=self.restaurant, user=self.owner, active=True,
                deleted=False,
            ).exists(),
        )

    def test_the_owner_password_stays_unusable_after_a_reissue(self):
        self.reissue()
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_usable_password())


# --- §26 no delivery ----------------------------------------------------------

class NoDeliveryTests(_LifecycleTestCase):
    """
    Every delivery path is patched to EXPLODE, and both operations still work. A
    passing suite therefore proves the paths were not merely unasserted — they were
    not reached.
    """

    def _explode(self):
        return [
            patch(target, side_effect=AssertionError(f'{target} was called'))
            for target in _DELIVERY_PATCHES
        ]

    def test_reissue_touches_no_delivery_path(self):
        patches = self._explode()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.assertIsNotNone(self.reissue().claim_token)

    def test_cancel_touches_no_delivery_path(self):
        patches = self._explode()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.assertTrue(self.cancel().changed)


# --- §5 / §25 the domain writes no audit row ---------------------------------

class TheDomainWritesNoAuditRowTests(_LifecycleTestCase):
    """
    The audit entry belongs to the ADAPTER, which is what makes audit-atomicity
    possible: the adapter's outer transaction can roll the mutation back if the audit
    write fails, and it could not if the service had already written one.
    """

    def test_reissue_writes_no_audit_entry(self):
        before = AdminAuditLog.objects.count()
        self.reissue()
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_cancel_writes_no_audit_entry(self):
        before = AdminAuditLog.objects.count()
        self.cancel()
        self.assertEqual(AdminAuditLog.objects.count(), before)


# --- validation ---------------------------------------------------------------

class ValidationTests(_LifecycleTestCase):

    def test_a_missing_expected_id_is_refused(self):
        for value in (None, '', '   '):
            self.assertRefused(
                lambda v=value: self.reissue(expected=v),
                onboarding_invitations.INVALID_EXPECTED_INVITATION_ID,
            )

    def test_a_malformed_expected_id_is_a_request_error_not_a_conflict(self):
        """
        §21: a typo must never be answered as a conflict. Sending an operator to
        reload a screen that was never stale wastes the one signal that means "the
        world moved".
        """
        exc = self.assertRefused(
            lambda: self.reissue(expected='not-a-uuid'),
            onboarding_invitations.INVALID_EXPECTED_INVITATION_ID,
        )
        self.assertIn(
            exc.code, onboarding_invitations.INVALID_REQUEST_CODES,
        )
        self.assertNotIn(exc.code, onboarding_invitations.CONFLICT_CODES)

    def test_a_short_or_missing_reason_is_refused(self):
        for value in (None, '', '   ', 'too short'):
            self.assertRefused(
                lambda v=value: self.reissue(reason=v),
                onboarding_invitations.INVALID_REASON,
            )
            self.assertRefused(
                lambda v=value: self.cancel(reason=v),
                onboarding_invitations.INVALID_REASON,
            )

    def test_the_reason_bar_is_imported_not_respelled(self):
        from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
        self.assertIs(onboarding_invitations.MIN_REASON_LENGTH, MIN_REASON_LENGTH)

    def test_a_restaurant_user_can_never_be_the_actor(self):
        self.assertRefused(
            lambda: self.reissue(actor=self.owner),
            onboarding_invitations.INVALID_ACTOR,
        )
        self.assertRefused(
            lambda: self.cancel(actor=self.owner),
            onboarding_invitations.INVALID_ACTOR,
        )

    def test_a_deactivated_admin_can_never_be_the_actor(self):
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assertRefused(
            self.reissue, onboarding_invitations.INVALID_ACTOR,
        )

    def test_the_actor_is_re_read_rather_than_trusted(self):
        """
        An in-memory instance can claim anything. Eligibility is a property of the ROW.
        """
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.admin.is_active = True  # a stale/forged instance
        self.assertRefused(
            self.reissue, onboarding_invitations.INVALID_ACTOR,
        )

    def test_nothing_is_written_when_validation_fails(self):
        with self.assertRaises(onboarding_invitations.OwnerInvitationError):
            self.reissue(reason='no')
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.superseded_at)
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )


class NoInvitationIssuedTests(TestCase):
    """
    A tracked ``admin_created`` restaurant whose invitation history is empty — reachable
    only by constructing one directly, since creation always mints. Its own code rather
    than ``stale``: there is nothing to be stale ABOUT, and telling an operator to
    reload would send them looking for a change that never happened.
    """

    def setUp(self):
        super().setUp()
        import uuid
        self.uuid4 = uuid.uuid4
        self.admin = _make_admin(email='oi-empty@t.com', username='oi-empty')
        self.owner = _make_restaurant_user('empty-owner@t.com')
        self.restaurant = Restaurant.objects.create(
            name='Empty Ltd', location='Kansanga', country='UG', owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        RestaurantOnboarding.objects.create(
            restaurant=self.restaurant,
            source='admin_created',
            created_by=self.admin,
        )

    def test_reissue_reports_not_issued(self):
        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as ctx:
            onboarding_invitations.reissue_owner_invitation(
                restaurant_id=self.restaurant.id,
                expected_invitation_id=self.uuid4(),
                actor=self.admin, reason=REASON,
            )
        self.assertEqual(
            ctx.exception.code,
            onboarding_invitations.OWNER_INVITATION_NOT_ISSUED,
        )

    def test_cancel_reports_not_issued(self):
        with self.assertRaises(onboarding_invitations.OwnerInvitationError) as ctx:
            onboarding_invitations.cancel_owner_invitation(
                restaurant_id=self.restaurant.id,
                expected_invitation_id=self.uuid4(),
                actor=self.admin, reason=REASON,
            )
        self.assertEqual(
            ctx.exception.code,
            onboarding_invitations.OWNER_INVITATION_NOT_ISSUED,
        )


# --- §36 structural ratchets --------------------------------------------------

_LIFECYCLE_MODULES = (
    'platform_admin_app/onboarding_invitations.py',
    'platform_admin_app/endpoints/owner_invitation.py',
)


def _source(relative):
    return (Path(__file__).resolve().parent.parent / relative).read_text()


class StructuralRatchetTests(TestCase):
    """
    AST scans over the two Step-2E modules. Auditing today's code fixes today; the
    scan is what fixes tomorrow.
    """

    def test_no_lifecycle_module_writes_customer_access_state(self):
        """
        The one invariant most likely to be broken by a well-meaning future change:
        "while we're here, mark them established". Redemption is the ONLY supported
        writer of that transition, and it does not exist yet.

        BOTH WRITE FORMS ARE SCANNED, and the second was added because the red team
        found the first insufficient: an earlier version looked only at attribute
        ASSIGNMENT (``user.customer_access_state = ...``), and a mutation written as
        ``User.objects.filter(...).update(customer_access_state=...)`` — the form this
        codebase actually reaches for — sailed straight past it. A ratchet that catches
        only the shape you happened to think of is a ratchet that will be stepped over.
        """
        for relative in _LIFECYCLE_MODULES:
            tree = ast.parse(_source(relative))
            for node in ast.walk(tree):
                # 1. `obj.customer_access_state = ...`
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        self.assertNotEqual(
                            target.attr, 'customer_access_state',
                            f'{relative} assigns customer_access_state',
                        )
                # 2. `...(customer_access_state=...)` — update(), create(), setattr…
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        self.assertNotEqual(
                            keyword.arg, 'customer_access_state',
                            f'{relative} passes customer_access_state to a call',
                        )
                    for arg in node.args:
                        if isinstance(arg, ast.Constant):
                            self.assertNotEqual(
                                arg.value, 'customer_access_state',
                                f'{relative} names customer_access_state in a call',
                            )

    def test_no_lifecycle_module_imports_a_delivery_or_credential_path(self):
        forbidden = {
            'send_sms', 'Messenger', 'Notification', 'save_action_log',
            'OtpManager', 'make_password', 'get_random_string', 'self_register',
            'create_employee', 'set_password', 'set_unusable_password',
        }
        for relative in _LIFECYCLE_MODULES:
            tree = ast.parse(_source(relative))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name, forbidden,
                            f'{relative} imports {alias.name}',
                        )
                if isinstance(node, (ast.Attribute, ast.Name)):
                    name = getattr(node, 'attr', None) or getattr(node, 'id', '')
                    self.assertNotIn(
                        name, forbidden, f'{relative} references {name}',
                    )

    def test_the_endpoint_module_performs_no_orm_write(self):
        """
        §5: the adapter is an adapter. Every mutation goes through the domain, and a
        write appearing here would be a second writer with none of the locking.
        """
        tree = ast.parse(_source('platform_admin_app/endpoints/owner_invitation.py'))
        banned = {
            'save', 'update', 'update_or_create', 'get_or_create', 'bulk_create',
            'delete',
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # `AdminAuditLog` is written through `self.audit(...)`, and the
                # `objects.create` that ultimately performs it lives in
                # `platform_admin_app.audit` — not here.
                self.assertNotIn(
                    node.func.attr, banned,
                    f'endpoints/owner_invitation.py calls .{node.func.attr}()',
                )

    def test_the_domain_module_writes_no_audit_row(self):
        """
        Asserted over the AST rather than the text: the module docstring legitimately
        EXPLAINS that the audit entry belongs to the adapter, and a substring search
        would fail on the explanation while a second module that quietly imported
        ``audit`` would pass on a well-worded comment.
        """
        tree = ast.parse(_source('platform_admin_app/onboarding_invitations.py'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    'audit', (node.module or '').split('.'),
                    'the domain module imports the audit machinery',
                )
                for alias in node.names:
                    self.assertNotIn(alias.name, {'AdminAuditLog', 'audit', 'record'})
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, 'AdminAuditLog')
            if isinstance(node, ast.Name):
                self.assertNotEqual(node.id, 'AdminAuditLog')

    def test_no_module_can_expose_a_token_hash(self):
        """
        Neither the projection nor the audit snapshot may name ``token_hash``. Asserted
        over the read module too, since that is what the write response returns.
        """
        for relative in _LIFECYCLE_MODULES + (
            'platform_admin_app/onboarding_reads.py',
        ):
            source = _source(relative)
            # The word appears in prose explaining that it is NEVER exposed; what must
            # not appear is an attribute access that would put it in a payload.
            self.assertNotIn('.token_hash', source, relative)

    def test_no_serializer_targets_owner_invitation(self):
        """
        §36: a generic ``ModelSerializer`` over ``OwnerInvitation`` would hand Admin
        arbitrary writes to a credential table, terminal stamps included.
        """
        from rest_framework import serializers

        def descendants(cls):
            for sub in cls.__subclasses__():
                yield sub
                yield from descendants(sub)

        for cls in descendants(serializers.ModelSerializer):
            model = getattr(getattr(cls, 'Meta', None), 'model', None)
            self.assertIsNot(
                model, OwnerInvitation,
                f'{cls.__module__}.{cls.__name__} is a ModelSerializer over '
                'OwnerInvitation',
            )
