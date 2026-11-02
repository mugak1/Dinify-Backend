"""
Dining model, Step 1 — a concurrent double submit writes exactly ONE round
(PostgreSQL only, two real connections).

A diner who double-taps "Place order" sends two submits for one draft carrying the
same quote reference. The acceptance takes ``advisory -> Table -> Order`` and reads
its D04 evidence under those locks, so the second request waits, then finds the
first one's ``OrderAcceptance`` and replays. The Step 1 writer runs after that
replay return, so it must run exactly once: one visit, one bill, one placement.

DETERMINISTIC, NOT TIMED. The first submit is parked INSIDE its transaction right
after the three INSERTs (it holds every acceptance lock and has written the rows but
not committed them). The second submit is started and the test waits until
PostgreSQL reports it blocked on a lock (``pg_locks.granted = false``), so it is
provably queued inside the window. Only then is the first released. The racy variant
beside it starts both at one barrier and asserts the invariant that must hold for
every interleaving.

Skipped on SQLite, where none of these locks exist.
"""
import threading
import time
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase

from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers import manage_order
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Bill, Order, OrderAcceptance, OrderPlacement, Visit
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


class ConcurrentDoubleSubmitTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('needs PostgreSQL row and advisory locks')
        owner = User.objects.create_user(
            first_name='Race', last_name='Owner', email='race@test.com',
            phone_number='256700880001', username='256700880001', country='Uganda',
            password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Race', location='Kampala', owner=owner, status=RestaurantStatus_Live)
        RestaurantEmployee.objects.create(
            user=owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True)
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=area, enabled=True,
            is_active=True, qr_mode='order_pay', has_qr=True)
        section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, approved=True, enabled=True,
            available=True)
        item = MenuItem.objects.create(
            name='Rolex', section=section, primary_price=Decimal('5000'),
            approved=True, enabled=True, available=True, in_stock=True)
        result = _create_order(restaurant=self.restaurant, table=self.table,
                               items=[{'item': str(item.pk), 'quantity': 1}])
        self.assertEqual(result['status'], 200, result)
        self.order_id = result['order'].pk
        self.ref = quote_ref(result['order'])

    def _submit(self, results, name, barrier=None):
        try:
            if barrier is not None:
                barrier.wait(timeout=10)
            order = Order.objects.get(pk=self.order_id)
            results[name] = manage_order.update_order_status(
                order, OrderStatus_Pending, None, quote_ref=self.ref)
        except Exception as exc:                     # pragma: no cover - reported
            results[name] = exc
        finally:
            connection.close()

    def _assert_one_round(self, results):
        statuses = sorted((r['status'], bool(r.get('idempotent')))
                          for r in results.values())
        self.assertEqual(statuses, [(200, False), (200, True)], results)
        self.assertEqual(OrderAcceptance.objects.filter(order_id=self.order_id)
                         .count(), 1)
        self.assertEqual(Visit.objects.count(), 1)
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual(OrderPlacement.objects.filter(order_id=self.order_id)
                         .count(), 1)

    def _waiting_lock_count(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM pg_locks WHERE NOT granted')
            return cursor.fetchone()[0]

    def test_the_second_submit_waits_inside_the_window_and_replays(self):
        written, release = threading.Event(), threading.Event()
        real = round_placement.place_first_round
        calls = []

        def parked(order, accepted_at):
            calls.append(order.pk)
            placement = real(order, accepted_at)
            written.set()
            release.wait(timeout=15)
            return placement

        results = {}
        # ``_submit_order`` imports the writer at call time, so patching the
        # module attribute reaches it.
        with mock.patch.object(round_placement, 'place_first_round', parked):
            first = threading.Thread(target=self._submit, args=(results, 'first'))
            first.start()
            self.assertTrue(written.wait(timeout=15), 'first submit never wrote')
            second = threading.Thread(target=self._submit, args=(results, 'second'))
            second.start()
            deadline = time.monotonic() + 10
            while self._waiting_lock_count() == 0:
                self.assertLess(time.monotonic(), deadline,
                                'second submit never queued on a lock')
                time.sleep(0.02)
            # Uncommitted: nothing is visible to this connection yet.
            self.assertEqual(Visit.objects.count(), 0)
            release.set()
            first.join(timeout=20)
            second.join(timeout=20)
        self.assertFalse(first.is_alive() or second.is_alive(), 'a submit hung')
        self.assertEqual(len(calls), 1, 'the writer ran on the replay')
        self.assertFalse(results['first'].get('idempotent'), results)
        self.assertTrue(results['second'].get('idempotent'), results)
        self._assert_one_round(results)

    def test_a_racing_double_submit_writes_one_round(self):
        barrier = threading.Barrier(2)
        results = {}
        threads = [threading.Thread(target=self._submit, args=(results, name, barrier))
                   for name in ('a', 'b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads), 'a submit hung')
        self._assert_one_round(results)
