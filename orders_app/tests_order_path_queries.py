"""
Order-creation query bounds (ORDER-N1-00).

The create path re-read the same rows several times per order line, all of it
inside the transaction that holds the table row lock:

  * the ``Order`` row ``_create_order`` had just created was re-``SELECT``ed once
    per line by ``add_order_item``, which then lazily loaded its ``Restaurant``;
  * the line's ``MenuItem`` was fetched by ``add_order_item`` (restaurant-scoped),
    then again — unscoped — by ``find_existing_order_item`` and again by
    ``construct_option_items``;
  * ``normalize_order_items`` issued one ``.get()`` per line;
  * ``find_existing_order_item`` called ``.count()`` on the same extras queryset at
    four independent ``if`` sites (all four run — they are separate ``if``\\s and the
    count sits left of the ``and``), a fifth inside the branch, and then iterated
    it, for up to six statements over one unchanging set of rows.

Every one of those is a REPEATED READ of a value already in hand, on the
authoritative side of the table lock. Nothing about *what* is read changed: no
``select_for_update`` exists on ``MenuItem`` anywhere on this path, and the
deliberately-fresh boundary — preflight vs authoritative, documented in
``con_orders.initiate_order`` and ``create_order._create_order`` — is untouched,
because no preflight-fetched instance is reused inside the transaction.

The counts below are exact rather than upper bounds: an exact count is what
catches a re-introduced N+1, and the flatness cases (1 line vs 4) are what prove
the saving is per-line rather than a one-off.

D02 REDUCED THEM AGAIN, and every number here moved DOWN. The order's whole
catalogue is now resolved by ONE coherent read (``catalogue_snapshot``), the
merge candidate comes from a per-order in-memory index rather than a database
lookup, allergen labels are one batched statement for the order instead of one
per line, and ``process_item_extras`` hands its rows back so the caller never
re-SELECTs the children it just wrote:

    1-line order   23 -> 20 -> 19
    4-line order   35 -> 23 -> 22
    per line        4 -> 1     (the INSERT, and nothing else)
    MenuItem reads  1 per line -> 0 per line (one batch for the order)

THE LAST STEP (D02 completion B) REMOVED A STATEMENT RATHER THAN ADDING ONE. The
batched allergen read was folded into the catalogue statement as an aggregate,
because two statements could observe ONE operator transaction half applied — the
old dish name beside its new allergen labels. The count going DOWN is a
consequence of the coherence fix, not its purpose; see
``catalogue_snapshot.py`` and ``tests_order_snapshot.py``.

MEASURED BREAKDOWN of the 19 a one-line order runs — every one of them is a
distinct, named piece of work rather than a repeat:

    1  SELECT restaurants            resolve the target
    1  SELECT tables                 scoped table resolve
    1  SELECT ?                      the admission advisory lock
    1  SELECT menu_items             THE catalogue snapshot (whole order),
                                     allergen labels aggregated into it
    1  SELECT restaurant_daily_...   counter row lock
    1  UPDATE restaurant_daily_...   counter increment
    1  INSERT orders                 the draft
    1  INSERT order_items            the line          <- the ONLY per-line cost
    1  SELECT order_items            the roll-up read
    2  SELECT orders                 replay lookup + the post-loop re-read
    1  UPDATE orders                 the roll-up write
    1  SELECT transactions           payments for the roll-up
    3  SAVEPOINT / 3 RELEASE         the two nested atomics

A 4-line order is the same list with four INSERTs instead of one.
"""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, PaymentStatus_Pending,
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.services.create_order import _create_order
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

OPTION_GROUP_ID = 'grp-size'
OPTION_SMALL_ID = 'ch-small'
OPTION_LARGE_ID = 'ch-large'


class OrderPathBase(TestCase):
    """A live restaurant with four orderable items, one extra and one options item."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='N1', last_name='Owner', email='n1_owner@test.com',
            phone_number='256700000950', username='256700000950',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='N Plus One', location='loc-n1', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.tables = [self._table(n) for n in range(1, 9)]
        self.items = [self._item(f'Dish {n}', 10000 + n) for n in range(1, 5)]

        self.extra = self._item('Extra Sauce', 1000, is_extra=True)
        self.parent = self._item('With Extras', 20000)
        self.parent.has_extras = True
        self.parent.extras_applicable = [str(self.extra.id)]
        self.parent.save(update_fields=['has_extras', 'extras_applicable'])

        self.options_item = self._item('Sized Dish', 15000)
        self.options_item.options = {
            'hasModifiers': True,
            'groups': [{
                'id': OPTION_GROUP_ID, 'name': 'Size',
                'minSelections': 0, 'maxSelections': 1,
                'choices': [
                    {'id': OPTION_SMALL_ID, 'name': 'Small', 'additionalCost': 500},
                    {'id': OPTION_LARGE_ID, 'name': 'Large', 'additionalCost': 900},
                ],
            }],
        }
        self.options_item.save(update_fields=['options'])

    def _table(self, number):
        return Table.objects.create(
            number=number, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

    def _item(self, name, price, is_extra=False):
        return MenuItem.objects.create(
            name=name, section=self.section, primary_price=Decimal(str(price)),
            approved=True, enabled=True, available=True, in_stock=True,
            is_extra=is_extra,
        )

    def create(self, items, table_index=0):
        result = _create_order(
            restaurant=self.restaurant,
            table=self.tables[table_index],
            items=items,
            created_by=None,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def lines(self, quantity=1, count=4):
        return [
            {'item': str(item.id), 'quantity': quantity}
            for item in self.items[:count]
        ]

    def snapshot(self, order):
        """Everything an order line persists, in a stable order."""
        rows = []
        for line in OrderItem.objects.filter(order=order):
            rows.append((
                line.item_name_snapshot, str(line.item_id), line.quantity,
                str(line.unit_price), str(line.discounted_price),
                str(line.cost_of_options), str(line.unit_cost_of_options),
                str(line.total_cost), str(line.discounted_cost),
                str(line.savings), str(line.actual_cost),
                line.selected_modifiers, line.options,
                line.modifiers_snapshot, line.allergen_tags_snapshot,
                line.available, line.status, line.discounted,
                line.parent_item is not None,
            ))
        return sorted(rows, key=lambda row: (row[0], row[1], row[-1]))


class CreateOrderQueryCountTests(OrderPathBase):
    """Exact query counts — an upper bound would not catch a returning N+1."""

    def measure(self, items, table_index):
        with CaptureQueriesContext(connection) as captured:
            self.create(items, table_index=table_index)
        return len(captured.captured_queries)

    def menu_item_reads(self, count, table_index):
        """Queries selecting FROM menu_items — not the tag join, not the ordering join."""
        with CaptureQueriesContext(connection) as captured:
            self.create(self.lines(count=count), table_index=table_index)
        return len([
            q for q in captured.captured_queries
            if q['sql'].startswith('SELECT') and 'FROM "menu_items"' in q['sql']
        ])

    def test_four_line_order_query_count(self):
        # Warm any one-off caches (content types, savepoint bookkeeping) on a
        # throwaway order first, as restaurants_app.tests does for the scan read.
        self.measure(self.lines(count=1), table_index=0)
        self.assertEqual(self.measure(self.lines(count=4), table_index=1), 22)

    def test_single_line_order_query_count(self):
        self.measure(self.lines(count=1), table_index=0)
        self.assertEqual(self.measure(self.lines(count=1), table_index=1), 19)

    # Measured on this fixture across all three passes: a 4-line order ran 54
    # queries before D01's collapse, 35 after it, 23 after D02's and 22 once the
    # allergen read was folded in; a 1-line order ran 27, then 23, then 20, now
    # 19. The per-line cost — the part that grows with
    # the size of the order, and so with how long the table row lock is held —
    # went 9 -> 4 -> 1.
    def test_per_line_cost_is_one_query(self):
        """Flatness: an extra line costs exactly its INSERT and nothing else.

        The three that went in D02: the scoped MenuItem guard (now served from
        the order's ONE catalogue snapshot, with the fallback query preserved for
        a caller that has no snapshot), the merge lookup (now a per-order
        in-memory index, whose database fallback is likewise preserved), and the
        allergen-tag read (now one batched statement for the whole order).
        """
        self.measure(self.lines(count=1), table_index=0)
        one = self.measure(self.lines(count=1), table_index=1)
        four = self.measure(self.lines(count=4), table_index=2)
        self.assertEqual((four - one) / 3, 1)

    def test_a_complex_basket_does_not_grow_per_line(self):
        """UPPER-GROWTH CHECK for a realistic basket, not a flat exact count.

        Modifiers, extras and a repeated (merging) line all on one order. The
        exact total is deliberately NOT pinned — it depends on how many extras
        the fixture attaches — but the SHAPE is: doubling the distinct lines must
        not more than double the work, and the per-extra cost must stay constant.
        A re-introduced per-line catalogue read or merge lookup fails this even
        when the simple flat case still passes.
        """
        complex_lines = [
            {'item': str(self.options_item.id), 'quantity': 2,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]}},
            {'item': str(self.parent.id), 'quantity': 1,
             'extras': [str(self.extra.id)]},
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]}},
        ]
        self.measure(complex_lines, table_index=0)          # warm
        small = self.measure(complex_lines, table_index=1)
        doubled = self.measure(complex_lines + [
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_LARGE_ID]}},
            {'item': str(self.parent.id), 'quantity': 3,
             'extras': [str(self.extra.id)]},
            {'item': str(self.items[0].id), 'quantity': 1},
        ], table_index=2)
        # Three more submitted lines, one of which carries an extra: at most one
        # INSERT each plus the extra's own three statements.
        self.assertLessEqual(doubled - small, 3 + 3, (small, doubled))

    def test_the_catalogue_is_read_once_per_order_not_per_line(self):
        """ZERO per-line catalogue reads: one batch resolves the whole order.

        D01 got this down to one scoped read per line and documented that read as
        load-bearing — it is what proves an item belongs to the order's
        restaurant, and the chokepoint may not trust its caller. D02 keeps that
        guarantee and removes the repetition: the ONE snapshot query is itself
        restaurant-scoped, so every line is still proven in-tenant, and
        ``add_order_item`` still issues its own scoped query when no snapshot is
        supplied. A per-line delta of ZERO is therefore the strongest form of the
        same contract, not a relaxation of it.
        """
        self.menu_item_reads(1, 0)                       # warm
        one = self.menu_item_reads(1, 1)
        four = self.menu_item_reads(4, 2)
        self.assertEqual(four - one, 0)
        self.assertEqual(one, 1, 'the order resolves its catalogue in one query')

    def test_the_order_row_is_not_re_read_per_line(self):
        with CaptureQueriesContext(connection) as captured:
            self.create(self.lines(count=4), table_index=0)
        order_reads = [
            q['sql'] for q in captured.captured_queries
            if q['sql'].startswith('SELECT') and 'FROM "orders"' in q['sql']
        ]
        # Exactly two: the idempotency-free path reads none up front, so these are
        # the post-loop select_for_update re-read and update_order_amounts' own.
        # Four more (one per line) is the regression this guards.
        self.assertLessEqual(len(order_reads), 2, order_reads)


class CreateOrderEquivalenceTests(OrderPathBase):
    """The collapsed path must persist exactly what the un-collapsed one did."""

    def build_via_fallback(self, items):
        """An order filled through add_order_item's ORIGINAL read pattern.

        Passing neither `order` nor `menu_item` is the pre-collapse path — still
        live for every caller that is not `_create_order` — so running it against
        the same payload and diffing the persisted rows is a direct A/B rather
        than a comparison against numbers hand-copied from the old code.
        """
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.tables[7],
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=OrderStatus_Initiated,
            payment_status=PaymentStatus_Pending,
            fulfilment_status='new',
        )
        normalized = ConOrder.normalize_order_items(self.restaurant, items)
        self.assertEqual(normalized.get('status'), 200, normalized)
        for item in normalized['items']:
            result = ConOrder.add_order_item(item=item, order_id=str(order.id))
            self.assertEqual(result.get('status'), 200, result)
        ConOrder.update_order_amounts(order=order)
        return order

    def assert_paths_agree(self, items):
        collapsed = self.create(items, table_index=0)
        fallback = self.build_via_fallback(items)
        self.assertEqual(self.snapshot(collapsed), self.snapshot(fallback))
        for field in ('total_cost', 'discounted_cost', 'savings', 'actual_cost'):
            self.assertEqual(
                getattr(collapsed, field), getattr(fallback, field), field,
            )
        return collapsed

    def test_plain_lines_are_identical(self):
        self.assert_paths_agree(self.lines(quantity=2, count=4))

    def test_lines_with_modifiers_are_identical(self):
        order = self.assert_paths_agree([{
            'item': str(self.options_item.id), 'quantity': 3,
            'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]},
        }])
        line = OrderItem.objects.get(order=order)
        # 15000 base + 500 for the chosen size, three of them.
        self.assertEqual(line.unit_cost_of_options, Decimal('500.00'))
        self.assertEqual(line.actual_cost, Decimal('46500.00'))

    def test_lines_with_extras_are_identical(self):
        order = self.assert_paths_agree([{
            'item': str(self.parent.id), 'quantity': 1,
            'extras': [str(self.extra.id)],
        }])
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 2)
        self.assertEqual(
            OrderItem.objects.get(order=order, parent_item__isnull=False).item_id,
            self.extra.id,
        )

    def test_repeat_line_merge_is_identical(self):
        # The path the extras-COUNT collapse actually touches: the same menu item
        # twice in one submission merges into a single line with summed quantity.
        item_id = str(self.items[0].id)
        order = self.assert_paths_agree([
            {'item': item_id, 'quantity': 2},
            {'item': item_id, 'quantity': 3},
        ])
        line = OrderItem.objects.get(order=order)
        self.assertEqual(line.quantity, 5)

    def test_distinct_modifier_selections_stay_separate_lines(self):
        # The other half of merge semantics: Small and Large are different lines,
        # and must not be collapsed into one by the now-shared MenuItem read.
        order = self.assert_paths_agree([
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]}},
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_LARGE_ID]}},
        ])
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 2)

    def test_an_empty_selection_does_not_merge_into_a_modified_line(self):
        """THE INVERTED PIN — this test used to assert the opposite (D03).

        It previously read "an empty selection STILL merges into a modified
        line", pinning the behaviour as pre-existing and noting that "whether
        that is the right merge rule is a separate question". This is that
        question, answered: it is not.

        ``find_existing_order_item`` derived ``has_modifiers`` from the INCOMING
        selection alone and returned on its first branch without comparing
        anything, so a plain dish merged into a modified one and the diner was
        served two of the modified version and charged for two. The reverse
        submission order produced two lines correctly, which is what made the
        defect order-dependent and easy to miss.

        ABSENCE IS NOT A WILDCARD: an empty selection matches only another empty
        selection, in either submission order.
        """
        order = self.assert_paths_agree([
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]}},
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {}},
        ])
        lines = list(OrderItem.objects.filter(order=order))
        self.assertEqual(len(lines), 2)
        selections = [line.selected_modifiers or {} for line in lines]
        self.assertIn({}, selections, 'the plain line survived')
        self.assertIn({OPTION_GROUP_ID: [OPTION_SMALL_ID]}, selections,
                      'the modified line survived')

    def test_the_reverse_submission_order_agrees(self):
        """The same two lines submitted the other way round give the same result.

        Line identity must not depend on which line arrived first — that
        asymmetry was the whole shape of the D03 defect.
        """
        order = self.assert_paths_agree([
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {}},
            {'item': str(self.options_item.id), 'quantity': 1,
             'selected_modifiers': {OPTION_GROUP_ID: [OPTION_SMALL_ID]}},
        ])
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 2)


class NormalizeOrderItemsBatchTests(OrderPathBase):
    """The batched resolve keeps every rejection it used to make."""

    def test_resolves_every_line_in_one_query(self):
        items = self.lines(count=4)
        with CaptureQueriesContext(connection) as captured:
            result = ConOrder.normalize_order_items(self.restaurant, items)
        self.assertEqual(result['status'], 200)
        self.assertEqual(len(captured.captured_queries), 1)

    def test_one_line_costs_the_same_as_four(self):
        for count in (1, 4):
            with CaptureQueriesContext(connection) as captured:
                ConOrder.normalize_order_items(self.restaurant, self.lines(count=count))
            self.assertEqual(len(captured.captured_queries), 1, count)

    def test_foreign_item_is_rejected(self):
        other_owner = User.objects.create_user(
            first_name='Oth', last_name='Owner', email='oth@test.com',
            phone_number='256700000951', username='256700000951',
            country='Uganda', password='password', roles=[],
        )
        other = Restaurant.objects.create(
            name='Other R', location='loc-other', owner=other_owner,
            status=RestaurantStatus_Live,
        )
        other_section = MenuSection.objects.create(
            name='Theirs', restaurant=other,
            approved=True, enabled=True, available=True,
        )
        foreign = MenuItem.objects.create(
            name='Foreign', section=other_section, primary_price=Decimal('100'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        result = ConOrder.normalize_order_items(
            self.restaurant, [{'item': str(foreign.id), 'quantity': 1}],
        )
        self.assertEqual(result['status'], 400)

    def test_malformed_id_is_rejected(self):
        result = ConOrder.normalize_order_items(
            self.restaurant, [{'item': 'not-a-uuid', 'quantity': 1}],
        )
        self.assertEqual(result['status'], 400)

    def test_missing_item_key_is_rejected(self):
        result = ConOrder.normalize_order_items(self.restaurant, [{'quantity': 1}])
        self.assertEqual(result['status'], 400)

    def test_uppercase_uuid_still_resolves(self):
        # The batch keys on parsed UUIDs, not on raw strings, so a client sending a
        # differently-cased id resolves exactly as `pk=` used to.
        result = ConOrder.normalize_order_items(
            self.restaurant,
            [{'item': str(self.items[0].id).upper(), 'quantity': 1}],
        )
        self.assertEqual(result['status'], 200, result)
