"""
Concurrency proof for the referenced-extra invariant (PR3, PostgreSQL only).

Invariant: concurrently ASSIGNING an extra to a parent and DEMOTING/DELETING that
same extra may result in either operation failing, but must NEVER commit an invalid
final relationship (an active has_extras parent referencing a non-extra / deleted
item). Both operations take a select_for_update lock on the EXTRA's own row, so they
serialise on one row.

Deterministic where it can be (sequential re-validation cases) and barrier-raced
for the true-contention case; no arbitrary sleeps are used for correctness — the DB
row lock supplies the real blocking. Each worker runs in its own transaction and
closes its connection. Requires a lock-capable backend (skipped on SQLite).
"""
import threading

from django.db import connection, transaction
from django.test import TransactionTestCase, tag

from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem,
)
from restaurants_app.serializers import SerializerPutMenuItem
from users_app.models import User
from dinify_backend.configs import ROLES
from dinify_backend.configss.string_definitions import RestaurantStatus_Active


def _assign(parent_id, extra_id, results, barrier=None):
    """Run the REAL assign chokepoint (SerializerPutMenuItem sets extras_applicable),
    which locks the extra row and re-reads is_extra under the lock."""
    try:
        with transaction.atomic():
            if barrier is not None:
                barrier.wait(timeout=10)
            parent = MenuItem.objects.get(pk=parent_id)
            ser = SerializerPutMenuItem(
                instance=parent, data={'extras_applicable': [str(extra_id)]},
                partial=True,
            )
            if ser.is_valid():
                ser.save()
                results['assign'] = 'ok'
            else:
                results['assign'] = 'rejected'
    except Exception as exc:                       # pragma: no cover - defensive
        results['assign'] = f'error:{exc!r}'
    finally:
        connection.close()


def _demote(extra_id, results, barrier=None):
    """Run the REAL demote chokepoint (is_extra True->False), which locks the extra
    row and scans inbound references under the lock."""
    try:
        with transaction.atomic():
            if barrier is not None:
                barrier.wait(timeout=10)
            extra = MenuItem.objects.get(pk=extra_id)
            ser = SerializerPutMenuItem(
                instance=extra, data={'is_extra': False}, partial=True,
            )
            if ser.is_valid():
                ser.save()
                results['demote'] = 'ok'
            else:
                results['demote'] = 'rejected'
    except Exception as exc:                       # pragma: no cover - defensive
        results['demote'] = f'error:{exc!r}'
    finally:
        connection.close()


@tag('concurrency')
class ReferencedExtraConcurrencyTests(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking requires PostgreSQL')
        self.owner = User.objects.create_user(
            first_name='Conc', last_name='Owner', email='conc_owner@test.com',
            phone_number='256700000700', username='256700000700',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Conc Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.section = MenuSection.objects.create(
            name='Sec', restaurant=self.restaurant, approved=True, enabled=True,
        )
        self.extra = MenuItem.objects.create(
            name='Extra', section=self.section, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )
        self.parent = MenuItem.objects.create(
            name='Parent', section=self.section, primary_price=5000,
            approved=True, enabled=True, has_extras=True, extras_applicable=[],
        )

    def _reset(self):
        MenuItem.objects.filter(pk=self.extra.pk).update(is_extra=True, deleted=False)
        MenuItem.objects.filter(pk=self.parent.pk).update(extras_applicable=[])

    def _assert_consistent(self):
        extra = MenuItem.objects.get(pk=self.extra.pk)
        parent = MenuItem.objects.get(pk=self.parent.pk)
        referenced = str(self.extra.pk) in [str(x) for x in parent.extras_applicable]
        invalid = referenced and (not extra.is_extra or extra.deleted)
        self.assertFalse(
            invalid,
            f'invalid final state: is_extra={extra.is_extra} deleted={extra.deleted} '
            f'referenced={referenced}',
        )
        return extra, parent, referenced

    def test_demote_after_committed_assign_is_rejected(self):
        # Sequential: assign commits first; the later demote must observe the
        # reference and REJECT (extra stays an extra).
        results = {}
        _assign(self.parent.pk, self.extra.pk, results)
        self.assertEqual(results['assign'], 'ok')
        _demote(self.extra.pk, results)
        self.assertEqual(results['demote'], 'rejected')
        extra, parent, referenced = self._assert_consistent()
        self.assertTrue(extra.is_extra)
        self.assertTrue(referenced)

    def test_assign_after_committed_demote_is_rejected(self):
        # Sequential: demote commits first; the later assign must re-read
        # is_extra=False under the lock and REJECT (no reference written).
        results = {}
        _demote(self.extra.pk, results)
        self.assertEqual(results['demote'], 'ok')
        _assign(self.parent.pk, self.extra.pk, results)
        self.assertEqual(results['assign'], 'rejected')
        extra, parent, referenced = self._assert_consistent()
        self.assertFalse(extra.is_extra)
        self.assertFalse(referenced)

    def test_barrier_raced_assign_vs_demote_never_invalid(self):
        # True contention: both operations reach a barrier, then race for the extra
        # row lock. Whichever wins, the loser serialises behind it and re-validates.
        # The final committed state is ALWAYS consistent, and exactly one wins.
        for _ in range(8):
            self._reset()
            results = {}
            barrier = threading.Barrier(2)
            t_assign = threading.Thread(
                target=_assign, args=(self.parent.pk, self.extra.pk, results),
                kwargs={'barrier': barrier},
            )
            t_demote = threading.Thread(
                target=_demote, args=(self.extra.pk, results),
                kwargs={'barrier': barrier},
            )
            t_assign.start()
            t_demote.start()
            t_assign.join(timeout=15)
            t_demote.join(timeout=15)
            self.assertFalse(t_assign.is_alive(), 'assign thread hung (lock regressed)')
            self.assertFalse(t_demote.is_alive(), 'demote thread hung (lock regressed)')

            self.assertIn(results.get('assign'), ('ok', 'rejected'), results)
            self.assertIn(results.get('demote'), ('ok', 'rejected'), results)
            # exactly one operation succeeds
            wins = [k for k, v in results.items() if v == 'ok']
            self.assertEqual(len(wins), 1, f'expected exactly one winner: {results}')
            self._assert_consistent()
