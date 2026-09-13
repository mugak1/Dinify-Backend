"""
D05 — kitchen command concurrency (PostgreSQL only).

TWO WORKERS, TWO CONNECTIONS, ONE REAL DATABASE LOCK. Nothing is mocked and no
test decides an outcome with a sleep or a repeated run.

WHY THE STAGE A SEAM IS NOT REUSED. The reproductions that established these
defects used a ``connection.execute_wrapper`` that ran the competitor
SYNCHRONOUSLY at the primary's UPDATE boundary. That worked precisely BECAUSE
there was no lock: the competitor could commit while the primary sat inside its
own statement. Now that the primary holds the Table row for the whole decision,
the same shape would have the primary waiting on a competitor that is waiting on
the primary's lock — the test deadlocking against the fix. The barriers here are
placed around LOCK ACQUISITION and COMMIT instead, and the held worker is
released by a flag its peer sets BEFORE the peer reaches for the lock, so nothing
ever waits on something that is waiting on it.

THREE KINDS OF PROOF, and they answer different questions:

  ``HoldsTheLockTests``   — POSITIVE proof the lock exists: the second worker
                            sets ``lock_timeout`` and PostgreSQL raises by name.
                            Never "a thread did not finish", which any bug can
                            also produce.
  ``WinnerOrderTests``    — DETERMINISTIC ordering, both ways round, so each
                            outcome has one correct answer rather than a
                            distribution of them.
  ``FreeRaceTests``       — a genuine race with no ordering imposed, asserting
                            the INVARIANT that must hold for every interleaving.
"""
import threading
import time

from django.db import OperationalError, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem
from orders_app.controllers.manage_order import update_order_status
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.order_pricing import PRICING_VERSION_CORRECTED
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User
from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Served,
    PaymentStatus_Pending,
    RESTAURANT_KITCHEN,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)

JOIN_TIMEOUT = 20
WAIT_TIMEOUT = 15


def _fulfilment_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/fulfilment-status/'


def _cancel_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/cancel/'


def _priority_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/priority/'


class KitchenRaceBase(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row locking requires PostgreSQL')

        self.owner = self._user('256900020001')
        self.restaurant = Restaurant.objects.create(
            name='D05 Kitchen Race', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live, country='UG')
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.kitchen_user = self._member('256900020002', [RESTAURANT_KITCHEN])
        self.manager_user = self._member('256900020003', [RESTAURANT_MANAGER])

        self.area = DiningArea.objects.create(restaurant=self.restaurant, name='Main')
        self.table = Table.objects.create(
            restaurant=self.restaurant, dining_area=self.area, number=1)
        self.section = MenuSection.objects.create(
            restaurant=self.restaurant, name='Mains', approved=True,
            enabled=True, available=True)
        self.item = MenuItem.objects.create(
            section=self.section, name='Dish', primary_price=1000,
            available=True, in_stock=True, approved=True, enabled=True)

    def _user(self, phone):
        return User.objects.create_user(
            first_name='K', last_name='M', email=f'{phone}@test.com',
            phone_number=phone, username=phone, country='Uganda',
            password='pw', roles=[])

    def _member(self, phone, roles):
        user = self._user(phone)
        RestaurantEmployee.objects.create(
            user=user, restaurant=self.restaurant, roles=roles)
        return user

    def _order(self, table=None, **over):
        defaults = dict(
            restaurant=self.restaurant, table=table or self.table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=OrderStatus_Pending, payment_status=PaymentStatus_Pending,
            fulfilment_status='new', order_date=timezone.localdate(),
            pricing_version=PRICING_VERSION_CORRECTED)
        defaults.update(over)
        order = Order.objects.create(**defaults)
        OrderItem.objects.create(
            order=order, item=self.item, quantity=1, available=True,
            unit_price=0, discounted_price=0, unit_cost_of_options=0,
            total_cost=0, discounted_cost=0, savings=0, cost_of_options=0,
            actual_cost=0, item_name_snapshot=self.item.name)
        return order

    # --- worker plumbing --------------------------------------------------

    def _put(self, user, url, body):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.put(url, body, format='json')

    def _spawn(self, fn, results, key, errors):
        def run():
            try:
                results[key] = fn()
            except BaseException as exc:            # surfaced, never swallowed
                errors.append(f'{key}: {exc!r}')
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, name=key)
        thread.start()
        return thread

    def _join(self, threads, errors):
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], f'workers hung: {alive} (deadlock?)')
        self.assertEqual(errors, [])

    def _held(self, fn, released):
        """Run ``fn`` with its connection parked immediately AFTER it takes a row
        lock, until ``released`` is set. The wrapper fires on the FIRST
        ``FOR UPDATE`` the worker issues, which is the Table lock."""
        fired = [False]

        def wrapper(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not fired[0] and 'FOR UPDATE' in sql:
                fired[0] = True
                released.wait(timeout=WAIT_TIMEOUT)
            return result

        def run():
            with connection.execute_wrapper(wrapper):
                return fn()
        run.fired = fired
        return run, fired


class HoldsTheLockTests(KitchenRaceBase):
    """POSITIVE proof that a second command really waits on the first.

    OBSERVED FROM A THIRD, READ-ONLY CONNECTION rather than by setting
    ``lock_timeout`` on the waiter. That was the first design and it was wrong in
    an instructive way: a waiter cancelled by ``lock_timeout`` stays in
    PostgreSQL's lock queue for the tuple, and the HOLDER's own later
    referential-integrity lock on the same ``tables`` row then queues behind it —
    so the test failed the worker it was supposed to be proving correct. That is
    exactly the "barrier deadlocks the test against the fix" hazard, reached by a
    different route. Watching ``pg_locks`` sets nothing on anybody and lets both
    workers finish.

    Never "a thread did not finish": that is also what a bug produces.
    """

    def _observe_waiter(self, timeout=8.0):
        """True once some backend in this database is WAITING on a lock.

        ``pg_stat_activity.wait_event_type = 'Lock'`` is the right question. The
        first attempt joined ``pg_locks`` to ``pg_class`` on ``relation``, which
        silently never matches: a row-level wait blocks on the HOLDER'S
        TRANSACTION ID (``locktype = 'transactionid'``), whose ``relation`` is
        NULL — so the probe reported "no waiter" while a waiter was sitting
        right there.
        """
        import psycopg
        settings = connection.settings_dict
        dsn = (f"dbname={settings['NAME']} user={settings['USER']} "
               f"host={settings['HOST']} port={settings['PORT']}")
        if settings.get('PASSWORD'):
            dsn += f" password={settings['PASSWORD']}"
        deadline = time.monotonic() + timeout
        with psycopg.connect(dsn, autocommit=True) as observer:
            while time.monotonic() < deadline:
                with observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = %s AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()",
                        [settings['NAME']])
                    if cursor.fetchone()[0] > 0:
                        return True
                threading.Event().wait(0.02)
        return False

    def test_a_second_command_waits_on_the_first(self):
        order = self._order(fulfilment_status='new')
        released = threading.Event()
        errors, results = [], {}

        def first():
            return self._put(self.kitchen_user, _fulfilment_url(order.pk),
                             {'action': 'advance', 'if_revision': 0}).status_code

        held, fired = self._held(first, released)
        t1 = self._spawn(held, results, 'first', errors)
        for _ in range(400):
            if fired[0]:
                break
            threading.Event().wait(0.01)
        self.assertTrue(fired[0], 'the first worker never took a row lock')

        def second():
            # NO lock_timeout: this worker really waits, and its answer is
            # decided on the state the first worker committed.
            return self._put(
                self.manager_user, _cancel_url(order.pk),
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': 0})

        t2 = self._spawn(second, results, 'second', errors)
        observed = self._observe_waiter()
        released.set()
        self._join([t1, t2], errors)

        self.assertTrue(observed,
                        'no backend was ever seen waiting for a lock on tables '
                        '— the second command did not serialize behind the first')
        self.assertEqual(results['first'], 200)
        # And having waited, it decided on the state the first one left.
        self.assertEqual(results['second'].status_code, 409)
        self.assertEqual(results['second'].json()['reason'],
                         'kitchen_precondition_stale')
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.fulfilment_status, 'preparing')
        self.assertEqual(row.fulfilment_revision, 1)
        self.assertIsNone(row.cancelled_at)


class WinnerOrderTests(KitchenRaceBase):
    """DETERMINISTIC: the first worker takes the lock and commits; the second
    then decides on the state the first actually left behind. Both orders."""

    def _sequenced(self, first_fn, second_fn):
        released = threading.Event()
        errors, results = [], {}
        held, fired = self._held(first_fn, released)
        t1 = self._spawn(held, results, 'first', errors)
        for _ in range(300):
            if fired[0]:
                break
            threading.Event().wait(0.01)
        self.assertTrue(fired[0], 'the first worker never took a row lock')
        # The second worker now queues on the lock the first is holding.
        t2 = self._spawn(second_fn, results, 'second', errors)
        threading.Event().wait(0.15)
        released.set()
        self._join([t1, t2], errors)
        return results

    def test_a_cancellation_that_wins_is_not_overwritten_by_a_serve(self):
        """REGRESSION (Stage A C1). The serve used to write
        `order_status='served'` straight over a committed cancellation, leaving a
        row simultaneously cancelled and sold."""
        order = self._order(fulfilment_status='ready')
        order.actual_cost = 25000
        order.save(update_fields=['actual_cost'])

        results = self._sequenced(
            lambda: self._put(
                self.manager_user, _cancel_url(order.pk),
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': 0}).status_code,
            lambda: self._put(
                self.kitchen_user, _fulfilment_url(order.pk),
                {'action': 'serve', 'if_revision': 0}),
        )
        self.assertEqual(results['first'], 200)
        self.assertEqual(results['second'].status_code, 409)
        self.assertIn(results['second'].json()['reason'],
                      ('order_cancelled', 'kitchen_precondition_stale'))

        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.order_status, OrderStatus_Cancelled)
        self.assertEqual(row.fulfilment_status, 'ready')
        self.assertIsNone(row.served_at)
        self.assertIsNotNone(row.cancelled_at)

    def test_a_serve_that_wins_is_not_overwritten_by_a_cancellation(self):
        """REGRESSION (Stage A C2). The cancel used to write over a committed
        serve, leaving a SERVED order marked cancelled."""
        order = self._order(fulfilment_status='ready')
        order.actual_cost = 25000
        order.save(update_fields=['actual_cost'])

        results = self._sequenced(
            lambda: self._put(
                self.kitchen_user, _fulfilment_url(order.pk),
                {'action': 'serve', 'if_revision': 0}).status_code,
            lambda: self._put(
                self.manager_user, _cancel_url(order.pk),
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': 0}),
        )
        self.assertEqual(results['first'], 200)
        self.assertEqual(results['second'].status_code, 409)

        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.order_status, OrderStatus_Served)
        self.assertEqual(row.fulfilment_status, 'served')
        self.assertIsNotNone(row.served_at)
        self.assertIsNone(row.cancelled_at)
        self.assertIsNone(row.cancellation_reason)

    def test_preparation_winning_re_evaluates_an_ordinary_staff_cancellation(self):
        """REGRESSION (Stage A C3). An ordinary kitchen user's free void,
        qualified against a `new` the order had already stopped being, used to
        survive preparation starting and write a cancellation the manager rule
        forbids."""
        order = self._order(fulfilment_status='new')

        results = self._sequenced(
            lambda: self._put(
                self.kitchen_user, _fulfilment_url(order.pk),
                {'action': 'advance', 'if_revision': 0}).status_code,
            lambda: self._put(
                self.kitchen_user, _cancel_url(order.pk),
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': 0}),
        )
        self.assertEqual(results['first'], 200)
        # 403, not a stale-precondition 409: the escalation is re-evaluated
        # against the fresh `preparing`, so this caller is refused on AUTHORITY.
        # Permission is decided before the precondition deliberately — an
        # operator who may not perform the command at all is told that, rather
        # than being invited to reload and retry something they still could not
        # do.
        self.assertEqual(results['second'].status_code, 403)
        self.assertEqual(results['second'].json()['reason'],
                         'kitchen_manage_required')

        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.order_status, OrderStatus_Pending)
        self.assertEqual(row.fulfilment_status, 'preparing')
        self.assertIsNone(row.cancelled_by_id)

    def test_a_manager_cancellation_of_a_now_preparing_order_still_works(self):
        """CONTROL: the escalation is re-evaluated, not universally tightened —
        a manager with a CURRENT revision still cancels."""
        order = self._order(fulfilment_status='new')
        self._put(self.kitchen_user, _fulfilment_url(order.pk),
                  {'action': 'advance', 'if_revision': 0})
        row = Order.objects.get(pk=order.pk)
        response = self._put(
            self.manager_user, _cancel_url(order.pk),
            {'cancellation_reason': CancellationReason_CustomerChangedMind,
             'if_revision': row.fulfilment_revision})
        self.assertEqual(response.status_code, 200)

    def test_a_recall_loses_to_an_acceptance_that_claimed_the_table(self):
        """REGRESSION (Stage A C4). The recall never asked whether the table had
        been taken, so a served order recalled onto a table a newly accepted
        order already occupied."""
        served = self._order(fulfilment_status='served',
                             order_status=OrderStatus_Served,
                             served_at=timezone.now())
        draft = self._order(order_status=OrderStatus_Initiated)
        ref = quote_ref(Order.objects.get(pk=draft.pk))

        results = self._sequenced(
            lambda: update_order_status(
                Order.objects.get(pk=draft.pk), OrderStatus_Pending, None, ref
            )['status'],
            lambda: self._put(
                self.kitchen_user, _fulfilment_url(served.pk),
                {'action': 'recall', 'if_revision': 0}),
        )
        self.assertEqual(results['first'], 200)
        self.assertEqual(results['second'].status_code, 409)
        self.assertEqual(results['second'].json()['reason'], 'table_occupied')
        self.assertEqual(self._occupants(), [draft.id])

    def _occupants(self):
        return list(
            Order.objects.filter(table=self.table, deleted=False)
            .exclude(order_status=OrderStatus_Initiated)
            .exclude(order_status=OrderStatus_Cancelled)
            .exclude(fulfilment_status='served')
            .values_list('id', flat=True))


class FreeRaceTests(KitchenRaceBase):
    """A genuine race with no ordering imposed: the INVARIANT must hold for
    every interleaving."""

    def _race(self, a, b):
        errors, results = [], {}
        start = threading.Barrier(2, timeout=WAIT_TIMEOUT)

        def wrap(fn, key):
            def run():
                start.wait()
                return fn()
            return run

        t1 = self._spawn(wrap(a, 'a'), results, 'a', errors)
        t2 = self._spawn(wrap(b, 'b'), results, 'b', errors)
        self._join([t1, t2], errors)
        return results

    def _occupants(self):
        return list(
            Order.objects.filter(table=self.table, deleted=False)
            .exclude(order_status=OrderStatus_Initiated)
            .exclude(order_status=OrderStatus_Cancelled)
            .exclude(fulfilment_status='served')
            .values_list('id', flat=True))

    def test_two_devices_advancing_the_same_ticket_apply_exactly_once(self):
        """REGRESSION (Stage A C6). Both used to return 200 and one decision was
        silently lost. Now exactly one applies and the other is told why."""
        order = self._order(fulfilment_status='new')
        advance = lambda: self._put(                       # noqa: E731
            self.kitchen_user, _fulfilment_url(order.pk),
            {'action': 'advance', 'if_revision': 0})
        results = self._race(advance, advance)

        codes = sorted(r.status_code for r in results.values())
        self.assertEqual(codes, [200, 409])
        loser = next(r for r in results.values() if r.status_code == 409)
        self.assertEqual(loser.json()['reason'], 'kitchen_precondition_stale')

        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.fulfilment_status, 'preparing')
        self.assertEqual(row.fulfilment_revision, 1)

    def test_cancel_racing_serve_leaves_one_coherent_outcome(self):
        order = self._order(fulfilment_status='ready')
        results = self._race(
            lambda: self._put(
                self.manager_user, _cancel_url(order.pk),
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': 0}),
            lambda: self._put(
                self.kitchen_user, _fulfilment_url(order.pk),
                {'action': 'serve', 'if_revision': 0}),
        )
        codes = sorted(r.status_code for r in results.values())
        self.assertEqual(codes, [200, 409])

        row = Order.objects.get(pk=order.pk)
        cancelled = (row.order_status == OrderStatus_Cancelled
                     and row.fulfilment_status == 'ready'
                     and row.served_at is None)
        served = (row.order_status == OrderStatus_Served
                  and row.fulfilment_status == 'served'
                  and row.served_at is not None
                  and row.cancelled_at is None)
        self.assertTrue(
            cancelled or served,
            f'incoherent row: status={row.order_status} '
            f'fulfilment={row.fulfilment_status} served_at={row.served_at} '
            f'cancelled_at={row.cancelled_at}')
        self.assertEqual(row.fulfilment_revision, 1)

    def test_two_recalls_leave_exactly_one_occupant(self):
        """REGRESSION (Stage A C5). Two served orders at one table both used to
        recall successfully, leaving two ongoing orders."""
        a = self._order(fulfilment_status='served',
                        order_status=OrderStatus_Served, served_at=timezone.now())
        b = self._order(fulfilment_status='served',
                        order_status=OrderStatus_Served, served_at=timezone.now())
        results = self._race(
            lambda: self._put(self.kitchen_user, _fulfilment_url(a.pk),
                              {'action': 'recall', 'if_revision': 0}),
            lambda: self._put(self.kitchen_user, _fulfilment_url(b.pk),
                              {'action': 'recall', 'if_revision': 0}),
        )
        codes = sorted(r.status_code for r in results.values())
        self.assertEqual(codes, [200, 409])
        loser = next(r for r in results.values() if r.status_code == 409)
        self.assertEqual(loser.json()['reason'], 'table_occupied')
        self.assertEqual(len(self._occupants()), 1)

    def test_two_priority_writes_of_different_values_apply_exactly_once(self):
        order = self._order(priority=False)
        results = self._race(
            lambda: self._put(self.kitchen_user, _priority_url(order.pk),
                              {'priority': True, 'if_revision': 0}),
            lambda: self._put(self.kitchen_user, _priority_url(order.pk),
                              {'priority': False, 'if_revision': 0}),
        )
        # THE INVARIANT IS "AT MOST ONE APPLIED", not "one of them failed".
        # Setting priority to False on an order that is already False is a
        # legitimate no-write `unchanged` 200 — so BOTH can answer 200, and that
        # is correct rather than a lost update. What must never happen is two
        # applied writes.
        outcomes = [r.json().get('outcome') for r in results.values()
                    if r.status_code == 200]
        self.assertLessEqual(outcomes.count('applied'), 1, outcomes)
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.fulfilment_revision, outcomes.count('applied'))
        if row.fulfilment_revision == 0:
            self.assertFalse(row.priority)


class RollbackTests(KitchenRaceBase):
    """A failure after the mutation must unwind state, revision, timestamps and
    provenance together — the write is not committed in pieces."""

    def test_a_failure_after_the_write_rolls_the_whole_command_back(self):
        from unittest import mock
        from orders_app.controllers.services import kitchen_transition as kt

        order = self._order(fulfilment_status='ready')
        before = Order.objects.get(pk=order.pk)

        real_state = kt.order_state
        calls = {'n': 0}

        def exploding_state(o):
            # Fires only on the projection built AFTER the save, so the mutation
            # has genuinely happened by the time this raises.
            calls['n'] += 1
            if calls['n'] >= 1 and o.fulfilment_status == 'served':
                # GUARD: prove the write really did occur before we unwind it.
                assert o.served_at is not None
                assert o.fulfilment_revision == before.fulfilment_revision + 1
                raise RuntimeError('injected failure after the write')
            return real_state(o)

        with mock.patch.object(kt, 'order_state', side_effect=exploding_state):
            with self.assertRaises(RuntimeError):
                kt.execute(
                    order.pk, self.kitchen_user,
                    kt.KitchenCommand(action=kt.ACTION_SERVE,
                                      if_revision=before.fulfilment_revision),
                )

        after = Order.objects.get(pk=order.pk)
        self.assertEqual(after.fulfilment_status, before.fulfilment_status)
        self.assertEqual(after.order_status, before.order_status)
        self.assertEqual(after.served_at, before.served_at)
        self.assertEqual(after.fulfilment_revision, before.fulfilment_revision)
        self.assertEqual(after.fulfilment_status_updated_at,
                         before.fulfilment_status_updated_at)


class DraftBoundaryAcrossAcceptanceTests(KitchenRaceBase):
    """A command formed against a DRAFT must not become a command against the
    order the diner has meanwhile placed. (Codex P2 on PR #319.)

    THE WINDOW IS REAL AND IT IS THIS SERVICE'S OWN. The kitchen locator reads
    the order BEFORE taking the table lock, and `_submit_order` holds that same
    lock while it flips `initiated -> pending`. So a kitchen command can locate a
    draft, block, and find a perfectly ordinary accepted order waiting for it —
    `_assert_operable` passes, and because SUBMISSION IS NOT A KITCHEN COMMAND it
    does not advance `fulfilment_revision`, so the precondition captured against
    the draft still matches.

    THE HARM IS THE ONE §3 NAMES: a delayed command becoming a DIFFERENT command.
    The operator formed "cancel this draft" — something the server refuses
    outright — and timing alone turns it into "cancel this diner's live order".
    """

    def _draft_and_ref(self):
        from orders_app.models import OrderItem
        draft = self._order(order_status=OrderStatus_Initiated,
                            fulfilment_status='new')
        return draft, quote_ref(Order.objects.get(pk=draft.pk))

    def _race_across_acceptance(self, kitchen_body, url_for):
        """Submit holds the table lock; the kitchen command has ALREADY located
        the draft and is queued behind it. Submit then commits."""
        draft, ref = self._draft_and_ref()
        submit_parked = threading.Event()
        located = threading.Event()
        release_submit = threading.Event()
        errors, results = [], {}

        def submit_worker():
            fired = [False]

            def wrapper(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if not fired[0] and 'FOR UPDATE' in sql:
                    fired[0] = True
                    submit_parked.set()
                    # Wait until the kitchen command has read the DRAFT.
                    located.wait(timeout=WAIT_TIMEOUT)
                    release_submit.wait(timeout=WAIT_TIMEOUT)
                return result

            with connection.execute_wrapper(wrapper):
                return update_order_status(
                    Order.objects.get(pk=draft.pk), OrderStatus_Pending, None, ref
                )['status']

        def kitchen_worker():
            fired = [False]

            def wrapper(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if not fired[0] and 'FROM "orders"' in sql and 'FOR UPDATE' not in sql:
                    # The locator has just read the row — still a draft.
                    fired[0] = True
                    located.set()
                    release_submit.set()
                return result

            with connection.execute_wrapper(wrapper):
                return self._put(self.kitchen_user, url_for(draft.pk), kitchen_body)

        t1 = self._spawn(submit_worker, results, 'submit', errors)
        self.assertTrue(submit_parked.wait(timeout=WAIT_TIMEOUT),
                        'the submit worker never took the table lock')
        t2 = self._spawn(kitchen_worker, results, 'kitchen', errors)
        self._join([t1, t2], errors)
        return draft, results

    def test_a_cancel_formed_against_a_draft_cannot_kill_the_placed_order(self):
        draft, results = self._race_across_acceptance(
            {'cancellation_reason': CancellationReason_CustomerChangedMind,
             'if_revision': 0},
            _cancel_url)
        self.assertEqual(results['submit'], 200, 'the diner placed their order')

        row = Order.objects.get(pk=draft.pk)
        self.assertEqual(
            results['kitchen'].status_code, 409,
            'a command formed against a draft was applied to the placed order')
        self.assertEqual(row.order_status, OrderStatus_Pending)
        self.assertIsNone(row.cancelled_at)
        self.assertIsNone(row.cancellation_reason)
        self.assertEqual(row.fulfilment_revision, 0)

    def test_an_advance_formed_against_a_draft_is_refused_too(self):
        draft, results = self._race_across_acceptance(
            {'action': 'advance', 'if_revision': 0}, _fulfilment_url)
        self.assertEqual(results['submit'], 200)
        row = Order.objects.get(pk=draft.pk)
        self.assertEqual(results['kitchen'].status_code, 409)
        self.assertEqual(row.fulfilment_status, 'new')
        self.assertEqual(row.fulfilment_revision, 0)

    def test_a_priority_command_formed_against_a_draft_is_refused_too(self):
        draft, results = self._race_across_acceptance(
            {'priority': True, 'if_revision': 0}, _priority_url)
        self.assertEqual(results['submit'], 200)
        row = Order.objects.get(pk=draft.pk)
        self.assertEqual(results['kitchen'].status_code, 409)
        self.assertFalse(row.priority)

    def test_control_an_ordinary_command_on_an_accepted_order_still_works(self):
        """The refusal is about CROSSING the boundary, not about accepted orders.
        A command formed AFTER acceptance is unaffected."""
        draft, ref = self._draft_and_ref()
        self.assertEqual(
            update_order_status(
                Order.objects.get(pk=draft.pk), OrderStatus_Pending, None, ref
            )['status'], 200)
        response = self._put(self.kitchen_user, _fulfilment_url(draft.pk),
                             {'action': 'advance', 'if_revision': 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Order.objects.get(pk=draft.pk).fulfilment_status, 'preparing')
