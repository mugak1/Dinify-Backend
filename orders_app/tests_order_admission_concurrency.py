"""
Order admission vs lifecycle transitions — the concurrency proof (PostgreSQL only).

Phase 1 puts a SUSPEND button and a GO-LIVE button in front of an operator. A
lifecycle control that can be raced is worse than no control at all: it tells the
operator trading has stopped when it has not. The sequential boundary was made
correct by PR-D; this suite is the part that only concurrency can demonstrate.

THE RACE, exactly as production ran it before the admission primitive:

    T1 (diner):  reads status=live in autocommit, passes the gate
    T2 (admin):  locks the restaurant row, live -> suspended, COMMITS
    T1 (diner):  locks the table, derives is_test from the STALE live, INSERTs

The two transactions shared no lock — the order path locked the ``Table`` and never
read or locked the ``Restaurant`` — so nothing made T1 notice. The window spanned
T1's BLOCKING table-lock acquisition, which is to say it was widest exactly when the
restaurant was busiest.

WHY THE WORKERS SPLIT THE CREATE PATH IN TWO. Each race worker loads the restaurant
instance, waits, and only then calls ``_create_order`` with it. That is not a
contrivance to force a failure — it is the SHAPE OF THE PRODUCTION CALL:
``ConOrder.initiate_order`` loads the restaurant in autocommit, runs a preflight,
and hands that same instance to ``_create_order``, which opens the transaction that
writes the order. The barrier only makes the interval deterministic; the interval
itself is production's.

DETERMINISTIC, NOT TIMED. Two barrier phases pin the ordering exactly — phase one
closes after the stale read, phase two after the transition has COMMITTED — so each
test has one correct outcome rather than a distribution of them, and no assertion
depends on a sleep or on which thread happens to win. The one genuinely racy test
(concurrent admissions) asserts an invariant that holds for every interleaving.

Every test here FAILS on the pre-admission code: the create races write an order
against a restaurant that is no longer trading, the go-live race writes a real
commercial order marked ``is_test``, and the submit race pushes a draft into the
kitchen at a suspended restaurant — that last one having had no lifecycle check on
its path at all.

Requires a lock-capable backend; skipped on SQLite, where advisory locks do not
exist and the whole suite would pass by not running.
"""
import threading
from decimal import Decimal
from unittest import mock

from django.db import connection, transaction
from django.test import TransactionTestCase, tag

from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    OrderStatus_Pending,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from orders_app.controllers import manage_order
from orders_app.controllers.services.create_order import _create_order
from orders_app.models import Order
from restaurants_app.controllers import lifecycle
from restaurants_app.controllers import lifecycle_policy as policy
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

REASON = 'concurrency proof for order admission'


def _run(workers):
    """Start every worker at a shared barrier, join, and fail loudly on a hang."""
    barrier = threading.Barrier(len(workers))
    threads = [
        threading.Thread(target=fn, args=args, kwargs={'barrier': barrier})
        for fn, args in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    for index, thread in enumerate(threads):
        assert not thread.is_alive(), f'worker {index} hung (lock regressed)'


# --- workers ----------------------------------------------------------------
# Each owns its own transaction (or lets the production code own it) and closes its
# connection, per the house concurrency-test convention.

def _sync(barrier, times=1):
    """Pass ``times`` barrier phases, tolerating a barrier already broken by a peer.

    A worker that raised before reaching its own phase must not leave the others
    blocked; ``_run``'s join timeout would catch the hang, but the failure it
    reported would be the hang rather than the original error.
    """
    if barrier is None:
        return
    for _ in range(times):
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:       # pragma: no cover - defensive
            return


def _create(ids, results, key='create', table='table', created_by=None,
            phases=2, barrier=None):
    """
    The REAL create path, entered the way production enters it: a restaurant
    instance loaded in autocommit, then handed to ``_create_order``.

    With the default two phases, phase 1 closes AFTER the stale read and phase 2
    AFTER the racing transition has committed — so by the time ``_create_order``
    runs, this thread holds an instance that is definitively out of date. A test
    with no transition to sequence against passes ``phases=1`` and simply races.
    """
    try:
        restaurant = Restaurant.objects.get(pk=ids['restaurant'])
        table_row = Table.objects.get(pk=ids[table])
        _sync(barrier, phases)
        results[key] = _create_order(
            restaurant=restaurant,
            table=table_row,
            items=[{'item': str(ids['item']), 'quantity': 1}],
            created_by=created_by,
        )
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        _sync(barrier, phases)
    finally:
        connection.close()


def _submit(ids, results, key='submit', phases=2, barrier=None):
    """The REAL submit path (``initiated`` -> ``pending``), same two-phase timing."""
    try:
        order = Order.objects.get(pk=ids['order'])
        _sync(barrier, phases)
        results[key] = manage_order.update_order_status(
            order, OrderStatus_Pending, None,
        )
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        _sync(barrier, phases)
    finally:
        connection.close()


def _transition(ids, to_state, results, key='transition', barrier=None):
    """
    The REAL lifecycle writer, committing BETWEEN the two barrier phases.

    Phase 1 lets the racing worker take its stale read first; the transition then
    runs and commits; phase 2 releases the racer into a world that has changed
    under it.
    """
    try:
        _sync(barrier)
        try:
            restaurant = Restaurant.objects.get(pk=ids['restaurant'])
            actor = User.objects.get(pk=ids['owner'])
            lifecycle.transition_restaurant(
                restaurant=restaurant, to_state=to_state,
                reason=REASON, actor=actor,
            )
            results[key] = 'ok'
        except Exception as exc:
            results[key] = f'error:{exc!r}'
        # Close BEFORE releasing the racer, so the transition is not merely
        # committed but fully off the connection when the create path resumes.
        connection.close()
        _sync(barrier)
    finally:
        connection.close()


@tag('concurrency')
class OrderAdmissionConcurrencyTests(TransactionTestCase):
    """One restaurant, two orderable tables, one orderable item."""

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('advisory locking requires PostgreSQL')

        self.owner = User.objects.create_user(
            first_name='Adm', last_name='Owner', email='adm_owner@test.com',
            phone_number='256700000801', username='256700000801',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Admission R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant,
        )
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.table_b = Table.objects.create(
            number=2, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=Decimal('10000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.ids = {
            'restaurant': self.restaurant.pk,
            'owner': self.owner.pk,
            'table': self.table.pk,
            'table_b': self.table_b.pk,
            'item': self.item.pk,
        }

    # --- helpers ------------------------------------------------------------

    def _at(self, state):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(status=state)
        self.restaurant.refresh_from_db()

    def _status(self):
        return Restaurant.objects.values_list('status', flat=True).get(
            pk=self.restaurant.pk,
        )

    def _draft(self):
        """A committed `initiated` draft, created while the restaurant is live."""
        result = _create_order(
            restaurant=self.restaurant,
            table=self.table,
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=None,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _go_live_ready(self):
        """
        Force the go-live readiness seam to pass.

        ``check_go_live_readiness`` fails CLOSED by design until Phase 1 wires the
        real checklist, so the onboarding -> live transition cannot otherwise be
        exercised at all. Patched exactly as ``tests_lifecycle`` patches it.
        """
        return mock.patch.object(
            lifecycle, 'check_go_live_readiness',
            return_value=lifecycle.ReadinessResult(True, []),
        )

    # --- race 1: diner creation vs suspension -------------------------------

    def test_raced_diner_creation_vs_suspend_never_admits(self):
        """
        A diner mid-creation when the restaurant is suspended must be REFUSED.

        The worker holds a `live` instance across the suspension — the exact
        production shape — so nothing but a re-read under the lock can catch it.
        Before the admission primitive this wrote the order.
        """
        results = {}
        _run([
            (_create, (self.ids, results)),
            (_transition, (self.ids, RestaurantStatus_Suspended, results)),
        ])

        self.assertEqual(results['transition'], 'ok', results)
        self.assertEqual(self._status(), RestaurantStatus_Suspended)
        self.assertEqual(results['create'].get('status'), 400, results['create'])
        self.assertEqual(
            Order.objects.filter(restaurant=self.restaurant).count(), 0,
            'an order was admitted at a suspended restaurant',
        )

    # --- race 2: draft submission vs suspension -----------------------------

    def test_raced_submit_vs_suspend_never_reaches_kitchen(self):
        """
        A draft submitted after suspension must NOT become `pending`.

        This is the defect with no gate at all: the submit path scoped the order
        to its table session and never consulted lifecycle state, so `initiated`
        -> `pending` succeeded at a suspended restaurant and the ticket appeared
        on the kitchen board after trading was supposed to have stopped.
        """
        draft = self._draft()
        self.ids['order'] = draft.pk
        results = {}
        _run([
            (_submit, (self.ids, results)),
            (_transition, (self.ids, RestaurantStatus_Suspended, results)),
        ])

        self.assertEqual(results['transition'], 'ok', results)
        self.assertEqual(results['submit'].get('status'), 400, results['submit'])
        draft.refresh_from_db()
        self.assertEqual(
            draft.order_status, OrderStatus_Initiated,
            'a draft was submitted into the kitchen at a suspended restaurant',
        )

    # --- race 3: the launch boundary ----------------------------------------

    def test_raced_rehearsal_vs_go_live_classifies_against_fresh_state(self):
        """
        An order crossing the go-live boundary is classified by the COMMITTED state.

        `is_test` is derived from lifecycle state, and the staff rehearsal order is
        permitted in BOTH states — so unlike the suspend races nothing is refused
        here and the only observable is the classification itself. Derived from the
        stale instance (as it was), a genuinely commercial order was written
        `is_test=True` and vanished from every revenue report, dashboard and diner
        analytic with no error anywhere.
        """
        self._at(RestaurantStatus_Onboarding)
        results = {}
        with self._go_live_ready():
            _run([
                (_create, (self.ids, results, 'create', 'table', self.owner)),
                (_transition, (self.ids, RestaurantStatus_Live, results)),
            ])

        self.assertEqual(results['transition'], 'ok', results)
        self.assertEqual(self._status(), RestaurantStatus_Live)
        self.assertEqual(results['create'].get('status'), 200, results['create'])

        order = Order.objects.get(restaurant=self.restaurant)
        self.assertFalse(
            order.is_test,
            'a commercial order was classified as a pre-go-live rehearsal',
        )

    # --- race 4: the shared side --------------------------------------------

    def test_concurrent_admissions_at_one_restaurant_both_succeed(self):
        """
        Admissions do not exclude each other — the point of taking the lock SHARED.

        Two diners at different tables of the same restaurant race with no
        transition in play. Both must be admitted: a restaurant is not a queue.
        This is also the self-deadlock canary — the advisory lock is held for the
        whole create transaction, so a second holder that could not share it would
        hang here and `_run` would report it.
        """
        results = {}
        _run([
            (_create, (self.ids, results, 'a', 'table', None, 1)),
            (_create, (self.ids, results, 'b', 'table_b', None, 1)),
        ])

        self.assertEqual(results['a'].get('status'), 200, results['a'])
        self.assertEqual(results['b'].get('status'), 200, results['b'])
        self.assertEqual(Order.objects.filter(restaurant=self.restaurant).count(), 2)

    # --- race 5: mixed load, documented lock order ---------------------------

    def test_mixed_admission_and_transition_load_does_not_deadlock(self):
        """
        Admissions and a transition under real contention: no deadlock, and the
        final state is coherent.

        Exercises the documented ordering — advisory first, then rows — from both
        sides at once. A worker that took a row lock before the advisory lock would
        close the cycle and hang here rather than failing an assertion.
        """
        results = {}
        _run([
            (_create, (self.ids, results, 'a')),
            (_create, (self.ids, results, 'b', 'table_b')),
            (_transition, (self.ids, RestaurantStatus_Suspended, results)),
        ])

        self.assertEqual(results['transition'], 'ok', results)
        self.assertEqual(self._status(), RestaurantStatus_Suspended)
        # Both admissions queue behind the exclusive lock, then re-read the state
        # the transition committed. Two waiters rather than one is the point: they
        # must serialise cleanly against the transition and against each other.
        for key in ('a', 'b'):
            self.assertEqual(results[key].get('status'), 400, results[key])
        self.assertEqual(Order.objects.filter(restaurant=self.restaurant).count(), 0)

    # --- sequential companions ----------------------------------------------
    # The races prove the locking. These prove the RULE, without threads, so a
    # failure here is unambiguous about which half broke.

    def test_submit_is_refused_once_suspended(self):
        """The submit gate itself, with no race: a draft cannot be submitted."""
        draft = self._draft()
        self._at(RestaurantStatus_Suspended)

        result = manage_order.update_order_status(draft, OrderStatus_Pending, None)

        self.assertEqual(result.get('status'), 400, result)
        draft.refresh_from_db()
        self.assertEqual(draft.order_status, OrderStatus_Initiated)

    def test_submit_still_works_while_live(self):
        """The gate refuses suspension, not submission — the happy path is intact."""
        draft = self._draft()

        result = manage_order.update_order_status(draft, OrderStatus_Pending, None)

        self.assertEqual(result.get('status'), 200, result)
        draft.refresh_from_db()
        self.assertEqual(draft.order_status, OrderStatus_Pending)

    def test_diner_draft_stays_a_diner_order_through_submit(self):
        """
        A diner's draft is judged by the DINER rule at submit, whoever taps submit.

        The submit path passes the ORDER's creator, not the submitting user. Were
        it to pass the caller, a staff member submitting a diner's draft would have
        it judged by the laxer staff rule and slip past the launch boundary at a
        restaurant that has not gone live.
        """
        draft = self._draft()
        self._at(RestaurantStatus_Onboarding)

        result = manage_order.update_order_status(draft, OrderStatus_Pending, self.owner)

        self.assertEqual(result.get('status'), 400, result)
        draft.refresh_from_db()
        self.assertEqual(draft.order_status, OrderStatus_Initiated)

    def test_in_flight_order_is_frozen_not_cancelled_by_suspension(self):
        """
        THE §2.4 POLICY, pinned: suspension freezes an accepted order, it does not
        drain or destroy it.

        The row survives untouched and keeps its status; what changes is that staff
        can no longer reach it, because the capability matrix closes the portal and
        the kitchen. Fulfilment resumes if the restaurant returns to `live`.
        """
        draft = self._draft()
        self.assertEqual(
            manage_order.update_order_status(
                draft, OrderStatus_Pending, None,
            ).get('status'),
            200,
        )

        self._at(RestaurantStatus_Suspended)

        draft.refresh_from_db()
        self.assertEqual(draft.order_status, OrderStatus_Pending)
        self.assertFalse(draft.deleted)
        # Frozen, and this is WHY: both staff-facing capabilities are closed.
        self.assertFalse(policy.grants_portal_access(RestaurantStatus_Suspended))
        self.assertFalse(policy.allows_kitchen(RestaurantStatus_Suspended))

        # ...and it thaws unchanged.
        self._at(RestaurantStatus_Live)
        draft.refresh_from_db()
        self.assertEqual(draft.order_status, OrderStatus_Pending)

    def test_create_chokepoint_self_guards_the_launch_boundary(self):
        """
        PR-D's boundary holds AT ``_create_order`` — not only at the endpoint above it.

        This calls the chokepoint directly, bypassing ``initiate_order``. Before the
        admission primitive that bypass succeeded: the lifecycle gate lived only in
        the caller, so the function that actually writes the order enforced nothing
        and a future caller could have reached it without passing a boundary at all.
        Menu publication had already been moved inside this transaction for exactly
        that reason ("this self-guards EVERY caller of _create_order"); lifecycle now
        matches. Behaviour through the real endpoint is unchanged.
        """
        self._at(RestaurantStatus_Onboarding)

        # The public is refused before go-live...
        diner = _create_order(
            restaurant=Restaurant.objects.get(pk=self.restaurant.pk),
            table=self.table,
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=None,
        )
        self.assertEqual(diner.get('status'), 400, diner)

        # ...while the owner's rehearsal order is allowed, and is a rehearsal.
        staff = _create_order(
            restaurant=Restaurant.objects.get(pk=self.restaurant.pk),
            table=self.table,
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=self.owner,
        )
        self.assertEqual(staff.get('status'), 200, staff)
        self.assertTrue(staff['order'].is_test)
