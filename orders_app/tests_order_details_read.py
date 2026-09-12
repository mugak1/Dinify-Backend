"""
D04/U1 — the diner's order-details read publishes the exact payable and a
truthful completeness statement.

WHAT THIS CLOSES, STATED PRECISELY. The browser journey asserted
``quote_total`` and ``quote_complete`` against
``GET orders/journey/order-details/`` and failed on both at d5d886e. That was
a DISAGREEMENT ABOUT WHICH FIELDS THE READ PUBLISHES — the two keys were
produced only by the *initiate* response assembler. It was never evidence that
a stored payable was wrong, and nothing here changes a stored amount.

EVERY ASSERTION DECODES THE RENDERED BODY, not ``response.data``. DRF encodes a
``Decimal`` through ``float()``, so the difference between what the view builds
and what the browser parses is exactly the subject of this module: a test that
inspects ``response.data`` cannot see it.
"""
import json
import re

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.models import Order, OrderItem
from orders_app.serializers import SerializerPublicOrderDetails
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

INITIATE_URL = '/api/v2/orders/initiate/'
DETAILS_URL = '/api/v1/orders/journey/order-details/'

#: An optional sign, digits, and EXACTLY two decimals.
CANONICAL_MONEY = re.compile(r'^-?\d+\.\d{2}$')


class _JsonFloat(tuple):
    """A value that arrived as a JSON float, kept with its source text."""

    def __repr__(self):  # pragma: no cover - only reached on failure
        return f'<json-float {self[0]}>'


def decode_wire(response):
    """The structure the diner's BROWSER parses, not the one the view built."""
    return json.loads(
        response.content.decode(),
        parse_float=lambda raw: _JsonFloat((raw,)),
    )


class _ReadBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='R', last_name='O', email='read-owner@test.com',
            phone_number='256700088001', username='256700088001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Read R', location='read', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
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
        self.client = Client()

    def item(self, name, price=10000, **kw):
        opts = dict(approved=True, enabled=True, available=True, in_stock=True,
                    primary_price=price)
        opts.update(kw)
        return MenuItem.objects.create(name=name, section=self.section, **opts)

    def with_extras(self, parent, extras):
        parent.has_extras = True
        parent.extras_applicable = [str(e.id) for e in extras]
        parent.extras_min_selections = 0
        parent.extras_max_selections = 0
        parent.save()
        return parent

    def initiate(self, items, table=None):
        if table is None:
            table = self.tables[self._next_table]
            self._next_table += 1
        response = self.client.post(
            INITIATE_URL, data=json.dumps({'items': items}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(table),
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return table, decode_wire(response)['data']

    def read(self, order_id, table, **extra):
        return self.client.get(
            DETAILS_URL, {'order': str(order_id)},
            HTTP_X_DINER_SESSION=issue_table_session(table), **extra,
        )


class ExactPayableOnTheWireTests(_ReadBase):
    """``quote_total`` is the canonical string, beside the legacy number."""

    def test_the_payable_is_a_canonical_decimal_string(self):
        dish = self.item('Plain', price='10000.00')
        table, data = self.initiate([{'item': str(dish.id), 'quantity': 1}])

        body = decode_wire(self.read(data['order_details']['id'], table))
        saved = body['data']

        self.assertIsInstance(saved['quote_total'], str)
        self.assertRegex(saved['quote_total'], CANONICAL_MONEY)
        self.assertEqual(saved['quote_total'], '10000.00')

    def test_a_trailing_zero_survives_the_wire(self):
        """THE SCALE IS KEPT — and this read's legacy field keeps it too.

        WORTH STATING PRECISELY, because the two surfaces differ and it is easy
        to assume otherwise. ``serialize_order_details`` puts a raw ``Decimal``
        into a plain dict, so the *initiate* response's ``order_details``
        carries ``actual_cost`` as a JSON FLOAT and ``899.10`` reaches the
        browser as ``899.1``. This serializer is a ``ModelSerializer``, whose
        ``DecimalField`` coerces to a string by default, so ``actual_cost`` was
        ALREADY exact here.

        So U1's real gap was not a lossy amount on this read — it was that the
        canonically NAMED key was absent, and with it any completeness
        statement. ``quote_total`` is the key every surface can be held to;
        ``actual_cost`` keeps whatever its serializer has always produced.
        """
        dish = self.item('Fractional', price='899.10')
        table, data = self.initiate([{'item': str(dish.id), 'quantity': 1}])

        saved = decode_wire(self.read(data['order_details']['id'], table))['data']

        self.assertEqual(saved['quote_total'], '899.10')
        # UNCHANGED, and already exact on THIS read.
        self.assertEqual(saved['actual_cost'], '899.10')
        # The INITIATE response is the surface where the numeric form is lossy.
        self.assertIsInstance(data['order_details']['actual_cost'], _JsonFloat)
        self.assertEqual(data['order_details']['actual_cost'][0], '899.1')

    def test_a_free_dish_reads_as_a_price_not_an_absence(self):
        dish = self.item('Free', price='0.00')
        table, data = self.initiate([{'item': str(dish.id), 'quantity': 1}])

        saved = decode_wire(self.read(data['order_details']['id'], table))['data']
        self.assertEqual(saved['quote_total'], '0.00')

    def test_the_read_agrees_with_the_initiate_response_it_follows(self):
        """ONE amount, two surfaces. They are formatted by the same helper, so
        a divergence here would mean two definitions of the payable."""
        dish = self.item('Agree', price='12345.67')
        table, data = self.initiate([{'item': str(dish.id), 'quantity': 3}])

        saved = decode_wire(self.read(data['order_details']['id'], table))['data']
        self.assertEqual(saved['quote_total'], data['order_details']['quote_total'])


class CompletenessClaimsOnlyWhatTheRuleProvesTests(_ReadBase):
    """``quote_complete`` is ``group_live_children``, not a second opinion."""

    def _order_with_extra(self):
        cheese = self.item('Cheese', price='2000.00', is_extra=True)
        burger = self.with_extras(self.item('Burger', price='10000.00'), [cheese])
        table, data = self.initiate([{
            'item': str(burger.id), 'quantity': 1, 'extras': [str(cheese.id)],
        }])
        return table, data['order_details']['id']

    def test_a_healthy_order_is_complete(self):
        table, order_id = self._order_with_extra()
        saved = decode_wire(self.read(order_id, table))['data']

        self.assertIs(saved['quote_complete'], True)

    def test_an_orphaned_live_child_is_disclosed_as_incomplete(self):
        """A live child under a soft-deleted parent belongs under no quoted
        line, yet its amount is still inside the saved payable. The read says
        so rather than presenting an itemisation that cannot add up."""
        table, order_id = self._order_with_extra()
        parent = OrderItem.objects.get(order_id=order_id, parent_item__isnull=True)
        OrderItem.objects.filter(pk=parent.pk).update(deleted=True)

        saved = decode_wire(self.read(order_id, table))['data']

        self.assertIs(saved['quote_complete'], False)
        # AND THE PAYABLE IS NOT REWRITTEN to make the remaining rows add up.
        self.assertEqual(saved['quote_total'], '12000.00')

    def test_completeness_is_a_real_boolean_on_the_wire(self):
        table, order_id = self._order_with_extra()
        saved = decode_wire(self.read(order_id, table))['data']
        self.assertIsInstance(saved['quote_complete'], bool)


class TheItemisedQuoteIsTheSameProjectionTests(_ReadBase):
    """``quote`` here is ``_quote_line``, not a second opinion about a line."""

    def _burger_with_cheese(self):
        cheese = self.item('Cheese', price='2000.00', is_extra=True)
        burger = self.with_extras(self.item('Burger', price='10000.00'), [cheese])
        table, data = self.initiate([{
            'item': str(burger.id), 'quantity': 2, 'extras': [str(cheese.id)],
        }])
        return table, data

    def test_the_read_publishes_the_same_lines_the_initiate_response_did(self):
        """ONE order, TWO surfaces, byte-identical lines. A recovered draft is
        reviewed against the same figures the diner saw the first time."""
        table, data = self._burger_with_cheese()

        saved = decode_wire(self.read(data['order_details']['id'], table))['data']

        self.assertEqual(saved['quote'], data['quote'])

    def test_every_quote_amount_is_a_canonical_decimal_string(self):
        table, data = self._burger_with_cheese()
        saved = decode_wire(self.read(data['order_details']['id'], table))['data']

        for line in saved['quote']:
            for key in ('unit_price', 'reference_unit_price', 'discounted_price',
                        'unit_cost_of_options', 'total_cost',
                        'reference_total_cost', 'discounted_cost', 'savings',
                        'line_actual_cost', 'line_total_with_extras'):
                self.assertIsInstance(line[key], str, f'{key} is not a string')
                self.assertRegex(line[key], CANONICAL_MONEY, key)
            for extra in line['extras']:
                for key in ('unit_price', 'discounted_price', 'actual_cost'):
                    self.assertRegex(extra[key], CANONICAL_MONEY, key)

    def test_the_quoted_lines_reconcile_to_the_payable_to_the_cent(self):
        """Exact integers from the canonical strings — no float anywhere."""
        table, data = self._burger_with_cheese()
        saved = decode_wire(self.read(data['order_details']['id'], table))['data']

        def minor(text):
            sign, whole, frac = re.fullmatch(
                r'(-?)(\d+)\.(\d{2})', text).groups()
            value = int(whole) * 100 + int(frac)
            return -value if sign else value

        summed = sum(minor(line['line_total_with_extras'])
                     for line in saved['quote'])
        self.assertEqual(summed, minor(saved['quote_total']))

    def test_a_soft_deleted_row_is_absent_from_the_quote(self):
        """The quote describes the LIVE population, the same one
        ``quote_complete`` and the acceptance check are about."""
        table, data = self._burger_with_cheese()
        order_id = data['order_details']['id']
        parent = OrderItem.objects.get(order_id=order_id, parent_item__isnull=True)
        OrderItem.objects.filter(pk=parent.pk).update(deleted=True)

        saved = decode_wire(self.read(order_id, table))['data']

        self.assertEqual(saved['quote'], [])
        self.assertIs(saved['quote_complete'], False)

    def test_the_quote_is_absent_from_an_unauthorised_read(self):
        table, data = self._burger_with_cheese()
        other = next(t for t in self.tables if t.pk != table.pk)

        response = self.read(data['order_details']['id'], other)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b'line_total_with_extras', response.content)


class TheReadIsUnchangedInEveryOtherRespectTests(_ReadBase):
    """Existing fields, authorization, caching and cost."""

    def _one_order(self):
        dish = self.item('Keep', price='7500.00')
        table, data = self.initiate([{'item': str(dish.id), 'quantity': 2}])
        return table, data['order_details']['id']

    def test_every_pre_existing_field_is_still_published(self):
        table, order_id = self._one_order()
        saved = decode_wire(self.read(order_id, table))['data']

        for key in ('id', 'table', 'total_cost', 'discounted_cost', 'savings',
                    'actual_cost', 'prepayment_required', 'payment_status',
                    'order_status', 'items', 'order_number', 'total_paid',
                    'balance_payable', 'time_last_updated'):
            self.assertIn(key, saved, f'{key} disappeared from the read')

    def test_a_request_with_no_diner_session_is_refused(self):
        _table, order_id = self._one_order()
        response = self.client.get(DETAILS_URL, {'order': str(order_id)})
        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn(b'quote_total', response.content)

    def test_another_tables_session_cannot_read_the_order(self):
        table, order_id = self._one_order()
        other = next(t for t in self.tables if t.pk != table.pk)

        response = self.read(order_id, other)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b'quote_total', response.content)

    def test_the_response_is_still_no_store(self):
        table, order_id = self._one_order()
        response = self.read(order_id, table)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_the_read_writes_nothing(self):
        table, order_id = self._one_order()
        before = Order.objects.get(pk=order_id)
        stamp, cost = before.time_last_updated, before.actual_cost

        self.read(order_id, table)

        after = Order.objects.get(pk=order_id)
        self.assertEqual(after.time_last_updated, stamp)
        self.assertEqual(after.actual_cost, cost)

    def test_the_three_new_keys_add_no_query(self):
        """THE COST CLAIM, PROVED BY COMPARISON rather than by an absolute
        number. Every method field reads ONE shared row fetch, so serializing
        with them must cost exactly what serializing without them costs.
        """
        table, order_id = self._one_order()
        order = Order.objects.get(pk=order_id)

        class WithoutTheNewKeys(SerializerPublicOrderDetails):
            class Meta(SerializerPublicOrderDetails.Meta):
                fields = tuple(
                    f for f in SerializerPublicOrderDetails.Meta.fields
                    if f not in ('quote', 'quote_total', 'quote_complete')
                )

        with CaptureQueriesContext(connection) as without:
            WithoutTheNewKeys(order, many=False).data
        with CaptureQueriesContext(connection) as with_them:
            SerializerPublicOrderDetails(order, many=False).data

        self.assertEqual(
            len(with_them.captured_queries), len(without.captured_queries),
            'the quote, the canonical total and the completeness flag must '
            'read the shared row fetch, not issue round trips of their own',
        )

    def test_the_read_does_not_grow_a_query_per_line(self):
        """FLATNESS, which is the property that actually matters. The shared
        fetch carries ``select_related('item')``, so a five-line order costs
        what a one-line order costs — it did NOT before, because
        ``SerializerListOrderItem.get_item`` dereferenced ``item.item`` per
        row. (``get_extra_items`` still issues one query per parent; that is
        pre-existing and deliberately untouched, so the comparison below uses
        orders with the SAME number of parents.)
        """
        dishes = [self.item(f'D{n}', price='1000.00') for n in range(5)]
        one_table, one = self.initiate([
            {'item': str(dishes[0].id), 'quantity': 1}])
        five_table, five = self.initiate([
            {'item': str(d.id), 'quantity': 1} for d in dishes])

        with CaptureQueriesContext(connection) as small:
            SerializerPublicOrderDetails(
                Order.objects.get(pk=one['order_details']['id'])).data
        with CaptureQueriesContext(connection) as large:
            SerializerPublicOrderDetails(
                Order.objects.get(pk=five['order_details']['id'])).data

        # 1 parent vs 5 parents: only `get_extra_items` may scale, so the
        # difference must be exactly the four extra parents and nothing more.
        self.assertEqual(
            len(large.captured_queries) - len(small.captured_queries), 4,
            'a per-line read other than the pre-existing extras fetch '
            f'appeared: {len(small.captured_queries)} -> '
            f'{len(large.captured_queries)}',
        )
        self.assertIsNotNone(one_table)
        self.assertIsNotNone(five_table)
