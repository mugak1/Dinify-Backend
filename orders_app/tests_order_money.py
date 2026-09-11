"""
D02 — monetary input from UNVALIDATED catalogue columns.

``MenuItem.options`` and ``discount_details`` are plain ``JSONField``s, and D01
validates the STRUCTURE of a modifier definition while saying in terms that it
"validates NO monetary configuration". This is that half.

THE RULE, and it has one shape everywhere: a malformed value is REFUSED through a
controlled diner-facing message and is NEVER defaulted to zero. Zero is a real,
supported price — a free modifier, a free dish, a waived extra — so using it as
the failure value would make a paid option free and be indistinguishable from an
operator configuring one deliberately.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from misc_app.controllers.money import (
    MoneyConfigError, extend_money, parse_money, quantize_money,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.models import Order, OrderItem
from restaurants_app.controllers.pricing_policy import resolve_price
from restaurants_app.models import MenuItem, MenuSection, Restaurant, Table
from users_app.models import User

D = Decimal


class MoneyContractTests(TestCase):
    """The shared parser, exercised directly."""

    def test_supported_catalogue_forms_are_accepted(self):
        cases = [
            (1500, D('1500.00')), ('1500', D('1500.00')),
            (1500.5, D('1500.50')), ('1500.50', D('1500.50')),
            (0, D('0.00')), ('0', D('0.00')), (D('12.345'), D('12.34')),
            ('  12 ', D('12.00')),
        ]
        for raw, expected in cases:
            self.assertEqual(parse_money(raw), expected, repr(raw))

    def test_malformed_and_unsupported_values_are_refused(self):
        for raw in ['abc', None, True, False, [], {}, (), object(),
                    'NaN', 'Infinity', '-Infinity', '1e400', '', ' ']:
            with self.assertRaises(MoneyConfigError, msg=repr(raw)):
                parse_money(raw)

    def test_non_finite_floats_are_refused_before_arithmetic(self):
        for raw in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(MoneyConfigError):
                parse_money(raw)

    def test_a_negative_is_refused_unless_the_call_site_allows_it(self):
        with self.assertRaises(MoneyConfigError):
            parse_money(-500)
        self.assertEqual(parse_money(-500, allow_negative=True), D('-500.00'))

    def test_out_of_range_magnitudes_are_refused(self):
        with self.assertRaises(MoneyConfigError):
            parse_money('1' + '0' * 60)
        with self.assertRaises(MoneyConfigError):
            extend_money(D('1' + '0' * 45), 10 ** 6)

    def test_extending_is_exact_and_repeatable(self):
        unit = quantize_money(D('899.105'))          # half-even -> 899.10
        self.assertEqual(unit, D('899.10'))
        self.assertEqual(extend_money(unit, 3), D('2697.30'))
        self.assertEqual(extend_money(unit, 3), extend_money(unit, 3))
        self.assertEqual(extend_money(unit, 0), D('0.00'))

    def test_a_quantity_is_an_integer_not_a_coercion(self):
        for bad in (True, '3', 2.0, None, [3]):
            with self.assertRaises(MoneyConfigError):
                extend_money(D('1.00'), bad)


class DiscountPolicyTests(TestCase):
    """The shared price verdict — supported absence vs unreadable."""

    def test_absent_and_zero_discounts_are_inactive_not_errors(self):
        for details in ({}, None, [], {'discount_percentage': 0},
                        {'discount_percentage': None},
                        {'discount_percentage': '', 'discount_amount': ''},
                        {'discount_percentage': [], 'discount_amount': {}}):
            verdict = resolve_price(10000, details)
            self.assertTrue(verdict.usable, repr(details))
            self.assertFalse(verdict.discount_active, repr(details))
            self.assertEqual(verdict.effective_base, D('10000.00'))

    def test_a_non_positive_magnitude_still_means_no_discount(self):
        """PRE-EXISTING semantics, deliberately preserved: a stored -5% has
        always meant "no discount", and turning that harmless data error into an
        unorderable item would be a regression in availability."""
        for details in ({'discount_percentage': -5},
                        {'discount_amount': -5},
                        {'discount_percentage': '-5'}):
            verdict = resolve_price(10000, details)
            self.assertTrue(verdict.usable, repr(details))
            self.assertFalse(verdict.discount_active, repr(details))
            self.assertEqual(verdict.effective_base, D('10000.00'))

    def test_an_unreadable_active_discount_is_unusable_not_undiscounted(self):
        for details in ({'discount_percentage': 'abc'},
                        {'discount_percentage': True},
                        {'discount_amount': 'NaN'},
                        {'discount_percentage': '1e400'}):
            verdict = resolve_price(10000, details)
            self.assertFalse(verdict.usable, repr(details))

    def test_a_discount_larger_than_the_price_is_unusable_not_free(self):
        """PRE-FIX ``effective_base_price`` ended in
        ``price if price > 0 else Decimal('0')``, so this made the dish FREE."""
        self.assertFalse(resolve_price(1000, {'discount_amount': 5000}).usable)
        self.assertFalse(resolve_price(1000, {'discount_percentage': 150}).usable)

    def test_a_complete_discount_is_legal_and_prices_at_zero(self):
        verdict = resolve_price(1000, {'discount_percentage': 100})
        self.assertTrue(verdict.usable)
        self.assertTrue(verdict.discount_active)
        self.assertEqual(verdict.effective_base, D('0.00'))

    def test_an_unreadable_base_price_is_unusable(self):
        self.assertFalse(resolve_price('abc', {}).usable)

    def test_a_null_base_price_keeps_its_established_reading(self):
        """``primary_price or 0`` is pre-existing and preserved. The column is
        NOT NULL so this is unreachable from stored data, but the reading must
        not change silently: None means 0, and 0 is a legal free price."""
        verdict = resolve_price(None, {})
        self.assertTrue(verdict.usable)
        self.assertEqual(verdict.reference_base, D('0.00'))


class CheckoutMoneyTests(TestCase):
    """The same rule reached through the real order path."""

    def setUp(self):
        owner = User.objects.create_user(
            first_name='M', last_name='O', email='m@t.com',
            phone_number='256700055001', username='256700055001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Money R', location='mr', owner=owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant, qr_mode='order_pay')
            for n in range(1, 30)
        ]
        self._next = 0

    def _item(self, name, cost):
        item = MenuItem.objects.create(
            name=name, section=self.section, primary_price=D('10000'),
            approved=True, enabled=True, available=True,
            options={
                'hasModifiers': True,
                'groups': [{
                    'id': 'g1', 'name': 'Size', 'type': 'single',
                    'minSelections': 0, 'maxSelections': 1,
                    'choices': [{'id': 'c1', 'name': 'Large',
                                 'additionalCost': cost, 'available': True}],
                }],
            },
        )
        return item

    def _order(self, item):
        table = self.tables[self._next]
        self._next += 1
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=[{'item': str(item.id), 'quantity': 1,
                    'selected_modifiers': {'g1': ['c1']}}],
        )

    def test_malformed_modifier_costs_are_controlled_refusals_not_500s(self):
        """PRE-FIX each of these raised ``decimal.InvalidOperation`` (or, for
        'NaN', a serializer error) straight out of checkout as an uncaught
        HTTP 500."""
        for index, cost in enumerate(
            ['abc', None, True, [], {}, 'NaN', 'Infinity', '1e400'],
        ):
            item = self._item(f'Bad {index}', cost)
            orders_before = Order.objects.count()
            items_before = OrderItem.objects.count()
            response = self._order(item)
            self.assertEqual(response.get('status'), 400, repr(cost))
            self.assertIn('cannot be ordered right now',
                          response.get('message', ''), repr(cost))
            self.assertEqual(Order.objects.count(), orders_before, repr(cost))
            self.assertEqual(OrderItem.objects.count(), items_before, repr(cost))

    def test_an_invalid_price_never_becomes_free(self):
        item = self._item('Never free', 'abc')
        response = self._order(item)
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(Order.objects.count(), 0, 'nothing was charged at all')

    def test_supported_costs_price_the_line(self):
        for cost, expected in (
            (1500, D('11500.00')), ('1500', D('11500.00')),
            (1500.5, D('11500.50')), ('0', D('10000.00')),
            (-500, D('9500.00')),               # signed adjustments stay legal
        ):
            item = self._item(f'Ok {cost!r}', cost)
            response = self._order(item)
            self.assertEqual(response.get('status'), 200, repr(cost))
            order = Order.objects.get(pk=response['data']['order_details']['id'])
            self.assertEqual(order.actual_cost, expected, repr(cost))

    def test_a_negative_adjustment_larger_than_the_price_is_refused(self):
        """Signed adjustments are legal; a NEGATIVE PAYABLE unit is not, and it
        is refused rather than clamped up to zero."""
        item = self._item('Too negative', -50000)
        response = self._order(item)
        self.assertEqual(response.get('status'), 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_free_dish_is_orderable(self):
        free = MenuItem.objects.create(
            name='Free', section=self.section, primary_price=D('0'),
            approved=True, enabled=True, available=True,
        )
        table = self.tables[self._next]
        self._next += 1
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=[{'item': str(free.id), 'quantity': 2}],
        )
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        self.assertEqual(order.actual_cost, D('0.00'))
        row = OrderItem.objects.get(order=order)
        self.assertTrue(row.available)
        self.assertEqual(row.quantity, 2)
