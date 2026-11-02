"""
The ``dashboard-v2`` tables card counts OCCUPIED tables by the canonical predicate
(DASH-OCCUPANCY-00).

``_build_tables`` used to restate occupancy for itself::

    Order.objects.filter(restaurant=..., payment_status='pending')
         .exclude(order_status__in=[initiated, cancelled, refunded])

while its own comment said the card "must agree with the occupancy helpers and the
table cards". It did not, in three ways:

1. it never excluded a SERVED order. Nothing in production moves ``payment_status``
   off ``pending`` (diner payment is not wired), so every order ever placed and not
   cancelled went on "occupying" its table forever;
2. it counted SOFT-DELETED orders;
3. it keyed on ``payment_status``, which occupancy must not consult (PR #186) — so
   an order marked paid but still being prepared vanished from the card.

The card now asks through ``orders_app.controllers.occupancy.ongoing_order_q``, the
same ``Q`` both ``any_present_ongoing_order`` copies use. Tests named
``THE_REGRESSION`` fail on the pre-change card; ``CONTROL`` tests pass on both.

Orders are written with ``Order.objects.create`` / ``.update`` because the question
is how the CARD reads a given row, whatever wrote it. ``payment_status`` is left at
its ``pending`` default throughout unless a test says otherwise, because that is the
only value production writes.
"""
import inspect
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Paid,
    OrderStatus_Pending,
    OrderStatus_Served,
    PaymentStatus_Paid,
    PaymentStatus_Pending,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from orders_app.controllers import initiate_order as standalone
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.occupancy import ongoing_order_q
from orders_app.models import Order
from reports_app.controllers.restaurant import dashboard
from reports_app.controllers.restaurant.dashboard import (
    _build_tables,
    generate_restaurant_dashboard_v2,
)
from restaurants_app.models import Restaurant, RestaurantEmployee, Table
from users_app.models import User


def _user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password', roles=[],
    )


class _FloorFixture(TestCase):
    """One live restaurant with six enabled tables and no orders yet."""

    def setUp(self):
        super().setUp()
        self.owner = _user('256700000731')
        self.restaurant = Restaurant.objects.create(
            name='Occupancy Grill', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.tables = [
            Table.objects.create(number=n, restaurant=self.restaurant, enabled=True)
            for n in range(1, 7)
        ]

    def order(self, table, *, order_status=OrderStatus_Pending,
              fulfilment_status='new', payment_status=PaymentStatus_Pending,
              deleted=False, is_test=False, restaurant=None):
        return Order.objects.create(
            restaurant=restaurant or self.restaurant, table=table,
            order_status=order_status, fulfilment_status=fulfilment_status,
            payment_status=payment_status, deleted=deleted, is_test=is_test,
            total_cost=Decimal('1000.00'), discounted_cost=Decimal('1000.00'),
            savings=Decimal('0.00'), actual_cost=Decimal('1000.00'),
        )

    def served(self, table, **kwargs):
        """Served the way the kitchen serves: both axes move, payment does not."""
        return self.order(table, order_status=OrderStatus_Served,
                          fulfilment_status='served', **kwargs)

    def occupied(self):
        return _build_tables(self.restaurant.id)['occupied']

    def helpers_say(self):
        """How many of this restaurant's tables each per-table helper calls occupied."""
        con = sum(
            1 for t in Table.objects.filter(restaurant=self.restaurant)
            if ConOrder.any_present_ongoing_order(t)['present']
        )
        twin = sum(
            1 for t in Table.objects.filter(restaurant=self.restaurant)
            if standalone.any_present_ongoing_order(t)['present']
        )
        return con, twin


class ServedOrdersFreeTheirTableTests(_FloorFixture):

    def test_THE_REGRESSION_a_served_order_frees_its_table_on_the_card(self):
        self.order(self.tables[0])
        self.served(self.tables[1])
        # Old card: 2 — the served order still had payment_status 'pending'.
        self.assertEqual(self.occupied(), 1)

    def test_THE_REGRESSION_serving_the_only_order_empties_the_floor(self):
        order = self.order(self.tables[0])
        self.assertEqual(self.occupied(), 1)
        Order.objects.filter(pk=order.pk).update(
            order_status=OrderStatus_Served, fulfilment_status='served',
        )
        self.assertEqual(self.occupied(), 0)
        self.assertEqual(_build_tables(self.restaurant.id)['occupancy_pct'], '0.0')

    def test_THE_REGRESSION_a_table_that_was_served_and_reseated_counts_once(self):
        # A day's history on one table: three served visits, then a fourth diner.
        for _ in range(3):
            self.served(self.tables[0])
        self.assertEqual(self.occupied(), 0)
        self.order(self.tables[0])
        self.assertEqual(self.occupied(), 1)

    def test_THE_REGRESSION_payment_status_is_not_consulted(self):
        # Marked paid, still in the kitchen: the diner is still at the table. The old
        # card dropped it, because it only looked at payment_status='pending'.
        self.order(self.tables[0], order_status=OrderStatus_Paid,
                   payment_status=PaymentStatus_Paid, fulfilment_status='preparing')
        self.assertEqual(self.occupied(), 1)

    def test_CONTROL_every_unserved_fulfilment_state_occupies(self):
        for table, state in zip(self.tables, ('new', 'preparing', 'ready')):
            self.order(table, fulfilment_status=state)
        self.assertEqual(self.occupied(), 3)


class SoftDeletedOrdersDoNotCountTests(_FloorFixture):

    def test_THE_REGRESSION_a_soft_deleted_order_does_not_occupy(self):
        self.order(self.tables[0], deleted=True)
        # Old card: 1.
        self.assertEqual(self.occupied(), 0)

    def test_THE_REGRESSION_a_deleted_order_beside_a_live_one_counts_the_table_once(self):
        self.order(self.tables[0], deleted=True)
        self.order(self.tables[1], deleted=True)
        self.order(self.tables[1])
        self.assertEqual(self.occupied(), 1)


class WhatStillDoesNotOccupyTests(_FloorFixture):
    """CONTROLS — the old card and the new one agree on all of these."""

    def test_CONTROL_a_draft_does_not_occupy(self):
        self.order(self.tables[0], order_status=OrderStatus_Initiated)
        self.assertEqual(self.occupied(), 0)

    def test_CONTROL_a_cancelled_order_does_not_occupy(self):
        self.order(self.tables[0], order_status=OrderStatus_Cancelled)
        self.assertEqual(self.occupied(), 0)

    def test_CONTROL_two_ongoing_orders_on_one_table_are_one_table(self):
        self.order(self.tables[0])
        self.order(self.tables[0], fulfilment_status='ready')
        self.assertEqual(self.occupied(), 1)

    def test_CONTROL_another_restaurants_floor_is_not_counted(self):
        other_owner = _user('256700000732')
        other = Restaurant.objects.create(
            name='Elsewhere', location='Entebbe', owner=other_owner,
            status=RestaurantStatus_Live,
        )
        other_table = Table.objects.create(number=1, restaurant=other, enabled=True)
        self.order(other_table, restaurant=other)
        self.assertEqual(self.occupied(), 0)

    def test_CONTROL_occupancy_pct_is_derived_from_the_same_count(self):
        self.order(self.tables[0])
        self.order(self.tables[1])
        self.order(self.tables[2])
        card = _build_tables(self.restaurant.id)
        self.assertEqual(card['total'], 6)
        self.assertEqual(card['occupied'], 3)
        self.assertEqual(card['occupancy_pct'], '50.0')


class TestOrdersStillOccupyTests(_FloorFixture):
    """LIVE FLOOR STATE: ``counted_orders_q`` deliberately does not apply here."""

    def test_CONTROL_a_practice_order_at_a_real_restaurant_occupies_its_table(self):
        # A flagged order at a restaurant that is NOT a test restaurant — exactly the
        # order `counted_orders_q` leaves out of the figures. It still has a diner.
        self.order(self.tables[0], is_test=True)
        self.assertEqual(self.occupied(), 1)

    def test_CONTROL_a_test_restaurants_order_occupies_its_table(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)
        self.order(self.tables[0], is_test=True)
        self.assertEqual(self.occupied(), 1)


class TheCardAgreesWithTheHelpersTests(_FloorFixture):
    """One fixture, three readers, one answer."""

    def mixed_floor(self):
        t = self.tables
        self.order(t[0])                                          # ongoing
        self.order(t[0], fulfilment_status='preparing')           # ...twice
        self.served(t[1])                                         # freed
        self.order(t[2], deleted=True)                            # deleted
        self.order(t[3], order_status=OrderStatus_Initiated)      # draft
        self.order(t[3], order_status=OrderStatus_Cancelled)      # cancelled
        self.order(t[4], is_test=True, fulfilment_status='ready')  # practice, ongoing
        self.order(t[5], order_status=OrderStatus_Paid,           # paid, still cooking
                   payment_status=PaymentStatus_Paid, fulfilment_status='new')
        self.served(t[5], payment_status=PaymentStatus_Paid)

    def test_the_card_equals_both_per_table_helpers(self):
        self.mixed_floor()
        con, twin = self.helpers_say()
        self.assertEqual(con, 3)          # tables 1, 5 and 6
        self.assertEqual(twin, con)
        self.assertEqual(self.occupied(), con)

    def test_the_card_equals_the_helpers_on_an_empty_floor(self):
        self.assertEqual(self.helpers_say(), (0, 0))
        self.assertEqual(self.occupied(), 0)

    def test_the_card_equals_the_helpers_after_every_table_is_served(self):
        self.mixed_floor()
        Order.objects.filter(restaurant=self.restaurant).update(
            order_status=OrderStatus_Served, fulfilment_status='served',
        )
        self.assertEqual(self.helpers_say(), (0, 0))
        self.assertEqual(self.occupied(), 0)

    def test_the_published_response_carries_the_same_figure(self):
        self.mixed_floor()
        result = generate_restaurant_dashboard_v2(
            restaurant_id=str(self.restaurant.id),
            date_from='2024-01-01', date_to='2024-01-02', bucket='day',
        )
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(result['data']['tables']['occupied'], self.helpers_say()[0])


class OneDefinitionTests(TestCase):
    """The card and both helpers ask through the SAME ``Q``, not three copies of it."""

    def test_every_reader_names_the_shared_predicate(self):
        for reader in (
            ConOrder.any_present_ongoing_order,
            dashboard._build_tables,
        ):
            with self.subTest(reader=reader.__qualname__):
                self.assertIn('ongoing_order_q()', inspect.getsource(reader))

    def test_the_standalone_twin_delegates_rather_than_restating(self):
        """Dining model Step 1 made the twin DELEGATE to ``ConOrder`` instead of
        repeating the query beside it: one function answering, not two copies of
        the same filter. It must name no query of its own."""
        source = inspect.getsource(standalone.any_present_ongoing_order)
        self.assertIn('ConOrder.any_present_ongoing_order(table)', source)
        self.assertNotIn('Order.objects', source)
        self.assertNotIn('.filter(', source)

    def test_the_card_no_longer_reads_the_payment_axis_for_occupancy(self):
        source = inspect.getsource(dashboard._build_tables)
        occupancy = source[:source.index('occupancy_pct')]
        code = '\n'.join(
            line for line in occupancy.splitlines()
            if not line.strip().startswith('#')
        )
        self.assertNotIn('payment_status', code)
        self.assertNotIn('PaymentStatus', code)

    def test_the_predicate_reads_exactly_the_documented_columns(self):
        sql = str(Order.objects.filter(ongoing_order_q()).query)
        where = sql[sql.index(' WHERE '):]
        for column in ('"deleted"', '"order_status"', '"fulfilment_status"'):
            self.assertIn(column, where)
        for value in ('initiated', 'cancelled', 'served'):
            self.assertIn(value, where)
        self.assertNotIn('payment_status', where)
        self.assertNotIn('is_test', where)


class TablesCardQueryCountTests(_FloorFixture):
    """Occupancy is still ONE statement, flat in the number of orders and tables."""

    def count(self):
        with CaptureQueriesContext(connection) as ctx:
            _build_tables(self.restaurant.id)
        return len(ctx)

    def test_the_card_costs_the_same_however_busy_the_floor(self):
        empty = self.count()
        for table in self.tables:
            self.order(table)
            self.served(table)
        self.assertEqual(self.count(), empty)

    def test_the_card_runs_seven_queries(self):
        # total tables, occupied, today's closed orders (iterated), their count,
        # yesterday's closed count, today's average ticket, yesterday's average.
        self.assertEqual(self.count(), 7)
