"""
Step 2, PR BE-F — HOLDING A ROUND AT ACCEPTANCE, behind ``DINING_ROUND_HOLDS_ENABLED``.

Everything else Step 2 needs was already built and was pinned against FABRICATED held
rounds (BE-C's kitchen filter, BE-D's floor, BE-E's diner reads). This PR adds the one
writer of a held round: ``round_placement.place_first_round`` writes ``released_at``
NULL when ``held_at_acceptance`` says so. Every held round below is produced by the
REAL acceptance, through the real submit endpoint, and then driven through the real
kitchen, floor and diner endpoints.

What is pinned, in the order the module reads:

* WHO IS HELD — the whole matrix through ``PUT orders/submit/``, with the holds switch
  on AND off: a diner order at pay-after (held only when on); a staff-created order at
  pay-after (never); a member of staff submitting a DINER's draft at pay-after (held:
  the hold follows the order's creator, not the submitter); ``not_chosen`` by both
  routes (no configuration row; the axis unset); ``pay_first``; the visit writer off
  (no placement, so released). And the pure decision over every combination.
* A HELD ROUND, end to end: on neither kitchen feed and refused by every kitchen
  command; on the floor with ``needs_staff``; occupying its table, so a second diner
  order there is refused exactly as today; a 0-minute ticket after Confirm with the
  business date re-stamped; cancelled ``declined_by_staff`` and the table freed after
  Decline.
* WHAT THE DINER IS TOLD: the submit reply (in the SAME response), the order read
  (both selectors), a replay and the table scan all say held.
* NO CLOCK: 25 hours later the round is still held; stale is a flag.
* W002: present exactly while the switch is off, and a Warning, never an Error.
* COST: a submit is 18 queries with holds on and off, held or released; a replay is 7.
"""
import json
import uuid
from datetime import time, timedelta
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app.models import (
    PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
)
from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind, CancellationReason_DeclinedByStaff,
    OrderStatus_Cancelled, OrderStatus_Pending,
)
from orders_app.checks import (
    CHECK_ID, HOLDS_CHECK_ID, check_dining_round_holds,
)
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.round_placement import (
    business_date_for, held_at_acceptance,
)
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN, Order, OrderAcceptance, OrderPlacement,
)
from orders_app.tests_dining_writer import DiningFixture, _eat
from restaurants_app.controllers.diner_capability import (
    issue_qr_credential, issue_table_session,
)
from restaurants_app.models import Restaurant, Table

SUBMIT_URL = '/api/v1/orders/submit/'
ORDER_READ = '/api/v1/orders/journey/order-details/'
TABLE_SCAN = '/api/v1/orders/journey/table-scan/'
ACTIVE_URL = '/api/v1/kitchen/orders/active/'
COMPLETED_URL = '/api/v1/kitchen/orders/completed/'
FLOOR_URL = '/api/v1/floor/'

HOLDS_ON = override_settings(DINING_ROUND_HOLDS_ENABLED=True)
HOLDS_OFF = override_settings(DINING_ROUND_HOLDS_ENABLED=False)


class HoldFixture(DiningFixture):
    """Step 1's fixture (a live restaurant, an owner, a waiter, four tables, one dish),
    with requests made the way the clients make them."""

    phone_seed = '25670097'

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.owner_token = str(RefreshToken.for_user(self.owner).access_token)

    # --- configuration -----------------------------------------------------------

    def timing(self, value):
        """Set the restaurant's payment timing (``None`` = the axis unset)."""
        configuration, _ = RestaurantServiceConfiguration.objects.get_or_create(
            restaurant=self.restaurant)
        configuration.payment_timing = value
        configuration.payment_timing_set_at = timezone.now() if value else None
        configuration.payment_timing_set_by = self.owner if value else None
        configuration.save()

    # --- orders ------------------------------------------------------------------

    def draft(self, table=None, created_by=None, restaurant=None, item=None):
        """A draft carrying an intent key, so the order read takes both selectors."""
        result = _create_order(
            restaurant=self.restaurant, table=table or self.tables[0],
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=created_by, client_order_id=str(uuid.uuid4()),
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def put_submit(self, order, *, as_staff=False, ref=None):
        """``PUT orders/submit/``: a diner on the table session, or the owner's JWT."""
        body = {'order': str(order.pk),
                'quote_ref': quote_ref(order) if ref is None else ref}
        if as_staff:
            auth = {'HTTP_AUTHORIZATION': f'Bearer {self.owner_token}'}
        else:
            table = Table.objects.get(pk=order.table_id)
            auth = {'HTTP_X_DINER_SESSION': issue_table_session(table)}
        return self.client.put(SUBMIT_URL, data=json.dumps(body),
                               content_type='application/json', **auth)

    def submitted(self, order, **kwargs):
        response = self.put_submit(order, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def placement(self, order):
        return OrderPlacement.objects.filter(order=order).first()

    def accepted_at(self, order):
        return OrderAcceptance.objects.get(order=order).accepted_at

    # --- reads -------------------------------------------------------------------

    def staff(self):
        client = APIClient()
        client.force_authenticate(user=self.owner)
        return client

    def feed_ids(self, url):
        response = self.staff().get(url, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return {t['id']: t for t in response.json()['data']}

    def floor_table(self, table):
        response = self.staff().get(FLOOR_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return {t['id']: t for t in response.json()['data']['tables']}[str(table.pk)]

    def read_round(self, order, **selector):
        params = selector or {'order': str(order.pk)}
        response = self.client.get(
            ORDER_READ, params,
            HTTP_X_DINER_SESSION=issue_table_session(
                Table.objects.get(pk=order.table_id)))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['checkout']['current']['round']

    def scan(self, table):
        table = Table.objects.get(pk=table.pk)
        response = self.client.get(
            TABLE_SCAN, HTTP_X_DINER_CREDENTIAL=issue_qr_credential(
                table.restaurant_id, table.pk, table.qr_version))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['current_order']

    def confirm(self, order):
        return self.staff().post(f'/api/v1/floor/rounds/{order.pk}/confirm/',
                                 {'restaurant': str(self.restaurant.pk)},
                                 format='json')

    def decline(self, order):
        return self.staff().post(f'/api/v1/floor/rounds/{order.pk}/decline/',
                                 {'restaurant': str(self.restaurant.pk)},
                                 format='json')

    # --- the held shape ----------------------------------------------------------

    def assert_held(self, order):
        placement = self.placement(order)
        self.assertIsNotNone(placement, 'a held round has a placement')
        self.assertIsNone(placement.released_at)
        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.order_status, OrderStatus_Pending)
        # The business date is the acceptance's, provisionally.
        accepted_at = self.accepted_at(order)
        cutoff = Restaurant.objects.get(pk=order.restaurant_id).business_day_cutoff
        self.assertEqual(placement.business_date,
                         business_date_for(accepted_at, cutoff))
        self.assertEqual(placement.visit.opened_at, accepted_at)
        return placement

    def assert_released_at_acceptance(self, order):
        placement = self.placement(order)
        self.assertIsNotNone(placement)
        self.assertEqual(placement.released_at, self.accepted_at(order))
        return placement


# --- who is held: the pure decision ------------------------------------------------

class TheDecisionTests(HoldFixture):
    """``held_at_acceptance`` over every combination of its three inputs."""

    def test_held_only_when_all_three_hold(self):
        timings = (PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST,
                   PAYMENT_TIMING_NOT_CHOSEN)
        creators = (None, uuid.uuid4())
        for holds_on in (True, False):
            for timing in timings:
                for creator in creators:
                    with self.subTest(holds_on=holds_on, timing=timing,
                                      creator=creator):
                        expected = (holds_on and timing == PAYMENT_TIMING_PAY_AFTER
                                    and creator is None)
                        self.assertIs(held_at_acceptance(
                            holds_on=holds_on, payment_timing=timing,
                            created_by_id=creator), expected)

    def test_the_switch_defaults_off(self):
        self.assertFalse(settings.DINING_ROUND_HOLDS_ENABLED)
        self.assertFalse(round_placement.holds_enabled())
        with HOLDS_ON:
            self.assertTrue(round_placement.holds_enabled())


# --- who is held: the matrix through the real submit endpoint --------------------

class WhoIsHeldThroughSubmitTests(HoldFixture):
    """Each case submitted twice, holds on (table 1) and off (table 2)."""

    def both(self, *, created_by=None, as_staff=False):
        results = {}
        for holds, table in ((True, self.tables[0]), (False, self.tables[1])):
            with override_settings(DINING_ROUND_HOLDS_ENABLED=holds):
                order = self.draft(table=table, created_by=created_by)
                self.submitted(order, as_staff=as_staff)
            results[holds] = order
        return results[True], results[False]

    def test_a_diner_order_at_pay_after_is_held_only_with_holds_on(self):
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        on, off = self.both()
        placement = self.assert_held(on)
        self.assertEqual(placement.payment_timing, PAYMENT_TIMING_PAY_AFTER)
        self.assertEqual(placement.visit.payment_timing, PAYMENT_TIMING_PAY_AFTER)
        self.assertIsNone(placement.visit.opened_by_id)
        self.assert_released_at_acceptance(off)

    def test_a_staff_created_order_at_pay_after_is_released(self):
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        on, off = self.both(created_by=self.owner, as_staff=True)
        self.assert_released_at_acceptance(on)
        self.assert_released_at_acceptance(off)

    def test_staff_submitting_a_diners_draft_at_pay_after_is_held(self):
        """The hold follows the ORDER's creator (a diner), not the submitter."""
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        on, off = self.both(as_staff=True)
        self.assert_held(on)
        self.assertEqual(Order.objects.get(pk=on.pk).last_updated_by_id, self.owner.pk)
        self.assert_released_at_acceptance(off)

    def test_no_configuration_row_is_released(self):
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        on, off = self.both()
        self.assertEqual(self.assert_released_at_acceptance(on).payment_timing,
                         PAYMENT_TIMING_NOT_CHOSEN)
        self.assert_released_at_acceptance(off)

    def test_an_unset_axis_is_released(self):
        self.timing(None)
        on, off = self.both()
        self.assertEqual(self.assert_released_at_acceptance(on).payment_timing,
                         PAYMENT_TIMING_NOT_CHOSEN)
        self.assert_released_at_acceptance(off)

    def test_pay_first_is_released(self):
        """Pay-first is enforced only from Step 4; until then it releases at once."""
        self.timing(PAYMENT_TIMING_PAY_FIRST)
        on, off = self.both()
        self.assertEqual(self.assert_released_at_acceptance(on).payment_timing,
                         PAYMENT_TIMING_PAY_FIRST)
        self.assert_released_at_acceptance(off)

    @override_settings(DINING_VISIT_WRITES_ENABLED=False)
    def test_the_visit_writer_off_writes_no_placement_so_the_round_is_released(self):
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        on, off = self.both()
        for order in (on, off):
            self.assertIsNone(self.placement(order))
            self.assertEqual(self.read_round(order)['state'], 'released')
        self.assertIn(str(on.pk), self.feed_ids(ACTIVE_URL))


# --- a held round, end to end ----------------------------------------------------

@HOLDS_ON
class AHeldRoundTests(HoldFixture):

    def setUp(self):
        super().setUp()
        self.timing(PAYMENT_TIMING_PAY_AFTER)

    def held(self, table=None):
        order = self.draft(table=table)
        reply = self.submitted(order)
        self.assert_held(order)
        return order, reply

    def test_it_is_on_neither_kitchen_feed(self):
        order, _ = self.held()
        self.assertNotIn(str(order.pk), self.feed_ids(ACTIVE_URL))
        self.assertNotIn(str(order.pk), self.feed_ids(COMPLETED_URL))

    def test_every_kitchen_command_refuses_it_and_writes_nothing(self):
        order, _ = self.held()
        commands = (
            ('fulfilment-status', {'action': 'advance'}),
            ('priority', {'priority': True}),
            ('cancel', {'cancellation_reason':
                        CancellationReason_CustomerChangedMind}),
        )
        before = Order.objects.values().get(pk=order.pk)
        for path, body in commands:
            with self.subTest(command=path):
                response = self.staff().put(
                    f'/api/v1/kitchen/orders/{order.pk}/{path}/',
                    dict(body, if_revision=0), format='json')
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()['reason'], 'order_held')
        self.assertEqual(Order.objects.values().get(pk=order.pk), before)
        self.assertIsNone(self.placement(order).released_at)

    def test_it_is_on_the_floor_needing_staff(self):
        order, _ = self.held()
        table = self.floor_table(self.tables[0])
        self.assertTrue(table['occupied'])
        self.assertTrue(table['needs_staff'])
        [round_] = table['rounds']
        self.assertEqual(round_['order_id'], str(order.pk))
        self.assertEqual((round_['state'], round_['source']), ('held', 'diner'))
        self.assertIsNone(round_['released_at'])

    def test_it_occupies_its_table_so_a_second_diner_order_is_refused(self):
        order, _ = self.held()
        # A second draft at the same table cannot be created.
        refused = _create_order(
            restaurant=self.restaurant, table=self.tables[0],
            items=[{'item': str(self.item.pk), 'quantity': 1}])
        self.assertEqual(refused.get('status'), 400, refused)
        # The scan reports the table busy.
        self.assertTrue(self.scan(self.tables[0])['ongoing'])

    def test_a_draft_created_before_the_hold_cannot_be_submitted_over_it(self):
        first = self.draft()
        second = self.draft()
        self.submitted(first)
        self.assert_held(first)
        response = self.put_submit(second)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIsNone(self.placement(second))

    def test_confirm_makes_a_zero_minute_ticket(self):
        order, _ = self.held()
        self.assertNotIn(str(order.pk), self.feed_ids(ACTIVE_URL))
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['data']['changed'])
        placement = self.placement(order)
        self.assertIsNotNone(placement.released_at)
        self.assertEqual(placement.released_by_id, self.owner.pk)
        tickets = self.feed_ids(ACTIVE_URL)
        self.assertIn(str(order.pk), tickets)
        # Aged from the release, which is now: a 0-minute ticket.
        from datetime import datetime
        ticket_release = datetime.fromisoformat(
            tickets[str(order.pk)]['released_at'].replace('Z', '+00:00'))
        self.assertEqual(ticket_release, placement.released_at)
        self.assertLess(timezone.now() - ticket_release, timedelta(minutes=1))
        self.assertEqual(self.read_round(order)['state'], 'released')
        self.assertIs(self.scan(self.tables[0])['held'], False)

    def test_confirm_re_stamps_the_business_date(self):
        """Accepted at 03:50, confirmed at 04:10: the new business day."""
        accepted = _eat(2026, 11, 3, 3, 50)
        confirmed = _eat(2026, 11, 3, 4, 10)
        order = self.draft()
        Order.objects.filter(pk=order.pk).update(
            time_created=accepted - timedelta(minutes=1))
        order.refresh_from_db()
        with mock.patch('django.utils.timezone.now', return_value=accepted):
            self.submitted(order)
        placement = self.assert_held(order)
        self.assertEqual(str(placement.business_date), '2026-11-02')
        with mock.patch('django.utils.timezone.now', return_value=confirmed):
            response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)
        placement.refresh_from_db()
        self.assertEqual(placement.released_at, confirmed)
        self.assertEqual(str(placement.business_date), '2026-11-03')

    def test_decline_cancels_it_and_frees_the_table(self):
        order, _ = self.held()
        response = self.decline(order)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.order_status, OrderStatus_Cancelled)
        self.assertEqual(order.cancellation_reason, CancellationReason_DeclinedByStaff)
        self.assertIsNone(self.placement(order).released_at)
        self.assertNotIn(str(order.pk), self.feed_ids(ACTIVE_URL))
        self.assertNotIn(str(order.pk), self.feed_ids(COMPLETED_URL))
        self.assertEqual(self.read_round(order)['state'], 'declined')
        # The table is free: the scan shows no ongoing order, and a new diner order
        # at it is accepted (and held, being another diner's at pay-after).
        self.assertFalse(self.scan(self.tables[0])['ongoing'])
        another = self.draft()
        self.submitted(another)
        self.assert_held(another)


# --- what the diner is told ------------------------------------------------------

@HOLDS_ON
class TheDinerIsToldTests(HoldFixture):

    def setUp(self):
        super().setUp()
        self.timing(PAYMENT_TIMING_PAY_AFTER)

    def test_the_reply_the_read_a_replay_and_the_scan_all_say_held(self):
        order = self.draft()
        ref = quote_ref(order)
        first = self.submitted(order, ref=ref)
        accepted_at = self.accepted_at(order)
        expected = {'state': 'held', 'held_since': accepted_at.isoformat(),
                    'stale': False}
        # In the SAME response that accepted it.
        self.assertFalse(first['idempotent'])
        self.assertEqual(first['checkout']['current']['round'], expected)
        self.assertEqual(first['checkout']['current']['round']['held_since'],
                         first['checkout']['acceptance']['accepted_at'])
        # The order read, by both selectors.
        self.assertEqual(self.read_round(order), expected)
        self.assertEqual(
            self.read_round(order, intent=str(order.client_order_id)), expected)
        # A replay.
        replay = self.submitted(order, ref=ref)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['checkout']['current']['round'], expected)
        # The table scan.
        self.assertEqual(self.scan(self.tables[0]),
                         {'ongoing': True, 'order_id': str(order.pk), 'held': True})

    def test_a_staff_submitted_diner_draft_says_held_in_the_reply(self):
        order = self.draft()
        reply = self.submitted(order, as_staff=True)
        self.assertEqual(reply['checkout']['current']['round']['state'], 'held')


# --- no clock acts on a held round ---------------------------------------------

@HOLDS_ON
class NoTimerTests(HoldFixture):

    def test_a_day_later_it_is_still_held_and_only_flagged_stale(self):
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        order = self.draft()
        self.submitted(order)
        self.assert_held(order)
        later = timezone.now() + timedelta(hours=25)
        with mock.patch('django.utils.timezone.now', return_value=later):
            round_ = self.read_round(order)
            table = self.floor_table(self.tables[0])
            feed = self.feed_ids(ACTIVE_URL)
            scan = self.scan(self.tables[0])
        self.assertEqual(round_['state'], 'held')
        self.assertIs(round_['stale'], True)
        self.assertEqual(table['rounds'][0]['state'], 'held')
        self.assertIs(table['rounds'][0]['stale'], True)
        self.assertNotIn(str(order.pk), feed)
        self.assertIs(scan['held'], True)
        # Nothing was written by the passage of time.
        self.assert_held(order)
        self.assertEqual(Order.objects.get(pk=order.pk).order_status,
                         OrderStatus_Pending)


# --- W002 --------------------------------------------------------------------------

class HoldsCheckTests(HoldFixture):

    def test_the_warning_is_present_exactly_while_off(self):
        self.assertEqual(check_dining_round_holds(True), [])
        [message] = check_dining_round_holds(False)
        self.assertEqual(message.id, HOLDS_CHECK_ID)
        self.assertEqual(HOLDS_CHECK_ID, 'orders_app.W002')
        self.assertEqual(message.level, 30)  # WARNING, never an Error
        self.assertFalse(message.is_serious())
        self.assertEqual(
            message.msg,
            'DINING_ROUND_HOLDS_ENABLED is off: pay-after rounds are released at '
            'acceptance, not held for staff.')

    def test_the_registered_check_reads_the_live_setting(self):
        from django.core.checks import run_checks
        with HOLDS_OFF:
            ids = [m.id for m in run_checks()]
        self.assertIn(HOLDS_CHECK_ID, ids)
        with HOLDS_ON:
            ids = [m.id for m in run_checks()]
        self.assertNotIn(HOLDS_CHECK_ID, ids)
        # Independent of the visit writer's W001.
        self.assertNotIn(CHECK_ID, ids)

    def test_a_deploy_check_is_not_stopped_by_it(self):
        from django.core.management import call_command
        from io import StringIO
        with HOLDS_OFF:
            err = StringIO()
            call_command('check', stdout=StringIO(), stderr=err)
        self.assertIn('orders_app.W002', err.getvalue())


# --- cost --------------------------------------------------------------------------

class WhatHoldingCostsTests(HoldFixture):
    """The decision reads only what the writer already has: no query is added."""

    def setUp(self):
        super().setUp()
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        # Warm (content types, the first-request caches) outside every measurement.
        from orders_app.controllers.manage_order import update_order_status
        self.submit_now = lambda order, ref: update_order_status(
            order, OrderStatus_Pending, None, quote_ref=ref)
        warm = self.draft(table=self.tables[3])
        self.submit_now(warm, quote_ref(warm))

    def measure(self, order, ref):
        with CaptureQueriesContext(connection) as captured:
            result = self.submit_now(order, ref)
        self.assertEqual(result.get('status'), 200, result)
        return len(captured.captured_queries), result

    def test_a_submission_costs_18_with_holds_on_and_off(self):
        for holds, table, held in ((True, self.tables[0], True),
                                   (False, self.tables[1], False)):
            with self.subTest(holds=holds), \
                    override_settings(DINING_ROUND_HOLDS_ENABLED=holds):
                order = self.draft(table=table)
                ref = quote_ref(order)
                count, result = self.measure(order, ref)
                self.assertEqual(count, 18)
                self.assertEqual(
                    result['checkout']['current']['round']['state'],
                    'held' if held else 'released')

    @HOLDS_ON
    def test_a_replay_of_a_held_round_costs_7(self):
        order = self.draft()
        ref = quote_ref(order)
        self.submit_now(order, ref)
        self.assert_held(order)
        count, result = self.measure(order, ref)
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['checkout']['current']['round']['state'], 'held')
        self.assertEqual(count, 7)


# --- the business date of a held round is the acceptance's until Confirm -----------

@HOLDS_ON
class HeldBusinessDateTests(HoldFixture):

    def test_a_held_round_keeps_its_acceptance_business_day(self):
        self.timing(PAYMENT_TIMING_PAY_AFTER)
        accepted = _eat(2026, 11, 2, 3, 59, 59, 999999)
        order = self.draft()
        Order.objects.filter(pk=order.pk).update(
            time_created=accepted - timedelta(minutes=1))
        order.refresh_from_db()
        with mock.patch('django.utils.timezone.now', return_value=accepted):
            self.submitted(order)
        placement = self.assert_held(order)
        self.assertEqual(str(placement.business_date), '2026-11-01')
        self.assertEqual(Restaurant.objects.get(
            pk=self.restaurant.pk).business_day_cutoff, time(4, 0))
