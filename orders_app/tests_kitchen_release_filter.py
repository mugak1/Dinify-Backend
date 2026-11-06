"""
Step 2, PR BE-C — the kitchen release filter.

THE RULE. A round reaches the kitchen only once it is RELEASED: it has NO placement
(it predates Step 1, or was accepted with ``DINING_VISIT_WRITES_ENABLED`` off) or its
placement's ``released_at`` is set. A HELD round is ``order_status='pending'`` with a
placement whose ``released_at`` is NULL. ``round_placement.released_round_q`` writes
the rule once; both kitchen feeds, the kitchen command boundary and the dashboard's
kitchen card all ask through it.

NOTHING WRITES A HELD ROUND YET (``place_first_round`` stamps the acceptance instant),
so every held round below is FABRICATED: a placement written directly with
``released_at`` NULL. On data shaped like today's — every placement released, or none
at all — the existing kitchen and dashboard suites pass unchanged, which is the
"no-op on today's data" half of this change.

What is pinned here:

* a held round is on neither feed, and every kitchen command refuses it with
  ``order_held`` having written nothing;
* an order with no placement, and a released round, appear as they always did, with
  an additive ``released_at`` (null without a placement);
* a command formed against a held round that is released while it waits is refused
  ``order_released_while_waiting`` — in process, and on two real connections;
* the dashboard's kitchen card leaves held rounds out and ages a ticket from its
  release, falling back to its creation;
* none of it costs a query.
"""
import threading
import time
from datetime import timedelta
from unittest import mock

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind,
    OrderStatus_Cancelled,
    OrderStatus_Pending,
    OrderStatus_Served,
)
from orders_app.controllers.services import kitchen_transition
from orders_app.controllers.services.round_placement import (
    is_held,
    released_at_of,
    released_round_q,
    round_is_held,
)
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN, Bill, Order, OrderPlacement, Visit,
)
from orders_app.tests_kitchen import (
    ACTIVE_URL,
    COMPLETED_URL,
    KitchenTestBase,
    _cancel_url,
    _fulfilment_url,
    _priority_url,
)
from orders_app.tests_kitchen_concurrency import (
    WAIT_TIMEOUT,
    KitchenRaceBase,
)
from reports_app.controllers.restaurant.dashboard import _build_kds
from restaurants_app.models import Table

HELD = 'order_held'
RELEASED_WHILE_WAITING = 'order_released_while_waiting'

#: The ticket keys the kitchen feeds carried before this change, plus the one key
#: it adds. Spelled out so a test can say "exactly as before, plus `released_at`".
TICKET_KEYS_BEFORE = {
    'id', 'order_number', 'table_label', 'order_source', 'order_status',
    'fulfilment_revision', 'fulfilment_status', 'priority', 'created_at',
    'served_at', 'items',
}


def _state_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/state/'


def _place(order, released_at):
    """A visit, its bill and the order's placement, written directly.

    ``released_at=None`` fabricates a HELD round, which nothing in production can
    write yet. Any other value is a released round, as ``place_first_round`` writes.
    """
    now = timezone.now()
    visit = Visit.objects.create(
        restaurant_id=order.restaurant_id, table_id=order.table_id, opened_at=now,
        payment_timing=PAYMENT_TIMING_NOT_CHOSEN,
    )
    bill = Bill.objects.create(visit=visit)
    return OrderPlacement.objects.create(
        order=order, visit=visit, bill=bill,
        payment_timing=PAYMENT_TIMING_NOT_CHOSEN, released_at=released_at,
        business_date=timezone.localdate(now), round_number=1,
    )


def _parse(stamp):
    """A wire timestamp back to an aware datetime (DRF renders UTC with ``Z``)."""
    from datetime import datetime
    return datetime.fromisoformat(stamp.replace('Z', '+00:00'))


def _row(order):
    """Every column a kitchen command can write, to prove a refusal wrote nothing."""
    return Order.objects.values(
        'order_status', 'fulfilment_status', 'priority', 'served_at',
        'cancelled_at', 'cancelled_by', 'cancellation_reason',
        'fulfilment_revision', 'fulfilment_status_updated_at',
        'fulfilment_status_updated_by', 'time_last_updated',
    ).get(pk=order.pk)


# --- the rule -----------------------------------------------------------------------

class TheReleaseRuleTests(KitchenTestBase):
    """``released_round_q`` and its Python twin agree on every shape a round has."""

    def setUp(self):
        super().setUp()
        self.unplaced = self._make_order(table=self.table1)
        self.held = self._make_order(table=self.table2)
        _place(self.held, None)
        self.released = self._make_order(table=self.table3)
        self.released_at = timezone.now() - timedelta(minutes=2)
        _place(self.released, self.released_at)

    def _loaded(self, order):
        return Order.objects.select_related('placement').get(pk=order.pk)

    def test_the_filter_keeps_unplaced_and_released_rounds_only(self):
        self.assertEqual(
            set(Order.objects.filter(released_round_q()).values_list('pk', flat=True)),
            {self.unplaced.pk, self.released.pk},
        )

    def test_the_python_twin_agrees_with_the_filter_on_every_shape(self):
        released = set(
            Order.objects.filter(released_round_q()).values_list('pk', flat=True))
        for order in (self.unplaced, self.held, self.released):
            with self.subTest(order=order.pk):
                self.assertEqual(is_held(self._loaded(order)),
                                 order.pk not in released)

    def test_the_held_rule_truth_table(self):
        now = timezone.now()
        self.assertFalse(round_is_held(False, None))   # no placement: never held
        self.assertTrue(round_is_held(True, None))     # placement, no release
        self.assertFalse(round_is_held(True, now))     # released

    def test_released_at_of_is_the_placements_value_or_none(self):
        self.assertIsNone(released_at_of(self._loaded(self.unplaced)))
        self.assertIsNone(released_at_of(self._loaded(self.held)))
        self.assertEqual(released_at_of(self._loaded(self.released)),
                         self.released_at)

    def test_reading_a_selected_placement_or_its_absence_costs_no_query(self):
        unplaced = self._loaded(self.unplaced)
        released = self._loaded(self.released)
        with self.assertNumQueries(0):
            is_held(unplaced)
            released_at_of(unplaced)
            is_held(released)
            released_at_of(released)


# --- the feeds ----------------------------------------------------------------------

class TheFeedsShowReleasedRoundsOnlyTests(KitchenTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)
        self.rid = str(self.restaurant.id)

    def _active(self):
        response = self.client.get(ACTIVE_URL, {'restaurant': self.rid})
        self.assertEqual(response.status_code, 200, response.content)
        return {t['id']: t for t in response.json()['data']}

    def _completed(self):
        response = self.client.get(COMPLETED_URL, {'restaurant': self.rid})
        self.assertEqual(response.status_code, 200, response.content)
        return {t['id']: t for t in response.json()['data']}

    def _served(self, table):
        return self._make_order(
            table=table, fulfilment_status='served',
            order_status=OrderStatus_Served, served_at=timezone.now())

    def test_a_held_round_is_not_on_the_active_board(self):
        held = self._make_order(table=self.table1)
        _place(held, None)
        visible = self._make_order(table=self.table2)
        board = self._active()
        self.assertNotIn(str(held.pk), board)
        self.assertIn(str(visible.pk), board)

    def test_a_held_round_is_not_on_the_completed_feed(self):
        """A held round cannot be served (the boundary refuses it), so this one is
        fabricated served to prove the completed feed applies the rule too."""
        held = self._served(self.table1)
        _place(held, None)
        visible = self._served(self.table2)
        feed = self._completed()
        self.assertNotIn(str(held.pk), feed)
        self.assertIn(str(visible.pk), feed)

    def test_releasing_a_held_round_puts_it_on_the_board(self):
        held = self._make_order(table=self.table1)
        placement = _place(held, None)
        self.assertNotIn(str(held.pk), self._active())
        placement.released_at = timezone.now()
        placement.save(update_fields=['released_at'])
        self.assertIn(str(held.pk), self._active())

    def test_an_unplaced_order_appears_as_before_with_a_null_release(self):
        order = self._make_order(table=self.table1)
        ticket = self._active()[str(order.pk)]
        self.assertEqual(set(ticket), TICKET_KEYS_BEFORE | {'released_at'})
        self.assertIsNone(ticket['released_at'])

        served = self._served(self.table2)
        ticket = self._completed()[str(served.pk)]
        self.assertEqual(set(ticket), TICKET_KEYS_BEFORE | {'released_at'})
        self.assertIsNone(ticket['released_at'])

    def test_a_released_round_appears_as_before_with_its_release_instant(self):
        """The placement changes the ticket in exactly one key: everything else is
        what the same order showed with no placement at all."""
        order = self._make_order(table=self.table1)
        before = self._active()[str(order.pk)]
        released_at = timezone.now() - timedelta(minutes=3)
        _place(order, released_at)
        after = self._active()[str(order.pk)]
        self.assertEqual(_parse(after.pop('released_at')), released_at)
        self.assertIsNone(before.pop('released_at'))
        self.assertEqual(after, before)

        served = self._served(self.table2)
        _place(served, released_at)
        ticket = self._completed()[str(served.pk)]
        self.assertEqual(_parse(ticket['released_at']), released_at)

    def test_released_at_shares_the_ticket_timestamp_format(self):
        order = self._make_order(table=self.table1)
        _place(order, order.time_created)
        ticket = self._active()[str(order.pk)]
        # The same instant renders to the same string as `created_at` does.
        self.assertEqual(ticket['released_at'], ticket['created_at'])

    def test_the_active_feed_costs_five_queries_and_stays_flat_with_placements(self):
        """5 = BE-C's 4 plus ONE deliberate COUNT for the envelope's
        ``waiting_for_staff`` (Step 2, BE-G): held rounds are not on the board, so
        they cannot be counted from the tickets. Flat in tickets and placements."""
        for table, released in ((self.table1, timezone.now()), (self.table2, None)):
            order = self._make_order(table=table)
            _place(order, released)
        with self.assertNumQueries(5):
            self.client.get(ACTIVE_URL, {'restaurant': self.rid})
        for table, released in ((self.table3, timezone.now()), (self.table4, None)):
            order = self._make_order(table=table)
            _place(order, released)
        self._make_order(table=self.table4)              # one with no placement
        with self.assertNumQueries(5):
            self.client.get(ACTIVE_URL, {'restaurant': self.rid})

    def test_the_completed_feed_costs_the_same_with_placements(self):
        """`WhatACommandCostsTests` pins the EMPTY feed at 3; a feed with tickets
        adds its one item prefetch, so 4 — with or without placements."""
        for table in (self.table1, self.table2):
            self._served(table)
        with self.assertNumQueries(4):
            self.client.get(COMPLETED_URL, {'restaurant': self.rid})
        for order in Order.objects.all():
            _place(order, timezone.now())
        self._served(self.table3)                        # one with no placement
        with self.assertNumQueries(4):
            self.client.get(COMPLETED_URL, {'restaurant': self.rid})


# --- the command boundary -----------------------------------------------------------

class EveryCommandRefusesAHeldRoundTests(KitchenTestBase):
    """``_assert_operable`` refuses a held round, ``order_held``, writing nothing."""

    #: (command, fixture overrides, request) — one per kitchen command.
    COMMANDS = (
        ('advance', {'fulfilment_status': 'new'},
         lambda pk: (_fulfilment_url(pk), {'action': 'advance'})),
        ('serve', {'fulfilment_status': 'ready'},
         lambda pk: (_fulfilment_url(pk), {'action': 'serve'})),
        ('correct', {'fulfilment_status': 'ready'},
         lambda pk: (_fulfilment_url(pk), {'action': 'correct'})),
        ('recall', {'fulfilment_status': 'served', 'order_status': OrderStatus_Served,
                    'served_at': None},
         lambda pk: (_fulfilment_url(pk), {'action': 'recall'})),
        ('cancel', {'fulfilment_status': 'new'},
         lambda pk: (_cancel_url(pk),
                     {'cancellation_reason': CancellationReason_CustomerChangedMind})),
        ('priority', {'fulfilment_status': 'new'},
         lambda pk: (_priority_url(pk), {'priority': True})),
    )

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager_user)

    def _held_round(self, overrides):
        overrides = dict(overrides)
        if 'served_at' in overrides:
            overrides['served_at'] = timezone.now()
        order = self._make_order(table=self.table1, **overrides)
        placement = _place(order, None)
        return order, placement

    def _send(self, order, request, if_revision=0):
        url, body = request(order.pk)
        return self.client.put(url, dict(body, if_revision=if_revision),
                               format='json')

    def test_every_command_refuses_a_held_round_and_writes_nothing(self):
        for name, overrides, request in self.COMMANDS:
            with self.subTest(command=name):
                Order.objects.all().delete()
                order, _placement = self._held_round(overrides)
                before = _row(order)
                response = self._send(order, request)
                self.assertEqual(response.status_code, 409, response.content)
                body = response.json()
                self.assertEqual(body['reason'], HELD)
                self.assertEqual(body['data']['id'], str(order.pk))
                self.assertEqual(_row(order), before)

    def test_held_is_decided_before_the_precondition(self):
        """Eligibility before staleness, as everywhere at this boundary."""
        order, _ = self._held_round({'fulfilment_status': 'new'})
        response = self._send(order, lambda pk: (_fulfilment_url(pk),
                                                 {'action': 'advance'}),
                              if_revision=99)
        self.assertEqual(response.json()['reason'], HELD)

    def test_a_cancelled_held_round_says_cancelled(self):
        """The more useful sentence for a held round that was cancelled."""
        order, _ = self._held_round({
            'fulfilment_status': 'new', 'order_status': OrderStatus_Cancelled,
            'cancellation_reason': CancellationReason_CustomerChangedMind,
            'cancelled_at': timezone.now(),
        })
        response = self._send(order, lambda pk: (_fulfilment_url(pk),
                                                 {'action': 'advance'}))
        self.assertEqual(response.json()['reason'], 'order_cancelled')

    def test_once_released_the_same_command_applies(self):
        order, placement = self._held_round({'fulfilment_status': 'new'})
        advance = lambda pk: (_fulfilment_url(pk), {'action': 'advance'})  # noqa: E731
        self.assertEqual(self._send(order, advance).json()['reason'], HELD)
        placement.released_at = timezone.now()
        placement.save(update_fields=['released_at'])
        response = self._send(order, advance)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Order.objects.get(pk=order.pk).fulfilment_status,
                         'preparing')

    def test_the_state_read_still_answers_for_a_held_round(self):
        order, _ = self._held_round({'fulfilment_status': 'new'})
        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['id'], str(order.pk))
        self.assertEqual(response.json()['data']['order_status'],
                         OrderStatus_Pending)

    def test_refusing_a_held_round_costs_what_any_refusal_costs(self):
        """10, the same figure `WhatACommandCostsTests` pins for a refusal."""
        self.client.force_authenticate(user=self.kitchen_user)
        order, _ = self._held_round({'fulfilment_status': 'new'})
        with self.assertNumQueries(10):
            self._send(order, lambda pk: (_fulfilment_url(pk), {'action': 'advance'}))

    def test_a_released_round_costs_what_an_unplaced_one_costs(self):
        """The placement rides the locator and the locked read: no query added."""
        self.client.force_authenticate(user=self.kitchen_user)
        order = self._make_order(table=self.table1, fulfilment_status='new')
        _place(order, timezone.now())
        with self.assertNumQueries(10):
            response = self._send(
                order, lambda pk: (_fulfilment_url(pk), {'action': 'advance'}))
        self.assertEqual(response.status_code, 200, response.content)


class ReleasedWhileWaitingTests(KitchenTestBase):
    """Held when the command LOCATED it, released by the time the lock is taken.

    Deterministic and in process: the release is committed from inside the
    pre-lock permission gate, which runs after the locator read and before the
    locks — exactly the window the boundary guards. The two-connection proof is
    ``ReleasedWhileWaitingRaceTests`` below.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager_user)

    def _released_during_the_gate(self, order, request):
        real_gate = kitchen_transition.can_user_access_module
        fired = []

        def gate(user, restaurant_id, module):
            if not fired:
                fired.append(True)
                OrderPlacement.objects.filter(order=order).update(
                    released_at=timezone.now())
            return real_gate(user, restaurant_id, module)

        with mock.patch.object(kitchen_transition, 'can_user_access_module',
                               side_effect=gate):
            url, body = request(order.pk)
            response = self.client.put(url, dict(body, if_revision=0),
                                       format='json')
        self.assertEqual(fired, [True], 'the gate never ran')
        return response

    def test_every_command_formed_against_a_held_round_is_refused(self):
        for name, overrides, request in EveryCommandRefusesAHeldRoundTests.COMMANDS:
            with self.subTest(command=name):
                Order.objects.all().delete()
                overrides = dict(overrides)
                if 'served_at' in overrides:
                    overrides['served_at'] = timezone.now()
                order = self._make_order(table=self.table1, **overrides)
                _place(order, None)
                before = _row(order)
                response = self._released_during_the_gate(order, request)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()['reason'], RELEASED_WHILE_WAITING)
                self.assertEqual(_row(order), before)
                # The release itself landed; only the command was refused.
                self.assertIsNotNone(
                    OrderPlacement.objects.get(order=order).released_at)

    def test_control_a_command_formed_after_the_release_applies(self):
        order = self._make_order(table=self.table1, fulfilment_status='new')
        _place(order, timezone.now())
        response = self.client.put(_fulfilment_url(order.pk),
                                   {'action': 'advance', 'if_revision': 0},
                                   format='json')
        self.assertEqual(response.status_code, 200, response.content)

    def test_control_an_unplaced_order_is_not_mistaken_for_a_held_one(self):
        """`released_at` reads None both for "no placement" and "held"; the locator
        tells them apart by the placement's id."""
        order = self._make_order(table=self.table1, fulfilment_status='new')
        response = self.client.put(_fulfilment_url(order.pk),
                                   {'action': 'advance', 'if_revision': 0},
                                   format='json')
        self.assertEqual(response.status_code, 200, response.content)


class ReleasedWhileWaitingRaceTests(KitchenRaceBase):
    """The release boundary on TWO REAL CONNECTIONS (PostgreSQL only).

    A releaser takes its locks and sits on them; the kitchen command locates the
    round while it is still held and then WAITS on a lock the releaser holds —
    observed in ``pg_stat_activity`` under a deadline, so the arrival order is
    fixed rather than hoped for — and only then does the releaser set
    ``released_at`` and commit.

    With the documented lock order, ``Table -> Order``, the command waits on the
    Table lock, its Order statement starts after the commit and reads the release:
    ``order_released_while_waiting``. That is why a release writer must take the
    Table lock first. The third test pins what happens when one does not.
    """

    APP_NAME = 'be-c-release-race'

    def _wait_until_kitchen_waits_on_a_lock(self, done):
        deadline = time.monotonic() + WAIT_TIMEOUT
        with connection.cursor() as monitor:
            while time.monotonic() < deadline:
                if done.is_set():
                    self.fail('the kitchen command finished without waiting')
                monitor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity '
                    'WHERE application_name = %s', [self.APP_NAME])
                row = monitor.fetchone()
                if row and row[0] == 'Lock':
                    return
                time.sleep(0.02)                   # observation, not ordering
        self.fail('the kitchen command never waited on a lock')

    def _race(self, release, lock_table=True):
        order = self._order(fulfilment_status='new')
        _place(order, None)
        releaser_parked = threading.Event()
        kitchen_waiting = threading.Event()
        kitchen_done = threading.Event()
        errors, results = [], {}

        def releaser():
            with transaction.atomic():
                if lock_table:
                    Table.objects.select_for_update().get(pk=self.table.pk)
                Order.objects.select_for_update().get(pk=order.pk)
                releaser_parked.set()
                kitchen_waiting.wait(timeout=WAIT_TIMEOUT)
                if release:
                    OrderPlacement.objects.filter(order=order).update(
                        released_at=timezone.now())
            return 'committed'

        def kitchen():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)",
                               [self.APP_NAME])
            try:
                return self._put(self.kitchen_user, f'/api/v1/kitchen/orders/'
                                 f'{order.pk}/fulfilment-status/',
                                 {'action': 'advance', 'if_revision': 0})
            finally:
                kitchen_done.set()

        t1 = self._spawn(releaser, results, 'releaser', errors)
        self.assertTrue(releaser_parked.wait(timeout=WAIT_TIMEOUT),
                        'the releaser never took its locks')
        t2 = self._spawn(kitchen, results, 'kitchen', errors)
        try:
            # The command located the round before reaching for a lock, so by now
            # it has read it HELD.
            self._wait_until_kitchen_waits_on_a_lock(kitchen_done)
        finally:
            kitchen_waiting.set()
        self._join([t1, t2], errors)
        return order, results

    def test_a_release_committed_while_the_command_waited_refuses_it(self):
        order, results = self._race(release=True)
        self.assertEqual(results['releaser'], 'committed')
        response = results['kitchen']
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['reason'], RELEASED_WHILE_WAITING)
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.fulfilment_status, 'new')
        self.assertEqual(row.fulfilment_revision, 0)

    def test_control_a_round_still_held_after_the_wait_is_refused_as_held(self):
        order, results = self._race(release=False)
        response = results['kitchen']
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['reason'], HELD)
        self.assertEqual(Order.objects.get(pk=order.pk).fulfilment_revision, 0)

    def test_a_release_that_skips_the_table_lock_is_read_stale_but_refused(self):
        """THE STATED LIMIT, measured. A releaser holding only the Order row commits
        while the command's Order statement is ALREADY waiting on that row. Under
        READ COMMITTED that statement reads the joined placement from its own
        snapshot, taken before the commit, so it still sees the round held and
        answers ``order_held``: stale, but a REFUSAL, never an applied command.
        Pinned so the comment on the locked read stays true; the remedy is the
        Table lock, not this outcome."""
        order, results = self._race(release=True, lock_table=False)
        response = results['kitchen']
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['reason'], HELD)
        self.assertIsNotNone(OrderPlacement.objects.get(order=order).released_at)
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.fulfilment_status, 'new')
        self.assertEqual(row.fulfilment_revision, 0)


# --- the dashboard's kitchen card ---------------------------------------------------

class TheKitchenCardTests(KitchenTestBase):
    """``_build_kds`` counts released rounds only and ages each from its release."""

    def _aged(self, table, created_minutes_ago, **overrides):
        order = self._make_order(table=table, **overrides)
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() - timedelta(minutes=created_minutes_ago))
        return Order.objects.get(pk=order.pk)

    def test_a_held_round_is_not_an_open_ticket(self):
        held = self._aged(self.table1, 60)
        _place(held, None)
        self._aged(self.table2, 1)
        card = _build_kds(self.restaurant.id)
        self.assertEqual(card['open_tickets'], 1)
        # The held round is an hour old: had it counted it would be over SLA and
        # the oldest ticket.
        self.assertEqual(card['over_sla'], 0)
        self.assertEqual(card['oldest_ticket_minutes'], 1)

    def test_a_ticket_ages_from_its_release(self):
        """Created 40 minutes ago, released one minute ago: one minute old."""
        order = self._aged(self.table1, 40)
        _place(order, timezone.now() - timedelta(minutes=1))
        card = _build_kds(self.restaurant.id)
        self.assertEqual(card['open_tickets'], 1)
        self.assertEqual(card['over_sla'], 0)
        self.assertEqual(card['at_risk'], 0)
        self.assertEqual(card['oldest_ticket_minutes'], 1)
        self.assertEqual(card['status'], 'on_track')

    def test_an_unplaced_ticket_ages_from_its_creation(self):
        """The fallback: no placement, so the creation time is all there is."""
        self._aged(self.table1, 20)
        card = _build_kds(self.restaurant.id)
        self.assertEqual(card['open_tickets'], 1)
        self.assertEqual(card['over_sla'], 1)
        self.assertEqual(card['oldest_ticket_minutes'], 20)

    def test_the_card_costs_three_queries_with_placements(self):
        """count, the open-ticket read, the served-today read — as at the head."""
        for table, released in ((self.table1, timezone.now()), (self.table2, None)):
            _place(self._aged(table, 5), released)
        self._aged(self.table3, 5)
        with CaptureQueriesContext(connection) as ctx:
            _build_kds(self.restaurant.id)
        self.assertEqual(len(ctx), 3)
