"""
Step 2, PR BE-E — the held-round state in the diner's reads (diner round state v1).

A diner's first round at a pay-after restaurant will be HELD until a member of staff
confirms it, and the phone must say so. This PR adds two ADDITIVE fields and nothing
else:

* ``current.round`` — ``{state, held_since, stale}`` — on the correlated checkout
  projection (``acceptance_result``), and therefore on every response that publishes
  it: the first-submission reply, the replayed submission, the quote-retirement 409
  for an already-accepted order, and the diner's own order read (both selectors);
* ``current_order.held`` on the table scan.

THE RULE IS WRITTEN ONCE, ``round_placement.diner_round``, and is pinned here at its
edges: ``declined`` needs the ``declined_by_staff`` reason (a kitchen cancellation is
``released``, as today); an order with NO placement is never held; ``stale`` flips at
exactly fifteen minutes.

NOTHING HOLDS A ROUND YET (BE-F will), so held placements are fabricated: an accepted
order's placement is set back to ``released_at = NULL``, as BE-C's and BE-D's tests
do, and the first-submission reply is exercised through a stand-in writer that does
the same thing inside the acceptance.

NO QUERY IS ADDED. The placement rides the statements that already read the order
(``select_related('placement')``), and the scan's ``held`` rides the scan's one
occupancy read. The counts below were measured on the head this branched from
(``d0883dc``) and are unchanged.
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind, CancellationReason_DeclinedByStaff,
    OrderStatus_Cancelled,
)
from orders_app.controllers.manage_order import retire_quote_for_review
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.round_placement import (
    DINER_ROUND_DECLINED, DINER_ROUND_HELD, DINER_ROUND_RELEASED,
    HELD_STALE_AFTER, diner_round,
)
from orders_app.models import Order, OrderAcceptance, OrderPlacement
from orders_app.tests_dining_writer import DiningFixture
from restaurants_app.controllers.diner_capability import (
    issue_qr_credential, issue_table_session,
)
from restaurants_app.controllers.handle_diner_journey import handle_table_scan
from restaurants_app.models import Table
from restaurants_app.serializers import SerializerPublicGetTableDetails

#: `current` as it was before this PR. `round` is the one key BE-E adds.
CURRENT_BEFORE = {'order_status', 'fulfilment_status', 'cancelled_at', 'served_at'}

ORDER_READ = '/api/v1/orders/journey/order-details/'


def _hold(order):
    """Fabricate a held round: the accepted order's placement, unreleased."""
    OrderPlacement.objects.filter(order=order).update(released_at=None)


def _holding_writer():
    """A stand-in for the holding writer BE-F will add: the real Step 1 writer, then
    the placement set back to held inside the same acceptance transaction. The
    instance is the one cached on the order, so the reply sees it."""
    real = round_placement.place_first_round

    def holding(order, accepted_at):
        placement = real(order, accepted_at)
        if placement is not None:
            placement.released_at = None
            placement.save(update_fields=['released_at'])
        return placement

    return mock.patch.object(round_placement, 'place_first_round', holding)


class RoundFixture(DiningFixture):
    phone_seed = '25670098'

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def draft(self, table=None, **kwargs):
        """Every draft here carries an intent key, so the order read can be asked
        through both of its selectors."""
        result = _create_order(
            restaurant=self.restaurant, table=table or self.tables[0],
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id=str(uuid.uuid4()),
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def accepted(self, table=None):
        order = self.draft(table=table)
        self.accept(order)
        return order

    def reload(self, order):
        return Order.objects.select_related('placement').get(pk=order.pk)

    def accepted_at(self, order):
        return OrderAcceptance.objects.get(order=order).accepted_at

    def read(self, order, /, **selector):
        params = selector or {'order': str(order.pk)}
        response = self.client.get(
            ORDER_READ, params,
            HTTP_X_DINER_SESSION=issue_table_session(order.table))
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def read_round(self, order, /, **selector):
        return self.read(order, **selector)['data']['checkout']['current']['round']

    def scan(self, table):
        table = Table.objects.get(pk=table.pk)
        request = APIRequestFactory().get(
            '/api/v1/orders/journey/table-scan/',
            HTTP_X_DINER_CREDENTIAL=issue_qr_credential(
                table.restaurant_id, table.pk, table.qr_version),
        )
        result = handle_table_scan(request)
        self.assertEqual(result['status'], 200, result)
        # As the client parses it (the UUID renders as a string).
        return json.loads(json.dumps(result['data']['current_order'], default=str))


# --- the rule, at its edges -----------------------------------------------------

class TheRuleTests(RoundFixture):
    """``diner_round`` for every state the contract names, over real rows."""

    def test_a_held_round_is_held_since_its_acceptance(self):
        order = self.accepted()
        _hold(order)
        accepted_at = self.accepted_at(order)
        result = diner_round(self.reload(order), accepted_at=accepted_at,
                             now=accepted_at)
        self.assertEqual(result, {
            'state': DINER_ROUND_HELD,
            'held_since': accepted_at.isoformat(),
            'stale': False,
        })

    def test_stale_flips_at_exactly_fifteen_minutes(self):
        order = self.accepted()
        _hold(order)
        accepted_at = self.accepted_at(order)
        order = self.reload(order)
        at = lambda delta: diner_round(order, accepted_at=accepted_at,
                                       now=accepted_at + delta)
        self.assertFalse(at(timedelta(minutes=14, seconds=59))['stale'])
        self.assertFalse(
            at(timedelta(minutes=14, seconds=59, microseconds=999999))['stale'])
        self.assertTrue(at(timedelta(minutes=15))['stale'])
        self.assertTrue(at(timedelta(hours=3))['stale'])
        # Stale is a flag only: still held, still held since the acceptance.
        self.assertEqual(at(timedelta(minutes=15))['state'], DINER_ROUND_HELD)
        self.assertEqual(at(timedelta(minutes=15))['held_since'],
                         accepted_at.isoformat())

    def test_the_threshold_is_the_floors(self):
        """The floor and the phone call a round stale at the same moment."""
        from orders_app.controllers.services.round_release import STALE_AFTER
        self.assertEqual(HELD_STALE_AFTER, STALE_AFTER)
        self.assertEqual(HELD_STALE_AFTER, timedelta(minutes=15))

    def test_a_declined_round_is_declined_and_never_stale(self):
        order = self.accepted()
        _hold(order)
        Order.objects.filter(pk=order.pk).update(
            order_status=OrderStatus_Cancelled,
            cancellation_reason=CancellationReason_DeclinedByStaff,
            cancelled_at=timezone.now())
        accepted_at = self.accepted_at(order)
        result = diner_round(self.reload(order), accepted_at=accepted_at,
                             now=accepted_at + timedelta(hours=1))
        self.assertEqual(result, {
            'state': DINER_ROUND_DECLINED, 'held_since': None, 'stale': False,
        })

    def test_a_released_round_is_released(self):
        order = self.accepted()
        accepted_at = self.accepted_at(order)
        result = diner_round(self.reload(order), accepted_at=accepted_at,
                             now=accepted_at + timedelta(hours=1))
        self.assertEqual(result, {
            'state': DINER_ROUND_RELEASED, 'held_since': None, 'stale': False,
        })

    def test_an_order_with_no_placement_is_released(self):
        """THE KITCHEN'S RULE: an order accepted before Step 1, or with the writer
        off, has no placement and was never held."""
        order = self.accepted()
        OrderPlacement.objects.filter(order=order).delete()
        accepted_at = self.accepted_at(order)
        result = diner_round(self.reload(order), accepted_at=accepted_at,
                             now=accepted_at + timedelta(hours=1))
        self.assertEqual(result, {
            'state': DINER_ROUND_RELEASED, 'held_since': None, 'stale': False,
        })

    def test_a_kitchen_cancellation_is_released_as_today(self):
        """Cancelled for any reason but ``declined_by_staff`` is NOT declined: the
        diner is told about it through ``current.order_status``, unchanged. Held or
        released placement alike."""
        for held in (False, True):
            with self.subTest(held_placement=held):
                order = self.accepted(table=self.tables[int(held)])
                if held:
                    _hold(order)
                Order.objects.filter(pk=order.pk).update(
                    order_status=OrderStatus_Cancelled,
                    cancellation_reason=CancellationReason_CustomerChangedMind,
                    cancelled_at=timezone.now())
                accepted_at = self.accepted_at(order)
                self.assertEqual(
                    diner_round(self.reload(order), accepted_at=accepted_at,
                                now=accepted_at + timedelta(hours=1)),
                    {'state': DINER_ROUND_RELEASED, 'held_since': None,
                     'stale': False})
                body = self.read(order)['data']['checkout']['current']
                self.assertEqual(body['order_status'], OrderStatus_Cancelled)
                self.assertEqual(body['round']['state'], DINER_ROUND_RELEASED)

    def test_a_draft_is_released(self):
        """"Everything else": a draft has no placement. The acceptance verdict
        (``not_accepted``) is what tells a client it is a draft."""
        order = self.draft()
        result = diner_round(self.reload(order), accepted_at=None,
                             now=timezone.now())
        self.assertEqual(result['state'], DINER_ROUND_RELEASED)


# --- one round, wherever it is published -------------------------------------------

class EveryProjectionCarriesTheSameRoundTests(RoundFixture):

    def test_submit_reply_order_read_and_replay_agree_on_a_held_round(self):
        order = self.draft()
        ref = quote_ref(order)
        with _holding_writer():
            first = self.submit(order, ref=ref)
        self.assertEqual(first['status'], 200, first)
        self.assertFalse(first['idempotent'])
        replay = self.submit(order, ref=ref)
        self.assertEqual(replay['status'], 200, replay)
        self.assertTrue(replay['idempotent'])

        accepted_at = self.accepted_at(order)
        expected = {
            'state': DINER_ROUND_HELD,
            'held_since': accepted_at.isoformat(),
            'stale': False,
        }
        self.assertEqual(first['checkout']['current']['round'], expected)
        self.assertEqual(replay['checkout']['current']['round'], expected)
        self.assertEqual(self.read_round(order), expected)
        self.assertEqual(
            self.read_round(order, intent=str(order.client_order_id)), expected)
        # `held_since` is byte-identical to the acceptance moment it repeats.
        self.assertEqual(
            first['checkout']['current']['round']['held_since'],
            first['checkout']['acceptance']['accepted_at'])

    def test_submit_reply_order_read_and_replay_agree_on_a_released_round(self):
        order = self.draft()
        ref = quote_ref(order)
        first = self.submit(order, ref=ref)
        replay = self.submit(order, ref=ref)
        expected = {'state': DINER_ROUND_RELEASED, 'held_since': None,
                    'stale': False}
        self.assertEqual(first['checkout']['current']['round'], expected)
        self.assertEqual(replay['checkout']['current']['round'], expected)
        self.assertEqual(self.read_round(order), expected)

    def test_the_writer_off_is_released_everywhere(self):
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            order = self.draft()
            ref = quote_ref(order)
            first = self.submit(order, ref=ref)
            replay = self.submit(order, ref=ref)
        self.assertFalse(OrderPlacement.objects.filter(order=order).exists())
        expected = {'state': DINER_ROUND_RELEASED, 'held_since': None,
                    'stale': False}
        self.assertEqual(first['checkout']['current']['round'], expected)
        self.assertEqual(replay['checkout']['current']['round'], expected)
        self.assertEqual(self.read_round(order), expected)

    def test_the_intent_selector_carries_the_same_round(self):
        order = self.accepted()
        _hold(order)
        self.assertIsNotNone(order.client_order_id)
        self.assertEqual(
            self.read_round(order, intent=str(order.client_order_id)),
            self.read_round(order))
        self.assertEqual(self.read_round(order)['state'], DINER_ROUND_HELD)

    def test_the_read_says_stale_once_fifteen_minutes_have_passed(self):
        order = self.accepted()
        _hold(order)
        moved = timezone.now() - timedelta(minutes=15, seconds=1)
        OrderAcceptance.objects.filter(order=order).update(accepted_at=moved)
        self.assertEqual(self.read_round(order), {
            'state': DINER_ROUND_HELD, 'held_since': moved.isoformat(),
            'stale': True,
        })
        fresh = timezone.now() - timedelta(minutes=14)
        OrderAcceptance.objects.filter(order=order).update(accepted_at=fresh)
        self.assertFalse(self.read_round(order)['stale'])

    def test_a_declined_round_reads_declined(self):
        order = self.accepted()
        _hold(order)
        Order.objects.filter(pk=order.pk).update(
            order_status=OrderStatus_Cancelled,
            cancellation_reason=CancellationReason_DeclinedByStaff,
            cancelled_at=timezone.now())
        self.assertEqual(self.read_round(order), {
            'state': DINER_ROUND_DECLINED, 'held_since': None, 'stale': False,
        })

    def test_the_retirement_409_for_an_accepted_order_carries_it_too(self):
        order = self.accepted()
        _hold(order)
        result = retire_quote_for_review(self.reload(order), quote_ref(order))
        self.assertEqual(result['status'], 409, result)
        self.assertEqual(result['checkout']['current']['round']['state'],
                         DINER_ROUND_HELD)

    def test_current_gains_exactly_one_key(self):
        """A response with ``round`` is otherwise today's: ``current`` is the four
        keys it was, plus ``round``, and the round's own keys are the contract's."""
        order = self.accepted()
        _hold(order)
        current = self.read(order)['data']['checkout']['current']
        self.assertEqual(set(current), CURRENT_BEFORE | {'round'})
        self.assertEqual(set(current['round']), {'state', 'held_since', 'stale'})


# --- the table scan ---------------------------------------------------------------

class TheScanSaysHeldTests(RoundFixture):

    def test_held_is_true_only_for_a_held_ongoing_order(self):
        empty, released, held, no_placement = self.tables
        declined = self._table(self.restaurant, 5)

        self.assertEqual(self.scan(empty),
                         {'ongoing': False, 'order_id': None, 'held': False})

        released_order = self.accepted(table=released)
        self.assertEqual(self.scan(released), {
            'ongoing': True, 'order_id': str(released_order.pk), 'held': False})

        held_order = self.accepted(table=held)
        _hold(held_order)
        self.assertEqual(self.scan(held), {
            'ongoing': True, 'order_id': str(held_order.pk), 'held': True})

        legacy = self.accepted(table=no_placement)
        OrderPlacement.objects.filter(order=legacy).delete()
        self.assertEqual(self.scan(no_placement), {
            'ongoing': True, 'order_id': str(legacy.pk), 'held': False})

        gone = self.accepted(table=declined)
        _hold(gone)
        Order.objects.filter(pk=gone.pk).update(
            order_status=OrderStatus_Cancelled,
            cancellation_reason=CancellationReason_DeclinedByStaff)
        # Declined frees the table: no ongoing order, so nothing is held.
        self.assertEqual(self.scan(declined),
                         {'ongoing': False, 'order_id': None, 'held': False})

    def test_the_gates_keep_their_statement(self):
        """Only the scan asks for ``held``: the occupancy gates' answer is the one
        they always had."""
        from orders_app.controllers.con_orders import ConOrder
        order = self.accepted()
        self.assertEqual(ConOrder.any_present_ongoing_order(self.tables[0]),
                         {'present': True, 'order_id': order.pk})


# --- the cost: unchanged ------------------------------------------------------------

class NoQueryIsAddedTests(RoundFixture):
    """Exact counts, each measured on ``d0883dc`` (the head before this PR) with the
    same fixture, and unchanged. Each is taken with a placement PRESENT, since that is
    where a missing ``select_related`` would show: the lazy ``order.placement``."""

    def count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            result = fn()
        return len(ctx), result

    def test_a_first_submission_costs_18_and_14_with_the_writer_off(self):
        self.accept(self.draft(table=self.tables[3]))     # warm
        order = self.draft()
        ref = quote_ref(order)
        count, result = self.count(lambda: self.submit(order, ref=ref))
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(count, 18)
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            order = self.draft(table=self.tables[1])
            ref = quote_ref(order)
            count, result = self.count(lambda: self.submit(order, ref=ref))
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(count, 14)

    def test_a_replay_costs_7_held_or_released(self):
        for held in (False, True):
            with self.subTest(held=held):
                order = self.accepted(table=self.tables[int(held)])
                if held:
                    _hold(order)
                ref = quote_ref(order)
                self.submit(order, ref=ref)               # warm
                count, result = self.count(lambda: self.submit(order, ref=ref))
                self.assertTrue(result['idempotent'], result)
                self.assertEqual(count, 7)

    def test_the_order_read_costs_4_on_either_selector(self):
        order = self.accepted()
        _hold(order)
        self.read(order)                                   # warm
        for selector in ({'order': str(order.pk)},
                         {'intent': str(order.client_order_id)}):
            with self.subTest(selector=list(selector)):
                count, body = self.count(lambda: self.read(order, **selector))
                self.assertEqual(
                    body['data']['checkout']['current']['round']['state'],
                    DINER_ROUND_HELD)
                self.assertEqual(count, 4)

    def test_the_scan_serializer_costs_4_empty_occupied_or_held(self):
        def serialize(table):
            table = (Table.objects.select_related('restaurant', 'dining_area')
                     .get(pk=table.pk))
            return SerializerPublicGetTableDetails(table).data
        released = self.accepted(table=self.tables[1])
        held = self.accepted(table=self.tables[2])
        _hold(held)
        serialize(self.tables[0])                          # warm
        for table, expected_held in ((self.tables[0], False),
                                     (self.tables[1], False),
                                     (self.tables[2], True)):
            with self.subTest(table=table.number):
                count, data = self.count(lambda: serialize(table))
                self.assertEqual(data['current_order']['held'], expected_held)
                self.assertEqual(count, 4)
        self.assertTrue(released)

    def test_the_retirement_409_costs_what_it_did(self):
        order = self.accepted()
        _hold(order)
        ref = quote_ref(order)
        retire_quote_for_review(self.reload(order), ref)   # warm
        count, result = self.count(
            lambda: retire_quote_for_review(Order.objects.get(pk=order.pk), ref))
        self.assertEqual(result['status'], 409, result)
        self.assertEqual(count, 7)
