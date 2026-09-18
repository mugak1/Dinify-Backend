"""
D06 completion, G1a — A CATALOGUE EDIT CANNOT COMMIT INSIDE AN ACCEPTANCE
(PostgreSQL only, two real connections).

THE RACE, exactly as the tree ran it before the barrier:

    T1 (diner):  submit -> advisory SHARED -> Table -> Order -> reads the
                 catalogue in ONE statement, decides the purchase still stands
    T2 (kitchen): 86s the dish, or the operator withdraws it, and COMMITS
    T1 (diner):  transitions the order to `pending`, onto the kitchen board

Nothing made T1 notice. Under READ COMMITTED its snapshot statement had already
taken its own view of the world, and the catalogue writers shared no lock with
it — the exact shape of the pause race the advisory lock was introduced for, one
level down and introduced by D06 itself, which made acceptance a catalogue
reader in the same change that said menu edits were "an edit that no admission
reads".

RETIREMENT IS THE WORSE HALF, and it gets its own proof. It runs the same
purchase-integrity check and writes a CLOSURE from the result — a row that is
irreversible by design. An acceptance losing this race sends an order back for
review; a retirement losing it destroys a quote that was never stale.

BLOCKING IS PROVED POSITIVELY. The writer's connection sets `lock_timeout` and
PostgreSQL raises by name, so "it blocked" is an assertion rather than an
inference from a thread that failed to finish — the trap
`tests_table_allocation_lock` records. Every barrier test is paired with a
control showing the same writer completes immediately when no decision is in
flight, and `BarrierRemovedTests` patches the barrier out at its import sites to
show the bad interleaving really does reproduce without it.

DETERMINISTIC, NOT TIMED. A two-phase barrier pins the ordering: phase one closes
once the deciding thread has taken its snapshot, phase two once the writer has
had its turn. No assertion depends on a sleep.
"""
import threading
from decimal import Decimal
from unittest import mock

from django.db import OperationalError, connection, transaction
from django.test import TransactionTestCase, tag
from rest_framework.test import APIRequestFactory, force_authenticate

from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers import manage_order
from orders_app.controllers.services import purchase_integrity
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.endpoints_kitchen import KitchenMenuItemStockView
from orders_app.models import Order, OrderQuoteClosure
from restaurants_app.controllers.diner_capability import capability_from_table
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

#: Short enough that a blocked writer fails fast, long enough that an UNBLOCKED
#: one never trips it on a loaded machine.
LOCK_TIMEOUT = '750ms'


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


def _sync(barrier, times=1):
    """Pass ``times`` barrier phases, tolerating a barrier a peer already broke."""
    if barrier is None:
        return
    for _ in range(times):
        try:
            barrier.wait(timeout=15)
        except threading.BrokenBarrierError:       # pragma: no cover - defensive
            return


def _parking_snapshot(barrier):
    """`build_snapshot`, plus a park at the exact seam this race lives in.

    The real function runs first and its result is returned untouched, so the
    deciding thread is parked holding EXACTLY what production would hold: its
    locks, its transaction and a catalogue view already taken.
    """
    real = purchase_integrity.build_snapshot

    def _wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        _sync(barrier)          # phase 1: the snapshot is taken
        _sync(barrier)          # phase 2: the writer has had its turn
        return result

    return _wrapped


# --- workers ---------------------------------------------------------------

def _accept(ids, results, key='accept', barrier=None):
    """The REAL acceptance path, parked after its catalogue read."""
    try:
        order = Order.objects.get(pk=ids['order'])
        table = Table.objects.get(pk=order.table_id)
        with mock.patch.object(
            purchase_integrity, 'build_snapshot', _parking_snapshot(barrier),
        ):
            results[key] = manage_order.update_order_status(
                order, OrderStatus_Pending, None,
                quote_ref=quote_ref(order),
                capability=capability_from_table(table),
            )
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        _sync(barrier, 2)
    finally:
        connection.close()


def _retire(ids, results, key='retire', barrier=None):
    """The REAL retire-for-review path, parked at the same seam."""
    try:
        order = Order.objects.get(pk=ids['order'])
        table = Table.objects.get(pk=order.table_id)
        with mock.patch.object(
            purchase_integrity, 'build_snapshot', _parking_snapshot(barrier),
        ):
            results[key] = manage_order.retire_quote_for_review(
                order, quote_ref(order), capability=capability_from_table(table),
            )
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        _sync(barrier, 2)
    finally:
        connection.close()


def _stock_toggle(ids, results, key='writer', in_stock=False, phases=1,
                  barrier=None):
    """The REAL kitchen 86 route, on its own connection with a lock timeout.

    `lock_timeout` is what turns "blocked" into an assertion: PostgreSQL cancels
    the wait and names it, so the test never has to infer a block from a thread
    that simply did not finish.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        _sync(barrier, phases)
        request = APIRequestFactory().put(
            f"/api/v1/kitchen/menu-items/{ids['item']}/stock/",
            {'in_stock': in_stock}, format='json',
        )
        request.user = User.objects.get(pk=ids['owner'])
        force_authenticate(request, user=request.user)
        try:
            response = KitchenMenuItemStockView.as_view()(
                request, pk=str(ids['item']))
            results[key] = ('committed', getattr(response, 'status_code', None))
        except OperationalError as exc:
            results[key] = ('blocked', str(exc)[:120])
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
    finally:
        _sync(barrier)
        connection.close()


@tag('concurrency')
class CatalogueAdmissionConcurrencyTests(TransactionTestCase):
    """One live restaurant, two tables, one dish, one saved draft per test."""

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('advisory locks require PostgreSQL')
        self.owner = User.objects.create_user(
            first_name='Cat', last_name='Owner', email='cat_owner@test.com',
            phone_number='256700000991', username='256700000991',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Cat R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant)
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

    # -- helpers -----------------------------------------------------------

    def _draft(self, table=None):
        result = _create_order(
            restaurant=self.restaurant, table=table or self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=None,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _ids(self, order):
        return {
            'order': order.pk, 'item': self.item.pk, 'owner': self.owner.pk,
            'restaurant': self.restaurant.pk,
        }

    # -- the barrier -------------------------------------------------------

    def test_an_86_cannot_commit_inside_an_acceptance(self):
        order = self._draft()
        results = {}
        _run([
            (_accept, (self._ids(order), results)),
            (_stock_toggle, (self._ids(order), results)),
        ])

        self.assertEqual(results['writer'][0], 'blocked', results)
        self.assertEqual(results['accept'].get('status'), 200, results)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.item.refresh_from_db()
        self.assertTrue(
            self.item.in_stock,
            'the 86 must not have landed inside the decision that read it',
        )

    def test_an_86_cannot_commit_inside_a_RETIREMENT(self):
        """The worse half: a closure is irreversible."""
        order = self._draft()
        results = {}
        _run([
            (_retire, (self._ids(order), results)),
            (_stock_toggle, (self._ids(order), results)),
        ])

        self.assertEqual(results['writer'][0], 'blocked', results)
        self.assertEqual(results['retire'].get('status'), 200, results)
        self.assertEqual(
            results['retire'].get('outcome'), 'quote_still_valid', results)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a quote that was good when it was read must not be retired',
        )

    # -- the controls ------------------------------------------------------

    def test_the_control_an_86_with_nothing_in_flight_commits_at_once(self):
        """The barrier must block a decision, not the operator."""
        order = self._draft()
        results = {}
        _run([(_stock_toggle, (self._ids(order), results))])

        self.assertEqual(results['writer'], ('committed', 200), results)
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)

    def test_the_control_two_acceptances_do_not_block_each_other(self):
        """SHARED means shared. If the order paths started excluding one another
        this barrier would have cost exactly what it was designed not to."""
        first = self._draft()
        second = self._draft(table=self.table_b)
        results = {}

        def _plain_accept(order_id, key, barrier=None):
            try:
                order = Order.objects.get(pk=order_id)
                table = Table.objects.get(pk=order.table_id)
                _sync(barrier)
                results[key] = manage_order.update_order_status(
                    order, OrderStatus_Pending, None,
                    quote_ref=quote_ref(order),
                    capability=capability_from_table(table),
                )
            finally:
                connection.close()

        _run([
            (_plain_accept, (first.pk, 'a')),
            (_plain_accept, (second.pk, 'b')),
        ])
        self.assertEqual(results['a'].get('status'), 200, results)
        self.assertEqual(results['b'].get('status'), 200, results)

    def test_mixed_load_does_not_deadlock(self):
        """Four transactions taking the same locks in the documented order."""
        first = self._draft()
        second = self._draft(table=self.table_b)
        results = {}
        _run([
            (_accept, (self._ids(first), results, 'accept_a')),
            (_stock_toggle, (self._ids(first), results, 'writer_a', False)),
            (_accept, (self._ids(second), results, 'accept_b')),
            (_stock_toggle, (self._ids(second), results, 'writer_b', True)),
        ])
        for key in ('accept_a', 'accept_b'):
            self.assertIsInstance(results[key], dict, results)
            self.assertNotIn('deadlock', str(results[key]).lower())


@tag('concurrency')
class BarrierRemovedTests(CatalogueAdmissionConcurrencyTests):
    """Neutralise the barrier at its import sites and the defect comes back.

    Patched where it is USED, not where it is defined, so the test exercises the
    call sites rather than the helper: a writer that stopped calling it would
    fail these as surely as one that called a broken version.
    """

    def test_an_86_cannot_commit_inside_an_acceptance(self):
        import restaurants_app.controllers.catalogue_admission as barrier_mod
        order = self._draft()
        results = {}
        with mock.patch.object(barrier_mod, 'lock_catalogue_for_write',
                               lambda *a, **k: None):
            _run([
                (_accept, (self._ids(order), results)),
                (_stock_toggle, (self._ids(order), results)),
            ])

        self.assertEqual(
            results['writer'], ('committed', 200),
            'without the barrier the 86 commits inside the decision',
        )
        self.assertEqual(results['accept'].get('status'), 200, results)
        self.item.refresh_from_db()
        self.assertFalse(self.item.in_stock)
        order.refresh_from_db()
        self.assertEqual(
            order.order_status, OrderStatus_Pending,
            'THE DEFECT: an order for a dish the kitchen had just run out of '
            'reached the kitchen board',
        )

    def test_an_86_cannot_commit_inside_a_RETIREMENT(self):
        import restaurants_app.controllers.catalogue_admission as barrier_mod
        order = self._draft()
        results = {}
        with mock.patch.object(barrier_mod, 'lock_catalogue_for_write',
                               lambda *a, **k: None):
            _run([
                (_retire, (self._ids(order), results)),
                (_stock_toggle, (self._ids(order), results)),
            ])

        self.assertEqual(results['writer'], ('committed', 200), results)
        self.assertEqual(
            results['retire'].get('outcome'), 'quote_still_valid', results,
        )
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'the retirement decided on a snapshot the 86 had already invalidated',
        )

    def test_the_control_an_86_with_nothing_in_flight_commits_at_once(self):
        """Unchanged by the patch — kept so the class stays a complete mirror."""
        super().test_the_control_an_86_with_nothing_in_flight_commits_at_once()

    def test_the_control_two_acceptances_do_not_block_each_other(self):
        super().test_the_control_two_acceptances_do_not_block_each_other()

    def test_mixed_load_does_not_deadlock(self):
        super().test_mixed_load_does_not_deadlock()
