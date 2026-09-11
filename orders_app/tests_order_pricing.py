"""
D02 / D03 — the corrected calculation and the canonical line identity.

EVERY EXPECTED AMOUNT BELOW IS WRITTEN OUT BY HAND from the stated inputs. No
assertion calls a production pricing helper, so nothing here can agree with the
code by construction.

The behavioural pre-fix defects each test closes are named in its docstring, with
the number the pre-fix code actually produced, so a reader can tell a reproduced
defect from a newly-stated policy.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, OrderStatus_Pending,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.manage_order import (
    REASON_NOTHING_TO_PREPARE, update_order_status,
)
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderItem
from restaurants_app.models import MenuItem, MenuSection, Restaurant, Table
from users_app.models import User

D = Decimal


class PricingBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='P', last_name='O', email='p@t.com',
            phone_number='256700088001', username='256700088001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Pricing R', location='pr', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.tables = [
            Table.objects.create(
                number=n, str_number=str(n), restaurant=self.restaurant,
                qr_mode='order_pay',
            )
            for n in range(1, 9)
        ]
        self._next_table = 0

    # -- fixtures --------------------------------------------------------
    def item(self, name, price=10000, **kw):
        opts = dict(approved=True, enabled=True, available=True,
                    primary_price=price)
        opts.update(kw)
        return MenuItem.objects.create(name=name, section=self.section, **opts)

    def with_choices(self, item, choices, group='g1', minimum=0, maximum=0):
        """`choices` is [(id, name, additionalCost), ...] — costs verbatim."""
        item.options = {
            'hasModifiers': True,
            'groups': [{
                'id': group, 'name': 'Options', 'type': 'multiple',
                'minSelections': minimum, 'maxSelections': maximum,
                'choices': [
                    {'id': cid, 'name': cname, 'additionalCost': cost,
                     'available': True}
                    for cid, cname, cost in choices
                ],
            }],
        }
        item.save(update_fields=['options'])
        return item

    def with_extras(self, parent, extras, minimum=0, maximum=0):
        parent.has_extras = True
        parent.extras_applicable = [str(e.id) for e in extras]
        parent.extras_min_selections = minimum
        parent.extras_max_selections = maximum
        parent.save()
        return parent

    def discount(self, item, percentage=None, amount=None):
        item.discount_details = {
            'discount_percentage': percentage or 0,
            'discount_amount': amount or 0,
            'start_date': '', 'end_date': '', 'recurring_days': [],
            'start_time': '', 'end_time': '',
        }
        item.save(update_fields=['discount_details'])
        return item

    # -- helpers ---------------------------------------------------------
    def place(self, items, created_by=None):
        table = self.tables[self._next_table]
        self._next_table += 1
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=items, created_by=created_by,
        )

    def order_of(self, response):
        self.assertEqual(response.get('status'), 200, response)
        return Order.objects.get(pk=response['data']['order_details']['id'])

    def parents(self, order):
        return list(OrderItem.objects.filter(
            order=order, parent_item__isnull=True, deleted=False,
        ))

    def children(self, order):
        return list(OrderItem.objects.filter(
            order=order, parent_item__isnull=False, deleted=False,
        ))


class ArithmeticTests(PricingBase):
    """Reference, effective and savings, at several quantities."""

    def test_the_worked_example(self):
        """qty 3; base 10 000 -> 8 000 at 20%; modifier 1 500; extra 2 000.

        Comparable original  (10 000 + 1 500 + 2 000) x 3 = 40 500
        Payable              ( 8 000 + 1 500 + 2 000) x 3 = 34 500
        Savings              (10 000 - 8 000)         x 3 =  6 000

        PRE-FIX this produced 32 000 / 30 500 / 1 500 — under-charging 4 000
        because the extra was persisted once instead of three times, and
        under-reporting savings because the reference excluded the modifier.
        """
        extra = self.item('Cheese', price=2000, is_extra=True)
        burger = self.with_extras(
            self.with_choices(self.discount(self.item('Burger'), percentage=20),
                              [('c1', 'Large', 1500)], maximum=1),
            [extra],
        )
        order = self.order_of(self.place([{
            'item': str(burger.id), 'quantity': 3,
            'selected_modifiers': {'g1': ['c1']},
            'extras': [str(extra.id)],
        }]))
        self.assertEqual(order.total_cost, D('40500.00'))
        self.assertEqual(order.actual_cost, D('34500.00'))
        self.assertEqual(order.savings, D('6000.00'))
        self.assertEqual(order.discounted_cost, D('34500.00'))

    def test_quantities_one_two_three_with_no_additions(self):
        """Base 7 250, no discount, no modifiers, no extras."""
        for quantity, expected in ((1, '7250.00'), (2, '14500.00'),
                                   (3, '21750.00')):
            plain = self.item(f'Plain {quantity}', price=7250)
            order = self.order_of(self.place(
                [{'item': str(plain.id), 'quantity': quantity}],
            ))
            self.assertEqual(order.actual_cost, D(expected))
            self.assertEqual(order.total_cost, D(expected))
            self.assertEqual(order.savings, D('0.00'))

    def test_a_paid_modifier_never_produces_negative_savings(self):
        """Base 10 000, no discount, modifier 1 500, qty 1.

        Reference 11 500, payable 11 500, savings 0. PRE-FIX: savings -1 500,
        with the order's net (11 500) exceeding its gross (10 000).
        """
        burger = self.with_choices(self.item('Burger'),
                                   [('c1', 'Large', 1500)], maximum=1)
        order = self.order_of(self.place([{
            'item': str(burger.id), 'quantity': 1,
            'selected_modifiers': {'g1': ['c1']},
        }]))
        self.assertEqual(order.total_cost, D('11500.00'))
        self.assertEqual(order.actual_cost, D('11500.00'))
        self.assertEqual(order.savings, D('0.00'))

    def test_a_discounted_extra_carries_its_own_savings(self):
        """Extra 3 000 at 25% -> 2 250, parent 5 000 undiscounted, qty 2.

        Reference (5 000 + 3 000) x 2 = 16 000
        Payable   (5 000 + 2 250) x 2 = 14 500
        Savings              (750) x 2 =  1 500
        """
        extra = self.discount(
            self.item('Sauce', price=3000, is_extra=True), percentage=25,
        )
        dish = self.with_extras(self.item('Dish', price=5000), [extra])
        order = self.order_of(self.place([{
            'item': str(dish.id), 'quantity': 2, 'extras': [str(extra.id)],
        }]))
        self.assertEqual(order.total_cost, D('16000.00'))
        self.assertEqual(order.actual_cost, D('14500.00'))
        self.assertEqual(order.savings, D('1500.00'))

    def test_repeated_quantity_updates_recompute_rather_than_compound(self):
        """Three submissions of the same configuration: 2 + 1 + 2 = 5.

        Base 10 000, modifier 1 500 -> unit 11 500, so 57 500 payable and
        cost_of_options 7 500. PRE-FIX the merge multiplied the ALREADY-EXTENDED
        cost_of_options by the new quantity (3 000 -> 9 000 -> 45 000) and never
        refreshed actual_cost at all.
        """
        burger = self.with_choices(self.item('Burger'),
                                   [('c1', 'Large', 1500)], maximum=1)
        line = {'item': str(burger.id),
                'selected_modifiers': {'g1': ['c1']}}
        order = self.order_of(self.place([
            dict(line, quantity=2), dict(line, quantity=1), dict(line, quantity=2),
        ]))
        rows = self.parents(order)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.quantity, 5)
        self.assertEqual(row.unit_price, D('11500.00'))
        self.assertEqual(row.cost_of_options, D('7500.00'))
        self.assertEqual(row.actual_cost, D('57500.00'))
        self.assertEqual(row.total_cost, D('57500.00'))
        self.assertEqual(order.actual_cost, D('57500.00'))

    def test_every_payable_row_is_counted_exactly_once(self):
        """Parent amounts exclude extras; the order sums the rows.

        Parent 4 000 x 2 = 8 000, extra 1 000 x 2 = 2 000, order 10 000. If the
        parent also carried its extras the order would read 12 000.
        """
        extra = self.item('Add', price=1000, is_extra=True)
        dish = self.with_extras(self.item('Dish', price=4000), [extra])
        order = self.order_of(self.place([{
            'item': str(dish.id), 'quantity': 2, 'extras': [str(extra.id)],
        }]))
        parent = self.parents(order)[0]
        child = self.children(order)[0]
        self.assertEqual(parent.actual_cost, D('8000.00'))
        self.assertEqual(child.actual_cost, D('2000.00'))
        self.assertEqual(order.actual_cost, D('10000.00'))


class RoundingTests(PricingBase):
    """ROUND_HALF_EVEN, applied to each unit component exactly once."""

    def test_half_even_ties_in_both_directions(self):
        """A discount landing exactly on a half-cent rounds to EVEN, both ways.

            200.25 at 50% = 100.125 -> 100.12   (half-UP would give 100.13)
            200.27 at 50% = 100.135 -> 100.14   (half-UP would also give .14)

        The first case is what distinguishes the two rules; the second is the
        control that shows half-even is not simply "always down".
        """
        cases = [('200.25', '50', D('100.12')), ('200.27', '50', D('100.14'))]
        for index, (primary, percentage, expected) in enumerate(cases):
            item = self.discount(
                self.item(f'Tie {index}', price=D(primary)), percentage=percentage,
            )
            order = self.order_of(self.place(
                [{'item': str(item.id), 'quantity': 1}],
            ))
            self.assertEqual(
                self.parents(order)[0].discounted_price, expected,
                f'{primary} at {percentage}% must quantize half-even',
            )

    def test_several_sub_cent_modifier_adjustments_are_canonical_each(self):
        """THE NEW COMPONENT BOUNDARY, stated as new.

        Three adjustments of 0.005 each. Canonicalised INDIVIDUALLY first:
            0.005 -> 0.00 (half-even, 0 is even), three times -> 0.00 total.
        Summing raw first would give 0.015 -> 0.02. The corrected convention
        rounds each component once, so a stored component and its contribution
        to the total always agree. Base 100 -> unit 100.00, qty 4 -> 400.00.
        """
        item = self.with_choices(self.item('Micro', price=100), [
            ('a', 'A', '0.005'), ('b', 'B', '0.005'), ('c', 'C', '0.005'),
        ], maximum=3)
        order = self.order_of(self.place([{
            'item': str(item.id), 'quantity': 4,
            'selected_modifiers': {'g1': ['a', 'b', 'c']},
        }]))
        row = self.parents(order)[0]
        self.assertEqual(row.unit_cost_of_options, D('0.00'))
        self.assertEqual(row.discounted_price, D('100.00'))
        self.assertEqual(row.actual_cost, D('400.00'))

    def test_the_group_subtotal_reconciles_with_the_line(self):
        """The displayed group cost equals the charged modifier component.

        Two choices at 12.34 and 7.66 -> group 20.00, which is exactly the
        line's unit_cost_of_options. Before D02 the label path summed into a
        float while pricing summed into a Decimal.
        """
        item = self.with_choices(self.item('Recon', price=1000), [
            ('a', 'A', '12.34'), ('b', 'B', 7.66),
        ], maximum=2)
        order = self.order_of(self.place([{
            'item': str(item.id), 'quantity': 1,
            'selected_modifiers': {'g1': ['a', 'b']},
        }]))
        row = self.parents(order)[0]
        self.assertEqual(row.unit_cost_of_options, D('20.00'))
        self.assertEqual(row.options[0]['cost_amount'], '20.00')
        self.assertEqual(D(str(row.options[0]['cost'])), D('20.00'))
        self.assertEqual(row.discounted_price, D('1020.00'))

    def test_recomputing_a_line_twice_is_identical(self):
        """Two merges onto the same line, then the same total either way."""
        item = self.with_choices(self.item('Stable', price=333),
                                 [('c1', 'C', '0.33')], maximum=1)
        line = {'item': str(item.id), 'selected_modifiers': {'g1': ['c1']}}
        once = self.order_of(self.place([dict(line, quantity=6)]))
        twice = self.order_of(self.place([
            dict(line, quantity=1), dict(line, quantity=2), dict(line, quantity=3),
        ]))
        # unit 333.33, six of them.
        self.assertEqual(once.actual_cost, D('1999.98'))
        self.assertEqual(twice.actual_cost, D('1999.98'))
        self.assertEqual(self.parents(once)[0].quantity, 6)
        self.assertEqual(self.parents(twice)[0].quantity, 6)


class ScalingTests(PricingBase):
    """One extra per unit of its parent dish (P1)."""

    def test_three_dishes_need_three_extras(self):
        extra = self.item('Cheese', price=2000, is_extra=True)
        dish = self.with_extras(self.item('Dish'), [extra])
        order = self.order_of(self.place([{
            'item': str(dish.id), 'quantity': 3, 'extras': [str(extra.id)],
        }]))
        child = self.children(order)[0]
        self.assertEqual(child.quantity, 3)
        self.assertEqual(child.actual_cost, D('6000.00'))
        self.assertEqual(order.actual_cost, D('36000.00'))

    def test_qty_three_equals_one_plus_two(self):
        extra = self.item('Cheese', price=2000, is_extra=True)
        dish = self.with_extras(
            self.with_choices(self.item('Dish'), [('c1', 'L', 1500)], maximum=1),
            [extra],
        )
        line = {'item': str(dish.id), 'selected_modifiers': {'g1': ['c1']},
                'extras': [str(extra.id)]}
        single = self.order_of(self.place([dict(line, quantity=3)]))
        split = self.order_of(self.place([
            dict(line, quantity=1), dict(line, quantity=2),
        ]))
        # (10 000 + 1 500 + 2 000) x 3 = 40 500
        for order in (single, split):
            self.assertEqual(order.actual_cost, D('40500.00'))
            self.assertEqual(len(self.parents(order)), 1)
            self.assertEqual(self.parents(order)[0].quantity, 3)
            self.assertEqual(self.children(order)[0].quantity, 3)
        # And the PER-LINE values agree too — the figure the reports aggregate.
        self.assertEqual(self.parents(single)[0].actual_cost,
                         self.parents(split)[0].actual_cost)

    def test_modifiers_do_not_compound_with_quantity(self):
        """unit_cost_of_options stays per-unit however many merges happen."""
        item = self.with_choices(self.item('M', price=1000),
                                 [('c1', 'C', 250)], maximum=1)
        line = {'item': str(item.id), 'selected_modifiers': {'g1': ['c1']}}
        order = self.order_of(self.place([dict(line, quantity=4)] * 3))
        row = self.parents(order)[0]
        self.assertEqual(row.quantity, 12)
        self.assertEqual(row.unit_cost_of_options, D('250.00'))
        self.assertEqual(row.cost_of_options, D('3000.00'))
        self.assertEqual(row.actual_cost, D('15000.00'))

    def test_a_merged_quantity_above_the_per_line_ceiling_stays_legal(self):
        """99 is the SUBMITTED ceiling, never the stored one (D01's rule)."""
        item = self.item('Bulk', price=100)
        line = {'item': str(item.id), 'quantity': 99}
        order = self.order_of(self.place([line, line, dict(line, quantity=2)]))
        row = self.parents(order)[0]
        self.assertEqual(row.quantity, 200)
        self.assertEqual(row.actual_cost, D('20000.00'))


class LineIdentityTests(PricingBase):
    """D03 — absence is not a wildcard, and order does not decide identity."""

    def two_configs(self):
        item = self.with_choices(self.item('Burger'), [
            ('A', 'A', 1000), ('B', 'B', 2000),
        ], maximum=2)
        return item

    def test_plain_then_modified_and_the_reverse(self):
        item = self.two_configs()
        forward = self.order_of(self.place([
            {'item': str(item.id), 'quantity': 1},
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['A']}},
        ]))
        reverse = self.order_of(self.place([
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['A']}},
            {'item': str(item.id), 'quantity': 1},
        ]))
        for order in (forward, reverse):
            self.assertEqual(len(self.parents(order)), 2)
            # 10 000 plain + 11 000 modified
            self.assertEqual(order.actual_cost, D('21000.00'))

    def test_extras_then_none_and_the_reverse(self):
        """The reverse direction used to raise TypeError -> HTTP 500."""
        extra = self.item('E', price=2000, is_extra=True)
        dish = self.with_extras(self.item('Dish'), [extra])
        forward = self.order_of(self.place([
            {'item': str(dish.id), 'quantity': 1},
            {'item': str(dish.id), 'quantity': 1, 'extras': [str(extra.id)]},
        ]))
        reverse = self.order_of(self.place([
            {'item': str(dish.id), 'quantity': 1, 'extras': [str(extra.id)]},
            {'item': str(dish.id), 'quantity': 1},
        ]))
        for order in (forward, reverse):
            self.assertEqual(len(self.parents(order)), 2)
            self.assertEqual(len(self.children(order)), 1)
            self.assertEqual(order.actual_cost, D('22000.00'))

    def test_two_distinct_variants_and_an_a_b_a_submission(self):
        item = self.two_configs()
        order = self.order_of(self.place([
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['A']}},
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['B']}},
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['A']}},
        ]))
        rows = self.parents(order)
        self.assertEqual(len(rows), 2, 'the A line was not first, and still matched')
        quantities = sorted(row.quantity for row in rows)
        self.assertEqual(quantities, [1, 2])
        # A x2 at 11 000 + B x1 at 12 000
        self.assertEqual(order.actual_cost, D('34000.00'))

    def test_reordered_equivalent_selections_compare_equal(self):
        item = self.two_configs()
        order = self.order_of(self.place([
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['A', 'B']}},
            {'item': str(item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['B', 'A']}},
        ]))
        self.assertEqual(len(self.parents(order)), 1)
        self.assertEqual(self.parents(order)[0].quantity, 2)
        self.assertEqual(order.actual_cost, D('26000.00'))

    def test_reordered_extras_lists_compare_equal_and_both_scale(self):
        e1 = self.item('E1', price=2000, is_extra=True)
        e2 = self.item('E2', price=3000, is_extra=True)
        dish = self.with_extras(self.item('Dish'), [e1, e2], maximum=2)
        order = self.order_of(self.place([
            {'item': str(dish.id), 'quantity': 1,
             'extras': [str(e1.id), str(e2.id)]},
            {'item': str(dish.id), 'quantity': 1,
             'extras': [str(e2.id), str(e1.id)]},
        ]))
        self.assertEqual(len(self.parents(order)), 1)
        self.assertEqual(self.parents(order)[0].quantity, 2)
        self.assertEqual(len(self.children(order)), 2)
        for child in self.children(order):
            self.assertEqual(child.quantity, 2)
        # (10 000 + 2 000 + 3 000) x 2
        self.assertEqual(order.actual_cost, D('30000.00'))

    def test_absent_null_and_empty_selections_are_the_same_line(self):
        item = self.two_configs()
        order = self.order_of(self.place([
            {'item': str(item.id), 'quantity': 1},
            {'item': str(item.id), 'quantity': 1, 'selected_modifiers': None},
            {'item': str(item.id), 'quantity': 1, 'selected_modifiers': {}},
        ]))
        self.assertEqual(len(self.parents(order)), 1)
        self.assertEqual(self.parents(order)[0].quantity, 3)

    def test_an_extra_and_a_top_level_line_of_the_same_dish_stay_separate(self):
        """A dish that is ALSO an extra must not merge into its own child row.

        PRE-FIX the candidate query had no ``parent_item__isnull=True`` and
        ``Meta.ordering`` is ``-time_created``, so the standalone line matched
        the extra row written moments earlier.
        """
        fries = self.item('Fries', price=2000, is_extra=True)
        burger = self.with_extras(self.item('Burger'), [fries])
        order = self.order_of(self.place([
            {'item': str(burger.id), 'quantity': 1, 'extras': [str(fries.id)]},
            {'item': str(fries.id), 'quantity': 1},
        ]))
        parents = self.parents(order)
        children = self.children(order)
        self.assertEqual(len(parents), 2)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].quantity, 1)
        self.assertEqual({row.item_id for row in parents}, {burger.id, fries.id})
        # burger 10 000 + its fries 2 000 + a standalone fries 2 000
        self.assertEqual(order.actual_cost, D('14000.00'))


class AvailabilityTests(PricingBase):
    """Deliverability, dependent extras and the submit guard."""

    def test_a_repeated_sold_out_line_never_acquires_cost(self):
        """PRE-FIX the merge added the incoming quantity onto the stored 0 and
        multiplied the unit price by it, so a sold-out dish came back payable at
        10 000 while still flagged unavailable."""
        sold_out = self.item('Sold out', in_stock=False)
        order = self.order_of(self.place([
            {'item': str(sold_out.id), 'quantity': 1},
            {'item': str(sold_out.id), 'quantity': 2},
        ]))
        row = self.parents(order)[0]
        self.assertEqual(row.quantity, 0)
        self.assertEqual(row.actual_cost, D('0.00'))
        self.assertFalse(row.available)
        self.assertEqual(order.actual_cost, D('0.00'))

    def test_an_undeliverable_parent_takes_its_extras_with_it(self):
        """PRE-FIX the extra stayed quantity 1, available and payable — and the
        kitchen board rendered no main to attach it to."""
        extra = self.item('Cheese', price=2000, is_extra=True)
        dish = self.with_extras(self.item('Dish', in_stock=False), [extra])
        response = self.place([{
            'item': str(dish.id), 'quantity': 2, 'extras': [str(extra.id)],
        }])
        order = self.order_of(response)
        child = self.children(order)[0]
        self.assertEqual(child.quantity, 0)
        self.assertEqual(child.actual_cost, D('0.00'))
        self.assertFalse(child.available)
        self.assertEqual(order.actual_cost, D('0.00'))
        # P5: ONE loss is reported, not two.
        details = response['data']['order_details']
        self.assertEqual(details['no_unavailable_items'], 1)
        self.assertEqual(details['no_unavailable_extras'], 0)
        # The snapshots survive for the diner's reconciliation.
        self.assertEqual(child.unit_price, D('2000.00'))
        self.assertEqual(child.item_name_snapshot, 'Cheese')

    def test_an_unavailable_optional_extra_leaves_its_parent_orderable(self):
        available = self.item('Ok', price=1000, is_extra=True)
        gone = self.item('Gone', price=1000, is_extra=True, in_stock=False)
        dish = self.with_extras(self.item('Dish'), [available, gone], maximum=2)
        response = self.place([{
            'item': str(dish.id), 'quantity': 1,
            'extras': [str(available.id), str(gone.id)],
        }])
        order = self.order_of(response)
        self.assertTrue(self.parents(order)[0].available)
        self.assertEqual(order.actual_cost, D('11000.00'))
        self.assertEqual(
            response['data']['order_details']['no_unavailable_extras'], 1,
        )

    def test_a_required_minimum_that_survives_keeps_the_parent(self):
        """P4's precision: ONE unavailable extra does not condemn the dish when
        the remaining selections still satisfy the minimum."""
        ok = self.item('Ok', price=1000, is_extra=True)
        gone = self.item('Gone', price=1000, is_extra=True, in_stock=False)
        dish = self.with_extras(self.item('Dish'), [ok, gone],
                                minimum=1, maximum=2)
        order = self.order_of(self.place([{
            'item': str(dish.id), 'quantity': 1,
            'extras': [str(ok.id), str(gone.id)],
        }]))
        self.assertTrue(self.parents(order)[0].available)
        self.assertEqual(order.actual_cost, D('11000.00'))

    def test_a_required_minimum_that_cannot_be_met_drops_the_parent(self):
        """PRE-FIX the dish shipped available and payable without its required
        extra, because the gate counted SUBMITTED rather than surviving extras.
        Nothing is substituted — the diner's own selection is simply not
        deliverable."""
        gone = self.item('Gone', price=1000, is_extra=True, in_stock=False)
        dish = self.with_extras(self.item('Dish'), [gone], minimum=1, maximum=1)
        response = self.place([{
            'item': str(dish.id), 'quantity': 1, 'extras': [str(gone.id)],
        }])
        order = self.order_of(response)
        row = self.parents(order)[0]
        self.assertFalse(row.available)
        self.assertEqual(row.quantity, 0)
        self.assertEqual(order.actual_cost, D('0.00'))
        details = response['data']['order_details']
        self.assertEqual(details['no_unavailable_items'], 1)
        self.assertEqual(details['no_unavailable_extras'], 0)

    def test_a_free_dish_is_orderable_and_submittable(self):
        """The submit guard tests DELIVERABILITY AND QUANTITY, never a payable
        amount — a legitimately free dish must still reach the kitchen."""
        free = self.item('Free water', price=0)
        response = self.place([{'item': str(free.id), 'quantity': 2}])
        order = self.order_of(response)
        self.assertEqual(order.actual_cost, D('0.00'))
        self.assertTrue(self.parents(order)[0].available)
        self.assertEqual(self.parents(order)[0].quantity, 2)
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
        )
        self.assertEqual(result['status'], 200, result)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)

    def test_an_order_with_nothing_to_prepare_cannot_be_submitted(self):
        sold_out = self.item('Sold out', in_stock=False)
        order = self.order_of(self.place([
            {'item': str(sold_out.id), 'quantity': 1},
        ]))
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['reason'], REASON_NOTHING_TO_PREPARE)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)
