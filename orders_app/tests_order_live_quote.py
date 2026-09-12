"""
R2 — the rendered quote, the counts and the reference describe ONE population.

THE DEFECT. ``serialize_order_details`` fetches every ``OrderItem`` of the
order, then splits that list two different ways. ``live_rows`` excludes
soft-deleted records and feeds the legacy total and ``quote_ref``; the
parent/child collections that build the NEW quote and the availability counts
came from the unfiltered list. A soft-deleted parent or child therefore
appeared as an active quoted purchase while the reference and the rollup that
the diner's acceptance is bound to excluded it — two answers to "what is in
this order" inside one response.

WHAT THIS IS NOT. There is no demonstrated public delete operation on the
reviewed ordering journey that produces such a row, so this is a COHERENCE
defect in the serializer, not an executed live exploit. The fixtures below set
``deleted`` directly, which is exactly what makes them fixtures.

THE RULE THESE TESTS PIN. One explicitly-defined live population — rows with
``deleted=False`` — supplies the review, the counts, the parent/child
relationships and the digest. Historical amounts are never rewritten to make
that population add up: where the record cannot supply a coherent quote (an
orphaned live child, whose parent is not in the live population and which
therefore belongs under no quoted line), the saved payable stands and the
response says so, producing a controlled non-confirmable result rather than a
silently trimmed one that claims to reconcile.
"""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, OrderStatus_Pending, RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from misc_app.controllers.money import working_context
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.manage_order import (
    REASON_QUOTE_INCOMPLETE, update_order_status,
)
from orders_app.controllers.orders.serializers import serialize_order_details
from orders_app.controllers.services.order_quote import (
    group_live_children, matches, quote_ref,
)
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

D = Decimal


class _QuoteFixture(TestCase):
    """Rows are written directly: this is about what the SERIALIZER does with a
    given persisted population, not about how one comes to exist."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='L', last_name='Q', email='live-quote@test.com',
            phone_number='256700099101', username='256700099101',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Live R', location='live', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )
        self._n = 0

    def dish(self, name, price=D('10000.00')):
        return MenuItem.objects.create(
            name=name, section=self.section, approved=True, enabled=True,
            available=True, in_stock=True, primary_price=price,
        )

    def order(self, payable):
        """An order whose saved totals are stated explicitly.

        ``actual_cost`` is what ``update_order_amounts`` would have reconciled
        over the LIVE rows, so each fixture states the true saved payable and
        the serializer is judged against it.
        """
        self._n += 1
        return Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            order_number=70000 + self._n, pricing_version=1,
            total_cost=payable, discounted_cost=payable,
            savings=D('0.00'), actual_cost=payable,
        )

    def row(self, order, dish, *, amount, parent=None, quantity=1,
            available=True, deleted=False):
        row = OrderItem.objects.create(
            order=order, item=dish, parent_item=parent, quantity=quantity,
            available=available, status='ok',
            unit_price=amount, discounted_price=amount,
            unit_cost_of_options=D('0.00'),
            total_cost=amount, discounted_cost=amount,
            cost_of_options=D('0.00'), savings=D('0.00'), actual_cost=amount,
            item_name_snapshot=dish.name,
        )
        if deleted:
            # Written after creation so the fixture is unambiguous about what
            # it is doing; `update` avoids touching any other column.
            OrderItem.objects.filter(pk=row.pk).update(deleted=True)
            row.refresh_from_db()
        return row

    # -- assertions ------------------------------------------------------
    def quoted_sum(self, payload):
        """Σ of every quoted line's parent-plus-extras figure, exactly."""
        with working_context():
            return sum(
                (D(line['line_total_with_extras']) for line in payload['quote']),
                D('0'),
            )

    def assert_reconciles(self, payload):
        details = payload['order']
        self.assertEqual(
            self.quoted_sum(payload), D(details['quote_total']),
            'the lines the diner is shown do not add up to the amount they are '
            'being asked to confirm',
        )


class DeletedRowsAreNotQuotedTests(_QuoteFixture):

    def test_a_deleted_child_is_not_nested_under_its_live_parent(self):
        order = self.order(D('10000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('2000.00'),
                 parent=parent, deleted=True)

        payload = serialize_order_details(order)
        self.assertEqual(len(payload['quote']), 1)
        self.assertEqual(
            payload['quote'][0]['extras'], [],
            'a soft-deleted extra was presented as part of the purchase',
        )

    def test_a_deleted_child_does_not_inflate_the_reviewed_line(self):
        order = self.order(D('10000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('2000.00'),
                 parent=parent, deleted=True)

        payload = serialize_order_details(order)
        self.assertEqual(payload['quote'][0]['line_total_with_extras'],
                         '10000.00')
        self.assert_reconciles(payload)

    def test_a_deleted_child_is_absent_from_the_flat_extras_collections(self):
        order = self.order(D('10000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('2000.00'),
                 parent=parent, deleted=True)

        payload = serialize_order_details(order)
        self.assertEqual(payload['extras'], [])
        self.assertEqual(payload['available_extras'], [])
        self.assertEqual(payload['order']['no_available_extras'], 0)

    def test_a_deleted_parent_and_its_children_are_not_an_active_purchase(self):
        order = self.order(D('10000.00'))
        gone = self.row(order, self.dish('Dropped'), amount=D('5000.00'),
                        deleted=True)
        self.row(order, self.dish('Dropped extra'), amount=D('500.00'),
                 parent=gone, deleted=True)
        self.row(order, self.dish('Burger'), amount=D('10000.00'))

        payload = serialize_order_details(order)
        self.assertEqual(
            [line['item_name'] for line in payload['quote']], ['Burger'],
            'a soft-deleted dish was quoted as an active purchase',
        )
        self.assertEqual(payload['order']['no_available_items'], 1)
        self.assertEqual(payload['order']['no_items'], 1)
        self.assertEqual(payload['order']['no_unavailable_items'], 0)
        self.assert_reconciles(payload)

    def test_a_deleted_parent_is_absent_from_the_flat_collections(self):
        order = self.order(D('10000.00'))
        self.row(order, self.dish('Dropped'), amount=D('5000.00'),
                 deleted=True)
        self.row(order, self.dish('Burger'), amount=D('10000.00'))

        payload = serialize_order_details(order)
        for key in ('order_items', 'available_items'):
            self.assertEqual([r['item_name'] for r in payload[key]], ['Burger'],
                             f'{key} still carries a soft-deleted row')
        self.assertEqual(payload['unavailable_items'], [])


class UnavailableRowsStillCountTests(_QuoteFixture):
    """Unavailable is NOT deleted. A live sold-out row is a real part of the
    record and must keep appearing, at its saved zero amount."""

    def test_a_live_unavailable_parent_is_still_quoted(self):
        order = self.order(D('10000.00'))
        self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Sold out'), amount=D('0.00'),
                 quantity=0, available=False)

        payload = serialize_order_details(order)
        self.assertEqual(len(payload['quote']), 2)
        self.assertEqual(payload['order']['no_available_items'], 1)
        self.assertEqual(payload['order']['no_unavailable_items'], 1)
        self.assertEqual(
            [r['item_name'] for r in payload['unavailable_items']],
            ['Sold out'],
        )
        self.assert_reconciles(payload)

    def test_an_entirely_unavailable_order_is_coherent_but_empty(self):
        order = self.order(D('0.00'))
        self.row(order, self.dish('Sold out'), amount=D('0.00'),
                 quantity=0, available=False)

        payload = serialize_order_details(order)
        self.assertEqual(payload['order']['no_available_items'], 0)
        self.assertEqual(len(payload['quote']), 1)
        self.assert_reconciles(payload)

    def test_an_unavailable_extra_under_a_live_parent_is_reported_once(self):
        order = self.order(D('10000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('0.00'), parent=parent,
                 quantity=0, available=False)

        payload = serialize_order_details(order)
        self.assertEqual(payload['order']['no_unavailable_extras'], 1)
        self.assertEqual(len(payload['quote'][0]['extras']), 1)
        self.assert_reconciles(payload)


class OrphanedLiveChildTests(_QuoteFixture):
    """A live child whose parent is NOT in the live population.

    Its amount is part of the saved payable, and there is no quoted line it can
    belong to. The response must not quietly drop it and then present a quote
    that appears to reconcile — it declares the quote incomplete and leaves
    every historical amount exactly as saved.
    """

    def _orphan_order(self):
        order = self.order(D('12000.00'))
        gone = self.row(order, self.dish('Dropped'), amount=D('0.00'),
                        deleted=True)
        self.row(order, self.dish('Cheese'), amount=D('2000.00'), parent=gone)
        self.row(order, self.dish('Burger'), amount=D('10000.00'))
        return order

    def test_the_quote_declares_itself_incomplete(self):
        payload = serialize_order_details(self._orphan_order())
        self.assertIs(payload['order']['quote_complete'], False)

    def test_the_saved_payable_is_not_rewritten_to_match_the_lines(self):
        payload = serialize_order_details(self._orphan_order())
        self.assertEqual(payload['order']['quote_total'], '12000.00')
        self.assertEqual(payload['order']['actual_cost'], D('12000.00'))

    def test_the_quoted_lines_visibly_fall_short_of_the_payable(self):
        """The mismatch is the point: a client that reconciles must refuse."""
        payload = serialize_order_details(self._orphan_order())
        self.assertEqual(self.quoted_sum(payload), D('10000.00'))
        self.assertLess(self.quoted_sum(payload),
                        D(payload['order']['quote_total']))

    def test_the_orphan_is_still_disclosed_in_the_flat_collections(self):
        payload = serialize_order_details(self._orphan_order())
        self.assertEqual([r['item_name'] for r in payload['extras']],
                         ['Cheese'])

    def test_a_healthy_order_declares_its_quote_complete(self):
        order = self.order(D('12000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('2000.00'), parent=parent)

        payload = serialize_order_details(order)
        self.assertIs(payload['order']['quote_complete'], True)
        self.assert_reconciles(payload)


class ReadIsPureAndStableTests(_QuoteFixture):

    def _healthy(self):
        order = self.order(D('12000.00'))
        parent = self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Cheese'), amount=D('2000.00'), parent=parent)
        return order

    def test_no_record_is_rewritten_merely_by_reading_it(self):
        order = self._healthy()
        with CaptureQueriesContext(connection) as captured:
            serialize_order_details(order)
        offending = [
            q['sql'] for q in captured.captured_queries
            if not q['sql'].lstrip().upper().startswith('SELECT')
        ]
        self.assertEqual(offending, [], 'serialization wrote to the database')

    def test_the_reference_does_not_churn_for_an_unchanged_healthy_draft(self):
        """The digest for an ordinary valid draft is byte-identical to the one
        the canonical helper derives on its own — so reconstructing the
        population moved no reference."""
        order = self._healthy()
        payload = serialize_order_details(order)
        self.assertEqual(payload['order']['quote_ref'], quote_ref(order))

    def test_the_reference_still_excludes_a_deleted_row(self):
        order = self._healthy()
        extra = OrderItem.objects.filter(parent_item__isnull=False).first()
        before = serialize_order_details(order)['order']['quote_ref']
        OrderItem.objects.filter(pk=extra.pk).update(deleted=True)
        after = serialize_order_details(order)['order']['quote_ref']
        self.assertNotEqual(before, after)
        self.assertEqual(after, quote_ref(order))

    def test_the_read_costs_the_same_for_one_line_as_for_many(self):
        """No per-line query: the rows are fetched once and partitioned."""
        small = self.order(D('10000.00'))
        self.row(small, self.dish('Solo'), amount=D('10000.00'))

        large = self.order(D('100000.00'))
        for i in range(10):
            parent = self.row(large, self.dish(f'Dish {i}'),
                              amount=D('10000.00'))
            self.row(large, self.dish(f'Extra {i}'), amount=D('0.00'),
                     parent=parent)

        with CaptureQueriesContext(connection) as one:
            serialize_order_details(small)
        with CaptureQueriesContext(connection) as many:
            serialize_order_details(large)
        self.assertEqual(len(one.captured_queries), len(many.captured_queries))

    def test_the_counts_agree_with_the_collections_they_summarise(self):
        order = self.order(D('10000.00'))
        self.row(order, self.dish('Burger'), amount=D('10000.00'))
        self.row(order, self.dish('Sold out'), amount=D('0.00'),
                 quantity=0, available=False)
        self.row(order, self.dish('Ghost'), amount=D('9999.00'), deleted=True)

        payload = serialize_order_details(order)
        details = payload['order']
        self.assertEqual(details['no_available_items'],
                         len(payload['available_items']))
        self.assertEqual(details['no_unavailable_items'],
                         len(payload['unavailable_items']))
        self.assertEqual(details['no_available_extras'],
                         len(payload['available_extras']))
        self.assertEqual(details['no_unavailable_extras'],
                         len(payload['unavailable_extras']))
        self.assertEqual(details['no_available_items'] + details['no_unavailable_items'],
                         len(payload['quote']))


class IncompleteQuoteIsRefusedAtAcceptanceTests(_QuoteFixture):
    """R2, completed on the SERVER: an incomplete quote cannot be accepted.

    Disclosing ``quote_complete: false`` makes the response honest; it does not
    make the order unacceptable. The flag is optional to a client, and the
    acceptance transition used to check only the reference and that one
    deliverable parent existed — so a caller holding the diner session (an older
    client, or any direct caller) could return the perfectly valid reference and
    move the order to the kitchen with part of its payable represented by no
    quoted line at all.

    These orders are built through the REAL ``initiate_order`` so the draft is
    genuinely acceptable in every other respect, and then ONE parent row is
    soft-deleted directly — which is what makes the fixture a fixture: no
    demonstrated public delete operation on the reviewed ordering journey
    produces such a row.
    """

    def setUp(self):
        super().setUp()
        self.extra = MenuItem.objects.create(
            name='Cheese', section=self.section, approved=True, enabled=True,
            available=True, in_stock=True, primary_price=D('2000.00'),
            is_extra=True,
        )
        self.with_extra = MenuItem.objects.create(
            name='Burger', section=self.section, approved=True, enabled=True,
            available=True, in_stock=True, primary_price=D('10000.00'),
            has_extras=True, extras_applicable=[str(self.extra.id)],
            extras_min_selections=0, extras_max_selections=1,
        )
        self.plain = self.dish('Fries', price=D('4000.00'))
        self._tables = 1

    def table_for_a_new_order(self):
        self._tables += 1
        return Table.objects.create(
            number=self._tables, str_number=str(self._tables),
            restaurant=self.restaurant, qr_mode='order_pay',
        )

    def draft(self):
        """A real two-parent draft: one line carries an extra, one does not."""
        table = self.table_for_a_new_order()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=[
                {'item': str(self.with_extra.id), 'quantity': 1,
                 'extras': [str(self.extra.id)]},
                {'item': str(self.plain.id), 'quantity': 1},
            ],
        )
        self.assertEqual(response.get('status'), 200, response)
        return Order.objects.get(pk=response['data']['order_details']['id'])

    def orphan(self, order):
        """Soft-delete the parent that carries the extra, leaving it orphaned.

        The saved payable is deliberately NOT recalculated: the child's amount
        stays in ``actual_cost``, which is the whole reason its disappearance
        from the quote matters.
        """
        parent = OrderItem.objects.get(
            order=order, parent_item__isnull=True, item=self.with_extra,
        )
        OrderItem.objects.filter(pk=parent.pk).update(deleted=True)
        return Order.objects.get(pk=order.pk)

    # -- the refusal -----------------------------------------------------
    def test_a_valid_reference_for_an_incomplete_quote_is_refused(self):
        order = self.orphan(self.draft())
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['reason'], REASON_QUOTE_INCOMPLETE)

    def test_the_order_is_not_moved_to_the_kitchen(self):
        order = self.orphan(self.draft())
        update_order_status(order, OrderStatus_Pending, None,
                            quote_ref=quote_ref(order))
        self.assertEqual(
            Order.objects.get(pk=order.pk).order_status, OrderStatus_Initiated,
            'an order whose quote cannot represent its payable was accepted',
        )

    def test_neither_pre_existing_invariant_would_have_refused_it(self):
        """The negative control: only the NEW rule stands between this draft and
        the kitchen. The reference matches exactly, and a deliverable parent
        remains — so the refusal above is not a stale reference or an empty
        ticket wearing a different name."""
        order = self.orphan(self.draft())
        self.assertTrue(matches(order, quote_ref(order)))
        self.assertGreaterEqual(ConOrder.deliverable_parent_count(order), 1)

    def test_the_payable_really_does_exceed_the_quotable_lines(self):
        """What is at stake: the orphan's amount is still in ``actual_cost``."""
        order = self.orphan(self.draft())
        live = OrderItem.objects.filter(order=order, deleted=False)
        _, orphaned = group_live_children(list(live))
        self.assertEqual(len(orphaned), 1)
        with working_context():
            quotable = sum(
                (row.actual_cost for row in live if row.parent_item_id is None),
                D('0'),
            )
        self.assertLess(quotable, order.actual_cost)

    # -- a refusal changes nothing ---------------------------------------
    def test_the_refusal_rewrites_nothing(self):
        order = self.orphan(self.draft())
        before = {
            row.pk: (row.deleted, row.quantity, row.actual_cost, row.available)
            for row in OrderItem.objects.filter(order=order)
        }
        reference = quote_ref(order)

        update_order_status(order, OrderStatus_Pending, None,
                            quote_ref=reference)

        after = {
            row.pk: (row.deleted, row.quantity, row.actual_cost, row.available)
            for row in OrderItem.objects.filter(order=order)
        }
        self.assertEqual(after, before, 'a refusal is not a repair')
        reloaded = Order.objects.get(pk=order.pk)
        self.assertEqual(reloaded.actual_cost, order.actual_cost)
        self.assertEqual(quote_ref(reloaded), reference,
                         'the refusal churned the reference it refused')

    # -- the healthy order is untouched ----------------------------------
    def test_a_complete_quote_is_still_accepted(self):
        order = self.draft()
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
        )
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(Order.objects.get(pk=order.pk).order_status,
                         OrderStatus_Pending)

    # -- one definition, two consumers -----------------------------------
    def test_the_disclosure_and_the_refusal_agree(self):
        """``quote_complete`` and the acceptance rule read the SAME split.

        A response saying the quote is complete while the transition refuses it
        — or the reverse, which is the defect Codex named — would be two answers
        to one question.
        """
        for build, complete, expected in (
            (lambda: self.draft(), True, 200),
            (lambda: self.orphan(self.draft()), False, 400),
        ):
            with self.subTest(complete=complete):
                order = build()
                payload = serialize_order_details(order)
                self.assertIs(payload['order']['quote_complete'], complete)
                result = update_order_status(
                    order, OrderStatus_Pending, None,
                    quote_ref=payload['order']['quote_ref'],
                )
                self.assertEqual(result['status'], expected, result)
