"""
Step 2, PR BE-D — the two floor reads, built to floor contract v1.

``GET api/v1/floor/?restaurant=`` and ``GET api/v1/floor/tables/<id>/?restaurant=``.
Pinned here: the exact key sets of the contract; occupancy from
``occupancy.ongoing_order_q`` (never ``Table.status``); tables needing staff first;
``stale`` at 14:59 versus 15:00; ``waiting_for_staff`` (held rounds); an order with
no placement; money as a decimal STRING on the wire; no payment claim but
``"unknown"``; the drawer's items, extras, modifiers and ``cannot_be_made``; the
query counts, flat in the number of tables; and that nothing is clock-driven.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from commercial_app.models import RestaurantServiceConfiguration
from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Served,
    CancellationReason_CustomerChangedMind,
)
from orders_app.models import Order, OrderAcceptance, OrderPlacement
from orders_app.tests_round_release import (
    FLOOR_URL,
    FloorTestBase,
    ONION_OPTIONS,
    table_url,
)
from restaurants_app.models import MenuItem, Table

TOP_KEYS = {'contract', 'server_time', 'restaurant', 'waiting_for_staff', 'tables'}
RESTAURANT_KEYS = {'id', 'accepting_orders', 'payment_timing'}
TABLE_KEYS = {'id', 'number', 'display_name', 'area_id', 'status', 'occupied',
              'needs_staff', 'visit', 'rounds'}
VISIT_KEYS = {'id', 'opened_at', 'payment'}
# The contract's round, plus ONE additive key: `accepted_at_provenance`.
ROUND_KEYS = {'order_id', 'round_number', 'state', 'source', 'accepted_at',
              'accepted_at_provenance', 'held_since', 'released_at', 'stale',
              'fulfilment_status', 'item_count', 'value'}
DETAIL_ROUND_KEYS = ROUND_KEYS | {'items', 'cannot_be_made'}
ITEM_KEYS = {'name', 'quantity', 'extras', 'modifiers', 'note'}

#: Keys that would claim something about payment. None may appear anywhere.
PAYMENT_CLAIMS = {'is_paid', 'paid', 'current_check', 'balance', 'balance_payable',
                  'total_paid', 'payment_status', 'amount_paid', 'settled', 'owed'}

ISO_WITH_OFFSET = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$')


class _Floats(float):
    """A JSON float, marked, so a test can see money that left as a number."""


def _wire(response):
    """The body as a CLIENT parses it: every JSON float is marked."""
    return json.loads(response.content, parse_float=lambda s: _Floats(s))


def _walk(value, path=''):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield f'{path}.{key}', key, inner
            yield from _walk(inner, f'{path}.{key}')
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _walk(inner, f'{path}[{index}]')


class FloorReadBase(FloorTestBase):

    def floor(self, user=None):
        response = self.client_for(user or self.staff).get(
            FLOOR_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def drawer(self, table, user=None):
        response = self.client_for(user or self.staff).get(
            table_url(table.pk), {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def tables_of(self, response):
        return {t['id']: t for t in response.json()['data']['tables']}


class FloorShapeTests(FloorReadBase):

    def test_the_contract_key_sets(self):
        self.make_round(table=self.table1)
        body = self.floor().json()
        self.assertEqual(set(body), {'status', 'message', 'data'})
        data = body['data']
        self.assertEqual(set(data), TOP_KEYS)
        self.assertEqual(data['contract'], 'floor.v1')
        self.assertEqual(set(data['restaurant']), RESTAURANT_KEYS)
        for table in data['tables']:
            self.assertEqual(set(table), TABLE_KEYS)
            if table['visit'] is not None:
                self.assertEqual(set(table['visit']), VISIT_KEYS)
            for round_ in table['rounds']:
                self.assertEqual(set(round_), ROUND_KEYS)

    def test_every_time_carries_an_offset(self):
        self.make_round(table=self.table1)
        data = self.floor().json()['data']
        stamps = [data['server_time']]
        for table in data['tables']:
            if table['visit']:
                stamps.append(table['visit']['opened_at'])
            for round_ in table['rounds']:
                stamps += [round_['accepted_at'], round_['held_since']]
        for stamp in filter(None, stamps):
            with self.subTest(stamp=stamp):
                self.assertRegex(stamp, ISO_WITH_OFFSET)
                self.assertTrue(stamp.endswith('+03:00'))

    def test_a_held_round_is_as_the_contract_states(self):
        order = self.make_round(table=self.table1)
        table = self.tables_of(self.floor())[str(self.table1.pk)]
        self.assertTrue(table['occupied'])
        self.assertTrue(table['needs_staff'])
        self.assertEqual(table['visit']['payment'], 'unknown')
        self.assertEqual(table['display_name'], '1')
        self.assertEqual(table['area_id'], str(self.area.pk))
        round_ = table['rounds'][0]
        self.assertEqual(round_['order_id'], str(order.pk))
        self.assertEqual((round_['state'], round_['source'], round_['round_number']),
                         ('held', 'diner', 1))
        self.assertIsNotNone(round_['held_since'])
        self.assertEqual(round_['held_since'], round_['accepted_at'])
        self.assertIsNone(round_['released_at'])
        self.assertEqual(round_['accepted_at_provenance'], 'acceptance')
        self.assertEqual((round_['item_count'], round_['value']), (2, '16000.00'))

    def test_a_released_round(self):
        order = self.make_round(table=self.table1, held=False, created_by=self.staff)
        round_ = self.tables_of(self.floor())[str(self.table1.pk)]['rounds'][0]
        self.assertEqual((round_['state'], round_['source']), ('released', 'staff'))
        self.assertIsNone(round_['held_since'])
        self.assertFalse(round_['stale'])
        self.assertEqual(
            round_['released_at'],
            round_['accepted_at'],
            'Step 1 releases at the acceptance instant')
        self.assertEqual(round_['fulfilment_status'], 'new')
        self.assertEqual(round_['order_id'], str(order.pk))

    def test_the_display_name_falls_back_as_the_kitchen_label_does(self):
        Table.objects.filter(pk=self.table1.pk).update(display_name='Patio 1')
        Table.objects.filter(pk=self.table2.pk).update(str_number='2B')
        tables = self.tables_of(self.floor())
        self.assertEqual(tables[str(self.table1.pk)]['display_name'], 'Patio 1')
        self.assertEqual(tables[str(self.table2.pk)]['display_name'], '2B')
        self.assertEqual(tables[str(self.table3.pk)]['display_name'], '3')

    def test_restaurant_payment_timing_is_the_configured_value_or_null(self):
        self.assertIsNone(self.floor().json()['data']['restaurant']['payment_timing'])
        RestaurantServiceConfiguration.objects.create(
            restaurant=self.restaurant, payment_timing='pay_after',
            payment_timing_set_at=timezone.now(), payment_timing_set_by=self.owner)
        restaurant = self.floor().json()['data']['restaurant']
        self.assertEqual(restaurant, {'id': str(self.restaurant.pk),
                                      'accepting_orders': True,
                                      'payment_timing': 'pay_after'})


class OccupancyTests(FloorReadBase):
    """``occupied`` comes from ``ongoing_order_q``, never from ``Table.status``."""

    def test_occupancy_follows_the_orders_not_the_floor_axis(self):
        self.make_round(table=self.table1, held=False)
        Table.objects.filter(pk=self.table1.pk).update(status='available')
        Table.objects.filter(pk=self.table2.pk).update(status='seated')
        tables = self.tables_of(self.floor())
        self.assertEqual((tables[str(self.table1.pk)]['occupied'],
                          tables[str(self.table1.pk)]['status']), (True, 'available'))
        self.assertEqual((tables[str(self.table2.pk)]['occupied'],
                          tables[str(self.table2.pk)]['status']), (False, 'seated'))

    def test_served_cancelled_declined_draft_and_deleted_rounds_are_not_listed(self):
        self.make_round(table=self.table1, held=False, order_status=OrderStatus_Served,
                        fulfilment_status='served', served_at=timezone.now())
        self.make_round(table=self.table1, held=False,
                        order_status=OrderStatus_Cancelled,
                        cancellation_reason=CancellationReason_CustomerChangedMind)
        self.make_round(table=self.table1, placed=False, accepted=False,
                        order_status=OrderStatus_Initiated)
        self.make_round(table=self.table1, held=False, deleted=True)
        declined = self.make_round(table=self.table1)
        self.decline(declined)
        table = self.tables_of(self.floor())[str(self.table1.pk)]
        self.assertEqual((table['occupied'], table['rounds'], table['visit']),
                         (False, [], None))

    def test_an_order_with_no_placement_is_a_released_round_with_no_visit(self):
        order = self.make_round(table=self.table1, placed=False)
        table = self.tables_of(self.floor())[str(self.table1.pk)]
        self.assertTrue(table['occupied'])
        self.assertFalse(table['needs_staff'])
        self.assertIsNone(table['visit'])
        round_ = table['rounds'][0]
        self.assertEqual((round_['state'], round_['round_number'], round_['released_at']),
                         ('released', 1, None))
        self.assertEqual(round_['accepted_at_provenance'], 'acceptance')
        self.assertEqual(round_['order_id'], str(order.pk))

    def test_a_legacy_order_without_an_acceptance_row_uses_its_creation(self):
        order = self.make_round(table=self.table1, placed=False, accepted=False)
        round_ = self.tables_of(self.floor())[str(self.table1.pk)]['rounds'][0]
        self.assertEqual(round_['accepted_at_provenance'], 'order_created')
        created = Order.objects.get(pk=order.pk).time_created
        self.assertEqual(
            round_['accepted_at'],
            timezone.localtime(created).isoformat(timespec='milliseconds'))
        self.assertEqual(round_['item_count'], 2)

    def test_item_count_counts_available_main_dishes_by_quantity(self):
        order = self.make_round(table=self.table1, with_extra=True, quantity=3)
        main = order.order.get(parent_item__isnull=True)
        # A zeroed line and a deleted one are not part of what is being made.
        for over in ({'available': False, 'quantity': 0}, {'deleted': True}):
            row = order.order.create(
                item=self.dish, quantity=over.get('quantity', 4),
                available=over.get('available', True), deleted=over.get('deleted', False),
                unit_price=0, discounted_price=0, total_cost=0, discounted_cost=0,
                savings=0, actual_cost=0)
            self.assertIsNotNone(row.pk)
        round_ = self.tables_of(self.floor())[str(self.table1.pk)]['rounds'][0]
        self.assertEqual(round_['item_count'], 3)
        self.assertIsNotNone(main.pk)


class OrderingAndStaleTests(FloorReadBase):

    def test_tables_needing_staff_come_first_then_table_number(self):
        t5 = self._table(5)
        t4 = self._table(4)
        self.make_round(table=t5)
        self.make_round(table=self.table3)
        self.make_round(table=self.table1, held=False)
        numbers = [(t['number'], t['needs_staff'])
                   for t in self.floor().json()['data']['tables']]
        self.assertEqual(numbers, [(3, True), (5, True), (1, False), (2, False),
                                   (4, False)])
        self.assertIsNotNone(t4.pk)

    def test_waiting_for_staff_counts_held_rounds(self):
        self.make_round(table=self.table1)
        self.make_round(table=self.table2)
        self.make_round(table=self.table3, held=False)
        self.assertEqual(self.floor().json()['data']['waiting_for_staff'], 2)

    def test_stale_at_14_59_versus_15_00(self):
        accepted = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        self.make_round(table=self.table1, accepted_at=accepted)
        for elapsed, stale in ((timedelta(minutes=14, seconds=59), False),
                               (timedelta(minutes=15), True),
                               (timedelta(minutes=15, seconds=1), True)):
            with self.subTest(elapsed=str(elapsed)):
                with mock.patch('django.utils.timezone.now',
                                return_value=accepted + elapsed):
                    round_ = self.tables_of(self.floor())[str(self.table1.pk)]['rounds'][0]
                self.assertEqual(round_['stale'], stale)

    def test_nothing_is_clock_driven(self):
        """Past 24 hours the round is still held and only ``stale`` (and
        ``server_time``) moved. Nothing was written."""
        order = self.make_round(table=self.table1)
        before = self.snapshot(order)
        fresh = self.tables_of(self.floor())[str(self.table1.pk)]
        with mock.patch('django.utils.timezone.now',
                        return_value=timezone.now() + timedelta(hours=25)):
            later = self.tables_of(self.floor())[str(self.table1.pk)]
            drawer = self.drawer(self.table1).json()['data']
        self.assertEqual(self.snapshot(order), before)
        self.assertFalse(fresh['rounds'][0]['stale'])
        self.assertTrue(later['rounds'][0]['stale'])
        fresh['rounds'][0].pop('stale')
        later['rounds'][0].pop('stale')
        self.assertEqual(later, fresh)
        self.assertEqual(drawer['rounds'][0]['state'], 'held')


class NoPaymentClaimTests(FloorReadBase):

    def test_money_leaves_as_a_decimal_string(self):
        self.make_round(table=self.table1, actual_cost=Decimal('16000.50'))
        data = _wire(self.floor())['data']
        values = [r['value'] for t in data['tables'] for r in t['rounds']]
        self.assertEqual(values, ['16000.50'])
        floats = [path for path, _key, value in _walk(data) if isinstance(value, _Floats)]
        self.assertEqual(floats, [])

    def test_no_payment_claim_but_unknown_even_when_the_order_says_paid(self):
        """The guard: a visit's payment is the constant ``unknown``, read from no
        column. The order below claims everything a payment column can claim."""
        self.make_round(table=self.table1, held=False, payment_status='paid',
                        total_paid=Decimal('16000.00'), balance_payable=0)
        for response in (self.floor(), self.drawer(self.table1)):
            data = _wire(response)['data']
            claims = [path for path, key, _v in _walk(data) if key in PAYMENT_CLAIMS]
            self.assertEqual(claims, [])
            payments = [value for _path, key, value in _walk(data) if key == 'payment']
            self.assertTrue(payments)
            self.assertEqual(set(payments), {'unknown'})


class DrawerTests(FloorReadBase):

    def test_the_drawer_is_the_same_table_object_with_items(self):
        self.make_round(table=self.table1, with_extra=True)
        floor_table = self.tables_of(self.floor())[str(self.table1.pk)]
        data = self.drawer(self.table1).json()['data']
        self.assertEqual(set(data), TABLE_KEYS)
        round_ = data['rounds'][0]
        self.assertEqual(set(round_), DETAIL_ROUND_KEYS)
        stripped = dict(data, rounds=[{k: v for k, v in r.items()
                                       if k not in ('items', 'cannot_be_made')}
                                      for r in data['rounds']])
        self.assertEqual(stripped, floor_table)

    def test_items_are_the_saved_snapshots(self):
        MenuItem.objects.filter(pk=self.dish.pk).update(options=ONION_OPTIONS)
        self.make_round(table=self.table1, with_extra=True, modified=True)
        # A rename after acceptance does not rewrite what was ordered.
        MenuItem.objects.filter(pk=self.dish.pk).update(name='Rolex Deluxe')
        round_ = self.drawer(self.table1).json()['data']['rounds'][0]
        self.assertEqual(round_['items'], [{
            'name': 'Rolex', 'quantity': 2,
            'extras': [{'name': 'Avocado', 'quantity': 1}],
            'modifiers': ['Onion: No onion'],
            'note': None,
        }])
        self.assertEqual(set(round_['items'][0]), ITEM_KEYS)
        self.assertIsNone(round_['cannot_be_made'])

    def test_cannot_be_made_names_the_item_on_a_held_round(self):
        self.make_round(table=self.table1)
        self.make_round(table=self.table2, held=False)
        MenuItem.objects.filter(pk=self.dish.pk).update(in_stock=False)
        held = self.drawer(self.table1).json()['data']['rounds'][0]
        released = self.drawer(self.table2).json()['data']['rounds'][0]
        self.assertEqual(held['cannot_be_made'],
                         {'reason': 'item_sold_out', 'item_name': 'Rolex'})
        # Only a held round is checked: a released one is the kitchen's.
        self.assertIsNone(released['cannot_be_made'])

    def test_the_drawer_warns_about_exactly_what_confirm_refuses(self):
        order = self.make_round(table=self.table1)
        MenuItem.objects.filter(pk=self.dish.pk).update(deleted=True)
        warned = self.drawer(self.table1).json()['data']['rounds'][0]['cannot_be_made']
        refused = self.confirm(order).json()
        self.assertEqual(refused['cannot_be_made'], warned)

    def test_an_unpublished_item_is_not_a_warning(self):
        self.make_round(table=self.table1)
        MenuItem.objects.filter(pk=self.dish.pk).update(approved=False)
        self.assertIsNone(
            self.drawer(self.table1).json()['data']['rounds'][0]['cannot_be_made'])

    def test_an_empty_table(self):
        data = self.drawer(self.table2).json()['data']
        self.assertEqual((data['occupied'], data['rounds'], data['visit']),
                         (False, [], None))


class ReadPermissionTests(FloorReadBase):

    def test_the_tables_module_is_required(self):
        for url in (FLOOR_URL, table_url(self.table1.pk)):
            with self.subTest(url=url):
                response = self.client_for(self.kitchen).get(
                    url, {'restaurant': str(self.restaurant.pk)})
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json(), {'status': 404, 'message': 'Not found'})

    def test_another_restaurant_is_404(self):
        cases = [
            (self.other_owner, FLOOR_URL, self.restaurant.pk),
            (self.other_owner, table_url(self.table1.pk), self.restaurant.pk),
            # Their own restaurant, our table.
            (self.other_owner, table_url(self.table1.pk), self.other.pk),
            # Our restaurant, their table.
            (self.staff, table_url(self.other_table.pk), self.restaurant.pk),
        ]
        for user, url, restaurant in cases:
            with self.subTest(url=url, restaurant=str(restaurant)):
                response = self.client_for(user).get(url, {'restaurant': str(restaurant)})
                self.assertEqual(response.status_code, 404, response.content)

    def test_missing_malformed_and_unknown(self):
        client = self.client_for(self.staff)
        self.assertEqual(client.get(FLOOR_URL).status_code, 400)
        self.assertEqual(client.get(FLOOR_URL, {'restaurant': ''}).status_code, 400)
        self.assertEqual(client.get(FLOOR_URL, {'restaurant': 'nope'}).status_code, 404)
        self.assertEqual(client.get(
            FLOOR_URL, {'restaurant': '00000000-0000-0000-0000-000000000000'}
        ).status_code, 404)
        self.assertEqual(client.get(
            table_url('nope'), {'restaurant': str(self.restaurant.pk)}).status_code, 404)

    def test_a_soft_deleted_table_is_not_on_the_floor(self):
        Table.objects.filter(pk=self.table3.pk).update(deleted=True)
        self.assertNotIn(str(self.table3.pk), self.tables_of(self.floor()))
        response = self.client_for(self.staff).get(
            table_url(self.table3.pk), {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 404)

    def test_reads_are_no_store_and_write_nothing(self):
        self.make_round(table=self.table1)
        with CaptureQueriesContext(connection) as ctx:
            responses = [self.floor(), self.drawer(self.table1)]
        for response in responses:
            self.assertIn('no-store', response['Cache-Control'])
        writes = [q['sql'] for q in ctx.captured_queries
                  if not q['sql'].lstrip().upper().startswith('SELECT')]
        self.assertEqual(writes, [])


class ReadQueryCountTests(FloorReadBase):
    """EXACT, and FLAT in the number of tables and rounds.

    floor ...... 5  module gate 2, restaurant, tables, rounds (placement, visit and
                    acceptance by LEFT JOIN, item_count by scalar subquery)
    drawer ..... 6  module gate 2, table, rounds, lines, + 1 catalogue statement
                    per HELD round (its cannot_be_made check); 5 with none held
    """

    def _count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx.captured_queries)

    def test_the_floor_read_is_constant_in_the_number_of_tables(self):
        self.make_round(table=self.table1)
        small = self._count(self.floor)
        for number in range(4, 12):
            table = self._table(number)
            self.make_round(table=table, held=bool(number % 2), with_extra=True)
        large = self._count(self.floor)
        self.assertEqual(small, 5)
        self.assertEqual(large, 5)

    def test_the_drawer(self):
        self.make_round(table=self.table1, with_extra=True)
        self.assertEqual(self._count(lambda: self.drawer(self.table1)), 6)
        self.make_round(table=self.table2, held=False, with_extra=True)
        self.assertEqual(self._count(lambda: self.drawer(self.table2)), 5)

    def test_the_drawer_of_an_empty_table(self):
        self.assertEqual(self._count(lambda: self.drawer(self.table3)), 4)

    def test_accepted_rows_cost_nothing_extra(self):
        """The acceptance rides the rounds statement."""
        self.make_round(table=self.table1, accepted=False, placed=False)
        self.assertEqual(self._count(self.floor), 5)
        self.assertFalse(OrderAcceptance.objects.filter(
            order__table=self.table1).exists())
        self.assertFalse(OrderPlacement.objects.filter(
            order__table=self.table1).exists())
