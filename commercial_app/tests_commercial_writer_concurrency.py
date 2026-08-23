"""
Real concurrency proofs for the commercial writers (Phase 1, Step 3C).

REQUIRES POSTGRESQL and is skipped elsewhere: ``select_for_update`` is a no-op on
SQLite, so on that backend every race below would "pass" by not actually racing —
the same reason every other concurrency suite in this repository guards itself.

WHAT THESE PROVE, and it is deliberately not "who wins": the winner of a genuine race
is not a property worth pinning. What must hold is that exactly ONE decision commits,
the loser gets a clean DOMAIN result (a no-op or a named conflict), no update is lost,
and no ``IntegrityError`` escapes to the caller. The partial unique index on open
terms remains the final database backstop behind all of it — but ordinary service
races must resolve above it, as domain outcomes.
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from commercial_app import errors, service_configuration, subscription_terms
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from restaurants_app.models import Restaurant
from users_app.models import User


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
        assert not thread.is_alive(), f'worker {index} hung (locking regressed)'


def _sync(barrier, times=1):
    """Pass ``times`` barrier phases, tolerating a barrier already broken by a peer."""
    if barrier is None:
        return
    for _ in range(times):
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:      # pragma: no cover - defensive
            return


# --- workers -----------------------------------------------------------------
# Each owns its own connection and closes it, per the house convention.

def _timing_worker(ids, results, key, value, expected, barrier=None):
    try:
        actor = User.objects.get(pk=ids['actor'])
        _sync(barrier)
        results[key] = service_configuration.set_payment_timing(
            restaurant_id=ids['restaurant'], value=value,
            actor=actor, expected_current=expected,
        )
    except Exception as exc:                       # noqa: BLE001 - recorded, asserted
        results[key] = exc
    finally:
        connection.close()


def _mode_worker(ids, results, key, value, expected, barrier=None):
    try:
        actor = User.objects.get(pk=ids['actor'])
        _sync(barrier)
        results[key] = service_configuration.set_payment_collection_mode(
            restaurant_id=ids['restaurant'], value=value,
            actor=actor, expected_current=expected,
        )
    except Exception as exc:                       # noqa: BLE001
        results[key] = exc
    finally:
        connection.close()


def _record_worker(ids, results, key, amount, effective, barrier=None):
    try:
        actor = User.objects.get(pk=ids['actor'])
        _sync(barrier)
        results[key] = subscription_terms.record_subscription_terms(
            restaurant_id=ids['restaurant'], recurring_amount=amount,
            currency='UGX', billing_interval_unit='month',
            billing_interval_count=1, effective_from=effective, actor=actor,
        )
    except Exception as exc:                       # noqa: BLE001
        results[key] = exc
    finally:
        connection.close()


def _replace_worker(ids, results, key, expected_id, amount, effective, barrier=None):
    try:
        actor = User.objects.get(pk=ids['actor'])
        _sync(barrier)
        results[key] = subscription_terms.replace_subscription_terms(
            restaurant_id=ids['restaurant'], expected_terms_id=expected_id,
            recurring_amount=amount, currency='UGX',
            billing_interval_unit='month', billing_interval_count=1,
            effective_from=effective, actor=actor,
        )
    except Exception as exc:                       # noqa: BLE001
        results[key] = exc
    finally:
        connection.close()


def _restaurant_lock_holder(ids, events, hold_seconds, barrier=None):
    """Hold the Restaurant row lock, then release it, recording when."""
    try:
        with transaction.atomic():
            Restaurant.objects.select_for_update().get(pk=ids['restaurant'])
            _sync(barrier)                 # the racing writer starts now
            time.sleep(hold_seconds)
            events.append('lock_released')
    except Exception as exc:                       # noqa: BLE001
        events.append(exc)
    finally:
        connection.close()


class CommercialWriterConcurrencyTests(TransactionTestCase):

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking proofs require PostgreSQL')

        self.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='cwc-owner@test.com',
            phone_number='256774000101', username='256774000101',
            country='Uganda', password='password', roles=[],
        )
        self.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='cwc-actor@test.com',
            phone_number='256774000102', username='256774000102',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Concurrent Ltd', location='loc-cwc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.ids = {'restaurant': self.restaurant.id, 'actor': self.actor.id}
        self.effective = timezone.now() - timedelta(days=30)

    def _outcomes(self, results):
        """Split recorded worker outcomes into successes and domain errors."""
        values = list(results.values())
        for value in values:
            self.assertNotIsInstance(
                value, (type(None),),
                'a worker recorded nothing — it never reached the service',
            )
        errs = [v for v in values if isinstance(v, Exception)]
        oks = [v for v in values if not isinstance(v, Exception)]
        for err in errs:
            # An ordinary race must surface as a DOMAIN refusal, never as a leaked
            # IntegrityError or a raw database exception.
            self.assertIsInstance(err, CommercialMutationError, repr(err))
        return oks, errs

    # =====================================================================
    # SERVICE CONFIGURATION
    # =====================================================================

    def test_two_identical_first_timing_writes_yield_one_change_and_one_noop(self):
        results = {}
        _run([
            (_timing_worker, (self.ids, results, 'a', 'pay_first', None)),
            (_timing_worker, (self.ids, results, 'b', 'pay_first', None)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(errs, [])
        self.assertEqual(len(oks), 2)
        self.assertEqual(sorted(r.changed for r in oks), [False, True])
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)
        config = RestaurantServiceConfiguration.objects.get()
        self.assertEqual(config.payment_timing, 'pay_first')

    def test_two_conflicting_first_timing_writes_yield_one_win_one_conflict(self):
        results = {}
        _run([
            (_timing_worker, (self.ids, results, 'a', 'pay_first', None)),
            (_timing_worker, (self.ids, results, 'b', 'pay_after', None)),
        ])
        oks, errs = self._outcomes(results)

        # Exactly one committed decision, one clean conflict, no lost update.
        self.assertEqual(len(oks), 1)
        self.assertTrue(oks[0].changed)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].code, errors.STALE_SERVICE_CONFIGURATION)

        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)
        config = RestaurantServiceConfiguration.objects.get()
        self.assertEqual(config.payment_timing, oks[0].current_value)

    def test_two_conflicting_first_collection_writes_yield_one_win_one_conflict(self):
        results = {}
        _run([
            (_mode_worker, (self.ids, results, 'a', 'offline', None)),
            (_mode_worker, (self.ids, results, 'b', 'psp_online', None)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].code, errors.STALE_SERVICE_CONFIGURATION)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)
        self.assertEqual(
            RestaurantServiceConfiguration.objects.get().payment_collection_mode,
            oks[0].current_value,
        )

    def test_two_identical_first_collection_writes_yield_one_change_and_one_noop(self):
        results = {}
        _run([
            (_mode_worker, (self.ids, results, 'a', 'offline', None)),
            (_mode_worker, (self.ids, results, 'b', 'offline', None)),
        ])
        oks, errs = self._outcomes(results)
        self.assertEqual(errs, [])
        self.assertEqual(sorted(r.changed for r in oks), [False, True])
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)

    def test_the_two_axes_serialize_cleanly_and_neither_clobbers_the_other(self):
        """
        Both facts must survive a simultaneous write. Because the two operations
        share the Restaurant lock they queue, and because each writes only its own
        triple the second does not carry the first's columns backwards.
        """
        results = {}
        _run([
            (_timing_worker, (self.ids, results, 'timing', 'pay_after', None)),
            (_mode_worker, (self.ids, results, 'mode', 'psp_online', None)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(errs, [])
        self.assertEqual(len(oks), 2)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)
        config = RestaurantServiceConfiguration.objects.get()
        self.assertEqual(config.payment_timing, 'pay_after')
        self.assertEqual(config.payment_collection_mode, 'psp_online')
        self.assertIsNotNone(config.payment_timing_set_at)
        self.assertIsNotNone(config.payment_collection_mode_set_at)

    def test_a_concurrent_noop_does_not_rewrite_the_winners_attribution(self):
        results = {}
        _run([
            (_timing_worker, (self.ids, results, 'a', 'pay_first', None)),
            (_timing_worker, (self.ids, results, 'b', 'pay_first', None)),
        ])
        config = RestaurantServiceConfiguration.objects.get()
        winner = [r for r in results.values() if r.changed][0]
        self.assertEqual(
            config.payment_timing_set_at, winner.configuration.payment_timing_set_at,
        )

    # =====================================================================
    # SUBSCRIPTION TERMS
    # =====================================================================

    def test_two_identical_first_recordings_produce_exactly_one_row(self):
        results = {}
        _run([
            (_record_worker,
             (self.ids, results, 'a', Decimal('250000.00'), self.effective)),
            (_record_worker,
             (self.ids, results, 'b', Decimal('250000.00'), self.effective)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(errs, [])
        self.assertEqual(sorted(r.changed for r in oks), [False, True])
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)
        # Both callers were handed the same canonical row.
        self.assertEqual(len({r.terms.pk for r in oks}), 1)

    def test_two_different_first_recordings_yield_one_win_and_one_conflict(self):
        results = {}
        _run([
            (_record_worker,
             (self.ids, results, 'a', Decimal('250000.00'), self.effective)),
            (_record_worker,
             (self.ids, results, 'b', Decimal('900000.00'), self.effective)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].code, errors.SUBSCRIPTION_TERMS_ALREADY_OPEN)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.get().recurring_amount,
            oks[0].terms.recurring_amount,
        )

    def test_two_identical_replacements_replace_once(self):
        """
        A double-submitted replacement. The loser reads under the lock, finds the
        completed replacement it can prove is its own, and returns a no-op — one
        replacement, not two.
        """
        first = subscription_terms.record_subscription_terms(
            restaurant_id=self.restaurant.id, recurring_amount=Decimal('250000.00'),
            currency='UGX', billing_interval_unit='month', billing_interval_count=1,
            effective_from=self.effective, actor=self.actor,
        )
        boundary = self.effective + timedelta(days=10)
        results = {}
        _run([
            (_replace_worker,
             (self.ids, results, 'a', first.terms.pk, Decimal('300000.00'), boundary)),
            (_replace_worker,
             (self.ids, results, 'b', first.terms.pk, Decimal('300000.00'), boundary)),
        ])
        oks, errs = self._outcomes(results)

        self.assertEqual(errs, [])
        self.assertEqual(sorted(r.changed for r in oks), [False, True])
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 2)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(
                ended_at__isnull=True).count(),
            1,
        )

    def test_two_different_replacements_of_the_same_terms_do_not_both_apply(self):
        first = subscription_terms.record_subscription_terms(
            restaurant_id=self.restaurant.id, recurring_amount=Decimal('250000.00'),
            currency='UGX', billing_interval_unit='month', billing_interval_count=1,
            effective_from=self.effective, actor=self.actor,
        )
        boundary = self.effective + timedelta(days=10)
        results = {}
        _run([
            (_replace_worker,
             (self.ids, results, 'a', first.terms.pk, Decimal('300000.00'), boundary)),
            (_replace_worker,
             (self.ids, results, 'b', first.terms.pk, Decimal('400000.00'), boundary)),
        ])
        oks, errs = self._outcomes(results)

        # The loser must NOT silently replace the winner's terms.
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].code, errors.STALE_SUBSCRIPTION_TERMS)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 2)

        open_rows = RestaurantSubscriptionTerms.objects.filter(ended_at__isnull=True)
        self.assertEqual(open_rows.count(), 1)
        self.assertEqual(open_rows.get().pk, oks[0].terms.pk)

    # =====================================================================
    # THE SERIALIZATION POINT ITSELF
    # =====================================================================

    def test_a_held_restaurant_lock_blocks_every_commercial_mutation(self):
        """
        THE INVARIANT THIS WHOLE DESIGN RESTS ON, proved directly rather than by
        asserting that ``select_for_update`` was called.

        A holder takes the ``Restaurant`` row — exactly what a lifecycle go-live or a
        future owner-approval snapshot does — and a commercial write races it. The
        write must not complete until the holder commits, which is what stops a
        readiness evaluation or an approval from being taken against a moving target.
        """
        events = []
        results = {}

        def _writer(ids, results, key, barrier=None):
            try:
                actor = User.objects.get(pk=ids['actor'])
                _sync(barrier)
                results[key] = service_configuration.set_payment_timing(
                    restaurant_id=ids['restaurant'], value='pay_first',
                    actor=actor, expected_current=None,
                )
                events.append('write_committed')
            except Exception as exc:               # noqa: BLE001
                results[key] = exc
            finally:
                connection.close()

        _run([
            (_restaurant_lock_holder, (self.ids, events, 0.75)),
            (_writer, (self.ids, results, 'w')),
        ])

        self.assertNotIsInstance(results['w'], Exception, repr(results.get('w')))
        self.assertTrue(results['w'].changed)
        self.assertEqual(
            events, ['lock_released', 'write_committed'],
            'the commercial write did not wait for the Restaurant row lock',
        )

    def test_the_lock_primitive_refuses_to_run_outside_a_transaction(self):
        """
        A ``select_for_update`` in autocommit serializes nothing, and would do so
        silently — every test above would still pass. The primitive asserts instead.

        Lives in this suite because ``TestCase`` wraps each test in a transaction,
        where the guard could never fire; ``TransactionTestCase`` runs in autocommit.
        """
        from commercial_app.mutation_context import lock_restaurant
        with self.assertRaises(RuntimeError):
            lock_restaurant(self.restaurant.id)
