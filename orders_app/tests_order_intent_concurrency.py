"""
D04 — concurrent creation under one intent key (PostgreSQL only).

WHAT ONLY CONCURRENCY CAN SHOW. `tests_order_intent.py` pins the RULE on one
connection: what counts as the same purchase, and what each non-matching
outcome answers. It cannot show what happens when two requests carrying the
same key are genuinely in flight at once, because the interesting window is
between one transaction's COMMIT and another's — and a single connection can
never observe that.

THE TWO WINDOWS, and they need different protections:

    step 1 lookup ─┬─ (A) closed by the TABLE LOCK + the post-wait recheck
                   │     the loser blocks on the table row, and when it is
                   │     released it asks again before doing any new-order work
    table lock ────┤
    recheck ───────┤
                   ├─ (B) closed by the UNIQUE CONSTRAINT + the recovery
    INSERT ────────┘     the loser's INSERT trips
                         `uniq_order_restaurant_client_order_id` and the
                         savepoint unwinds the daily number with the row

Window A is same-table only — the table lock serialises nothing else — which is
why B exists and why neither is redundant. Both are exercised here.

DETERMINISTIC, NOT TIMED. A barrier pins the ordering so each test has ONE
correct outcome rather than a distribution of them; no assertion depends on a
sleep or on which thread happens to win. The genuinely racy test at the end
asserts an invariant that holds for EVERY interleaving, which is the only
honest way to test a race whose order is not fixed.

WHAT IS DELIBERATELY NOT CLAIMED. This is not exactly-once network delivery.
The guarantee is at most ONE order per scoped intent, with a recoverable
result — a response can still be lost on the wire, and the client's retry is
what recovers it.

Requires a lock-capable backend; skipped on SQLite, where the whole suite would
pass by not running.
"""
import threading
import uuid
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase, tag

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
    RestaurantStatus_Suspended,
)
from orders_app.controllers.services import create_order as create_order_module
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_intent import (
    REASON_INTENT_MISMATCH, REASON_INTENT_UNUSABLE,
)
from orders_app.models import (
    Order, OrderItem, RestaurantDailyOrderCounter,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


def _sync(barrier, times=1):
    """Pass `times` barrier phases, tolerating a barrier broken by a peer.

    A worker that raised before reaching its own phase must not leave the
    others blocked; the join timeout would catch the hang, but the failure it
    reported would be the hang rather than the original error.
    """
    if barrier is None:
        return
    for _ in range(times):
        try:
            barrier.wait(timeout=15)
        except threading.BrokenBarrierError:       # pragma: no cover
            return


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
        thread.join(timeout=30)
    for index, thread in enumerate(threads):
        assert not thread.is_alive(), f'worker {index} hung (lock regressed)'


def _attempt(ids, results, key_name, *, intent_key, quantity=1, table='table',
             created_by=None, phases=1, post_phases=0, barrier=None):
    """The REAL create path, entered as production enters it.

    `ConOrder.initiate_order` loads the restaurant in autocommit, runs its
    preflights and hands that instance to `_create_order`, which opens the
    transaction that writes the order. The barrier only makes the interval
    deterministic; the interval itself is production's.

    `post_phases` releases a peer AFTER this attempt has committed and let go
    of its connection — so the peer resumes into a world where this order is
    not merely written but fully visible to another connection.
    """
    try:
        restaurant = Restaurant.objects.get(pk=ids['restaurant'])
        table_row = Table.objects.get(pk=ids[table])
        _sync(barrier, phases)
        results[key_name] = _create_order(
            restaurant=restaurant,
            table=table_row,
            items=[{'item': str(ids['item']), 'quantity': quantity}],
            created_by=created_by,
            client_order_id=intent_key,
        )
        if post_phases:
            connection.close()
            _sync(barrier, post_phases)
    except Exception as exc:                       # pragma: no cover
        results[key_name] = f'error:{exc!r}'
        _sync(barrier, phases + post_phases)
    finally:
        connection.close()


def _suspend(ids, results, key_name='suspend', barrier=None):
    """Suspend the restaurant BETWEEN the two barrier phases.

    Phase 1 lets the waiting attempt take its stale read; the update commits;
    phase 2 releases it into a world that no longer admits new orders.
    """
    try:
        _sync(barrier)
        try:
            Restaurant.objects.filter(pk=ids['restaurant']).update(
                status=RestaurantStatus_Suspended)
            results[key_name] = 'ok'
        except Exception as exc:                   # pragma: no cover
            results[key_name] = f'error:{exc!r}'
        connection.close()
        _sync(barrier)
    finally:
        connection.close()


def _withdraw_dish(ids, results, key_name='withdraw', barrier=None):
    """Take the dish off the menu between the two barrier phases."""
    try:
        _sync(barrier)
        try:
            MenuItem.objects.filter(pk=ids['item']).update(enabled=False)
            results[key_name] = 'ok'
        except Exception as exc:                   # pragma: no cover
            results[key_name] = f'error:{exc!r}'
        connection.close()
        _sync(barrier)
    finally:
        connection.close()



class _ParkBeforeTheInsert:
    """Hold ONE named thread at step 2b — past the post-wait recheck, before
    the daily number and the INSERT — so a competitor can commit in between.

    This is the ONLY way to reach window B deliberately. At the same table the
    table lock closes the race before the INSERT is ever attempted, so the
    constraint path is reachable only across tables, and only if the winner
    commits inside this exact interval. `build_snapshot` is the seam because
    it is the first thing the create path does after the recheck and before
    steps 3+4; it is patched for every thread and acts for one, since
    `mock.patch` is process-global and both workers share the process.
    """

    def __init__(self, thread_name, barrier):
        self.thread_name = thread_name
        self.barrier = barrier

    def __enter__(self):
        real = create_order_module.build_snapshot

        def _hook(*args, **kwargs):
            snapshot = real(*args, **kwargs)
            if threading.current_thread().name == self.thread_name:
                _sync(self.barrier, 2)   # release the winner, then wait for it
            return snapshot

        self._patch = mock.patch.object(
            create_order_module, 'build_snapshot', side_effect=_hook)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def _run_named(barrier, workers):
    """`_run`, but with a barrier the caller already holds a reference to."""
    threads = [
        threading.Thread(target=fn, name=name, kwargs={'barrier': barrier})
        for name, fn in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive(), f'{thread.name} hung (lock regressed)'


@tag('concurrency')
class ConcurrentIntentTests(TransactionTestCase):
    """One restaurant, two orderable tables, one orderable dish."""

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row locking and advisory locks require PostgreSQL')

        self.owner = User.objects.create_user(
            first_name='Con', last_name='Owner', email='con_owner@test.com',
            phone_number='256700000951', username='256700000951',
            country='Uganda', password='password', roles=[],
        )
        self.staff = User.objects.create_user(
            first_name='Con', last_name='Staff', email='con_staff@test.com',
            phone_number='256700000952', username='256700000952',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Concurrent R', location='loc', owner=self.owner,
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
            'table': self.table.pk,
            'table_b': self.table_b.pk,
            'item': self.item.pk,
        }

    # --- helpers ----------------------------------------------------------

    def _rows(self, key):
        return Order.objects.filter(
            restaurant=self.restaurant, client_order_id=key)

    def _numbers(self):
        """Every daily number this restaurant has actually issued."""
        return sorted(
            Order.objects.filter(restaurant=self.restaurant)
            .exclude(order_number=None)
            .values_list('order_number', flat=True)
        )

    def _next_number(self):
        counter = RestaurantDailyOrderCounter.objects.filter(
            restaurant=self.restaurant).first()
        return None if counter is None else counter.next_number

    def _statuses(self, results):
        return {
            name: (value.get('status') if isinstance(value, dict) else value)
            for name, value in results.items()
        }

    # --- identical contenders ---------------------------------------------

    def test_two_identical_contenders_produce_exactly_one_order(self):
        """The double-tap. Both requests are the same purchase at the same
        scope, so both must be answered with the SAME order — one 200 that
        created it and one 200 that recovered it."""
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'a',
                                   intent_key=key, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'b',
                                   intent_key=key, **kw), ()),
        ])

        self.assertEqual(self._statuses(results), {'a': 200, 'b': 200},
                         results)
        ids = {str(results[name]['order'].id) for name in ('a', 'b')}
        self.assertEqual(len(ids), 1, results)
        self.assertEqual(self._rows(key).count(), 1)
        # exactly one of them created it
        self.assertEqual(
            sorted(results[name]['idempotent'] for name in ('a', 'b')),
            [False, True],
            results,
        )

    def test_the_loser_consumes_no_daily_number(self):
        """One order, ONE number — and at the same table, for the CHEAPER of
        the two reasons.

        The loser blocks on the table row, and its post-wait recheck returns
        the winner before any number is allocated, so nothing is taken and
        nothing has to be unwound. Stated explicitly because the reading a
        passing test invites here is the wrong one: this case does NOT
        exercise the shared rollback boundary, and it still passes with that
        boundary reverted. `test_a_loser_that_reaches_the_insert_unwinds_its_
        daily_number` is the one that does.
        """
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'a',
                                   intent_key=key, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'b',
                                   intent_key=key, **kw), ()),
        ])
        self.assertEqual(self._statuses(results), {'a': 200, 'b': 200},
                         results)
        self.assertEqual(self._numbers(), [1])
        self.assertEqual(self._next_number(), 2)

    def test_the_loser_writes_no_second_set_of_lines(self):
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'a',
                                   intent_key=key, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'b',
                                   intent_key=key, **kw), ()),
        ])
        order = self._rows(key).get()
        lines = OrderItem.objects.filter(
            order=order, deleted=False, parent_item__isnull=True)
        self.assertEqual(lines.count(), 1)
        self.assertEqual(lines.get().quantity, 1)

    # --- conflicting contenders -------------------------------------------

    def test_conflicting_contenders_yield_one_order_and_one_conflict(self):
        """Same key, different purchase, in flight together. One wins; the
        other is REFUSED rather than silently handed an order for a basket it
        never sent — and which of the two wins is not determined, so the
        assertion is on the pair."""
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'one',
                                   intent_key=key, quantity=1, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'three',
                                   intent_key=key, quantity=3, **kw), ()),
        ])

        statuses = sorted(self._statuses(results).values())
        self.assertEqual(statuses, [200, 409], results)
        self.assertEqual(self._rows(key).count(), 1)

        winner_name = next(n for n, v in results.items()
                           if v.get('status') == 200)
        loser_name = next(n for n, v in results.items()
                          if v.get('status') == 409)
        self.assertEqual(results[loser_name]['reason'],
                         REASON_INTENT_MISMATCH)
        self.assertNotIn('order', results[loser_name])
        # the surviving order is the winner's purchase, untouched
        stored = self._rows(key).get()
        self.assertEqual(stored.id, results[winner_name]['order'].id)
        self.assertEqual(
            OrderItem.objects.get(
                order=stored, deleted=False, parent_item__isnull=True,
            ).quantity,
            1 if winner_name == 'one' else 3,
        )
        self.assertEqual(self._numbers(), [1])

    def test_a_contender_at_another_table_is_refused_opaquely(self):
        """The restaurant-wide namespace, raced. Two tables cannot share a key,
        and the one that loses learns nothing about the other's order.

        This is the window the TABLE lock cannot close — the two attempts lock
        different rows — so it is the unique constraint and its recovery that
        answer here.
        """
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'here',
                                   intent_key=key, table='table', **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'there',
                                   intent_key=key, table='table_b', **kw), ()),
        ])

        statuses = sorted(self._statuses(results).values())
        self.assertEqual(statuses, [200, 409], results)
        self.assertEqual(self._rows(key).count(), 1)

        loser = next(v for v in results.values() if v.get('status') == 409)
        self.assertEqual(loser['reason'], REASON_INTENT_UNUSABLE)
        self.assertNotIn('order', loser)
        self.assertNotIn('data', loser)
        self.assertNotIn(str(self._rows(key).get().id), str(loser))
        self.assertEqual(self._numbers(), [1])

    def test_a_contender_with_different_provenance_is_refused(self):
        key = uuid.uuid4()
        results = {}
        staff_id = self.staff
        _run([
            (lambda **kw: _attempt(self.ids, results, 'diner',
                                   intent_key=key, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'staff',
                                   intent_key=key, created_by=staff_id,
                                   **kw), ()),
        ])
        statuses = sorted(self._statuses(results).values())
        self.assertEqual(statuses, [200, 409], results)
        self.assertEqual(
            next(v for v in results.values() if v.get('status') == 409
                 )['reason'],
            REASON_INTENT_MISMATCH,
        )
        self.assertEqual(self._rows(key).count(), 1)

    # --- the winner's world changes while the loser waits ------------------

    def test_a_replay_survives_a_suspension_that_lands_during_the_wait(self):
        """The retroactive-refusal case, and the reason the admission VERDICT
        is applied at step 1d rather than where the lock is taken.

        The first attempt commits. The restaurant is then suspended. The second
        attempt — the same purchase, the same key — has already passed step 1
        and is waiting; when it resumes it must be handed the order that
        exists, not refused because a NEW order would now be disallowed. A
        diner's own draft must not disappear because trading stopped a moment
        after it was created.
        """
        key = uuid.uuid4()
        first = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id=key,
        )
        self.assertEqual(first.get('status'), 200, first)

        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'retry',
                                   intent_key=key, phases=2, **kw), ()),
            (lambda **kw: _suspend(self.ids, results, **kw), ()),
        ])

        self.assertEqual(results['suspend'], 'ok')
        self.assertEqual(results['retry'].get('status'), 200,
                         results['retry'])
        self.assertTrue(results['retry']['idempotent'])
        self.assertEqual(results['retry']['order'].id, first['order'].id)
        self.assertEqual(self._rows(key).count(), 1)

    def test_a_new_request_is_still_refused_after_that_suspension(self):
        """The negative control for the test above: a replay is exempt because
        it is not new work, NOT because the admission gate went soft."""
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'fresh',
                                   intent_key=uuid.uuid4(), phases=2,
                                   **kw), ()),
            (lambda **kw: _suspend(self.ids, results, **kw), ()),
        ])
        self.assertEqual(results['suspend'], 'ok')
        self.assertEqual(results['fresh'].get('status'), 400,
                         results['fresh'])
        self.assertNotIn('reason', results['fresh'])
        self.assertEqual(Order.objects.filter(
            restaurant=self.restaurant).count(), 0)

    def test_a_replay_survives_the_dish_leaving_the_menu_during_the_wait(self):
        """The same argument for the menu: the recheck returns before the
        authoritative publication re-validation at step 2b, so a dish withdrawn
        a moment after the order was created cannot hide it."""
        key = uuid.uuid4()
        first = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id=key,
        )
        self.assertEqual(first.get('status'), 200, first)

        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'retry',
                                   intent_key=key, phases=2, **kw), ()),
            (lambda **kw: _withdraw_dish(self.ids, results, **kw), ()),
        ])

        self.assertEqual(results['withdraw'], 'ok')
        self.assertEqual(results['retry'].get('status'), 200,
                         results['retry'])
        self.assertEqual(results['retry']['order'].id, first['order'].id)

    # --- the same-table window, and an invariant over every interleaving ----

    def test_same_table_contenders_serialize_on_the_table_row(self):
        """Window A. Both attempts lock the SAME table row, so the loser blocks
        until the winner commits and then re-resolves under that lock — no
        admission refusal, no occupancy rejection, no daily number, no INSERT.

        The observable consequence is that the loser is never refused with the
        table-occupied 400 it would otherwise have hit, since an `initiated`
        draft does not occupy a table but the recheck returns before the gate
        is consulted at all.
        """
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda **kw: _attempt(self.ids, results, 'a',
                                   intent_key=key, **kw), ()),
            (lambda **kw: _attempt(self.ids, results, 'b',
                                   intent_key=key, **kw), ()),
        ])
        for name in ('a', 'b'):
            self.assertEqual(results[name].get('status'), 200, results[name])
            self.assertNotIn('data', results[name])

    def test_five_contenders_leave_one_order_and_one_number(self):
        """The invariant that holds for EVERY interleaving, which is the only
        honest assertion about a race whose order is not pinned."""
        key = uuid.uuid4()
        results = {}
        _run([
            (lambda name=f'w{i}', **kw: _attempt(
                self.ids, results, name, intent_key=key, **kw), ())
            for i in range(5)
        ])

        self.assertEqual(set(self._statuses(results).values()), {200},
                         results)
        ids = {str(v['order'].id) for v in results.values()}
        self.assertEqual(len(ids), 1, results)
        self.assertEqual(self._rows(key).count(), 1)
        self.assertEqual(self._numbers(), [1])
        self.assertEqual(self._next_number(), 2)
        self.assertEqual(
            sum(1 for v in results.values() if not v['idempotent']), 1,
            results,
        )

    def test_five_distinct_intents_are_five_orders_with_five_numbers(self):
        """The other direction, and the reason the key is what identifies an
        attempt: five diners deliberately ordering the same meal at five
        tables are five purchases, and nothing here deduplicates them.

        Only two tables exist, so this runs at one table each in turn would
        defeat the point — instead every attempt carries its OWN key at the two
        tables, and the assertion is that distinct keys never collapse.
        """
        results = {}
        keys = [uuid.uuid4() for _ in range(4)]
        _run([
            (lambda name=f'w{i}', k=keys[i], t=('table', 'table_b')[i % 2],
             **kw: _attempt(self.ids, results, name, intent_key=k, table=t,
                            **kw), ())
            for i in range(4)
        ])

        # each key produced at most one order, and no two keys shared one
        created = [v for v in results.values()
                   if isinstance(v, dict) and v.get('status') == 200]
        order_ids = {str(v['order'].id) for v in created}
        self.assertEqual(len(order_ids), len(created), results)
        for key in keys:
            self.assertLessEqual(self._rows(key).count(), 1)
        # every order that exists holds a distinct daily number
        numbers = self._numbers()
        self.assertEqual(len(numbers), len(set(numbers)), numbers)

    # --- window B: the constraint and the shared rollback boundary ---------

    def test_a_loser_that_reaches_the_insert_unwinds_its_daily_number(self):
        """Window B, reached deliberately.

        The loser is parked at step 2b — past its post-wait recheck, so it has
        already concluded the key is free — and the winner commits at another
        table while it waits. The loser therefore allocates a daily number and
        attempts the INSERT, which trips
        `uniq_order_restaurant_client_order_id`.

        THE ASSERTION THAT MATTERS is the number ledger. The allocation used
        to sit OUTSIDE the savepoint wrapping the INSERT, so the loser's
        savepoint rolled the row back and the outer block COMMITTED the number
        it had taken on its way out: one order, two numbers consumed. Both
        statements now share one boundary, so the loser unwinds both.

        This is the ONLY test in this file that reaches the constraint — at
        the same table the lock closes the race first, which is why the
        deterministic single-connection proofs in `tests_order_intent.py`
        exist alongside it.
        """
        key = uuid.uuid4()
        results = {}
        barrier = threading.Barrier(2)

        with _ParkBeforeTheInsert('loser', barrier):
            _run_named(barrier, [
                ('loser', lambda **kw: _attempt(
                    self.ids, results, 'loser', intent_key=key,
                    table='table', phases=0, **kw)),
                ('winner', lambda **kw: _attempt(
                    self.ids, results, 'winner', intent_key=key,
                    table='table_b', phases=1, post_phases=1, **kw)),
            ])

        self.assertEqual(results['winner'].get('status'), 200,
                         results['winner'])
        self.assertEqual(results['loser'].get('status'), 409,
                         results['loser'])
        self.assertEqual(results['loser']['reason'], REASON_INTENT_UNUSABLE)
        self.assertEqual(self._rows(key).count(), 1)
        # one order, ONE number — the loser's allocation went with its INSERT
        self.assertEqual(self._numbers(), [1])
        self.assertEqual(self._next_number(), 2)
