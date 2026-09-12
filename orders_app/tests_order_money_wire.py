"""
D02 completion A — exact money through calculation AND the rendered HTTP wire.

Three things are proved here that no existing suite proves, because each of them
is invisible to a test that stops at ``response.data``:

  1. WHAT THE DINER'S BROWSER ACTUALLY RECEIVES. DRF's ``JSONRenderer`` encodes a
     ``Decimal`` as ``float(obj)``, so a quote amount assembled as an exact
     ``Decimal`` still leaves the server as a JSON float — ``Decimal('899.10')``
     renders ``899.1``, and a large exact amount loses digits outright. Asserting
     on ``response.data`` compares the value the view built, never the value the
     client parses, so it cannot see this at all. These tests decode
     ``response.content``.

  2. ARITHMETIC OUTSIDE THE MODULE'S OWN DECIMAL CONTEXT. ``price_unit`` and
     ``extend`` take ``working_context()``; the ORDER ROLLUP and the QUOTE
     SERIALIZER did not, and Python's default context is 28 significant digits
     against columns that hold 50. Addition and subtraction ROUND SILENTLY there
     rather than raising, so a sum of schema-valid amounts can come back short of
     what every row stores, with nothing to notice.

  3. ROUNDING RESCUING AN INVALID CONFIGURATION. An over-100% discount produces a
     negative raw payable, which is refused — unless it quantizes to ``-0.00``,
     which compares equal to zero and sails through the guard as a free dish.

The amounts here are deliberately at the edge of what the COLUMNS support
(``max_digits=50``), not of what a restaurant would charge. ``money.py`` states
that its bounds are technical capacity and "deliberately NOT a commercial price
cap", so a test that only exercises realistic prices would leave the supported
range unverified.
"""
import json
import re
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from misc_app.controllers.money import working_context

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.models import Order, OrderItem
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.controllers.pricing_policy import resolve_price
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

D = Decimal
INITIATE_URL = '/api/v2/orders/initiate/'

#: A canonical wire amount: an optional sign, digits, and EXACTLY two decimals.
CANONICAL_MONEY = re.compile(r'^-?\d+\.\d{2}$')

#: Every monetary key the quote block publishes, at each of its two levels.
QUOTE_LINE_MONEY = (
    'unit_price', 'reference_unit_price', 'discounted_price',
    'unit_cost_of_options', 'total_cost', 'reference_total_cost',
    'discounted_cost', 'savings', 'line_actual_cost', 'line_total_with_extras',
)
QUOTE_EXTRA_MONEY = ('unit_price', 'discounted_price', 'actual_cost')


class _JsonFloat(tuple):
    """A number that arrived on the wire as a JSON float, kept with its source
    text so a failure can name what was actually sent."""

    def __repr__(self):  # pragma: no cover - only reached on failure
        return f'<json-float {self[0]}>'


def decode_wire(response):
    """Decode the RENDERED body, marking every JSON float as it is parsed.

    ``response.data`` is the structure the view assembled; this is the structure
    the diner's browser sees. They are not the same thing, and the difference is
    the whole subject of this module.
    """
    return json.loads(
        response.content.decode(),
        parse_float=lambda raw: _JsonFloat((raw,)),
    )


class _WireBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='W', last_name='O', email='wire-owner@test.com',
            phone_number='256700099001', username='256700099001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Wire R', location='wire', owner=self.owner,
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
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant,
                                 qr_mode='order_pay')
            for n in range(1, 9)
        ]
        self._next_table = 0
        self.client = Client()

    # -- fixtures --------------------------------------------------------
    def item(self, name, price=10000, **kw):
        opts = dict(approved=True, enabled=True, available=True, in_stock=True,
                    primary_price=price)
        opts.update(kw)
        return MenuItem.objects.create(name=name, section=self.section, **opts)

    def discount(self, item, percentage=None, amount=None):
        item.discount_details = {
            'discount_percentage': percentage or 0,
            'discount_amount': amount or 0,
            'start_date': '', 'end_date': '', 'recurring_days': [],
            'start_time': '', 'end_time': '',
        }
        item.save(update_fields=['discount_details'])
        return item

    def with_choices(self, item, choices, group='g1'):
        """``choices`` is ``[(id, name, additionalCost), ...]`` — costs verbatim."""
        item.options = {
            'hasModifiers': True,
            'groups': [{
                'id': group, 'name': 'Options', 'type': 'multiple',
                'minSelections': 0, 'maxSelections': 0,
                'choices': [
                    {'id': cid, 'name': cname, 'additionalCost': cost,
                     'available': True}
                    for cid, cname, cost in choices
                ],
            }],
        }
        item.save(update_fields=['options'])
        return item

    def with_extras(self, parent, extras):
        parent.has_extras = True
        parent.extras_applicable = [str(e.id) for e in extras]
        parent.extras_min_selections = 0
        parent.extras_max_selections = 0
        parent.save()
        return parent

    # -- helpers ---------------------------------------------------------
    def initiate(self, items):
        table = self.tables[self._next_table]
        self._next_table += 1
        return self.client.post(
            INITIATE_URL, data=json.dumps({'items': items}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(table),
        )

    def wire_quote(self, items):
        """Initiate over HTTP and return the DECODED wire payload plus quote."""
        response = self.initiate(items)
        self.assertEqual(response.status_code, 200, response.content[:400])
        payload = decode_wire(response)
        data = payload['data']
        return payload, data, data['order_details'], data['quote']

    def assert_canonical(self, value, where):
        self.assertIsInstance(
            value, str,
            f'{where}: rendered as {value!r} — the wire must carry a canonical '
            f'decimal string, not a JSON number',
        )
        self.assertRegex(value, CANONICAL_MONEY, f'{where}: not fixed-scale')


class QuoteWireFormatTests(_WireBase):
    """The review the diner confirms is rendered exactly, not approximately."""

    def test_every_quote_amount_is_a_canonical_two_decimal_string(self):
        dish = self.item('Dish', price=D('5000'))
        extra = self.item('Cheese', price=D('999'), is_extra=True)
        self.with_extras(dish, [extra])
        _payload, _data, _order, quote = self.wire_quote(
            [{'item': str(dish.id), 'quantity': 2, 'extras': [str(extra.id)]}],
        )
        self.assertEqual(len(quote), 1)
        line = quote[0]
        for key in QUOTE_LINE_MONEY:
            self.assert_canonical(line[key], f'quote[0].{key}')
        self.assertEqual(len(line['extras']), 1)
        for key in QUOTE_EXTRA_MONEY:
            self.assert_canonical(line['extras'][0][key],
                                  f'quote[0].extras[0].{key}')

    def test_no_quote_amount_is_rendered_as_a_json_float(self):
        """The strongest form of the same statement, and the one that fails
        loudly for any amount a future field forgets to format."""
        dish = self.item('Dish', price=D('5000'))
        _payload, _data, _order, quote = self.wire_quote(
            [{'item': str(dish.id), 'quantity': 1}],
        )
        floats = []

        def walk(node, path):
            if isinstance(node, _JsonFloat):
                floats.append((path, node[0]))
            elif isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f'{path}.{key}')
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f'{path}[{index}]')

        walk(quote, 'quote')
        self.assertEqual(floats, [], f'JSON floats in the quote: {floats}')

    def test_the_payable_total_is_published_as_an_exact_string(self):
        """The amount the confirmation label states.

        The legacy ``actual_cost`` key keeps its established numeric form for
        older clients; the exact figure is ADDITIVE so the review can be bound to
        a value that was never a float.
        """
        dish = self.item('Dish', price=D('1234.56'))
        _payload, _data, order, _quote = self.wire_quote(
            [{'item': str(dish.id), 'quantity': 3}],
        )
        self.assert_canonical(order['quote_total'], 'order.quote_total')
        self.assertEqual(order['quote_total'], '3703.68')

    def test_a_legitimate_zero_is_a_zero_string_not_an_absence(self):
        free = self.item('Water', price=D('0'))
        _payload, _data, order, quote = self.wire_quote(
            [{'item': str(free.id), 'quantity': 1}],
        )
        self.assertEqual(quote[0]['line_actual_cost'], '0.00')
        self.assertEqual(order['quote_total'], '0.00')


class DiscountedExtraTests(_WireBase):
    """An extra priced 999 at 10% off is 899.10 per unit — never 899."""

    def _extra_line(self, quantity):
        # Unique names: `menu_items` is UNIQUE on (name, section), and the
        # reconciliation test below builds three orders in one test method.
        dish = self.item(f'Dish {quantity}', price=D('5000'))
        extra = self.item(f'Sauce {quantity}', price=D('999'), is_extra=True)
        self.discount(extra, percentage=10)
        self.with_extras(dish, [extra])
        _payload, _data, order, quote = self.wire_quote(
            [{'item': str(dish.id), 'quantity': quantity,
              'extras': [str(extra.id)]}],
        )
        return order, quote[0]['extras'][0]

    def test_a_ten_percent_extra_on_999_is_899_10_per_unit(self):
        _order, extra = self._extra_line(1)
        self.assertEqual(extra['discounted_price'], '899.10')
        self.assertEqual(extra['actual_cost'], '899.10')

    def test_the_same_extra_at_quantity_three_is_exactly_three_times(self):
        _order, extra = self._extra_line(3)
        self.assertEqual(extra['discounted_price'], '899.10')
        self.assertEqual(extra['actual_cost'], '2697.30')

    def test_the_order_total_reconciles_across_both_quantities(self):
        """3 x one dish must equal what 1 + 2 of the same dish costs, on the
        wire, with the sub-cent extra attached."""
        totals = {}
        for quantity in (1, 2, 3):
            order, _extra = self._extra_line(quantity)
            totals[quantity] = D(order['quote_total'])
        self.assertEqual(totals[3], totals[1] + totals[2])


class PerComponentRoundingTests(_WireBase):
    """Each modifier adjustment is rounded ONCE, before it is added.

    Two adjustments of 1.005 are 1.00 each under ROUND_HALF_EVEN, so a 1000 base
    prices at 1002.00. Summing first and rounding after gives 1002.01, and IEEE
    doubles give 1002.0099999999999. This is the golden value the frontend's
    exact helper is held to as well — the same arithmetic has to happen twice, in
    two languages, and agree.
    """

    def test_two_sub_cent_adjustments_each_round_before_they_are_added(self):
        dish = self.item('Dish', price=D('1000'))
        self.with_choices(dish, [
            ('c1', 'A', '1.005'),
            ('c2', 'B', '1.005'),
        ])
        _payload, _data, order, quote = self.wire_quote([{
            'item': str(dish.id), 'quantity': 1,
            'selected_modifiers': {'g1': ['c1', 'c2']},
        }])
        self.assertEqual(quote[0]['reference_unit_price'], '1002.00')
        self.assertEqual(order['quote_total'], '1002.00')


class LargeExactAmountTests(_WireBase):
    """The supported column range, end to end.

    ``max_digits=50`` means 48 integer digits. Python's DEFAULT decimal context
    is 28 significant digits, so any money arithmetic that forgets
    ``working_context()`` silently rounds a schema-valid amount — and the range
    check downstream then waves the rounded product through as ordinary.
    """

    #: 29 significant digits with the cents: one more than the default context
    #: carries, and 19 fewer than the column holds.
    BIG = D('10000000000000000000000000000.00')

    def test_an_order_rollup_keeps_every_digit(self):
        big = self.item('Big', price=self.BIG)
        cent = self.item('Cent', price=D('0.01'))
        _payload, _data, order, _quote = self.wire_quote([
            {'item': str(big.id), 'quantity': 1},
            {'item': str(cent.id), 'quantity': 1},
        ])
        saved = Order.objects.get(pk=order['id'])
        self.assertEqual(
            saved.actual_cost, D('10000000000000000000000000000.01'),
            'the rollup rounded a schema-valid sum to the ambient context',
        )
        self.assertEqual(order['quote_total'], '10000000000000000000000000000.01')

    def test_a_large_savings_subtraction_keeps_every_digit(self):
        big = self.item('Big', price=self.BIG)
        self.discount(big, amount='0.01')
        _payload, _data, order, _quote = self.wire_quote(
            [{'item': str(big.id), 'quantity': 1}],
        )
        saved = Order.objects.get(pk=order['id'])
        self.assertEqual(saved.savings, D('0.01'))
        self.assertEqual(
            saved.actual_cost, D('9999999999999999999999999999.99'),
        )
        self.assertEqual(order['quote_total'], '9999999999999999999999999999.99')

    def test_a_large_amount_survives_quote_serialization(self):
        big = self.item('Big', price=self.BIG)
        _payload, _data, _order, quote = self.wire_quote(
            [{'item': str(big.id), 'quantity': 1}],
        )
        self.assertEqual(quote[0]['line_actual_cost'],
                         '10000000000000000000000000000.00')
        self.assertEqual(quote[0]['line_total_with_extras'],
                         '10000000000000000000000000000.00')

    def test_the_quote_reference_is_a_digest_not_a_repr_fallback(self):
        """``order_quote._money`` quantized outside the module context, so an
        amount above the ambient precision fell to its ``!repr`` escape hatch —
        a stable key, but one built from a Python repr rather than the value."""
        big = self.item('Big', price=self.BIG)
        _payload, _data, order, _quote = self.wire_quote(
            [{'item': str(big.id), 'quantity': 1}],
        )
        from orders_app.controllers.services import order_quote as quote_module
        saved = Order.objects.get(pk=order['id'])
        rows = list(OrderItem.objects.filter(order=saved, deleted=False))
        fingerprints = [
            part for row in rows
            for part in quote_module._row_fingerprint(row)
        ]
        self.assertFalse(
            [p for p in fingerprints if isinstance(p, str) and p.startswith('!')],
            'a monetary field fell through to the repr fallback',
        )
        self.assertEqual(order['quote_ref'], quote_module.quote_ref(saved, rows=rows))


class NegativeZeroDiscountTests(_WireBase):
    """Rounding must not rescue an incoherent discount into a free dish.

    ``effective < 0`` is the guard. ``Decimal('-0.00') == Decimal('0')``, so a
    negative raw payable that quantizes to negative zero passes it — and the item
    is published, and sold, for nothing.
    """

    def test_an_over_hundred_percent_discount_is_not_quantized_into_free(self):
        verdict = resolve_price(D('0.01'), {'discount_percentage': '100.5'},
                                now=timezone.localtime())
        self.assertFalse(
            verdict.usable,
            f'an over-100% discount resolved usable at '
            f'{verdict.effective_base!r}',
        )

    def test_the_same_configuration_is_refused_at_checkout(self):
        broken = self.item('Broken', price=D('0.01'))
        self.discount(broken, percentage='100.5')
        response = self.initiate([{'item': str(broken.id), 'quantity': 1}])
        self.assertEqual(
            response.status_code, 400,
            f'an unpriceable item was sold: {response.content[:300]}',
        )
        self.assertEqual(Order.objects.count(), 0)

    def test_a_legitimate_hundred_percent_discount_is_still_free(self):
        """The control. A waived dish is supported and must stay orderable."""
        waived = self.item('Waived', price=D('5000'))
        self.discount(waived, percentage=100)
        _payload, _data, order, quote = self.wire_quote(
            [{'item': str(waived.id), 'quantity': 2}],
        )
        self.assertEqual(quote[0]['line_actual_cost'], '0.00')
        self.assertEqual(order['quote_total'], '0.00')

    def test_a_zero_priced_dish_is_still_free(self):
        """The other control: free by price rather than by discount."""
        free = self.item('Free', price=D('0'))
        _payload, _data, order, _quote = self.wire_quote(
            [{'item': str(free.id), 'quantity': 1}],
        )
        self.assertEqual(order['quote_total'], '0.00')


class LegacyViewExactnessTests(_WireBase):
    """The LEGACY-shaped figures are recovered exactly, not approximately.

    ``_legacy_view`` subtracts the modifier component back out of a CORRECTED
    row to reproduce what ``unit_price`` / ``total_cost`` held before D02. That
    subtraction is composite Decimal arithmetic on money, so it belongs inside
    ``working_context()`` for exactly the reason the module-level docstring
    gives: the process default is 28 significant digits against columns that
    hold 50, and ``-`` ROUNDS SILENTLY there rather than raising.

    ``_legacy_total`` wrapped its own ``sum`` and so looked covered — but it
    calls ``_legacy_view`` INSIDE that context only for the summation, while
    ``_quote_line`` and the per-item legacy serializer both invoke
    ``_legacy_view`` from outside any context at all. The rounding therefore
    happened before the sum ever saw the value.

    THE ORACLE IS AN INDEPENDENT LITERAL, never the production helper run twice:
    the expected string is written out here in full, so a regression that
    changes both sides together still fails.

    THE AMOUNTS ARE AT THE EDGE OF WHAT THE COLUMNS SUPPORT (``max_digits=50``),
    not of what a Kampala restaurant charges. ``money.py`` states that its
    bounds are technical capacity and "deliberately NOT a commercial price cap",
    so the advertised range is what gets verified. This is not a claim that any
    real order carries these figures.
    """

    #: 31 significant digits — schema-valid, and three past the 28 the process
    #: default allows.
    SAVED_REFERENCE_UNIT = D('10000000000000000000000000001.01')
    SAVED_MODIFIER_UNIT = D('1.00')
    #: Written out by hand: 1, twenty-seven zeros, then `0.01`.
    EXPECTED_LEGACY_BASE = D('10000000000000000000000000000.01')

    def _corrected_row(self):
        """One persisted CORRECTED line carrying the schema-edge amounts."""
        dish = self.item('Edge', price=self.SAVED_REFERENCE_UNIT)
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.tables[0],
            order_number=90001, pricing_version=1,
            total_cost=self.SAVED_REFERENCE_UNIT,
            discounted_cost=self.SAVED_REFERENCE_UNIT,
            savings=D('0.00'), actual_cost=self.SAVED_REFERENCE_UNIT,
        )
        return OrderItem.objects.create(
            order=order, item=dish, quantity=1, available=True, status='ok',
            unit_price=self.SAVED_REFERENCE_UNIT,
            discounted_price=self.SAVED_REFERENCE_UNIT,
            unit_cost_of_options=self.SAVED_MODIFIER_UNIT,
            total_cost=self.SAVED_REFERENCE_UNIT,
            discounted_cost=self.SAVED_REFERENCE_UNIT,
            cost_of_options=self.SAVED_MODIFIER_UNIT,
            savings=D('0.00'), actual_cost=self.SAVED_REFERENCE_UNIT,
        )

    def test_the_legacy_unit_subtraction_keeps_the_last_cent(self):
        from orders_app.controllers.orders.serializers import _legacy_view
        unit, _total, _savings = _legacy_view(self._corrected_row(), True)
        self.assertEqual(
            unit, self.EXPECTED_LEGACY_BASE,
            'the modifier component was subtracted under the ambient 28-digit '
            'context, so the cent was rounded away before anything could sum it',
        )

    def test_the_legacy_total_subtraction_keeps_the_last_cent(self):
        from orders_app.controllers.orders.serializers import _legacy_view
        _unit, total, _savings = _legacy_view(self._corrected_row(), True)
        self.assertEqual(total, self.EXPECTED_LEGACY_BASE)

    def test_the_quote_line_renders_the_exact_legacy_figures(self):
        """``_quote_line`` calls ``_legacy_view`` before entering its own
        context, so the rendered strings carry whatever the subtraction left."""
        from orders_app.controllers.orders.serializers import _quote_line
        line = _quote_line(self._corrected_row(), [], True)
        self.assertEqual(line['unit_price'], '10000000000000000000000000000.01')
        self.assertEqual(line['total_cost'], '10000000000000000000000000000.01')

    def test_the_per_item_legacy_serializer_renders_the_exact_figures(self):
        """The second direct caller, reached by the flat ``order_items`` list."""
        from orders_app.controllers.orders.serializers import (
            serialize_order_item_details,
        )
        detail = serialize_order_item_details(
            item=self._corrected_row(), corrected=True, children=[])
        self.assertEqual(detail['unit_price'], self.EXPECTED_LEGACY_BASE)
        self.assertEqual(detail['total_cost'], self.EXPECTED_LEGACY_BASE)

    def test_an_ordinary_amount_is_completely_unchanged(self):
        """The context widens precision; it never alters an ordinary result."""
        from orders_app.controllers.orders.serializers import _legacy_view

        class _Row:
            unit_price = D('10000.00')
            unit_cost_of_options = D('1500.00')
            total_cost = D('30000.00')
            cost_of_options = D('4500.00')
            savings = D('0.00')

        unit, total, savings = _legacy_view(_Row(), True)
        self.assertEqual(unit, D('8500.00'))
        self.assertEqual(total, D('25500.00'))
        self.assertEqual(savings, D('0.00'))


class OptionGroupLabelExactnessTests(_WireBase):
    """A modifier group's DISPLAYED cost is exact, like the charged one.

    ``option_breakdown`` exists so that "the cost a diner is shown for a group
    and the cost they are charged for it are the same number by construction" —
    its own words. The charged side sums the adjustments inside
    ``price_unit``'s ``working_context()``; the LABEL side accumulated
    ``group_total += adjustment`` under the ambient context, where ``+`` rounds
    silently at 28 significant digits. So the two could disagree on a
    schema-valid amount, which is exactly the split the single traversal was
    written to remove.

    ``cost_amount`` is also rendered here rather than through ``str()``:
    ``str(Decimal('1E+28'))`` is scientific notation, which is the one form a
    field documented as "the exact decimal string" must not take. For every
    ordinary amount the two spellings are byte-identical, so nothing existing
    moves. Note ``options`` is not part of the quote fingerprint, so this
    changes no ``quote_ref``.
    """

    def _breakdown(self, costs):
        from orders_app.controllers.con_orders import ConOrder
        dish = self.with_choices(self.item('Opt', price=D('1000.00')), [
            (f'c{i}', f'Choice {i}', cost) for i, cost in enumerate(costs)
        ])
        return ConOrder.option_breakdown(
            dish, {'g1': [f'c{i}' for i in range(len(costs))]})

    def test_a_group_total_keeps_every_digit(self):
        result = self._breakdown(['10000000000000000000000000001.01', '-1.00'])
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(
            result['options'][0]['cost_amount'],
            '10000000000000000000000000000.01',
            'the group label was accumulated under the ambient 28-digit '
            'context, so the shown cost no longer matches the charged one',
        )

    def test_the_label_and_the_charge_agree_on_a_large_group(self):
        """The two sides of the single traversal, compared directly."""
        from decimal import Decimal as _D
        result = self._breakdown(['10000000000000000000000000001.01', '-1.00'])
        with working_context():
            charged = sum(result['adjustments'], _D('0'))
        self.assertEqual(result['options'][0]['cost_amount'], f'{charged:f}')

    def test_an_ordinary_group_total_is_completely_unchanged(self):
        result = self._breakdown(['1500.00', '500.00'])
        self.assertEqual(result['options'][0]['cost_amount'], '2000.00')
        self.assertEqual(result['options'][0]['cost'], 2000.0)

    def test_a_negative_group_total_keeps_its_sign(self):
        """A "no cheese, -500" group is legal and must not be normalised away."""
        result = self._breakdown(['-500.00'])
        self.assertEqual(result['options'][0]['cost_amount'], '-500.00')
