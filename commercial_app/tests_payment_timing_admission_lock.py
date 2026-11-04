"""
The payment-timing writer takes the EXCLUSIVE admission lock first (Step 2, PR BE-B).

From Step 2 the timing copied onto a visit at acceptance decides whether a round is
held, so a timing change must not commit in the middle of an acceptance. The
acceptance reads the timing after ``order_admission.admit`` has taken the SHARED
admission lock (``round_placement._cutoff_and_timing``); the writer takes the
EXCLUSIVE side before the ``Restaurant`` row, the lifecycle transition's own order.

Two layers of proof:

- ``LockOrderTests`` (any backend) pins WHICH locks each axis takes and in WHAT
  ORDER, by wrapping the real primitives; and that ``pay_first`` is refused before
  any lock or query.
- ``TimingWriteAgainstSubmitTests`` (PostgreSQL only, real connections) parks one
  side INSIDE its transaction at a real seam and waits until PostgreSQL reports the
  other side blocked on an ADVISORY lock (``pg_locks``). Nothing is timed. The
  visit the submit writes then shows which side won: the old timing when the submit
  held the lock first, the new one when the write did.

DETERMINISTIC, NOT TIMED, and the wait observed is specifically an advisory one:
without the admission lock a submit still waits at COMMIT on the ``Restaurant`` row
(its deferred foreign key takes ``FOR KEY SHARE``), which is a row wait and would
not prove the barrier.
"""
import threading
import time
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from commercial_app import errors, service_configuration
from commercial_app.errors import CommercialMutationError
from commercial_app.models import RestaurantServiceConfiguration
from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers import manage_order
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, Visit
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


def _user(email, phone):
    return User.objects.create_user(
        first_name='T', last_name='U', email=email, phone_number=phone,
        username=phone, country='Uganda', password='password', roles=[],
    )


def _seed_timing(restaurant, value, actor):
    """A stored timing, written directly (the writer refuses `pay_first`)."""
    return RestaurantServiceConfiguration.objects.create(
        restaurant=restaurant, payment_timing=value,
        payment_timing_set_at=timezone.now(), payment_timing_set_by=actor,
    )


class LockOrderTests(TestCase):

    def setUp(self):
        self.actor = _user('plo-actor@test.com', '256775100001')
        self.restaurant = Restaurant.objects.create(
            name='Lock Order', location='loc', status=RestaurantStatus_Live,
            owner=_user('plo-owner@test.com', '256775100002'),
        )
        self.calls = []
        real_admission = service_configuration.lock_admission_exclusive
        real_restaurant = service_configuration.lock_restaurant

        def admission(restaurant_id):
            self.calls.append(('admission_exclusive', restaurant_id))
            return real_admission(restaurant_id)

        def restaurant(restaurant_id):
            self.calls.append(('restaurant_row', restaurant_id))
            return real_restaurant(restaurant_id)

        patches = [
            mock.patch.object(service_configuration, 'lock_admission_exclusive',
                              admission),
            mock.patch.object(service_configuration, 'lock_restaurant', restaurant),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _timing(self, value, expected=None, restaurant_id=None):
        return service_configuration.set_payment_timing(
            restaurant_id=restaurant_id or self.restaurant.id, value=value,
            actor=self.actor, expected_current=expected,
        )

    def test_a_timing_write_takes_the_admission_lock_before_the_restaurant_row(self):
        self._timing('pay_after')
        self.assertEqual(
            [name for name, _ in self.calls],
            ['admission_exclusive', 'restaurant_row'],
        )
        # The advisory key is derived from the restaurant's UUID, parsed first.
        self.assertEqual(self.calls[0][1], self.restaurant.id)

    def test_a_string_restaurant_id_is_parsed_before_the_lock(self):
        self._timing('pay_after', restaurant_id=str(self.restaurant.id))
        self.assertEqual(self.calls[0], ('admission_exclusive', self.restaurant.id))

    def test_a_no_op_retry_still_takes_both_locks_in_order(self):
        self._timing('pay_after')
        self.calls.clear()
        result = self._timing('pay_after', expected='pay_after')
        self.assertFalse(result.changed)
        self.assertEqual(
            [name for name, _ in self.calls],
            ['admission_exclusive', 'restaurant_row'],
        )

    def test_the_collection_mode_axis_takes_no_admission_lock(self):
        service_configuration.set_payment_collection_mode(
            restaurant_id=self.restaurant.id, value='offline',
            actor=self.actor, expected_current=None,
        )
        self.assertEqual([name for name, _ in self.calls], ['restaurant_row'])

    def test_a_malformed_restaurant_id_is_refused_before_any_lock(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._timing('pay_after', restaurant_id='not a uuid')
        self.assertEqual(ctx.exception.code, errors.INVALID_RESTAURANT_ID)
        self.assertEqual(self.calls, [])

    def test_pay_first_is_refused_before_any_lock_and_any_query(self):
        with self.assertNumQueries(0):
            with self.assertRaises(CommercialMutationError) as ctx:
                self._timing('pay_first')
        self.assertEqual(
            ctx.exception.code,
            service_configuration.PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE,
        )
        self.assertEqual(self.calls, [])


class TimingWriteAgainstSubmitTests(TransactionTestCase):
    """A submit and a timing write, on two real connections, never interleave."""

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('needs PostgreSQL advisory locks')
        owner = _user('ptw-owner@test.com', '256775200001')
        self.actor = _user('ptw-actor@test.com', '256775200002')
        self.restaurant = Restaurant.objects.create(
            name='Timing Race', location='Kampala', owner=owner,
            status=RestaurantStatus_Live)
        RestaurantEmployee.objects.create(
            user=owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True)
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=area, enabled=True,
            is_active=True, qr_mode='order_only', has_qr=True)
        section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, approved=True, enabled=True,
            available=True)
        item = MenuItem.objects.create(
            name='Rolex', section=section, primary_price=Decimal('5000'),
            approved=True, enabled=True, available=True, in_stock=True)
        result = _create_order(restaurant=self.restaurant, table=table,
                               items=[{'item': str(item.pk), 'quantity': 1}])
        self.assertEqual(result['status'], 200, result)
        self.order_id = result['order'].pk
        self.ref = quote_ref(result['order'])
        # Stored before BE-B's refusal: the write under test moves it to pay_after,
        # so the visit the submit writes says which side went first.
        _seed_timing(self.restaurant, 'pay_first', self.actor)

    # --- the two sides, each on its own connection ---

    def _submit(self, results):
        try:
            order = Order.objects.get(pk=self.order_id)
            results['submit'] = manage_order.update_order_status(
                order, OrderStatus_Pending, None, quote_ref=self.ref)
        except Exception as exc:                     # pragma: no cover - reported
            results['submit'] = exc
        finally:
            connection.close()

    def _write(self, results):
        try:
            actor = User.objects.get(pk=self.actor.pk)
            results['write'] = service_configuration.set_payment_timing(
                restaurant_id=self.restaurant.id, value='pay_after',
                actor=actor, expected_current='pay_first',
            )
        except Exception as exc:                     # pragma: no cover - reported
            results['write'] = exc
        finally:
            connection.close()

    # --- observation, from this (third) connection ---

    def _await_advisory_wait(self, results, key):
        """Until PostgreSQL reports a backend waiting on an ADVISORY lock."""
        deadline = time.monotonic() + 10
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted"
                )
                if cursor.fetchone()[0]:
                    return
            self.assertNotIn(
                key, results,
                f'the {key} finished instead of waiting for the admission lock',
            )
            self.assertLess(time.monotonic(), deadline,
                            f'the {key} never queued on the admission lock')
            time.sleep(0.02)

    def _join(self, *threads):
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads), 'a side hung')

    def _visit_timing(self):
        return Visit.objects.get().payment_timing

    def test_a_submit_holding_the_shared_lock_makes_a_timing_write_wait(self):
        read, release = threading.Event(), threading.Event()
        real = round_placement._cutoff_and_timing

        def parked(restaurant_id):
            # The submit has been admitted (SHARED lock held) and has read the
            # timing it will stamp on the visit. Park here, inside its transaction.
            value = real(restaurant_id)
            read.set()
            release.wait(timeout=15)
            return value

        results = {}
        with mock.patch.object(round_placement, '_cutoff_and_timing', parked):
            submit = threading.Thread(target=self._submit, args=(results,))
            submit.start()
            self.assertTrue(read.wait(timeout=15), 'the submit never read the timing')
            write = threading.Thread(target=self._write, args=(results,))
            write.start()
            self._await_advisory_wait(results, 'write')
            self.assertNotIn('write', results)
            release.set()
            self._join(submit, write)

        self.assertEqual(results['submit']['status'], 200, results)
        self.assertNotIsInstance(results['write'], Exception, repr(results['write']))
        self.assertTrue(results['write'].changed)
        # The submit committed first, with the timing it read: the old value.
        self.assertEqual(self._visit_timing(), 'pay_first')
        self.assertEqual(
            RestaurantServiceConfiguration.objects.get().payment_timing, 'pay_after',
        )

    def test_a_timing_write_holding_the_exclusive_lock_makes_a_submit_wait(self):
        locked, release = threading.Event(), threading.Event()
        real = service_configuration.lock_restaurant

        def parked(restaurant_id):
            # The write holds the EXCLUSIVE admission lock and now the Restaurant
            # row too. Park here, inside its transaction, before it writes.
            restaurant = real(restaurant_id)
            locked.set()
            release.wait(timeout=15)
            return restaurant

        results = {}
        with mock.patch.object(service_configuration, 'lock_restaurant', parked):
            write = threading.Thread(target=self._write, args=(results,))
            write.start()
            self.assertTrue(locked.wait(timeout=15), 'the write never took its locks')
            submit = threading.Thread(target=self._submit, args=(results,))
            submit.start()
            self._await_advisory_wait(results, 'submit')
            self.assertNotIn('submit', results)
            release.set()
            self._join(write, submit)

        self.assertNotIsInstance(results['write'], Exception, repr(results['write']))
        self.assertTrue(results['write'].changed)
        self.assertEqual(results['submit']['status'], 200, results)
        # The submit was admitted only after the write committed: the new value.
        self.assertEqual(self._visit_timing(), 'pay_after')
