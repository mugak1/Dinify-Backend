"""
A TEST RESTAURANT CAN DO EVERYTHING A LIVE RESTAURANT CAN.

A test restaurant (``Restaurant.is_test``) exists so somebody can check that everything
a live restaurant does actually works. Its orders are still FLAGGED as test orders
(``Order.is_test``) — that label is useful and stays — but the flag must never switch
anything off there. It used to: every consumer filtered ``is_test=False``, so a test
restaurant's orders could not be reviewed and were missing from its own reports, both
dashboards, the transactions report and customer matching. Reviews were where it
showed, being the one real-wired surface that refused outright ("This order is not
eligible for review.").

THE RULE, stated once in ``orders_app.controllers.test_orders``: the only orders left
out anywhere are PRACTICE orders — test orders at a restaurant that is NOT a test
restaurant (a rehearsal a real restaurant ran before it went live, or an order from a
restaurant's time as a test restaurant). ``tests_launch_boundary`` still pins that
half, one consumer at a time; this suite pins the other half the same way.

EVERY CONSUMER TEST IS A COMPARISON. Two restaurants built identically — one real, one
test, both live — take the same order, and each consumer must give both the SAME
answer. A comparison can pass by both sides being empty, so each also asserts the test
restaurant's own figure is non-zero: a consumer that dropped both would still fail.
"""
import ast
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    OrderStatus_Paid,
    OrderStatus_Served,
    PaymentStatus_Paid,
    RestaurantStatus_Live,
    RESTAURANT_OWNER,
    TransactionStatus_Success,
    TransactionType_OrderPayment,
)
from finance_app.models import DinifyTransaction
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.test_orders import counted_orders_q, is_practice_order
from orders_app.models import Order
from orders_app.tests_launch_boundary import _user
from reports_app.controllers.common.sale_filters import sale_orders
from reports_app.controllers.restaurant.dashboard import (
    generate_restaurant_dashboard_details,
    generate_restaurant_dashboard_v2,
    summarize_revenue,
)
from reports_app.controllers.restaurant.transactions import (
    generate_restaurant_transaction_summary,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from reviews_app.controllers.submit_review import submit_review
from reviews_app.models import Review


PRICE = Decimal('10000')


class _Venue:
    """One live restaurant with a table and an orderable item. Built identically twice."""

    def __init__(self, *, name, phone, is_test):
        self.owner = _user(phone)
        self.restaurant = Restaurant.objects.create(
            name=name, location='Kampala', owner=self.owner, country='UG',
            status=RestaurantStatus_Live, is_test=is_test,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=area,
            enabled=True, is_active=True, qr_mode='order_only', has_qr=True,
        )
        section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=section, primary_price=PRICE,
            approved=True, enabled=True, available=True, in_stock=True,
        )

    @property
    def id(self):
        return str(self.restaurant.id)

    def completed_order(self, test_case):
        """A diner order placed the ordinary way, then served and settled."""
        response = ConOrder.initiate_order(
            restaurant_id=self.id,
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=None,
        )
        test_case.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        Order.objects.filter(pk=order.pk).update(
            order_status=OrderStatus_Served,
            payment_status=PaymentStatus_Paid,
            fulfilment_status='served',
            served_at=timezone.now(),
            total_cost=PRICE, discounted_cost=PRICE,
            actual_cost=PRICE, savings=Decimal('0'),
        )
        order.refresh_from_db()
        return order

    def pay(self, order):
        return DinifyTransaction.objects.create(
            restaurant=self.restaurant, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_amount=PRICE, payment_mode='cash',
        )


class _ParityFixture(TestCase):
    def setUp(self):
        super().setUp()
        self.real = _Venue(name='Real Grill', phone='256770000021', is_test=False)
        self.test = _Venue(name='Test Grill', phone='256770000022', is_test=True)
        self.real_order = self.real.completed_order(self)
        self.test_order = self.test.completed_order(self)
        today = timezone.localdate()
        self.window = (today - timedelta(days=1), today + timedelta(days=1))

    def _dates(self):
        return {'date_from': str(self.window[0]), 'date_to': str(self.window[1])}

    def _v2(self, venue):
        return generate_restaurant_dashboard_v2(
            venue.id, **self._dates(), bucket='day')['data']


class TestRestaurantParityTests(_ParityFixture):
    """One test per consumer — each builds its own queryset, so each can drift alone."""

    def test_the_test_restaurants_orders_are_still_flagged_test(self):
        """The label stays. Flagged is all it means."""
        self.assertTrue(self.test_order.is_test)
        self.assertFalse(self.real_order.is_test)

    # 1 — the chokepoint every Order-based report derives from (sales, diners, menu)
    def test_sales_diners_and_menu_reports_count_it(self):
        real = set(sale_orders(self.real.id, *self.window).values_list('id', flat=True))
        test = set(sale_orders(self.test.id, *self.window).values_list('id', flat=True))
        self.assertEqual(real, {self.real_order.id})
        self.assertEqual(test, {self.test_order.id})

    # 2 — dashboard v1: every figure, compared whole
    def test_dashboard_v1_matches_a_live_restaurant(self):
        real = generate_restaurant_dashboard_details(self.real.id, **self._dates())['data']
        test = generate_restaurant_dashboard_details(self.test.id, **self._dates())['data']
        self.assertEqual(test['num_sales'], 1)
        self.assertEqual(Decimal(str(test['sales_amount'])), PRICE)
        self.assertEqual(test, real)

    # 3 — the all-time revenue helper
    def test_summarize_revenue_matches_a_live_restaurant(self):
        real = summarize_revenue(self.real.id)
        test = summarize_revenue(self.test.id)
        self.assertEqual(Decimal(str(test['total'])), PRICE)
        self.assertEqual(test, real)

    # 4 — dashboard v2: revenue, orders, popular items
    def test_dashboard_v2_revenue_orders_and_items_match_a_live_restaurant(self):
        real, test = self._v2(self.real), self._v2(self.test)
        self.assertEqual(Decimal(str(test['revenue']['totals']['gross'])), PRICE)
        self.assertEqual(test['revenue']['totals'], real['revenue']['totals'])
        self.assertEqual(test['revenue']['series'], real['revenue']['series'])
        self.assertEqual(test['orders']['total'], 1)
        self.assertEqual(test['orders'], real['orders'])
        self.assertEqual(
            [row['qty'] for row in test['popular_items']],
            [row['qty'] for row in real['popular_items']],
        )
        self.assertEqual(sum(row['qty'] for row in test['popular_items']), 1)

    # 5 — dashboard v2: payment methods (a TRANSACTION queryset joined through order)
    def test_dashboard_v2_payment_methods_match_a_live_restaurant(self):
        self.real.pay(self.real_order)
        self.test.pay(self.test_order)
        real, test = self._v2(self.real), self._v2(self.test)
        self.assertEqual(
            sum(Decimal(str(row['amount'])) for row in test['payment_methods']), PRICE,
        )
        self.assertEqual(test['payment_methods'], real['payment_methods'])

    # 6 — dashboard v2: the tables card's history metrics (turns, visit, ticket)
    def test_dashboard_v2_tables_card_matches_a_live_restaurant(self):
        real, test = self._v2(self.real), self._v2(self.test)
        self.assertEqual(Decimal(str(test['tables']['turns_today'])), Decimal('1'))
        self.assertEqual(test['tables'], real['tables'])

    # 7 — the transactions report (scoped by restaurant, NOT through the order FK)
    def test_transactions_report_matches_a_live_restaurant(self):
        self.real.pay(self.real_order)
        self.test.pay(self.test_order)
        real = generate_restaurant_transaction_summary(self.real.id, **self._dates())['data']
        test = generate_restaurant_transaction_summary(self.test.id, **self._dates())['data']
        self.assertEqual(test['total_transactions'], 1)
        self.assertEqual(test, real)

    # 8 — customer matching
    def test_customer_matching_treats_it_like_a_live_restaurant(self):
        # Keyed on the PAYMENT's phone number, which is the path the command matches
        # on (it does not read the order's own `customer_phone`; that is a separate,
        # pre-existing gap that affects real and test restaurants alike).
        for venue, order, phone in ((self.real, self.real_order, '256770009991'),
                                    (self.test, self.test_order, '256770009992')):
            Order.objects.filter(pk=order.pk).update(
                customer=None, customer_match_attempted=False,
                customer_phone=None, customer_email=None,
            )
            payment = venue.pay(order)
            DinifyTransaction.objects.filter(pk=payment.pk).update(msisdn=phone)
        call_command('determine-customers', stdout=open(os.devnull, 'w'))
        for order in (self.real_order, self.test_order):
            order.refresh_from_db()
            self.assertTrue(order.customer_match_attempted, order.restaurant.name)
            self.assertIsNotNone(order.customer_id, order.restaurant.name)

    # 9 — reviews: the one that was visible
    def test_a_test_restaurants_order_can_be_reviewed(self):
        for venue, order in ((self.real, self.real_order), (self.test, self.test_order)):
            response = submit_review(
                order_id=str(order.id),
                rating_fields={'overall_rating': 5, 'food_rating': 4},
                comment='Noice',
                tags=['great_flavour'],
                session_restaurant_id=venue.id,
                session_table_id=str(venue.table.id),
            )
            self.assertEqual(response['status'], 201, (venue.restaurant.name, response))
        self.assertEqual(Review.objects.filter(restaurant=self.test.restaurant).count(), 1)

    def test_a_reviewed_test_order_still_answers_already_reviewed(self):
        """Parity includes the refusals a live restaurant gives: one review per order."""
        kwargs = dict(
            order_id=str(self.test_order.id),
            rating_fields={'overall_rating': 5},
            session_restaurant_id=self.test.id,
            session_table_id=str(self.test.table.id),
        )
        self.assertEqual(submit_review(**kwargs)['status'], 201)
        self.assertEqual(submit_review(**kwargs)['status'], 409)


class PracticeOrderRuleTests(_ParityFixture):
    """The half that stays: a test order at a REAL restaurant is still left out."""

    def _practice_order(self):
        """A test order at the real restaurant — e.g. a pre-go-live rehearsal."""
        Order.objects.filter(pk=self.real_order.pk).update(is_test=True)
        self.real_order.refresh_from_db()
        return self.real_order

    def test_the_rule_truth_table(self):
        cases = {
            # (order flagged test, restaurant is a test restaurant): counts?
            (False, False): True,
            (False, True): True,
            (True, True): True,     # a test restaurant's order: counts
            (True, False): False,   # a PRACTICE order: left out
        }
        for (order_test, restaurant_test), counts in cases.items():
            with self.subTest(order_test=order_test, restaurant_test=restaurant_test):
                venue = self.test if restaurant_test else self.real
                order = self.test_order if restaurant_test else self.real_order
                Order.objects.filter(pk=order.pk).update(is_test=order_test)
                order = Order.objects.select_related('restaurant').get(pk=order.pk)
                self.assertEqual(is_practice_order(order), not counts)
                in_q = Order.objects.filter(counted_orders_q(), pk=order.pk).exists()
                self.assertEqual(in_q, counts)
                self.assertEqual(venue.restaurant.is_test, restaurant_test)

    def test_a_practice_order_is_still_left_out_of_reports_and_reviews(self):
        practice = self._practice_order()
        self.assertNotIn(
            practice.id,
            set(sale_orders(self.real.id, *self.window).values_list('id', flat=True)),
        )
        response = submit_review(
            order_id=str(practice.id),
            rating_fields={'overall_rating': 5},
            session_restaurant_id=self.real.id,
            session_table_id=str(self.real.table.id),
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], 'This order is not eligible for review.')

    def test_switching_a_test_restaurant_to_real_takes_its_test_orders_out(self):
        """
        The documented consequence of reclassifying. A demo restaurant that becomes a
        real customer keeps its demo orders flagged, and once it is real they are
        practice orders — out of its figures. Switching back brings them back.
        """
        def counted():
            return set(sale_orders(self.test.id, *self.window).values_list('id', flat=True))

        self.assertEqual(counted(), {self.test_order.id})
        Restaurant.objects.filter(pk=self.test.restaurant.pk).update(is_test=False)
        self.assertEqual(counted(), set())
        Restaurant.objects.filter(pk=self.test.restaurant.pk).update(is_test=True)
        self.assertEqual(counted(), {self.test_order.id})

    def test_the_rule_costs_no_query(self):
        """It joins the restaurant inside the same statement — never a second one."""
        with self.assertNumQueries(1):
            list(sale_orders(self.test.id, *self.window))


# --- the ratchet: no consumer may switch a test restaurant off again ---------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PRUNE_DIRS = frozenset({
    'migrations', '__pycache__', 'node_modules', 'site-packages',
    'venv', 'env', 'staticfiles', 'media',
})
# The one module entitled to spell the flag out: it DEFINES the rule.
RULE_MODULE = 'orders_app/controllers/test_orders.py'
FILTER_METHODS = frozenset({'filter', 'get', 'get_or_create', 'count', 'exists'})


def _production_modules():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith('.')
        ]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            if filename.startswith('test') or '/tests' in dirpath:
                continue
            path = Path(dirpath) / filename
            yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(
                encoding='utf-8', errors='replace',
            )


def _is_test_keyword(name):
    return name is not None and (
        name in ('is_test', 'is_test__exact')
        or name.endswith('__is_test')
        or name.endswith('__is_test__exact')
    )


def _bare_test_filters(source):
    """
    Every ``is_test=False`` in a filter (or ``is_test=True`` in an exclude).

    An AST walk over CALLS, so the many comments and docstrings that discuss the flag
    are not false positives, and so a model default or an order being CREATED with the
    flag (``Order(is_test=...)``) is not mistaken for a filter.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else None
        )
        for keyword in node.keywords:
            if not _is_test_keyword(keyword.arg):
                continue
            value = keyword.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, bool):
                continue
            excluding = (
                (name in FILTER_METHODS or name == 'Q') and value.value is False
            ) or (name == 'exclude' and value.value is True)
            if excluding:
                hits.append(node.lineno)
    return hits


class NoBareTestOrderFilterTests(TestCase):
    """
    TEST-RESTAURANT-PARITY-00. A bare ``is_test=False`` filter switches a test
    restaurant off. Every consumer asks through ``counted_orders_q`` instead.
    """

    def test_no_production_module_filters_out_test_orders_directly(self):
        offenders = []
        for relative, source in _production_modules():
            if relative == RULE_MODULE:
                continue
            for line in _bare_test_filters(source):
                offenders.append(f'{relative}:{line}')
        self.assertEqual(
            offenders, [],
            'These filter out test orders directly, which removes them from a TEST '
            'restaurant too. Use orders_app.controllers.test_orders.counted_orders_q '
            '(or is_practice_order for one order in hand).',
        )

    def test_the_scanner_fires_on_each_shape_it_exists_to_catch(self):
        for snippet in (
            "Order.objects.filter(restaurant=r, is_test=False)",
            "OrderItem.objects.filter(order__is_test=False)",
            "qs.exclude(is_test=True)",
            "Q(order__is_test=False)",
            "Order.objects.filter(is_test__exact=False)",
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(_bare_test_filters(snippet), [1])

    def test_the_scanner_ignores_what_is_not_a_filter(self):
        for snippet in (
            "Order.objects.create(is_test=False)",          # creating, not filtering
            "Order.objects.filter(is_test=True)",           # selecting test orders
            "is_test = models.BooleanField(default=False)",  # a field declaration
            "# filter(is_test=False) in a comment",
            "Order.objects.filter(counted_orders_q())",
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(_bare_test_filters(snippet), [])

    def test_the_scan_actually_reaches_the_consumers(self):
        """A scan that read nothing would pass vacuously."""
        scanned = {relative for relative, _ in _production_modules()}
        for consumer in (
            'reports_app/controllers/common/sale_filters.py',
            'reports_app/controllers/restaurant/dashboard.py',
            'reports_app/controllers/restaurant/transactions.py',
            'reviews_app/controllers/submit_review.py',
            'orders_app/management/commands/determine-customers.py',
        ):
            self.assertIn(consumer, scanned)
