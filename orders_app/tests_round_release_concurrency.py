"""
Step 2, PR BE-D — Confirm and Decline on TWO REAL CONNECTIONS (PostgreSQL only).

Every race below fixes its arrival order rather than hoping for it: the first
command is PARKED at a real seam (an ``execute_wrapper`` on its own connection,
after a named statement), the second is started and observed WAITING ON A LOCK in
``pg_stat_activity`` under a deadline, and only then is the first let go. Nothing
is mocked and no ``sleep`` decides an outcome.

* Confirm vs Decline, both arrival orders: exactly one wins and the other gets its
  409 (``round_released`` / ``round_declined``).
* A kitchen command racing a Confirm is refused ``order_released_while_waiting``.
* THE LOCK ORDER: a kitchen command holding the Table and a Confirm never
  deadlock — Confirm waits on the Table before it ever touches the Order.
* The ``tables`` module is RE-CHECKED UNDER THE LOCK: a membership deactivated, or
  a restaurant suspended, while Confirm waits on the admission lock is respected.
"""
import threading
import time

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from dinify_backend.configss.string_definitions import (
    CancellationReason_DeclinedByStaff,
    OrderStatus_Cancelled,
    OrderStatus_Pending,
    RestaurantStatus_Suspended,
)
from orders_app.models import Order, OrderPlacement
from orders_app.tests_round_release import FloorFixture
from restaurants_app.controllers.admission_lock import lock_admission_exclusive
from restaurants_app.models import Restaurant, RestaurantEmployee

WAIT_TIMEOUT = 15
JOIN_TIMEOUT = 25


class FloorRaceBase(FloorFixture, TransactionTestCase):
    PHONE_BASE = 256700520000

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row and advisory locking require PostgreSQL')
        super().setUp()
        self.build_floor()
        self.results = {}
        self.errors = []
        self.threads = []

    # --- plumbing -------------------------------------------------------------

    def spawn(self, key, fn, *, application_name=None):
        def run():
            try:
                if application_name:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('application_name', %s, false)",
                            [application_name])
                self.results[key] = fn()
            except BaseException as exc:            # surfaced, never swallowed
                self.errors.append(f'{key}: {exc!r}')
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, name=key)
        thread.start()
        self.threads.append(thread)
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(timeout=JOIN_TIMEOUT)
        alive = [t.name for t in self.threads if t.is_alive()]
        self.assertEqual(alive, [], f'workers hung: {alive} (deadlock?)')
        self.assertEqual(self.errors, [])

    @staticmethod
    def parked(fn, predicate, parked_event, release_event):
        """``fn`` run with its connection parked right AFTER the first statement
        matching ``predicate``, until ``release_event`` is set."""
        fired = [False]

        def wrapper(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not fired[0] and predicate(sql):
                fired[0] = True
                parked_event.set()
                release_event.wait(timeout=WAIT_TIMEOUT)
            return result

        def run():
            with connection.execute_wrapper(wrapper):
                return fn()
        return run

    def wait_until_waiting(self, application_name, done=None):
        """Until the named backend is WAITING ON A LOCK. Observation only."""
        deadline = time.monotonic() + WAIT_TIMEOUT
        with connection.cursor() as monitor:
            while time.monotonic() < deadline:
                monitor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity '
                    'WHERE application_name = %s', [application_name])
                row = monitor.fetchone()
                if row and row[0] == 'Lock':
                    return
                if done is not None and done in self.results:
                    self.fail(f'{application_name} finished without waiting')
                time.sleep(0.02)                   # observation, not ordering
        self.fail(f'{application_name} never waited on a lock')

    @staticmethod
    def updates(table):
        return lambda sql: sql.startswith(f'UPDATE "{table}"')

    def kitchen_advance(self, order):
        return self.client_for(self.kitchen).put(
            f'/api/v1/kitchen/orders/{order.pk}/fulfilment-status/',
            {'action': 'advance', 'if_revision': 0}, format='json')


class ConfirmVersusDeclineTests(FloorRaceBase):

    def test_confirm_first_then_decline_is_round_released(self):
        order = self.make_round()
        parked, go = threading.Event(), threading.Event()
        self.spawn('confirm', self.parked(lambda: self.confirm(order),
                                          self.updates('order_placements'),
                                          parked, go))
        self.assertTrue(parked.wait(WAIT_TIMEOUT), 'confirm never wrote')
        self.spawn('decline', lambda: self.decline(order, user=self.owner),
                   application_name='be-d-decline')
        try:
            self.wait_until_waiting('be-d-decline', done='decline')
        finally:
            go.set()
        self.join()

        self.assertEqual(self.results['confirm'].status_code, 200)
        self.assertTrue(self.results['confirm'].json()['data']['changed'])
        loser = self.results['decline']
        self.assertEqual(loser.status_code, 409, loser.content)
        self.assertEqual((loser.json()['reason'], loser.json()['state']),
                         ('round_released', 'released'))
        row = Order.objects.get(pk=order.pk)
        self.assertEqual((row.order_status, row.fulfilment_revision),
                         (OrderStatus_Pending, 0))
        self.assertIsNotNone(OrderPlacement.objects.get(order=order).released_at)

    def test_decline_first_then_confirm_is_round_declined(self):
        order = self.make_round()
        parked, go = threading.Event(), threading.Event()
        self.spawn('decline', self.parked(lambda: self.decline(order),
                                          self.updates('orders'), parked, go))
        self.assertTrue(parked.wait(WAIT_TIMEOUT), 'decline never wrote')
        self.spawn('confirm', lambda: self.confirm(order, user=self.owner),
                   application_name='be-d-confirm')
        try:
            self.wait_until_waiting('be-d-confirm', done='confirm')
        finally:
            go.set()
        self.join()

        self.assertEqual(self.results['decline'].status_code, 200)
        loser = self.results['confirm']
        self.assertEqual(loser.status_code, 409, loser.content)
        self.assertEqual((loser.json()['reason'], loser.json()['state']),
                         ('round_declined', 'declined'))
        row = Order.objects.get(pk=order.pk)
        self.assertEqual((row.order_status, row.cancellation_reason),
                         (OrderStatus_Cancelled, CancellationReason_DeclinedByStaff))
        self.assertIsNone(OrderPlacement.objects.get(order=order).released_at)


class KitchenRacingConfirmTests(FloorRaceBase):

    def test_a_kitchen_command_racing_a_confirm_is_released_while_waiting(self):
        """The kitchen locates the round while it is HELD, then waits on the Table
        lock Confirm holds; its Order statement starts after Confirm commits and
        reads the release."""
        order = self.make_round()
        parked, go = threading.Event(), threading.Event()
        self.spawn('confirm', self.parked(lambda: self.confirm(order),
                                          self.updates('order_placements'),
                                          parked, go))
        self.assertTrue(parked.wait(WAIT_TIMEOUT), 'confirm never wrote')
        self.spawn('kitchen', lambda: self.kitchen_advance(order),
                   application_name='be-d-kitchen')
        try:
            self.wait_until_waiting('be-d-kitchen', done='kitchen')
        finally:
            go.set()
        self.join()

        self.assertEqual(self.results['confirm'].status_code, 200)
        refused = self.results['kitchen']
        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertEqual(refused.json()['reason'], 'order_released_while_waiting')
        row = Order.objects.get(pk=order.pk)
        self.assertEqual((row.fulfilment_status, row.fulfilment_revision), ('new', 0))


class LockOrderTests(FloorRaceBase):

    def test_a_kitchen_command_holding_the_table_and_a_confirm_never_deadlock(self):
        """The kitchen takes ``Table -> Order``; so must Confirm. Park the kitchen
        right after its Table lock, start Confirm, see it WAIT, let the kitchen go.

        With Table first, Confirm waits on the Table holding nothing the kitchen
        needs: the kitchen refuses the held round (``order_held``) and commits,
        then Confirm releases it. Taking the Order first would hold the row the
        kitchen reaches for next while waiting on the Table the kitchen holds — a
        deadlock PostgreSQL resolves by aborting one of them."""
        order = self.make_round()
        parked, go = threading.Event(), threading.Event()
        self.spawn('kitchen', self.parked(
            lambda: self.kitchen_advance(order),
            lambda sql: 'FOR UPDATE' in sql and 'FROM "tables"' in sql,
            parked, go))
        self.assertTrue(parked.wait(WAIT_TIMEOUT), 'the kitchen never locked')
        self.spawn('confirm', lambda: self.confirm(order),
                   application_name='be-d-lock-order')
        try:
            self.wait_until_waiting('be-d-lock-order', done='confirm')
        finally:
            go.set()
        self.join()

        kitchen = self.results['kitchen']
        self.assertEqual(kitchen.status_code, 409, kitchen.content)
        self.assertEqual(kitchen.json()['reason'], 'order_held')
        confirmed = self.results['confirm']
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertTrue(confirmed.json()['data']['changed'])


class AuthorityUnderTheLockTests(FloorRaceBase):
    """A revocation committed while Confirm waits on the admission lock is seen by
    the re-check under the lock. The holder stands in for any writer that takes the
    EXCLUSIVE side (a restaurants PUT, a lifecycle transition)."""

    def _revoked_while_waiting(self, revoke):
        order = self.make_round()
        holding, commit = threading.Event(), threading.Event()

        def holder():
            with transaction.atomic():
                lock_admission_exclusive(self.restaurant.pk)
                revoke()
                holding.set()
                commit.wait(timeout=WAIT_TIMEOUT)
            return 'committed'

        self.spawn('holder', holder)
        self.assertTrue(holding.wait(WAIT_TIMEOUT), 'the holder never locked')
        # The pre-lock gate still passes: the revocation is not committed yet.
        self.spawn('confirm', lambda: self.confirm(order),
                   application_name='be-d-revoked')
        try:
            self.wait_until_waiting('be-d-revoked', done='confirm')
        finally:
            commit.set()
        self.join()
        return order, self.results['confirm']

    def test_a_membership_deactivated_while_confirm_waits_is_respected(self):
        order, response = self._revoked_while_waiting(
            lambda: RestaurantEmployee.objects.filter(user=self.staff).update(
                active=False))
        self.assertEqual(response.status_code, 404, response.content)
        self.assertIsNone(OrderPlacement.objects.get(order=order).released_at)

    def test_a_suspension_committed_while_confirm_waits_is_respected(self):
        order, response = self._revoked_while_waiting(
            lambda: Restaurant.objects.filter(pk=self.restaurant.pk).update(
                status=RestaurantStatus_Suspended))
        self.assertEqual(response.status_code, 404, response.content)
        self.assertIsNone(OrderPlacement.objects.get(order=order).released_at)

    def test_control_nothing_revoked_confirms(self):
        order, response = self._revoked_while_waiting(lambda: None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(OrderPlacement.objects.get(order=order).released_at)
