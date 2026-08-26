"""
The Step-2E concurrency matrix, against a real PostgreSQL.

``TransactionTestCase`` rather than ``TestCase``: the point is what two threads in two
real transactions do to each other, and ``TestCase``'s wrapping transaction would hide
exactly the row-lock behaviour under examination.

━━ WHAT SERIALIZES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The ``Restaurant`` row. Every invitation operation for a tenant takes it FIRST, so
reissue-vs-reissue, reissue-vs-cancel and cancel-vs-cancel become a queue with a
well-defined winner and a clean domain refusal for the loser.

``one_unresolved_owner_invitation_per_onboarding`` is the final database backstop, NOT
the concurrency user experience: relying on it would turn an ordinary, expected race
into an ``IntegrityError`` surfacing at an operator as a 500. These tests assert that
the loser gets ``stale_owner_invitation``, and separately that the invariant still
holds at the end.

━━ THE HARNESS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``threading.Barrier`` releases both threads at the same instant; no ``sleep`` decides
anything, and every join carries a timeout so a genuine deadlock fails the test loudly
instead of hanging CI. Each thread closes its own connection, because Django gives a
thread its own and a leaked one keeps a transaction open.
"""
import threading
from datetime import timedelta

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import onboarding_creation, onboarding_invitations
from platform_admin_app.models import OwnerInvitation
from platform_admin_app.onboarding_creation import NewOwner
from platform_admin_app.onboarding_reads import onboarding_summary
from users_app.models import User

REASON = 'Rotating the claim credential during the concurrency exercise.'
JOIN_TIMEOUT = 30

# Distinct phone range from every other admin suite.
_PHONE = iter(f'0772{n:06d}' for n in range(830000, 839999))

_POSTGRES = connection.vendor == 'postgresql'
_SKIP = 'Row-lock semantics are only meaningful on PostgreSQL.'


def _run_pair(first, second):
    """
    Run two callables in two threads released simultaneously.

    Returns ``[outcome_first, outcome_second]``, each ``('ok', value)`` or
    ``('error', exception)``. Both are reported rather than raised, because which
    thread wins is the thing under test and neither outcome is a failure by itself.
    """
    barrier = threading.Barrier(2)
    outcomes = [None, None]

    def wrap(index, call):
        def run():
            try:
                barrier.wait(timeout=JOIN_TIMEOUT)
                outcomes[index] = ('ok', call())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                outcomes[index] = ('error', exc)
            finally:
                connections.close_all()
        return run

    threads = [
        threading.Thread(target=wrap(0, first)),
        threading.Thread(target=wrap(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), 'a thread deadlocked'
    return outcomes


class _ConcurrencyTestCase(TransactionTestCase):
    """One admin-created restaurant with its initial pending invitation."""

    def setUp(self):
        super().setUp()
        if not _POSTGRES:
            self.skipTest(_SKIP)
        self.admin = User.objects.create_user(
            first_name='Ada', last_name='Min', email='oic-admin@t.com',
            username='oic-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Race Cafe', location='Kololo', is_test=False,
            owner=NewOwner('Jane', 'Doe', next(_PHONE), None),
            actor=self.admin, reason='Creating the concurrency fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.onboarding = self.creation.onboarding
        self.invitation = self.creation.invitation

    # --- helpers ---

    def reissue(self, expected=None):
        return lambda: onboarding_invitations.reissue_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=expected or self.invitation.id,
            actor=self.admin, reason=REASON,
        )

    def cancel(self, expected=None):
        return lambda: onboarding_invitations.cancel_owner_invitation(
            restaurant_id=self.restaurant.id,
            expected_invitation_id=expected or self.invitation.id,
            actor=self.admin, reason=REASON,
        )

    def unresolved(self):
        return list(
            OwnerInvitation.objects.filter(
                onboarding=self.onboarding,
                consumed_at__isnull=True,
                cancelled_at__isnull=True,
                superseded_at__isnull=True,
            )
        )

    def assertInvariantHolds(self):
        """§29 E: at all times, at most ONE unresolved invitation per onboarding."""
        self.assertLessEqual(len(self.unresolved()), 1)

    def split(self, outcomes):
        """``(successes, errors)`` from a ``_run_pair`` result."""
        successes = [value for kind, value in outcomes if kind == 'ok']
        errors = [value for kind, value in outcomes if kind == 'error']
        return successes, errors

    def assertStale(self, error):
        self.assertIsInstance(error, onboarding_invitations.OwnerInvitationError)
        self.assertEqual(
            error.code, onboarding_invitations.STALE_OWNER_INVITATION,
            f'expected a clean domain refusal, got {error.code}: {error}',
        )


# --- §29 A reissue vs reissue -------------------------------------------------

class ReissueVersusReissueTests(_ConcurrencyTestCase):
    """
    Both threads start from A. Exactly ONE may reissue it.

    The loser must get ``stale_owner_invitation`` — a clean domain refusal meaning
    "somebody else changed this, reload" — and NOT an ``IntegrityError`` from the unique
    index reaching an operator as a 500. The index is the final backstop; it is not the
    concurrency user experience.
    """

    def setUp(self):
        super().setUp()
        self.outcomes = _run_pair(self.reissue(), self.reissue())
        self.successes, self.errors = self.split(self.outcomes)

    def test_exactly_one_thread_succeeds(self):
        self.assertEqual(len(self.successes), 1, self.outcomes)
        self.assertEqual(len(self.errors), 1, self.outcomes)

    def test_the_loser_gets_a_clean_domain_refusal(self):
        self.assertStale(self.errors[0])

    def test_the_original_is_superseded_exactly_once(self):
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.superseded_at)

    def test_only_the_winners_invitation_is_unresolved(self):
        unresolved = self.unresolved()
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].id, self.successes[0].invitation.id)

    def test_only_two_invitations_exist(self):
        """The loser inserted nothing — its whole transaction rolled back."""
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 2,
        )

    def test_the_invariant_holds(self):
        self.assertInvariantHolds()

    def test_the_read_agrees_with_the_winner(self):
        block = onboarding_summary(self.restaurant)['invitation']
        self.assertEqual(block['id'], str(self.successes[0].invitation.id))
        self.assertEqual(block['status'], 'pending')


# --- §29 B reissue vs cancel --------------------------------------------------

class ReissueVersusCancelTests(_ConcurrencyTestCase):
    """
    Both start from A, and BOTH orderings are legitimate outcomes — which is exactly
    why the assertions branch on what actually happened rather than pinning a winner.

    If REISSUE wins: A is superseded, B is pending, and the cancel naming A is stale.

    If CANCEL wins: A is cancelled — and A is STILL THE HEAD, because nothing
    unresolved exists and A is the latest resolved row. So the reissue naming A may
    legitimately proceed and mint B. That is §30's rule made concrete:
    ``expected_invitation_id`` asserts the invitation's IDENTITY, not its status, and
    the reissue re-checks its own preconditions under the lock. The operator would have
    reached the same place by reloading (seeing ``cancelled``, id A) and deliberately
    clicking Reissue — which §31 requires to work.
    """

    def setUp(self):
        super().setUp()
        self.outcomes = _run_pair(self.reissue(), self.cancel())
        self.successes, self.errors = self.split(self.outcomes)
        self.invitation.refresh_from_db()

    def test_at_least_one_thread_succeeds(self):
        self.assertGreaterEqual(len(self.successes), 1, self.outcomes)

    def test_any_refusal_is_a_clean_stale_conflict(self):
        for error in self.errors:
            self.assertStale(error)

    def test_the_outcome_is_one_of_the_two_documented_orderings(self):
        if self.invitation.superseded_at is not None:
            # Reissue won. A was unresolved, so it was superseded and cancel is stale.
            self.assertIsNone(self.invitation.cancelled_at)
            self.assertEqual(len(self.errors), 1, self.outcomes)
        else:
            # Cancel won. A is cancelled; the reissue may or may not have reached the
            # lock in time to see A still as head.
            self.assertIsNotNone(self.invitation.cancelled_at)

    def test_a_terminal_stamp_is_never_doubled(self):
        """
        Terminal-state exclusivity is a database constraint; asserted here as an
        OUTCOME so a race that tried to violate it would show up as a violation rather
        than as an exception nobody looked at.
        """
        stamps = [
            self.invitation.consumed_at,
            self.invitation.cancelled_at,
            self.invitation.superseded_at,
        ]
        self.assertLessEqual(len([s for s in stamps if s is not None]), 1)

    def test_the_invariant_holds(self):
        self.assertInvariantHolds()

    def test_the_read_and_the_rows_agree(self):
        block = onboarding_summary(self.restaurant)['invitation']
        unresolved = self.unresolved()
        if unresolved:
            self.assertEqual(block['id'], str(unresolved[0].id))
            self.assertEqual(block['status'], 'pending')
        else:
            self.assertEqual(block['status'], 'cancelled')
            self.assertEqual(block['id'], str(self.invitation.id))


# --- §29 C cancel vs cancel ---------------------------------------------------

class CancelVersusCancelTests(_ConcurrencyTestCase):
    """
    Both start from A. One cancels; the other finds it already cancelled and reports
    the exact-retry no-op — never a second terminal event, and never a moved timestamp.
    """

    def setUp(self):
        super().setUp()
        self.outcomes = _run_pair(self.cancel(), self.cancel())
        self.successes, self.errors = self.split(self.outcomes)
        self.invitation.refresh_from_db()

    def test_both_threads_succeed(self):
        self.assertEqual(len(self.errors), 0, self.outcomes)
        self.assertEqual(len(self.successes), 2, self.outcomes)

    def test_exactly_one_reports_a_change(self):
        changed = [result for result in self.successes if result.changed]
        self.assertEqual(len(changed), 1, self.outcomes)

    def test_the_invitation_is_cancelled_once(self):
        self.assertIsNotNone(self.invitation.cancelled_at)
        self.assertIsNone(self.invitation.superseded_at)
        self.assertIsNone(self.invitation.consumed_at)

    def test_no_replacement_is_created(self):
        self.assertEqual(
            OwnerInvitation.objects.filter(onboarding=self.onboarding).count(), 1,
        )

    def test_the_two_results_describe_the_same_row(self):
        self.assertEqual(
            {result.invitation.id for result in self.successes},
            {self.invitation.id},
        )

    def test_the_invariant_holds(self):
        self.assertInvariantHolds()


# --- §29 D an expired invitation ----------------------------------------------

class ExpiredInvitationUnderContentionTests(_ConcurrencyTestCase):
    """
    §29 D: both operations work correctly on an EXPIRED head without any clock-driven
    write. Nothing sweeps, nothing stamps "expired", and the row keeps holding the slot
    until something resolves it.
    """

    def setUp(self):
        super().setUp()
        past = timezone.now() - timedelta(days=3)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=past - timedelta(days=1), expires_at=past,
        )
        self.invitation.refresh_from_db()

    def test_two_reissues_still_produce_one_winner(self):
        outcomes = _run_pair(self.reissue(), self.reissue())
        successes, errors = self.split(outcomes)
        self.assertEqual(len(successes), 1, outcomes)
        self.assertStale(errors[0])
        self.assertInvariantHolds()

    def test_reissue_and_cancel_still_resolve_cleanly(self):
        outcomes = _run_pair(self.reissue(), self.cancel())
        successes, errors = self.split(outcomes)
        self.assertGreaterEqual(len(successes), 1, outcomes)
        for error in errors:
            self.assertStale(error)
        self.assertInvariantHolds()

    def test_no_expired_stamp_was_ever_written(self):
        _run_pair(self.reissue(), self.reissue())
        self.invitation.refresh_from_db()
        # It ended by being SUPERSEDED — the thing that actually happened to it.
        self.assertIsNotNone(self.invitation.superseded_at)
        self.assertIsNone(self.invitation.cancelled_at)
        self.assertIsNone(self.invitation.consumed_at)


# --- the lock is load-bearing -------------------------------------------------

# --- what this suite does NOT claim -------------------------------------------
#
# There is deliberately no "remove the lock and watch it break" control here, and the
# absence is worth recording rather than leaving as a gap somebody re-fills badly.
#
# One was written and then removed. Patching ``_lock_target`` away — and, separately,
# the head-invitation ``select_for_update``, and then both together, each with the
# critical section artificially widened by a pause between the head read and the write
# — produced a clean ``stale_owner_invitation`` every time. Two barrier-released
# threads did not interleave inside the section under any of those configurations, and
# the reason was not established.
#
# So the control was proving nothing: it would have passed whether or not the locks
# existed, which is the worst kind of concurrency test — it reports confidence that
# was never earned. What the suite asserts instead is what it can actually observe:
# the positive matrix above (A, B, C, D), and the database invariant (E) holding at
# the end of every one of them.
#
# The locks are justified on their own terms rather than by that demonstration. The
# ``Restaurant`` row is the documented serialization point this repository already
# uses for ``onboarding_adoption`` and every ``commercial_app`` mutation, and taking it
# FIRST is what keeps this domain inside the global lock order
# (``Restaurant -> RestaurantOnboarding -> OwnerInvitation``) rather than inventing a
# new one. A future change that widens the window for real — a slower check, a bigger
# transaction, an added query — is exactly the change that would need them.
