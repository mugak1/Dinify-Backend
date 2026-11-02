"""
Occupancy has ONE definition, and every reader agrees with the predicate it replaced.

``orders_app.controllers.occupancy.ongoing_order_q`` is the definition. Three things
ask through it and must give the same answer for every order state:

- ``ConOrder.any_present_ongoing_order`` — the order-create, submit and recall gates
  and the table cards;
- ``initiate_order.any_present_ongoing_order`` — which now DELEGATES to ``ConOrder``
  instead of restating the query;
- ``round_placement.visit_state`` — a Step 1 visit is open exactly when its one
  order is ongoing, derived on read so the kitchen writes nothing.

The ORACLE is the predicate as it stood before #381 consolidated it, written out
literally rather than imported, so a change to the shared definition cannot pass by
changing the oracle with it. The matrix is every ``order_status`` (including the
unreachable ``refunded``) × every ``fulfilment_status`` × ``deleted``.
"""
import itertools
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled, OrderStatus_Initiated, OrderStatus_Paid,
    OrderStatus_Pending, OrderStatus_Preparing, OrderStatus_Refunded,
    OrderStatus_Served, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers import initiate_order
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.occupancy import ongoing_order_q
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.round_placement import (
    VISIT_ENDED, VISIT_OPEN, open_visits_q, visit_state,
)
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN, Bill, Order, OrderPlacement, Visit,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

ORDER_STATUSES = (
    OrderStatus_Initiated, OrderStatus_Pending, OrderStatus_Preparing,
    OrderStatus_Served, OrderStatus_Paid, OrderStatus_Refunded, OrderStatus_Cancelled,
)
FULFILMENT_STATUSES = ('new', 'preparing', 'ready', 'served')


def _oracle(table):
    """The pre-#381 predicate, literally."""
    return (
        Order.objects
        .filter(table=table, deleted=False)
        .exclude(order_status=OrderStatus_Initiated)
        .exclude(order_status=OrderStatus_Cancelled)
        .exclude(fulfilment_status='served')
        .exists()
    )


class OccupancyEquivalenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            first_name='Occ', last_name='Owner', email='occ@test.com',
            phone_number='256700770001', username='256700770001', country='Uganda',
            password='password', roles=[],
        )
        restaurant = Restaurant.objects.create(
            name='Occupancy', location='Kampala', owner=owner,
            status=RestaurantStatus_Live)
        RestaurantEmployee.objects.create(
            user=owner, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True)
        area = DiningArea.objects.create(name='Main', restaurant=restaurant)
        cls.table = Table.objects.create(
            number=1, restaurant=restaurant, dining_area=area, enabled=True,
            is_active=True, qr_mode='order_pay', has_qr=True)
        section = MenuSection.objects.create(
            name='Mains', restaurant=restaurant, approved=True, enabled=True,
            available=True)
        item = MenuItem.objects.create(
            name='Chapati', section=section, primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True, in_stock=True)
        result = _create_order(restaurant=restaurant, table=cls.table,
                               items=[{'item': str(item.pk), 'quantity': 1}])
        cls.order = result['order']
        visit = Visit.objects.create(
            restaurant=restaurant, table=cls.table, opened_at=cls.order.time_created,
            payment_timing=PAYMENT_TIMING_NOT_CHOSEN)
        bill = Bill.objects.create(visit=visit)
        OrderPlacement.objects.create(
            order=cls.order, visit=visit, bill=bill,
            payment_timing=PAYMENT_TIMING_NOT_CHOSEN,
            released_at=cls.order.time_created,
            business_date=cls.order.time_created.date(), round_number=1)
        cls.visit = visit

    def test_every_reader_agrees_with_the_old_predicate(self):
        seen = {True: 0, False: 0}
        for status, fulfilment, deleted in itertools.product(
                ORDER_STATUSES, FULFILMENT_STATUSES, (False, True)):
            with self.subTest(order_status=status, fulfilment=fulfilment,
                              deleted=deleted):
                Order.objects.filter(pk=self.order.pk).update(
                    order_status=status, fulfilment_status=fulfilment,
                    deleted=deleted)
                expected = _oracle(self.table)
                seen[expected] += 1
                self.assertEqual(
                    Order.objects.filter(ongoing_order_q(), table=self.table)
                    .exists(), expected)
                self.assertEqual(
                    ConOrder.any_present_ongoing_order(self.table)['present'],
                    expected)
                self.assertEqual(
                    initiate_order.any_present_ongoing_order(self.table)['present'],
                    expected)
                self.assertEqual(
                    visit_state(self.visit)['state'],
                    VISIT_OPEN if expected else VISIT_ENDED)
                self.assertEqual(
                    Visit.objects.filter(open_visits_q(), pk=self.visit.pk).exists(),
                    expected)
        # The matrix exercises both answers, and the open set is exactly
        # {pending, preparing, served, paid, refunded} x {new, preparing, ready},
        # not deleted.
        self.assertEqual(seen[True], 5 * 3)
        self.assertEqual(seen[False], 7 * 4 * 2 - 15)

    def test_both_callers_return_the_same_order(self):
        Order.objects.filter(pk=self.order.pk).update(
            order_status=OrderStatus_Pending, fulfilment_status='new', deleted=False)
        self.assertEqual(
            ConOrder.any_present_ongoing_order(self.table),
            initiate_order.any_present_ongoing_order(self.table))
        self.assertEqual(
            initiate_order.any_present_ongoing_order(self.table),
            {'present': True, 'order_id': self.order.pk})

    def test_a_null_fulfilment_status_is_unreachable(self):
        """The one input the oracle and ``~Q(...)`` could treat differently (SQL
        three-valued logic on NULL) cannot exist: the column is NOT NULL."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.filter(pk=self.order.pk).update(fulfilment_status=None)
