"""
D03 completion B — the merge key must compare the facts it claims to compare.

``line_identity`` binds a parent line to its item, selections, unit components,
deliverability and preparation snapshots. Its extras component is the set of
extra IDS ONLY, so two lines merge on "same dish, same extras chosen" while the
extras' own resolved PRICE, DELIVERABILITY and PREPARATION snapshots are absent
from the comparison.

TWO DISTINCT DEFECTS, and they are reachable by different routes. Both are
stated here rather than collapsed, because their reachability is not the same
and a fix must not be justified by the wrong one:

  1. THE REQUIRED-EXTRA OUTCOME IS DECIDED AFTER THE MERGE KEY IS ASSIGNED.
     Reachable over HTTP, in one ordinary request. ``add_order_item`` computes
     the lookup identity from the parent's OWN deliverability, then writes the
     row, then processes the extras, and only then applies the
     ``extras_min_selections`` rule that can flip the line undeliverable — and
     re-keys the index on that FLIPPED state. So the key a line is STORED under
     is not the key the next identical line is LOOKED UP with, and two identical
     selections become two rows.

  2. A MERGE CAN KEEP ONE CHILD'S OLD PRICE FOR A NEW SELECTION. NOT reachable
     over HTTP today, and this file does not claim otherwise: no live route adds
     an item to an existing order (v2 `add-items` was retired, `submit` adds
     nothing), and within ONE `initiate` every line is priced from ONE catalogue
     snapshot, so equal extra ids always resolve to equal child facts. It is
     reachable through ``add_order_item``'s SUPPORTED no-snapshot path, whose own
     docstring states it self-guards for "a caller that skips the endpoint" — a
     supported contract, exercised by this repository's own tenant-isolation
     suite, not a hypothetical future caller.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from orders_app.controllers.con_orders import ConOrder
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantTag, Table,
)
from users_app.models import User

D = Decimal


class _MergeBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='M', last_name='O', email='merge-owner@test.com',
            phone_number='256700088001', username='256700088001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Merge R', location='merge', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant, qr_mode='order_pay')
            for n in range(1, 6)
        ]
        self._next_table = 0

    def item(self, name, price=D('10000'), **kw):
        opts = dict(approved=True, enabled=True, available=True, in_stock=True,
                    primary_price=price)
        opts.update(kw)
        return MenuItem.objects.create(name=name, section=self.section, **opts)

    def with_extras(self, parent, extras, minimum=0, maximum=0):
        parent.has_extras = True
        parent.extras_applicable = [str(e.id) for e in extras]
        parent.extras_min_selections = minimum
        parent.extras_max_selections = maximum
        parent.save()
        return parent

    def place(self, lines):
        table = self.tables[self._next_table]
        self._next_table += 1
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=lines,
        )

    def parent_rows(self, order):
        return list(OrderItem.objects.filter(order=order,
                                             parent_item__isnull=True))


class RequiredExtraOutcomeOrderingTests(_MergeBase):
    """Defect 1 — the HTTP-reachable one."""

    def test_two_identical_lines_with_an_unmet_required_extra_are_one_line(self):
        dish = self.item('Curry', price=D('12000'))
        sauce = self.item('Sauce', price=D('1500'), is_extra=True)
        self.with_extras(dish, [sauce], minimum=1)
        # The required extra has sold out: every line carrying it is
        # undeliverable, by the P4 rule.
        sauce.in_stock = False
        sauce.save(update_fields=['in_stock'])

        line = {'item': str(dish.id), 'quantity': 1, 'extras': [str(sauce.id)]}
        response = self.place([dict(line), dict(line)])
        self.assertEqual(response.get('status'), 200, response)

        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = self.parent_rows(order)
        self.assertEqual(
            len(rows), 1,
            'the same selection was stored twice: the line was keyed on its '
            'post-flip deliverability but looked up on its pre-flip one',
        )
        self.assertFalse(rows[0].available)

    def test_the_deliverable_equivalent_still_merges(self):
        """The control: with the required extra in stock, nothing changes."""
        dish = self.item('Curry 2', price=D('12000'))
        sauce = self.item('Sauce 2', price=D('1500'), is_extra=True)
        self.with_extras(dish, [sauce], minimum=1)

        line = {'item': str(dish.id), 'quantity': 1, 'extras': [str(sauce.id)]}
        response = self.place([dict(line), dict(line)])
        self.assertEqual(response.get('status'), 200, response)

        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = self.parent_rows(order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
        self.assertTrue(rows[0].available)
        # One extra row, scaled to the parent — the D02/P1 rule, unchanged.
        children = list(OrderItem.objects.filter(parent_item=rows[0]))
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].quantity, 2)

    def test_a_dish_without_a_required_minimum_is_untouched(self):
        """The other control: the flip only applies where a minimum exists."""
        dish = self.item('Curry 3', price=D('12000'))
        sauce = self.item('Sauce 3', price=D('1500'), is_extra=True)
        self.with_extras(dish, [sauce], minimum=0)
        sauce.in_stock = False
        sauce.save(update_fields=['in_stock'])

        line = {'item': str(dish.id), 'quantity': 1, 'extras': [str(sauce.id)]}
        response = self.place([dict(line), dict(line)])
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = self.parent_rows(order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
        self.assertTrue(rows[0].available, 'the dish itself is still makeable')


class ChildSnapshotIdentityTests(_MergeBase):
    """Defect 2 — the supported no-snapshot path.

    Each test drives ``add_order_item`` directly, WITHOUT a snapshot, which is
    the contract that path documents. None of this is claimed to be reachable
    from an HTTP route today.
    """

    def _order_with_one_line(self, dish, extra):
        response = self.place([
            {'item': str(dish.id), 'quantity': 1, 'extras': [str(extra.id)]},
        ])
        self.assertEqual(response.get('status'), 200, response)
        return Order.objects.get(pk=response['data']['order_details']['id'])

    def test_a_repriced_extra_does_not_merge_into_the_old_price(self):
        dish = self.item('Pie', price=D('8000'))
        cheese = self.item('Cheese', price=D('1000'), is_extra=True)
        self.with_extras(dish, [cheese])
        order = self._order_with_one_line(dish, cheese)

        # The operator reprices the EXTRA only. The dish is untouched, so the
        # parent half of the key still matches exactly.
        cheese.primary_price = D('2000')
        cheese.save(update_fields=['primary_price'])

        result = ConOrder.add_order_item(
            item={'item': str(dish.id), 'quantity': 1,
                  'extras': [str(cheese.id)]},
            order_id=str(order.id),
        )
        self.assertEqual(result.get('status'), 200, result)

        rows = self.parent_rows(order)
        self.assertEqual(
            len(rows), 2,
            'a second dish was merged onto a line whose stored extra is priced '
            f'at {D("1000")}, so its cheese was charged at the old price',
        )
        prices = sorted(
            OrderItem.objects.filter(order=order, parent_item__isnull=False)
            .values_list('unit_price', flat=True)
        )
        self.assertEqual(prices, [D('1000.00'), D('2000.00')])

    def test_a_sold_out_extra_does_not_merge_into_an_available_one(self):
        dish = self.item('Pie 2', price=D('8000'))
        cheese = self.item('Cheese 2', price=D('1000'), is_extra=True)
        self.with_extras(dish, [cheese])
        order = self._order_with_one_line(dish, cheese)

        cheese.in_stock = False
        cheese.save(update_fields=['in_stock'])

        result = ConOrder.add_order_item(
            item={'item': str(dish.id), 'quantity': 1,
                  'extras': [str(cheese.id)]},
            order_id=str(order.id),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(
            len(self.parent_rows(order)), 2,
            'a dish whose cheese has sold out merged into one whose cheese is '
            'still being made',
        )

    def test_a_renamed_extra_does_not_merge_into_the_old_preparation(self):
        dish = self.item('Pie 3', price=D('8000'))
        cheese = self.item('Cheese 3', price=D('1000'), is_extra=True)
        self.with_extras(dish, [cheese])
        order = self._order_with_one_line(dish, cheese)

        cheese.name = 'Vegan cheese'
        cheese.save(update_fields=['name'])

        result = ConOrder.add_order_item(
            item={'item': str(dish.id), 'quantity': 1,
                  'extras': [str(cheese.id)]},
            order_id=str(order.id),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(
            len(self.parent_rows(order)), 2,
            'the kitchen would have been told to make two of the OLD cheese',
        )

    def test_a_changed_extra_allergen_snapshot_does_not_merge(self):
        dish = self.item('Pie 4', price=D('8000'))
        cheese = self.item('Cheese 4', price=D('1000'), is_extra=True)
        self.with_extras(dish, [cheese])
        order = self._order_with_one_line(dish, cheese)

        milk = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Milk', category='allergen',
            icon='milk', colour='white',
        )
        cheese.sync_tag_links([milk.id])

        result = ConOrder.add_order_item(
            item={'item': str(dish.id), 'quantity': 1,
                  'extras': [str(cheese.id)]},
            order_id=str(order.id),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(
            len(self.parent_rows(order)), 2,
            'an allergen label is a preparation instruction, not a display '
            'detail — two lines carrying different ones are not one line',
        )

    def test_an_unchanged_extra_still_merges(self):
        """THE CONTROL. Widening the key must not stop identical lines merging,
        which would be its own defect (a diner served two separate rows for one
        repeated selection)."""
        dish = self.item('Pie 5', price=D('8000'))
        cheese = self.item('Cheese 5', price=D('1000'), is_extra=True)
        self.with_extras(dish, [cheese])
        order = self._order_with_one_line(dish, cheese)

        result = ConOrder.add_order_item(
            item={'item': str(dish.id), 'quantity': 1,
                  'extras': [str(cheese.id)]},
            order_id=str(order.id),
        )
        self.assertEqual(result.get('status'), 200, result)
        rows = self.parent_rows(order)
        self.assertEqual(len(rows), 1, 'an identical repeat stopped merging')
        self.assertEqual(rows[0].quantity, 2)

    def test_reordered_extras_still_merge(self):
        """The order the diner tapped the extras in is not part of identity."""
        dish = self.item('Pie 6', price=D('8000'))
        a = self.item('Bacon 6', price=D('1000'), is_extra=True)
        b = self.item('Onion 6', price=D('500'), is_extra=True)
        self.with_extras(dish, [a, b])

        response = self.place([
            {'item': str(dish.id), 'quantity': 1,
             'extras': [str(a.id), str(b.id)]},
            {'item': str(dish.id), 'quantity': 1,
             'extras': [str(b.id), str(a.id)]},
        ])
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = self.parent_rows(order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
