"""
PR-D — the launch boundary, and what a pre-go-live rehearsal order may touch.

`onboarding` and `live` used to be byte-identical in the capability matrix, so a
restaurant could take real money from real diners before anyone had asserted it was
ready — which is what the go-live transition and its readiness gate exist to prevent.
Now `onboarding` refuses the public but still lets the owner place ONE end-to-end
rehearsal order, because proving that path is a hard blocker on the Phase-1 checklist.

THE GOVERNING RULE, asserted throughout: **a test order is operationally real and
commercially invisible.** It occupies its table, reaches the kitchen board and is
served or cancelled like any other — that is the rehearsal. It never reaches revenue,
history or diner analytics, and it cannot be reviewed.

The exclusion tests are deliberately one-per-consumer rather than one representative
case. Each consumer builds its own queryset, and a filter that is missing from exactly
one of them is precisely the bug this suite exists to catch.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    OrderStatus_Paid,
    OrderStatus_Served,
    PaymentStatus_Paid,
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
    RESTAURANT_OWNER,
    TransactionStatus_Success,
    TransactionType_OrderPayment,
)
from finance_app.models import DinifyTransaction
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.test_orders import has_completed_test_order
from orders_app.models import Order, OrderItem
from reports_app.controllers.common.sale_filters import sale_orders
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


def _user(phone):
    return User.objects.create_user(
        first_name='Bound', last_name='Ary', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class LaunchBoundaryFixture(TestCase):
    """A restaurant with one orderable table and one orderable item."""

    def setUp(self):
        super().setUp()
        self.owner = _user('256770000001')
        self.restaurant = Restaurant.objects.create(
            name='Boundary R', location='loc', owner=self.owner,
            status=RestaurantStatus_Onboarding,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant,
        )
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=Decimal('10000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )

    def _at(self, state):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(status=state)
        self.restaurant.refresh_from_db()

    def _order(self, created_by=None):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=created_by,
        )

    def _free_table(self):
        """Clear occupancy so another order can be placed at the same table."""
        Order.objects.filter(table=self.table).update(fulfilment_status='served')

    def _rehearsal_order(self, *, complete=True):
        """A test order, placed the way an owner really places one."""
        self._at(RestaurantStatus_Onboarding)
        response = self._order(created_by=self.owner)
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertTrue(order.is_test)
        if complete:
            Order.objects.filter(pk=order.pk).update(
                order_status=OrderStatus_Served,
                payment_status=PaymentStatus_Paid,
                fulfilment_status='served',
                served_at=timezone.now(),
                total_cost=Decimal('10000'), discounted_cost=Decimal('10000'),
                actual_cost=Decimal('10000'), savings=Decimal('0'),
            )
            order.refresh_from_db()
        return order

    def _real_order(self, *, complete=True):
        """A commercial order at a live restaurant, for the contrast."""
        self._free_table()
        self._at(RestaurantStatus_Live)
        response = self._order()
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertFalse(order.is_test)
        if complete:
            Order.objects.filter(pk=order.pk).update(
                order_status=OrderStatus_Served,
                payment_status=PaymentStatus_Paid,
                fulfilment_status='served',
                served_at=timezone.now(),
                total_cost=Decimal('10000'), discounted_cost=Decimal('10000'),
                actual_cost=Decimal('10000'), savings=Decimal('0'),
            )
            order.refresh_from_db()
        return order


# --- the boundary itself -----------------------------------------------------------

class DinerOrderingBoundaryTests(LaunchBoundaryFixture):
    """A restaurant that has not gone live does not trade with the public."""

    def test_diner_is_refused_while_onboarding_with_the_plain_message(self):
        self._at(RestaurantStatus_Onboarding)
        response = self._order()

        self.assertEqual(response.get('status'), 400)
        self.assertEqual(response.get('message'), MESSAGES.get('NOT_OPEN_YET'))
        # Non-technical: a diner standing at a table reads this and it names no
        # internal state, no restaurant id and nothing for them to fix.
        self.assertNotIn('onboarding', response['message'].lower())
        self.assertNotIn('lifecycle', response['message'].lower())
        self.assertEqual(Order.objects.count(), 0)

    def test_diner_may_order_once_live(self):
        self._at(RestaurantStatus_Live)
        self.assertEqual(self._order().get('status'), 200)
        self.assertEqual(Order.objects.filter(is_test=False).count(), 1)

    def test_the_refusal_is_distinct_from_the_suspended_one(self):
        """
        Not open YET and no longer open are different facts.

        A restaurant still being set up gets an encouraging message; a suspended one
        keeps the pre-existing blocked-restaurant wording.
        """
        self._at(RestaurantStatus_Onboarding)
        onboarding = self._order()
        self._at('suspended')
        suspended = self._order()

        self.assertEqual(onboarding['status'], suspended['status'])
        self.assertNotEqual(onboarding['message'], suspended['message'])
        self.assertEqual(
            suspended['message'], MESSAGES.get('BLOCKED_RESTAURANT'),
        )

    def test_staff_rehearsal_is_still_allowed_while_onboarding(self):
        self._at(RestaurantStatus_Onboarding)
        self.assertEqual(self._order(created_by=self.owner).get('status'), 200)

    def test_the_owner_retains_full_portal_access_while_onboarding(self):
        """Non-regression: PR-5's widening is untouched by the boundary."""
        from dinify_backend.configss.string_definitions import (
            MODULE_KITCHEN, MODULE_MENU, MODULE_TABLES,
        )
        from users_app.controllers.permissions_check import can_user_access_module

        self._at(RestaurantStatus_Onboarding)
        for module in (MODULE_MENU, MODULE_TABLES, MODULE_KITCHEN):
            self.assertTrue(
                can_user_access_module(self.owner, str(self.restaurant.id), module),
                module,
            )


# --- is_test is derived, not supplied ----------------------------------------------

class TestOrderFlaggingTests(LaunchBoundaryFixture):
    def test_onboarding_order_is_flagged_and_live_order_is_not(self):
        rehearsal = self._rehearsal_order(complete=False)
        self.assertTrue(rehearsal.is_test)

        real = self._real_order(complete=False)
        self.assertFalse(real.is_test)

    def test_the_flag_cannot_be_supplied_by_the_caller(self):
        """
        Server-derived from the lifecycle state, with no request field behind it.

        Neither direction is spoofable: a diner cannot mark a real order as a test,
        and an owner cannot pass one off as commerce.
        """
        self._at(RestaurantStatus_Live)
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=self.owner,
        )
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertFalse(order.is_test)

    def test_has_completed_test_order_is_false_until_one_completes(self):
        self.assertFalse(has_completed_test_order(self.restaurant))
        self._rehearsal_order(complete=False)
        self.assertFalse(
            has_completed_test_order(self.restaurant),
            'an unfinished rehearsal must not satisfy the checklist',
        )

    def test_has_completed_test_order_is_true_after_a_served_rehearsal(self):
        self._rehearsal_order(complete=True)
        self.assertTrue(has_completed_test_order(self.restaurant))

    def test_a_real_order_does_not_satisfy_the_rehearsal_fact(self):
        self._real_order(complete=True)
        self.assertFalse(has_completed_test_order(self.restaurant))


# --- one assertion per consumer ----------------------------------------------------

class TestOrderExclusionTests(LaunchBoundaryFixture):
    """
    Every consumer that represents money, history or diner analytics.

    One test per consumer on purpose: each builds its own queryset, so a filter
    missing from exactly one of them is the failure mode worth catching.
    """

    def setUp(self):
        super().setUp()
        self.test_order = self._rehearsal_order(complete=True)
        self.real_order = self._real_order(complete=True)
        self.today = timezone.localdate()
        self.window = (self.today - timedelta(days=1), self.today + timedelta(days=1))

    def _dates(self):
        return {'date_from': str(self.window[0]), 'date_to': str(self.window[1])}

    # 1 — the chokepoint
    def test_sale_orders_chokepoint_excludes_it(self):
        ids = set(sale_orders(str(self.restaurant.id), *self.window)
                  .values_list('id', flat=True))
        self.assertIn(self.real_order.id, ids)
        self.assertNotIn(self.test_order.id, ids)

    # 2 — dashboard v1
    def test_dashboard_v1_excludes_it(self):
        from reports_app.controllers.restaurant.dashboard import (
            generate_restaurant_dashboard_details,
        )
        data = generate_restaurant_dashboard_details(
            str(self.restaurant.id), **self._dates())['data']
        self.assertEqual(data['num_sales'], 1)
        self.assertEqual(Decimal(str(data['sales_amount'])), Decimal('10000'))

    # 3 — the dead-but-live all-time revenue helper
    def test_summarize_revenue_excludes_it(self):
        from reports_app.controllers.restaurant.dashboard import summarize_revenue
        totals = summarize_revenue(str(self.restaurant.id))
        self.assertEqual(Decimal(str(totals['total'])), Decimal('10000'))

    # 4 — dashboard v2 revenue + orders + popular items
    def test_dashboard_v2_revenue_and_orders_exclude_it(self):
        from reports_app.controllers.restaurant.dashboard import (
            generate_restaurant_dashboard_v2,
        )
        data = generate_restaurant_dashboard_v2(
            str(self.restaurant.id), **self._dates())['data']
        self.assertEqual(
            Decimal(str(data['revenue']['totals']['gross'])), Decimal('10000'),
        )
        self.assertEqual(data['orders']['total'], 1)
        item_qty = sum(row['qty'] for row in data['popular_items'])
        self.assertEqual(item_qty, 1)

    # 5 — dashboard v2 payment methods (a TRANSACTION queryset joined through order)
    def test_dashboard_v2_payment_methods_exclude_it(self):
        from reports_app.controllers.restaurant.dashboard import (
            generate_restaurant_dashboard_v2,
        )
        for order in (self.test_order, self.real_order):
            DinifyTransaction.objects.create(
                restaurant=self.restaurant, order=order,
                transaction_type=TransactionType_OrderPayment,
                transaction_status=TransactionStatus_Success,
                transaction_amount=Decimal('10000'), payment_mode='cash',
            )
        data = generate_restaurant_dashboard_v2(
            str(self.restaurant.id), **self._dates())['data']
        total = sum(Decimal(str(row['amount'])) for row in data['payment_methods'])
        self.assertEqual(total, Decimal('10000'))

    # 6 — the transactions report (scoped by restaurant, NOT through the order FK)
    def test_transactions_report_excludes_it_but_keeps_orderless_rows(self):
        from reports_app.controllers.restaurant.transactions import (
            generate_restaurant_transaction_summary,
        )
        for order in (self.test_order, self.real_order):
            DinifyTransaction.objects.create(
                restaurant=self.restaurant, order=order,
                transaction_type=TransactionType_OrderPayment,
                transaction_status=TransactionStatus_Success,
                transaction_amount=Decimal('10000'), payment_mode='cash',
            )
        # A subscription row has NO order at all and must survive the exclusion —
        # the reason the filter is a Q(order__isnull=True) | Q(order__is_test=False)
        # rather than a bare exclude(), which would have dropped it.
        DinifyTransaction.objects.create(
            restaurant=self.restaurant, order=None,
            transaction_type='subscription',
            transaction_status=TransactionStatus_Success,
            transaction_amount=Decimal('50000'), payment_mode='momo',
        )
        data = generate_restaurant_transaction_summary(
            str(self.restaurant.id), **self._dates())['data']
        # The real order's payment plus the order-less subscription row — the test
        # order's payment is gone, the subscription one survived.
        self.assertEqual(data['total_transactions'], 2)
        amount = sum(Decimal(str(row['amount'])) for row in data['by_status'])
        self.assertEqual(amount, Decimal('60000'))
        by_type = {row['type']: row['count'] for row in data['by_type']}
        self.assertEqual(by_type.get('subscription'), 1)
        self.assertEqual(by_type.get(TransactionType_OrderPayment), 1)

    # 7 — the management command that MINTS REAL USERS
    def test_determine_customers_skips_it(self):
        from django.core.management import call_command
        Order.objects.filter(pk=self.test_order.pk).update(
            customer=None, customer_match_attempted=False,
            customer_phone='256770009999',
        )
        before = User.objects.count()
        call_command('determine-customers')
        self.assertEqual(
            User.objects.count(), before,
            'a rehearsal order minted a real platform user',
        )
        self.test_order.refresh_from_db()
        self.assertFalse(self.test_order.customer_match_attempted)

    # 8 — reviews
    def test_a_test_order_cannot_be_reviewed(self):
        from reviews_app.controllers.submit_review import submit_review
        response = submit_review(
            order_id=str(self.test_order.id),
            rating_fields={'overall_rating': 5},
            session_restaurant_id=str(self.restaurant.id),
            session_table_id=str(self.table.id),
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], 'This order is not eligible for review.')

    def test_a_real_order_is_still_reviewable(self):
        """The gate is about test orders, not a blanket tightening."""
        from reviews_app.controllers.submit_review import submit_review
        response = submit_review(
            order_id=str(self.real_order.id),
            rating_fields={'overall_rating': 5},
            session_restaurant_id=str(self.restaurant.id),
            session_table_id=str(self.table.id),
        )
        self.assertEqual(response['status'], 201, response)


# --- what a test order MUST still reach --------------------------------------------

class TestOrderRemainsOperationalTests(LaunchBoundaryFixture):
    """
    The other half of the rule. A rehearsal that nothing could see would prove nothing.
    """

    def test_it_occupies_its_table_once_submitted(self):
        """
        A SUBMITTED rehearsal order holds its table, exactly like a real one.

        Occupancy deliberately ignores drafts (`initiated`) — a table is claimed at
        submit, not at create (PR #210) — so the rehearsal is moved past that first.
        """
        order = self._rehearsal_order(complete=False)
        self.assertFalse(
            ConOrder.any_present_ongoing_order(self.table)['present'],
            'a draft must not occupy the table',
        )

        Order.objects.filter(pk=order.pk).update(order_status='pending')
        ongoing = ConOrder.any_present_ongoing_order(self.table)
        self.assertTrue(ongoing['present'])
        self.assertEqual(str(ongoing['order_id']), str(order.id))

    def test_it_blocks_table_deletion_like_any_live_order(self):
        self._rehearsal_order(complete=False)
        self.table.refresh_from_db()
        self.assertTrue(self.table.has_unsettled_orders())
        self.assertIsNotNone(self.table.deletion_blockers())

    def test_it_reaches_the_kitchen_board(self):
        order = self._rehearsal_order(complete=False)
        Order.objects.filter(pk=order.pk).update(order_status='pending')
        board = Order.objects.filter(
            restaurant=self.restaurant, deleted=False,
        ).exclude(order_status__in=['cancelled', 'initiated']).exclude(
            fulfilment_status='served')
        self.assertIn(order.id, set(board.values_list('id', flat=True)))

    def test_its_own_totals_are_still_computed(self):
        order = self._rehearsal_order(complete=False)
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 1)
        order.refresh_from_db()
        self.assertGreater(order.actual_cost, 0)
