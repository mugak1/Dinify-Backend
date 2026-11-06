"""
Step 2, PR BE-G — staff alerts for rounds HELD for staff.

Two nudges, both counting through ONE definition, ``round_placement.held_rounds``:

* THE BELL: one ``round_waiting`` notification per restaurant (module ``tables``,
  dedupe key ``round_waiting``), written by ``producers.notify_round_waiting`` after
  commit. A held acceptance schedules it; so do a Confirm and a Decline that changed
  something. It RE-ALERTS only when the row is new or more rounds wait than it last
  said, and otherwise updates the payload; the payload is read under the row lock, so
  callbacks arriving in any order converge on what stands.
* THE KITCHEN: the active feed's envelope gains ``waiting_for_staff``, at one
  deliberate extra query (4 -> 5).

Every held round here is produced by the REAL acceptance through the real submit
endpoint (BE-F), and every Confirm and Decline goes through the real floor routes.
``TestCase`` runs ``on_commit`` callbacks only when a test asks for them
(``captureOnCommitCallbacks``), which is what lets these tests show what each
request SCHEDULES, and run the scheduled callbacks in any order. The class at the
bottom is a ``TransactionTestCase`` on real PostgreSQL: real commits, a real
rollback, and the bell written by a real ``on_commit``.
"""
import itertools
import json
import unittest
import uuid
from unittest import mock

from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app.models import (
    PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
)
from dinify_backend.configss.string_definitions import (
    MODULE_TABLES, RESTAURANT_MANAGER, OrderStatus_Cancelled,
)
from notifications_app import producers
from notifications_app.models import NotificationKind, RestaurantNotification
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.models import Order, OrderAcceptance, OrderPlacement
from orders_app.tests_dining_writer import DiningFixture, _user
from orders_app.tests_round_hold import (
    ACTIVE_URL, FLOOR_URL, HOLDS_OFF, HOLDS_ON, HoldFixture,
)
from restaurants_app.models import RestaurantEmployee

COUNT_URL = '/api/v1/notifications/inbox/unread-count/'
PAYLOAD_KEYS = {'waiting', 'oldest_held_since', 'latest_table'}


def _read_url(pk):
    return f'/api/v1/notifications/inbox/{pk}/read/'


def _iso(value):
    """The floor contract's rendering of an instant (``round_release.iso``)."""
    return timezone.localtime(value, timezone.get_default_timezone()).isoformat(
        timespec='milliseconds')


class StaffAlertFixture(HoldFixture):
    """BE-F's fixture at a PAY-AFTER restaurant, plus a manager who reads the bell."""

    phone_seed = '25670096'

    def setUp(self):
        super().setUp()
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        self.manager = _user(f'{self.phone_seed}0003')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant,
            roles=[RESTAURANT_MANAGER], active=True,
        )

    # --- requests, with their after-commit callbacks ----------------------------

    def submit_capturing(self, order, *, as_staff=False):
        """Submit through the endpoint; return the response and the callbacks it
        scheduled, NOT yet run."""
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.put_submit(order, as_staff=as_staff)
        self.assertEqual(response.status_code, 200, response.content)
        return response, callbacks

    def held(self, table, run=True):
        """A diner's round, held at acceptance (holds on, pay-after)."""
        with HOLDS_ON:
            order = self.draft(table=table)
            if run:
                with self.captureOnCommitCallbacks(execute=True):
                    self.submitted(order)
            else:
                self.submitted(order)
        return order

    def floor_command(self, command, order, execute=True):
        with self.captureOnCommitCallbacks(execute=execute) as callbacks:
            response = getattr(self, command)(order)
        return response, callbacks

    # --- the bell ----------------------------------------------------------------

    def row(self):
        return RestaurantNotification.objects.filter(
            restaurant=self.restaurant, dedupe_key='round_waiting').first()

    def unread(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(COUNT_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['unread_count']

    def mark_read(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(_read_url(self.row().pk))
        self.assertEqual(response.status_code, 200, response.content)

    def waiting_on_feed(self):
        response = self.staff().get(ACTIVE_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()


# --- the bell: what a held acceptance writes ---------------------------------------

class AHeldAcceptanceAlertsTests(StaffAlertFixture):

    def test_the_first_held_acceptance_creates_the_one_row(self):
        order = self.held(self.tables[0])
        row = self.row()
        self.assertIsNotNone(row)
        self.assertEqual(RestaurantNotification.objects.count(), 1)
        self.assertEqual(
            (row.kind, row.module, row.dedupe_key, row.actor_id),
            (NotificationKind.ROUND_WAITING, MODULE_TABLES, 'round_waiting', None))
        self.assertEqual(row.payload, {
            'waiting': 1,
            'oldest_held_since': _iso(self.accepted_at(order)),
            'latest_table': '1',
        })
        self.assertEqual(self.unread(self.owner), 1)
        self.assertEqual(self.unread(self.manager), 1)

    def test_the_payload_carries_exactly_three_keys_and_no_diner_data(self):
        """A count, an instant and a table's display name. No order id, no diner
        contact, no free text, no amount."""
        self.tables[0].display_name = 'Patio 4'
        self.tables[0].save(update_fields=['display_name'])
        order = self.held(self.tables[0])
        payload = self.row().payload
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload['latest_table'], 'Patio 4')
        serialized = json.dumps(payload)
        for fact in (str(order.pk), str(order.restaurant_id), str(order.table_id),
                     str(self.item.pk), 'Matooke', '12000'):
            self.assertNotIn(fact, serialized)

    def test_a_new_held_round_realerts_for_a_user_who_had_read_it(self):
        first = self.held(self.tables[0])
        self.mark_read(self.manager)
        self.assertEqual(self.unread(self.manager), 0)
        alerted_at = self.row().occurred_at

        self.held(self.tables[1])
        row = self.row()
        self.assertGreater(row.occurred_at, alerted_at)
        self.assertEqual(self.unread(self.manager), 1, 'unread again')
        self.assertEqual(self.unread(self.owner), 1)
        self.assertEqual(row.payload, {
            'waiting': 2,
            'oldest_held_since': _iso(self.accepted_at(first)),
            'latest_table': '2',
        })
        self.assertEqual(RestaurantNotification.objects.count(), 1, 'still one row')

    def test_after_a_confirm_brings_it_down_the_next_held_round_realerts(self):
        first = self.held(self.tables[0])
        self.held(self.tables[1])
        self.floor_command('confirm', first)
        self.assertEqual(self.row().payload['waiting'], 1)
        self.mark_read(self.manager)

        self.held(self.tables[2])
        self.assertEqual(self.row().payload['waiting'], 2)
        self.assertEqual(self.unread(self.manager), 1, '2 > 1: a new order waits')


# --- the bell: what does NOT write -------------------------------------------------

class NothingElseAlertsTests(StaffAlertFixture):

    def assert_schedules_nothing(self, order, *, as_staff=False):
        _response, callbacks = self.submit_capturing(order, as_staff=as_staff)
        self.assertEqual(callbacks, [], 'a released acceptance schedules nothing')
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_holds_off_a_diner_round_at_pay_after_writes_nothing(self):
        with HOLDS_OFF:
            order = self.draft()
            self.assert_schedules_nothing(order)
        self.assert_released_at_acceptance(order)

    def test_a_staff_order_writes_nothing(self):
        with HOLDS_ON:
            order = self.draft(created_by=self.owner)
            self.assert_schedules_nothing(order, as_staff=True)
        self.assert_released_at_acceptance(order)

    def test_a_released_round_at_pay_first_writes_nothing(self):
        RestaurantServiceConfiguration.objects.filter(
            restaurant=self.restaurant).update(payment_timing=PAYMENT_TIMING_PAY_FIRST)
        with HOLDS_ON:
            order = self.draft()
            self.assert_schedules_nothing(order)
        self.assert_released_at_acceptance(order)

    def test_a_released_acceptance_next_to_a_held_round_leaves_the_row_alone(self):
        """A released round beside an existing alert schedules nothing either, so it
        can neither re-alert nor touch the row."""
        self.held(self.tables[0])
        before = RestaurantNotification.objects.values().get()
        with HOLDS_ON:
            order = self.draft(table=self.tables[1], created_by=self.owner)
            _response, callbacks = self.submit_capturing(order, as_staff=True)
        self.assertEqual(callbacks, [])
        self.assertEqual(RestaurantNotification.objects.values().get(), before)

    def test_the_visit_writer_off_writes_nothing(self):
        with HOLDS_ON, override_settings(DINING_VISIT_WRITES_ENABLED=False):
            order = self.draft()
            self.assert_schedules_nothing(order)
        self.assertIsNone(self.placement(order))

    def test_a_held_acceptance_schedules_exactly_one_callback(self):
        with HOLDS_ON:
            order = self.draft()
            _response, callbacks = self.submit_capturing(order)
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(RestaurantNotification.objects.exists(),
                         'nothing is written before commit')
        callbacks[0]()
        self.assertEqual(self.row().payload['waiting'], 1)


# --- the bell: Confirm and Decline -------------------------------------------------

class ConfirmAndDeclineUpdateWithoutRealertTests(StaffAlertFixture):

    def test_confirm_and_decline_bring_the_count_down_and_keep_it_read(self):
        first = self.held(self.tables[0])
        second = self.held(self.tables[1])
        self.mark_read(self.manager)
        alerted_at = self.row().occurred_at

        response, callbacks = self.floor_command('confirm', first)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(callbacks), 1)
        row = self.row()
        self.assertEqual(row.occurred_at, alerted_at, 'no re-alert')
        self.assertEqual(row.payload, {
            'waiting': 1,
            'oldest_held_since': _iso(self.accepted_at(second)),
            'latest_table': '2',
        })
        self.assertEqual(self.unread(self.manager), 0, 'still read')

        response, callbacks = self.floor_command('decline', second)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(callbacks), 1)
        row = self.row()
        self.assertEqual(row.occurred_at, alerted_at, 'no re-alert')
        self.assertEqual(row.payload, {
            'waiting': 0, 'oldest_held_since': None, 'latest_table': None})
        self.assertEqual(self.unread(self.manager), 0, 'still read')
        self.assertEqual(self.unread(self.owner), 1, 'the owner never read it')

    def test_a_command_that_changes_nothing_schedules_nothing(self):
        order = self.held(self.tables[0])
        self.floor_command('confirm', order)
        before = RestaurantNotification.objects.values().get()

        resent, callbacks = self.floor_command('confirm', order, execute=False)
        self.assertEqual(resent.status_code, 200, resent.content)
        self.assertFalse(resent.json()['data']['changed'])
        self.assertEqual(callbacks, [])

        refused, callbacks = self.floor_command('decline', order, execute=False)
        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertEqual(callbacks, [])
        self.assertEqual(RestaurantNotification.objects.values().get(), before)

    def test_with_no_row_and_nothing_waiting_a_confirm_writes_nothing(self):
        """The acceptance's callback never ran (it failed, say); the Confirm's runs on
        a restaurant with nothing waiting: there is nothing to announce."""
        order = self.held(self.tables[0], run=False)
        response, callbacks = self.floor_command('confirm', order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_a_decline_callback_running_first_still_creates_the_row(self):
        """Out of order: the acceptance's callback has not run, the Decline's runs on
        a restaurant where another round still waits. A new row is an alert."""
        first = self.held(self.tables[0], run=False)
        self.held(self.tables[1], run=False)
        self.floor_command('decline', first)
        row = self.row()
        self.assertEqual(row.payload['waiting'], 1)
        self.assertEqual(self.unread(self.manager), 1)


# --- the bell: callbacks in any order ----------------------------------------------

class OutOfOrderCallbacksConvergeTests(StaffAlertFixture):
    """Two held acceptances and then a Confirm, with their three callbacks run in
    every order. Each reads the database as it stands under the row lock, so the
    final payload is the state that stands — one waiting — whatever the order."""

    def test_every_order_of_the_three_callbacks_gives_one_waiting(self):
        with HOLDS_ON:
            first, second = self.draft(table=self.tables[0]), self.draft(table=self.tables[1])
            _r, accept_first = self.submit_capturing(first)
            _r, accept_second = self.submit_capturing(second)
        _r, confirm_first = self.floor_command('confirm', first, execute=False)
        callbacks = {'accept_first': accept_first[0], 'accept_second': accept_second[0],
                     'confirm_first': confirm_first[0]}
        expected = {
            'waiting': 1,
            'oldest_held_since': _iso(self.accepted_at(second)),
            'latest_table': '2',
        }
        for order in itertools.permutations(callbacks):
            with self.subTest(order=order):
                RestaurantNotification.objects.all().delete()
                for name in order:
                    callbacks[name]()
                self.assertEqual(self.row().payload, expected)
                self.assertEqual(RestaurantNotification.objects.count(), 1)


# --- the bell: transactions and failures -------------------------------------------

class RollbackAndFailureTests(StaffAlertFixture):

    def test_a_rolled_back_acceptance_writes_nothing(self):
        with HOLDS_ON:
            order = self.draft()
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                try:
                    with transaction.atomic():
                        self.accept(order)
                        self.assertTrue(OrderPlacement.objects.filter(order=order).exists())
                        raise RuntimeError('roll the acceptance back')
                except RuntimeError:
                    pass
        self.assertEqual(callbacks, [], 'the callback went with the savepoint')
        self.assertFalse(RestaurantNotification.objects.exists())
        self.assertFalse(OrderPlacement.objects.filter(order=order).exists())

    def _broken(self):
        return mock.patch.object(round_placement, 'held_rounds_summary',
                                 side_effect=RuntimeError('bell down'))

    def test_a_producer_failure_never_changes_the_submit_response(self):
        with HOLDS_ON:
            control = self.draft(table=self.tables[1])
            with self.captureOnCommitCallbacks(execute=True):
                healthy = self.put_submit(control)
            order = self.draft(table=self.tables[0])
            with self._broken(), self.captureOnCommitCallbacks(execute=True) as callbacks:
                response = self.put_submit(order)
        self.assertEqual(len(callbacks), 1, 'the producer did run, and failed')
        self.assertEqual(response.status_code, healthy.status_code)
        self.assertEqual(response.json()['checkout']['current']['round']['state'],
                         'held')
        self.assertEqual(set(response.json()), set(healthy.json()))
        self.assertEqual(set(response.json()['checkout']), set(healthy.json()['checkout']))
        self.assertEqual(self.row().payload['waiting'], 1, 'the control wrote it')

    def test_a_producer_failure_never_changes_a_confirm_or_decline_response(self):
        first = self.held(self.tables[0])
        second = self.held(self.tables[1])
        before = RestaurantNotification.objects.values().get()
        with self._broken():
            confirmed, c_callbacks = self.floor_command('confirm', first)
            declined, d_callbacks = self.floor_command('decline', second)
        self.assertEqual((len(c_callbacks), len(d_callbacks)), (1, 1))
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertTrue(confirmed.json()['data']['changed'])
        self.assertEqual(confirmed.json()['data']['round']['state'], 'released')
        self.assertEqual(declined.status_code, 200, declined.content)
        self.assertEqual(declined.json()['data']['round']['state'], 'declined')
        self.assertEqual(RestaurantNotification.objects.values().get(), before,
                         'a failed producer writes nothing')


# --- what it costs ---------------------------------------------------------------

class TheBellCostsTheRequestNothingTests(StaffAlertFixture):
    """The bell's reads and writes run in the after-commit callback, not in the
    request's transaction: the pinned counts do not move."""

    def test_submit_stays_18_and_confirm_15_and_decline_10(self):
        """The pins of ``WhatHoldingCostsTests`` (the acceptance, 18) and
        ``round_release.QueryCountTests`` (Confirm 15, Decline 10), measured with the
        bell's callback SCHEDULED: it is registered, and none of its queries is in
        the count."""
        from orders_app.controllers.manage_order import update_order_status
        from orders_app.controllers.services.order_quote import quote_ref
        with HOLDS_ON:
            warm = self.draft(table=self.tables[3])
            self.accept(warm)              # content types and first-use caches
            order = self.draft(table=self.tables[0])
            other = self.draft(table=self.tables[1])
            ref = quote_ref(order)
            with self.captureOnCommitCallbacks(execute=False) as callbacks, \
                    self.assertNumQueries(18):
                result = update_order_status(order, 'pending', None, quote_ref=ref)
            self.assertEqual(result['checkout']['current']['round']['state'], 'held')
            self.assertEqual(len(callbacks), 1)
            self.submitted(other)
        with self.captureOnCommitCallbacks(execute=False) as callbacks, \
                self.assertNumQueries(15):
            self.assertEqual(self.confirm(order).status_code, 200)
        self.assertEqual(len(callbacks), 1)
        with self.captureOnCommitCallbacks(execute=False) as callbacks, \
                self.assertNumQueries(10):
            self.assertEqual(self.decline(other).status_code, 200)
        self.assertEqual(len(callbacks), 1)

    def test_what_the_callback_itself_costs(self):
        """Measured, so the after-commit work is stated rather than hidden: the first
        alert creates the row; a later one updates it. Each count includes the
        producer's own SAVEPOINT pair (in production its ``atomic`` is a real
        transaction, after the acceptance's COMMIT)."""
        with HOLDS_ON:
            first, second = self.draft(table=self.tables[0]), self.draft(table=self.tables[1])
            _r, create = self.submit_capturing(first)
            _r, update = self.submit_capturing(second)
        with CaptureQueriesContext(connection) as created:
            create[0]()
        with CaptureQueriesContext(connection) as updated:
            update[0]()
        self.assertEqual(self.row().payload['waiting'], 2)
        # Recorded here so a change in the producer's cost is a visible diff.
        # lock read, count, latest table, INSERT (in its own savepoint), prune read
        self.assertEqual(len(created), 9, [q['sql'] for q in created])
        # lock read, count, latest table, UPDATE, prune read
        self.assertEqual(len(updated), 7, [q['sql'] for q in updated])


# --- the kitchen: waiting_for_staff ------------------------------------------------

class TheKitchenFeedCountsHeldRoundsTests(StaffAlertFixture):

    def other_restaurant_held_round(self):
        owner = _user(f'{self.phone_seed}0009')
        other = DiningFixture._restaurant(self, 'Elsewhere', owner)
        RestaurantServiceConfiguration.objects.create(
            restaurant=other, payment_timing=PAYMENT_TIMING_PAY_AFTER,
            payment_timing_set_at=timezone.now(), payment_timing_set_by=owner)
        table = DiningFixture._table(self, other, 1)
        item = DiningFixture._item(self, other)
        with HOLDS_ON:
            order = _create_order(restaurant=other, table=table,
                                  items=[{'item': str(item.pk), 'quantity': 1}],
                                  created_by=None, client_order_id=str(uuid.uuid4()))
            self.assertEqual(order.get('status'), 200, order)
            self.accept(order['order'])
        self.assertTrue(round_placement.is_held(
            Order.objects.select_related('placement').get(pk=order['order'].pk)))

    def test_zero_one_and_several(self):
        body = self.waiting_on_feed()
        self.assertEqual(body['waiting_for_staff'], 0)
        self.assertEqual(body['data'], [])

        self.held(self.tables[0])
        body = self.waiting_on_feed()
        self.assertEqual(body['waiting_for_staff'], 1)
        self.assertEqual(body['data'], [], 'a held round is not on the board')

        self.held(self.tables[1])
        self.held(self.tables[2])
        self.assertEqual(self.waiting_on_feed()['waiting_for_staff'], 3)

    def test_it_ignores_other_restaurants_cancelled_released_and_unplaced_orders(self):
        held = self.held(self.tables[0])
        confirmed = self.held(self.tables[1])
        declined = self.held(self.tables[2])
        self.floor_command('confirm', confirmed)
        self.floor_command('decline', declined)
        self.other_restaurant_held_round()
        # A kitchen-cancelled round that was released, and an order with no placement.
        with HOLDS_OFF:
            released = self.draft(table=self.tables[3])
            self.submitted(released)
        Order.objects.filter(pk=released.pk).update(order_status=OrderStatus_Cancelled)
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            unplaced = self.draft(table=self.tables[3])
            self.submitted(unplaced)
        self.assertIsNone(self.placement(unplaced))

        body = self.waiting_on_feed()
        self.assertEqual(body['waiting_for_staff'], 1)
        ids = {ticket['id'] for ticket in body['data']}
        self.assertNotIn(str(held.pk), ids)
        self.assertEqual(self.row().payload['waiting'], 1, 'the bell agrees')
        floor = self.staff().get(FLOOR_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(floor.json()['data']['waiting_for_staff'], 1, 'the floor agrees')

    def test_a_soft_deleted_held_round_is_not_waiting(self):
        order = self.held(self.tables[0])
        Order.objects.filter(pk=order.pk).update(deleted=True)
        self.assertEqual(self.waiting_on_feed()['waiting_for_staff'], 0)

    def test_the_envelope_shape_is_additive(self):
        body = self.waiting_on_feed()
        self.assertEqual(set(body), {'status', 'message', 'kitchen_protocol',
                                     'waiting_for_staff', 'data'})
        self.assertIsInstance(body['waiting_for_staff'], int)

    def test_the_active_feed_costs_five_queries_flat(self):
        """4 (BE-C) + ONE deliberate COUNT for ``waiting_for_staff``."""
        self.held(self.tables[0])
        with HOLDS_OFF:
            for table in self.tables[1:3]:
                self.submitted(self.draft(table=table))
        with self.assertNumQueries(5):
            body = self.waiting_on_feed()
        self.assertEqual((body['waiting_for_staff'], len(body['data'])), (1, 2))
        self.held(self.tables[3])
        with self.assertNumQueries(5):
            self.assertEqual(self.waiting_on_feed()['waiting_for_staff'], 2)

    def test_the_completed_feed_is_unchanged(self):
        self.held(self.tables[0])
        response = self.staff().get('/api/v1/kitchen/orders/completed/',
                                    {'restaurant': str(self.restaurant.pk)})
        self.assertNotIn('waiting_for_staff', response.json())


# --- the kind's database constraint ------------------------------------------------

class TheKindVocabularyTests(StaffAlertFixture):

    def create(self, kind):
        return RestaurantNotification.objects.create(
            restaurant=self.restaurant, module=MODULE_TABLES, kind=kind,
            dedupe_key=f'probe:{kind}', payload={})

    def test_the_constraint_accepts_round_waiting(self):
        self.assertEqual(self.create('round_waiting').kind, 'round_waiting')

    def test_the_constraint_still_refuses_an_unknown_kind(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create('round_waitingx')

    def test_the_database_holds_the_new_vocabulary(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, RestaurantNotification._meta.db_table)
        self.assertIn('restaurant_notification_kind_vocabulary', constraints)
        self.assertTrue(constraints['restaurant_notification_kind_vocabulary']['check'])


# --- real commits on real PostgreSQL -----------------------------------------------

@unittest.skipUnless(connection.vendor == 'postgresql', 'real PostgreSQL only')
class RealCommitTests(TransactionTestCase):
    """No test transaction: the submit commits for real, and the bell is written by
    the real ``transaction.on_commit``. And a real rollback writes nothing."""

    phone_seed = '25670095'
    restaurant_status = DiningFixture.restaurant_status
    restaurant_is_test = False

    # The fixture's helpers, borrowed: none of them uses ``super()``.
    _restaurant = DiningFixture._restaurant
    _table = DiningFixture._table
    _item = DiningFixture._item
    submit = DiningFixture.submit
    accept = DiningFixture.accept
    draft = HoldFixture.draft
    put_submit = HoldFixture.put_submit

    def setUp(self):
        self.owner = _user(f'{self.phone_seed}0001')
        self.restaurant = self._restaurant('Real Commit', self.owner)
        self.tables = [self._table(self.restaurant, n) for n in (1, 2)]
        self.item = self._item(self.restaurant)
        RestaurantServiceConfiguration.objects.create(
            restaurant=self.restaurant, payment_timing=PAYMENT_TIMING_PAY_AFTER,
            payment_timing_set_at=timezone.now(), payment_timing_set_by=self.owner)
        self.client = APIClient()
        self.owner_token = str(RefreshToken.for_user(self.owner).access_token)

    def test_a_committed_held_acceptance_writes_the_bell_after_commit(self):
        seen = {}
        real = producers.notify_round_waiting

        def spy(restaurant_id):
            # When the callback starts: no transaction is open on this connection,
            # and a SECOND connection already sees the acceptance: it committed.
            seen['in_atomic_block'] = connection.in_atomic_block
            other = connections.create_connection('default')
            try:
                with other.cursor() as cursor:
                    cursor.execute(
                        'SELECT count(*) FROM order_acceptances WHERE order_id = %s',
                        [str(order.pk)])
                    seen['committed'] = cursor.fetchone()[0]
            finally:
                other.close()
            real(restaurant_id)

        with HOLDS_ON:
            order = self.draft(table=self.tables[0])
            with mock.patch.object(producers, 'notify_round_waiting', spy):
                response = self.put_submit(order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(seen, {'in_atomic_block': False, 'committed': 1})
        row = RestaurantNotification.objects.get()
        self.assertEqual(row.payload['waiting'], 1)
        accepted = OrderAcceptance.objects.get(order=order).accepted_at
        self.assertEqual(row.payload['oldest_held_since'], _iso(accepted))

    def test_a_real_rollback_writes_nothing(self):
        with HOLDS_ON:
            order = self.draft(table=self.tables[0])
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.accept(order)
                    raise RuntimeError('roll the acceptance back')
        self.assertFalse(RestaurantNotification.objects.exists())
        self.assertFalse(OrderPlacement.objects.filter(order=order).exists())
