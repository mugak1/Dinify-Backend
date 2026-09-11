"""
D01 — order-input integrity: the enforced contract and its regressions.

TWO KINDS OF TEST LIVE HERE, AND THE SPLIT IS DELIBERATE.

The ``*ReproductionTests`` classes exercise ONLY pre-existing public API (the
live HTTP route, ``ConOrder``, the models). None of them import the D01
validator, so every one of them runs unchanged against the PRE-FIX revision and
fails there for a BEHAVIOURAL reason — a persisted negative quantity, a 500 from
``dict.fromkeys`` — never because a new module is missing. They are the evidence
that a real defect existed.

The remaining classes establish the NEWLY APPROVED bounds and the shared-rule
guarantees. They are not reproductions of an old defect and are not presented as
such: before D01 there were no ceilings to violate.

Fixtures are deliberately valid — a live restaurant, a published section, an
orderable item — so nothing here can pass or fail for an unrelated authorization
or publication reason and mask the behaviour under test.
"""
import copy
import inspect
import json
import uuid
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import Client, TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from finance_app.models import DinifyTransaction
from orders_app.controllers import con_orders as con_orders_module
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.services import order_input
from orders_app.controllers.services.create_order import _create_order
from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.controllers.menu_publication import NOT_ON_MENU_MESSAGE
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

INITIATE_URL = '/api/v2/orders/initiate/'


class _D01Base(TestCase):
    """Valid tenant/menu fixtures, so nothing here is masked by an unrelated
    authorization or publication rejection."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='D01', last_name='Owner', email='d01-owner@test.com',
            phone_number='256700031001', username='256700031001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='D01 R', location='d01', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='D01 Section', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self.item = MenuItem.objects.create(
            name='Burger', section=self.section, primary_price=10000,
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.other_item = MenuItem.objects.create(
            name='Fries', section=self.section, primary_price=4000,
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.mod_item = MenuItem.objects.create(
            name='Pizza', section=self.section, primary_price=20000,
            approved=True, enabled=True, available=True, in_stock=True,
            options={
                'hasModifiers': True,
                'groups': [{
                    'id': 'g1', 'name': 'Size',
                    'minSelections': 0, 'maxSelections': 2,
                    'choices': [
                        {'id': 'c1', 'name': 'Large', 'additionalCost': 1000},
                        {'id': 'c2', 'name': 'Small', 'additionalCost': 0},
                    ],
                }],
            },
        )
        # Authority on the staff path resolves from an ACTIVE owner-role
        # membership, not from Restaurant.owner, so the compatibility tests
        # need the real thing.
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )
        self.client = Client()

    # -- helpers ---------------------------------------------------------
    def post(self, body):
        return self.client.post(
            INITIATE_URL, data=json.dumps(body),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(self.table),
        )

    def line(self, item=None, quantity=1, **extra):
        payload = {'item': str((item or self.item).id), 'quantity': quantity}
        payload.update(extra)
        return payload

    def assert_no_rows(self):
        self.assertEqual(Order.objects.count(), 0, 'an Order was created')
        self.assertEqual(OrderItem.objects.count(), 0, 'an OrderItem was created')
        self.assertEqual(
            RestaurantDailyOrderCounter.objects.count(), 0,
            'the daily order counter was advanced',
        )

    def assert_rejected(self, body):
        """400 with a usable top-level message, and nothing persisted."""
        response = self.post(body)
        self.assertEqual(response.status_code, 400, response.content[:300])
        payload = response.json()
        self.assertEqual(payload.get('status'), 400)
        self.assertTrue(
            isinstance(payload.get('message'), str) and payload['message'].strip(),
            f'no usable top-level message: {payload!r}',
        )
        self.assert_no_rows()
        return payload


class D01QuantityReproductionTests(_D01Base):
    """A submitted quantity must be a real positive integer."""

    def test_negative_quantity_is_rejected_and_persists_nothing(self):
        # PRE-FIX: HTTP 200 and an OrderItem with quantity == -3.
        self.assert_rejected({'items': [self.line(quantity=-3)]})

    def test_zero_quantity_is_rejected_and_persists_nothing(self):
        # PRE-FIX: HTTP 200 and an OrderItem with quantity == 0.
        self.assert_rejected({'items': [self.line(quantity=0)]})

    def test_positive_plus_negative_basket_creates_no_order(self):
        # PRE-FIX: HTTP 200 and BOTH lines persisted (quantity -1 alongside 2),
        # combining into a reduced payable amount.
        self.assert_rejected({'items': [
            self.line(quantity=2),
            self.line(item=self.other_item, quantity=-1),
        ]})

    def test_float_quantity_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError (Decimal * float).
        self.assert_rejected({'items': [self.line(quantity=2.5)]})

    def test_integral_float_representation_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError. 2.0 must NOT be coerced to 2.
        self.assert_rejected({'items': [self.line(quantity=2.0)]})

    def test_numeric_string_quantity_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError (sequence * Decimal).
        self.assert_rejected({'items': [self.line(quantity='3')]})

    def test_boolean_quantity_is_rejected_with_400(self):
        # PRE-FIX: 500 (bare Exception raised from serializer errors).
        self.assert_rejected({'items': [self.line(quantity=True)]})

    def test_container_quantity_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError.
        self.assert_rejected({'items': [self.line(quantity=[1])]})
        self.assert_rejected({'items': [self.line(quantity={'a': 1})]})

    def test_excessive_quantity_is_rejected_with_400(self):
        # PRE-FIX: 10**20 -> 500; 999999 -> 200 and persisted.
        self.assert_rejected({'items': [self.line(quantity=10 ** 20)]})
        self.assert_rejected({'items': [self.line(quantity=999999)]})

    def test_valid_quantities_still_succeed(self):
        for quantity in (1, 2):
            with self.subTest(quantity=quantity):
                response = self.post({'items': [self.line(quantity=quantity)]})
                self.assertEqual(response.status_code, 200, response.content[:300])
                line = OrderItem.objects.get()
                self.assertEqual(line.quantity, quantity)
                Order.objects.all().delete()
                OrderItem.objects.all().delete()
                RestaurantDailyOrderCounter.objects.all().delete()


class D01OuterShapeReproductionTests(_D01Base):
    """The outer body must be validated before unsafe mapping/length access."""

    def test_non_object_root_is_rejected_with_400(self):
        # PRE-FIX: 500 AttributeError ('list'/'str' object has no attribute 'get').
        for body in ([self.line()], 'hello'):
            with self.subTest(body=type(body).__name__):
                response = self.post(body)
                self.assertEqual(response.status_code, 400, response.content[:300])
                self.assert_no_rows()

    def test_non_list_items_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError (object of type 'int' has no len()).
        self.assert_rejected({'items': 5})

    def test_malformed_client_order_id_is_rejected_with_400(self):
        # PRE-FIX: 500 django.core.exceptions.ValidationError from the ORM filter.
        for bad in ('not-a-uuid', '', {'a': 1}, 5):
            with self.subTest(client_order_id=repr(bad)):
                self.assert_rejected(
                    {'items': [self.line()], 'client_order_id': bad},
                )


class D01NestedSelectionReproductionTests(_D01Base):
    """A nested list/dict must never reach a hashing operation."""

    def test_dict_choice_member_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError: unhashable type: 'dict' (dict.fromkeys).
        self.assert_rejected({'items': [self.line(
            item=self.mod_item, selected_modifiers={'g1': [{'a': 1}]},
        )]})

    def test_list_choice_member_is_rejected_with_400(self):
        # PRE-FIX: 500 TypeError: unhashable type: 'list'.
        self.assert_rejected({'items': [self.line(
            item=self.mod_item, selected_modifiers={'g1': [['c1']]},
        )]})


class D01StoredDefinitionReproductionTests(_D01Base):
    """A malformed stored definition must fail safely, never 500, and never by
    dropping a required selection."""

    def _item_with(self, options, name):
        return MenuItem.objects.create(
            name=name, section=self.section, primary_price=1000,
            approved=True, enabled=True, available=True, options=options,
        )

    def test_unhashable_group_id_is_a_controlled_rejection(self):
        # PRE-FIX: raises TypeError: unhashable type: 'dict'.
        item = self._item_with({'hasModifiers': True, 'groups': [
            {'id': {'k': 1}, 'choices': [{'id': 'c1'}]},
        ]}, 'BadGroupId')
        result = ConOrder.normalize_selected_modifiers(item, {'g': ['c1']})
        self.assertEqual(result.get('status'), 400, result)

    def test_unhashable_choice_id_is_a_controlled_rejection(self):
        # PRE-FIX: raises TypeError: unhashable type: 'dict'.
        item = self._item_with({'hasModifiers': True, 'groups': [
            {'id': 'g1', 'choices': [{'id': {'z': 1}}]},
        ]}, 'BadChoiceId')
        result = ConOrder.normalize_selected_modifiers(item, {'g1': ['c1']})
        self.assertEqual(result.get('status'), 400, result)

    def test_non_integer_selection_bounds_are_a_controlled_rejection(self):
        # PRE-FIX: raises TypeError ('<' / '>' between int and str).
        for key in ('minSelections', 'maxSelections'):
            with self.subTest(key=key):
                item = self._item_with({'hasModifiers': True, 'groups': [
                    {'id': 'g1', key: '2', 'choices': [{'id': 'c1'}]},
                ]}, f'BadBound-{key}')
                result = ConOrder.normalize_selected_modifiers(item, {'g1': ['c1']})
                self.assertEqual(result.get('status'), 400, result)

    def test_invalid_groups_container_fails_closed(self):
        # PRE-FIX: returns 200 with {} — silently "no modifiers required",
        # which is the required-selection bypass.
        item = self._item_with(
            {'hasModifiers': True, 'groups': 'oops'}, 'BadGroupsContainer',
        )
        result = ConOrder.normalize_selected_modifiers(item, {})
        self.assertEqual(result.get('status'), 400, result)


class D01QuantityHelperReproductionTests(_D01Base):
    """``update_item_quantity`` must enforce the rule itself, not rely on its
    current caller."""

    def setUp(self):
        super().setUp()
        response = self.post({'items': [self.line(quantity=5)]})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.line_row = OrderItem.objects.get()
        self.assertEqual(self.line_row.quantity, 5)

    def test_negative_increment_is_rejected_even_though_result_is_non_negative(self):
        # Existing 5 + incoming -1 == 4, which SATISFIES quantity >= 0, so the
        # database constraint cannot catch this. PRE-FIX: silently updates to 4.
        result = ConOrder.update_item_quantity(
            order_item=self.line_row,
            item={'item': str(self.item.id), 'quantity': -1},
        )
        self.assertEqual(result.get('status'), 400, result)
        self.line_row.refresh_from_db()
        self.assertEqual(
            self.line_row.quantity, 5, 'the row was mutated by a rejected call',
        )

    def test_non_integer_increments_are_rejected(self):
        # PRE-FIX: float/str raise TypeError; True silently adds 1.
        for bad in (2.5, 2.0, '3', True, None, [1], {'a': 1}, 0, -1, 10 ** 20):
            with self.subTest(quantity=repr(bad)):
                result = ConOrder.update_item_quantity(
                    order_item=self.line_row,
                    item={'item': str(self.item.id), 'quantity': bad},
                )
                self.assertEqual(result.get('status'), 400, result)
                self.line_row.refresh_from_db()
                self.assertEqual(self.line_row.quantity, 5)

    def test_a_valid_increment_still_merges(self):
        result = ConOrder.update_item_quantity(
            order_item=self.line_row,
            item={'item': str(self.item.id), 'quantity': 3},
        )
        self.assertEqual(result.get('status'), 200, result)
        self.line_row.refresh_from_db()
        self.assertEqual(self.line_row.quantity, 8)


class D01DatabaseInvariantReproductionTests(_D01Base):
    """The persisted non-negative invariant must be enforced by the DATABASE,
    reachable through paths that bypass every request serializer."""

    def _a_line(self):
        response = self.post({'items': [self.line(quantity=2)]})
        self.assertEqual(response.status_code, 200, response.content[:300])
        return OrderItem.objects.latest('time_created')

    def test_direct_orm_save_cannot_persist_a_negative_quantity(self):
        # PRE-FIX: no CHECK constraint exists, so this save succeeds.
        line = self._a_line()
        line.quantity = -1
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                line.save(update_fields=['quantity'])

    def test_queryset_update_cannot_persist_a_negative_quantity(self):
        # A bulk UPDATE bypasses model save() entirely.
        line = self._a_line()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderItem.objects.filter(pk=line.pk).update(quantity=-5)

    def test_zero_and_positive_quantities_remain_legal(self):
        line = self._a_line()
        for legal in (0, 1, 250):
            with self.subTest(quantity=legal):
                with transaction.atomic():
                    OrderItem.objects.filter(pk=line.pk).update(quantity=legal)
                line.refresh_from_db()
                self.assertEqual(line.quantity, legal)


# =====================================================================
# Newly approved bounds and shared-rule guarantees.
# These establish D01 policy; they are NOT reproductions of an old defect.
# =====================================================================


class D01LimitsAreDefinedOnceTests(TestCase):
    """The ceilings are named constants in ONE module, and the values are the
    approved ones. A limit re-spelled at a call site is how two boundaries start
    disagreeing about the same rule."""

    def test_the_approved_values(self):
        self.assertEqual(order_input.MAX_QUANTITY_PER_LINE, 99)
        self.assertEqual(order_input.MAX_LINES_PER_ORDER, 100)
        self.assertEqual(order_input.MAX_TOTAL_UNITS, 500)
        self.assertEqual(order_input.MAX_MODIFIER_GROUPS_PER_LINE, 32)
        self.assertEqual(order_input.MAX_CHOICES_PER_GROUP, 64)
        self.assertEqual(order_input.MAX_EXTRAS_PER_LINE, 64)
        self.assertEqual(order_input.MAX_MODIFIER_ID_LENGTH, 128)
        self.assertEqual(order_input.MAX_SELECTION_ENTRIES_PER_REQUEST, 2048)

    def test_every_quantity_boundary_uses_the_one_rule(self):
        # Identity, not a similar-looking copy: the request validator, the
        # order-item chokepoint and the merge helper must call THIS function.
        source = inspect.getsource(con_orders_module)
        self.assertIn('quantity_error(item.get(\'quantity\'))', source)
        self.assertEqual(
            con_orders_module.quantity_error, order_input.quantity_error,
        )

    def test_the_empty_basket_message_is_not_restated(self):
        self.assertEqual(order_input.NO_ITEMS_MESSAGE,
                         MESSAGES['NO_ORDER_ITEMS'])

    def test_an_id_shaped_failure_keeps_the_opaque_menu_message(self):
        # Load-bearing: a malformed id must stay indistinguishable from a
        # foreign or nonexistent one.
        self.assertEqual(order_input.MENU_ID_MESSAGE, NOT_ON_MENU_MESSAGE)


class D01QuantityRuleTests(TestCase):
    """The one rule, exercised directly."""

    def test_accepts_only_real_positive_integers_in_range(self):
        for good in (1, 2, 50, order_input.MAX_QUANTITY_PER_LINE):
            with self.subTest(quantity=good):
                self.assertIsNone(order_input.quantity_error(good))

    def test_rejects_everything_else(self):
        # A LIST of pairs, not a dict: `False == 0` and `True == 1`, so a dict
        # literal would silently collapse the bool cases onto the int keys —
        # which is the very conflation this rule exists to refuse.
        cases = [
            (None, order_input.QUANTITY_MISSING),
            (0, order_input.QUANTITY_NOT_POSITIVE),
            (-1, order_input.QUANTITY_NOT_POSITIVE),
            (True, order_input.QUANTITY_NOT_AN_INTEGER),
            (False, order_input.QUANTITY_NOT_AN_INTEGER),
            (2.0, order_input.QUANTITY_NOT_AN_INTEGER),
            (2.5, order_input.QUANTITY_NOT_AN_INTEGER),
            ('3', order_input.QUANTITY_NOT_AN_INTEGER),
            (order_input.MAX_QUANTITY_PER_LINE + 1,
             order_input.QUANTITY_ABOVE_LIMIT),
        ]
        for value, expected in cases:
            with self.subTest(quantity=repr(value)):
                self.assertEqual(order_input.quantity_error(value), expected)
        for container in ([1], {'a': 1}, (1,)):
            with self.subTest(quantity=repr(container)):
                self.assertEqual(
                    order_input.quantity_error(container),
                    order_input.QUANTITY_NOT_AN_INTEGER,
                )


class D01BoundsTests(_D01Base):
    """Exact limit and limit+1 for every ceiling. Collection ceilings count RAW
    entries — before de-duplication."""

    def test_quantity_per_line_boundary(self):
        limit = order_input.MAX_QUANTITY_PER_LINE
        response = self.post({'items': [self.line(quantity=limit)]})
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(OrderItem.objects.get().quantity, limit)
        Order.objects.all().delete()
        OrderItem.objects.all().delete()
        RestaurantDailyOrderCounter.objects.all().delete()
        self.assert_rejected({'items': [self.line(quantity=limit + 1)]})

    def test_lines_per_order_boundary(self):
        limit = order_input.MAX_LINES_PER_ORDER
        # Distinct ids are irrelevant to the ceiling — it counts submitted
        # lines, so repeated lines cannot slip past it.
        at_limit = [self.line(quantity=1) for _ in range(limit)]
        over = at_limit + [self.line(quantity=1)]
        self.assert_rejected({'items': over})
        response = self.post({'items': at_limit})
        self.assertEqual(response.status_code, 200, response.content[:200])

    def test_total_units_boundary(self):
        limit = order_input.MAX_TOTAL_UNITS
        per_line = order_input.MAX_QUANTITY_PER_LINE
        # Repeated lines for the SAME item: the per-line ceiling alone would let
        # these through, so the aggregate is what bounds them.
        full, remainder = divmod(limit, per_line)
        at_limit = [self.line(quantity=per_line) for _ in range(full)]
        if remainder:
            at_limit.append(self.line(quantity=remainder))
        self.assertEqual(sum(l['quantity'] for l in at_limit), limit)
        over = at_limit + [self.line(quantity=1)]
        self.assert_rejected({'items': over})
        response = self.post({'items': at_limit})
        self.assertEqual(response.status_code, 200, response.content[:200])

    def test_modifier_groups_per_line_boundary(self):
        limit = order_input.MAX_MODIFIER_GROUPS_PER_LINE
        over = {f'g{i}': [] for i in range(limit + 1)}
        result = order_input.validate_order_items(
            [self.line(item=self.mod_item, selected_modifiers=over)],
        )
        self.assertEqual(result['status'], 400)
        at_limit = {f'g{i}': [] for i in range(limit)}
        result = order_input.validate_order_items(
            [self.line(item=self.mod_item, selected_modifiers=at_limit)],
        )
        self.assertEqual(result['status'], 200, result)

    def test_choices_per_group_boundary_counts_raw_entries(self):
        limit = order_input.MAX_CHOICES_PER_GROUP
        # Every entry is the SAME id: after de-duplication this is one choice,
        # so a ceiling applied post-de-dup would never fire. It must count raw.
        over = {'g1': ['c1'] * (limit + 1)}
        result = order_input.validate_order_items(
            [self.line(item=self.mod_item, selected_modifiers=over)],
        )
        self.assertEqual(result['status'], 400, result)
        at_limit = {'g1': ['c1'] * limit}
        result = order_input.validate_order_items(
            [self.line(item=self.mod_item, selected_modifiers=at_limit)],
        )
        self.assertEqual(result['status'], 200, result)

    def test_extras_per_line_boundary_counts_raw_entries(self):
        limit = order_input.MAX_EXTRAS_PER_LINE
        repeated = str(self.item.id)
        result = order_input.validate_order_items(
            [self.line(extras=[repeated] * (limit + 1))],
        )
        self.assertEqual(result['status'], 400, result)
        result = order_input.validate_order_items(
            [self.line(extras=[repeated] * limit)],
        )
        self.assertEqual(result['status'], 200, result)

    def test_modifier_identifier_length_boundary(self):
        limit = order_input.MAX_MODIFIER_ID_LENGTH
        ok = order_input.validate_order_items(
            [self.line(item=self.mod_item,
                       selected_modifiers={'x' * limit: ['c' * limit]})],
        )
        self.assertEqual(ok['status'], 200, ok)
        for oversized in ({'x' * (limit + 1): ['c1']},
                          {'g1': ['c' * (limit + 1)]}):
            with self.subTest(selection=list(oversized)[0][:8]):
                result = order_input.validate_order_items(
                    [self.line(item=self.mod_item,
                               selected_modifiers=oversized)],
                )
                self.assertEqual(result['status'], 400, result)

    def test_total_selection_entries_boundary_spans_the_whole_request(self):
        limit = order_input.MAX_SELECTION_ENTRIES_PER_REQUEST
        per_group = order_input.MAX_CHOICES_PER_GROUP
        groups_per_line = order_input.MAX_MODIFIER_GROUPS_PER_LINE
        per_line = {f'g{i}': ['c1'] * per_group for i in range(groups_per_line)}
        entries_per_line = per_group * groups_per_line
        lines = limit // entries_per_line
        at_limit = [
            self.line(item=self.mod_item, selected_modifiers=dict(per_line))
            for _ in range(lines)
        ]
        self.assertEqual(len(at_limit) * entries_per_line, limit)
        result = order_input.validate_order_items(at_limit)
        self.assertEqual(result['status'], 200, result)
        over = at_limit + [
            self.line(item=self.mod_item, selected_modifiers={'g1': ['c1']}),
        ]
        result = order_input.validate_order_items(over)
        self.assertEqual(result['status'], 400, result)


class D01MergedRowIsNotCappedTests(_D01Base):
    """The per-line ceiling bounds what may be SUBMITTED. It is not a database
    maximum and not a cap on a merged row."""

    def test_two_valid_lines_merge_above_the_per_line_ceiling(self):
        # 60 + 60 = 120, which exceeds MAX_QUANTITY_PER_LINE (99) in the stored
        # row while every submitted line stayed inside it. That is legal.
        response = self.post({'items': [self.line(quantity=60),
                                        self.line(quantity=60)]})
        self.assertEqual(response.status_code, 200, response.content[:300])
        line = OrderItem.objects.get()
        self.assertEqual(line.quantity, 120)
        self.assertGreater(line.quantity, order_input.MAX_QUANTITY_PER_LINE)

    def test_the_merge_helper_accepts_an_increment_that_crosses_the_ceiling(self):
        response = self.post({'items': [self.line(quantity=95)]})
        self.assertEqual(response.status_code, 200, response.content[:300])
        line = OrderItem.objects.get()
        result = ConOrder.update_item_quantity(
            order_item=line, item={'item': str(self.item.id), 'quantity': 10},
        )
        self.assertEqual(result.get('status'), 200, result)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 105)

    def test_the_order_wide_ceiling_is_what_bounds_repeated_lines(self):
        # Repeated lines for one item cannot escape the aggregate.
        over = [self.line(quantity=99)
                for _ in range(order_input.MAX_TOTAL_UNITS // 99 + 1)]
        self.assertGreater(sum(l['quantity'] for l in over),
                           order_input.MAX_TOTAL_UNITS)
        self.assert_rejected({'items': over})


class D01NestedSelectionShapeTests(_D01Base):
    """Supported omitted / null / empty forms, and the containers that are not."""

    def test_supported_omitted_and_null_forms_are_preserved(self):
        for line in (
            {'item': str(self.item.id), 'quantity': 1},
            {'item': str(self.item.id), 'quantity': 1,
             'selected_modifiers': None, 'extras': None},
            {'item': str(self.item.id), 'quantity': 1,
             'selected_modifiers': {}, 'extras': []},
        ):
            with self.subTest(line=sorted(line)):
                response = self.post({'items': [line]})
                self.assertEqual(response.status_code, 200,
                                 response.content[:200])
                Order.objects.all().delete()
                OrderItem.objects.all().delete()
                RestaurantDailyOrderCounter.objects.all().delete()

    def test_absent_keys_stay_absent_and_null_stays_null(self):
        result = order_input.validate_order_items([
            {'item': str(self.item.id), 'quantity': 1},
            {'item': str(self.item.id), 'quantity': 1,
             'selected_modifiers': None, 'extras': None},
        ])
        self.assertEqual(result['status'], 200, result)
        self.assertNotIn('selected_modifiers', result['items'][0])
        self.assertNotIn('extras', result['items'][0])
        self.assertIsNone(result['items'][1]['selected_modifiers'])
        self.assertIsNone(result['items'][1]['extras'])

    def test_wrong_containers_are_rejected(self):
        for selection in ('c1', ['c1'], 5):
            with self.subTest(selected_modifiers=repr(selection)):
                self.assert_rejected({'items': [self.line(
                    item=self.mod_item, selected_modifiers=selection)]})
        for extras in ({'a': 1}, 'x', 5):
            with self.subTest(extras=repr(extras)):
                self.assert_rejected({'items': [self.line(extras=extras)]})

    def test_invalid_identifier_types_are_rejected(self):
        for selection in ({5: ['c1']}, {'': ['c1']}, {'g1': [5]},
                          {'g1': ['']}, {'g1': [None]}):
            with self.subTest(selected_modifiers=repr(selection)):
                result = order_input.validate_order_items(
                    [self.line(item=self.mod_item,
                               selected_modifiers=selection)],
                )
                self.assertEqual(result['status'], 400, result)

    def test_duplicate_and_reordered_valid_choices_still_canonicalise(self):
        for selection in ({'g1': ['c1', 'c2']}, {'g1': ['c2', 'c1']},
                          {'g1': ['c1', 'c1', 'c2']}):
            with self.subTest(selected_modifiers=selection):
                response = self.post({'items': [self.line(
                    item=self.mod_item, selected_modifiers=selection)]})
                self.assertEqual(response.status_code, 200,
                                 response.content[:200])
                line = OrderItem.objects.get()
                # Menu-definition order, duplicates collapsed — unchanged.
                self.assertEqual(line.selected_modifiers, {'g1': ['c1', 'c2']})
                Order.objects.all().delete()
                OrderItem.objects.all().delete()
                RestaurantDailyOrderCounter.objects.all().delete()

    def test_opaque_modifier_ids_are_never_treated_as_uuids_or_trimmed(self):
        # The shipped operator UI mints UUIDs, but the column is unvalidated
        # JSON and the repository's own fixtures use 'g1' / 'c1'. Identity is
        # preserved exactly.
        result = order_input.validate_order_items(
            [self.line(item=self.mod_item,
                       selected_modifiers={'  g 1  ': ['  c 1  ']})],
        )
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(result['items'][0]['selected_modifiers'],
                         {'  g 1  ': ['  c 1  ']})


class D01ValidatorStabilityTests(_D01Base):
    """Validating already-validated output is stable, and validation never
    mutates the caller's payload."""

    def _payload(self):
        return [
            {'item': str(self.item.id).upper(), 'quantity': 2},
            {'item': str(self.mod_item.id), 'quantity': 1,
             'selected_modifiers': {'g1': ['c1', 'c1']},
             'extras': [str(self.other_item.id).upper()]},
            {'item': str(self.item.id), 'quantity': 1,
             'selected_modifiers': None, 'extras': None},
        ]

    def test_validation_is_idempotent(self):
        once = order_input.validate_order_items(self._payload())
        self.assertEqual(once['status'], 200, once)
        twice = order_input.validate_order_items(once['items'])
        self.assertEqual(twice['status'], 200, twice)
        self.assertEqual(once['items'], twice['items'])

    def test_validated_ids_are_canonical_strings_not_uuid_objects(self):
        # A UUID OBJECT at one boundary would be rejected as a non-string at
        # the next; canonical strings re-validate to themselves.
        result = order_input.validate_order_items(self._payload())
        for line in result['items']:
            self.assertIsInstance(line['item'], str)
            self.assertEqual(line['item'], line['item'].lower())
        self.assertIsInstance(result['items'][1]['extras'][0], str)

    def test_the_callers_payload_is_never_mutated(self):
        payload = self._payload()
        snapshot = copy.deepcopy(payload)
        order_input.validate_order_items(payload)
        self.assertEqual(payload, snapshot)

    def test_client_order_id_round_trips(self):
        supplied = str(uuid.uuid4()).upper()
        once = order_input.validate_order_request(
            {'items': [self.line()], 'client_order_id': supplied},
        )
        self.assertEqual(once['status'], 200, once)
        twice = order_input.validate_order_request(
            {'items': once['items'], 'client_order_id': once['client_order_id']},
        )
        self.assertEqual(twice['status'], 200, twice)
        self.assertEqual(once['client_order_id'], twice['client_order_id'])

    def test_absent_and_null_client_order_id_both_stay_optional(self):
        for payload in ({'items': [self.line()]},
                        {'items': [self.line()], 'client_order_id': None}):
            with self.subTest(payload=sorted(payload)):
                result = order_input.validate_order_request(payload)
                self.assertEqual(result['status'], 200, result)
                self.assertIsNone(result['client_order_id'])

    def test_there_is_no_pre_validated_bypass_flag(self):
        # A caller-supplied "already validated" marker would let any caller opt
        # out of the rule. Assert on the SIGNATURES rather than on the module
        # text, which legitimately discusses trust in prose.
        for function in (order_input.validate_order_items,
                         order_input.validate_order_request,
                         order_input.quantity_error):
            parameters = set(
                inspect.signature(function).parameters,
            )
            for bypass in ('validated', 'skip_validation', 'trusted',
                           'already_validated', 'force'):
                self.assertNotIn(bypass, parameters)
        # And every parameter is positional data, never a policy switch.
        self.assertEqual(
            list(inspect.signature(order_input.validate_order_items).parameters),
            ['items'],
        )


class D01NoPartialOrderTests(_D01Base):
    """One invalid line rejects the WHOLE order — wherever it sits, and with or
    without an existing daily counter row."""

    def _invalid_at(self, position, total=3):
        lines = [self.line(quantity=1) for _ in range(total)]
        lines[position] = self.line(quantity=-1)
        return lines

    def test_invalid_first_middle_or_last_line_creates_nothing(self):
        for position in (0, 1, 2):
            with self.subTest(position=position):
                self.assert_rejected({'items': self._invalid_at(position)})

    def test_rejection_with_no_pre_existing_counter_row(self):
        self.assertEqual(RestaurantDailyOrderCounter.objects.count(), 0)
        self.assert_rejected({'items': self._invalid_at(1)})

    def test_rejection_leaves_an_existing_counter_untouched(self):
        ok = self.post({'items': [self.line(quantity=1)]})
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        counter = RestaurantDailyOrderCounter.objects.get()
        before = counter.next_number
        orders_before = Order.objects.count()

        response = self.post({'items': self._invalid_at(2)})
        self.assertEqual(response.status_code, 400, response.content[:200])
        counter.refresh_from_db()
        self.assertEqual(counter.next_number, before,
                         'the daily order number was advanced by a rejection')
        self.assertEqual(Order.objects.count(), orders_before)

    def test_no_payment_record_is_created_by_a_rejection(self):
        self.assert_rejected({'items': self._invalid_at(0)})
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_a_foreign_item_rejects_the_whole_order_before_any_write(self):
        # EARLY rejection, and named for what it is. `validate_order_selections`
        # is a BATCH gate at step 2b of `_create_order`: it resolves every id in
        # ONE restaurant-scoped query, so the foreign second item is refused
        # before the daily counter is allocated, before the Order row exists and
        # before `add_order_item` is called even once. Instrumenting the service
        # confirms it — counter_allocated=False, add_order_item_calls=0.
        #
        # That makes this a tenant-boundary test, not a rollback test: it proves
        # one foreign id rejects the whole basket and leaves nothing behind,
        # which is worth keeping, but it exercises no unwinding because there is
        # nothing yet to unwind. Genuine late-rollback evidence lives in
        # `D01LateRollbackTests` below.
        foreign_owner = User.objects.create_user(
            first_name='F', last_name='O', email='d01-foreign@test.com',
            phone_number='256700031099', username='256700031099',
            country='Uganda', password='password', roles=[],
        )
        foreign = Restaurant.objects.create(
            name='D01 Foreign', location='fx', owner=foreign_owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        foreign_section = MenuSection.objects.create(
            name='FX', restaurant=foreign, approved=True, enabled=True,
            available=True, availability='always',
        )
        foreign_item = MenuItem.objects.create(
            name='Foreign', section=foreign_section, primary_price=1000,
            approved=True, enabled=True, available=True,
        )
        result = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=1),
                   {'item': str(foreign_item.id), 'quantity': 1}],
        )
        self.assertEqual(result.get('status'), 400, result)
        self.assert_no_rows()


class D01InternalWriterTests(_D01Base):
    """The authoritative service and the lower-level writers enforce the rule
    themselves — they do not rely on the endpoint having run it."""

    def test_direct_create_order_cannot_bypass_the_rule(self):
        for bad in (-1, 0, 2.5, '3', True, order_input.MAX_QUANTITY_PER_LINE + 1):
            with self.subTest(quantity=repr(bad)):
                result = _create_order(
                    restaurant=self.restaurant, table=self.table,
                    items=[self.line(quantity=bad)],
                )
                self.assertEqual(result.get('status'), 400, result)
                self.assert_no_rows()

    def test_direct_create_order_enforces_the_collection_ceilings(self):
        over = [self.line(quantity=1)
                for _ in range(order_input.MAX_LINES_PER_ORDER + 1)]
        result = _create_order(
            restaurant=self.restaurant, table=self.table, items=over,
        )
        self.assertEqual(result.get('status'), 400, result)
        self.assert_no_rows()

    def test_initiate_order_enforces_the_rule_for_a_non_http_caller(self):
        result = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=[self.line(quantity=-2)],
        )
        self.assertEqual(result.get('status'), 400, result)
        self.assert_no_rows()

    def test_add_order_item_chokepoint_rejects_without_persisting(self):
        ok = self.post({'items': [self.line(quantity=1)]})
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        order = Order.objects.get()
        before = OrderItem.objects.count()
        for bad in (-1, 0, 2.0, '2', True, None):
            with self.subTest(quantity=repr(bad)):
                result = ConOrder.add_order_item(
                    item={'item': str(self.other_item.id), 'quantity': bad},
                    order_id=str(order.id),
                )
                self.assertEqual(result.get('status'), 400, result)
                self.assertEqual(OrderItem.objects.count(), before)


class D01CompatibilityTests(_D01Base):
    """The live diner caller and an authorized staff caller both still work, and
    authority is unchanged."""

    def _staff_payload(self, **overrides):
        payload = {
            'source': 'admin',
            'restaurant': str(self.restaurant.pk),
            'table': str(self.table.pk),
            'items': [self.line(quantity=2)],
        }
        payload.update(overrides)
        return payload

    def test_the_live_diner_payload_shape_succeeds(self):
        # Exactly what basket-body.component.ts sends.
        response = self.post({
            'client_order_id': str(uuid.uuid4()),
            'items': [{
                'item': str(self.mod_item.id),
                'quantity': 2,
                'selected_modifiers': {'g1': ['c1']},
                'extras': [],
            }],
        })
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(OrderItem.objects.get().quantity, 2)

    def _staff_client(self, user):
        client = APIClient()
        token = str(RefreshToken.for_user(user).access_token)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_an_authorized_staff_caller_succeeds(self):
        response = self._staff_client(self.owner).post(
            INITIATE_URL, self._staff_payload(), format='json',
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(OrderItem.objects.get().quantity, 2)

    def test_a_staff_caller_is_held_to_the_same_quantity_rule(self):
        response = self._staff_client(self.owner).post(
            INITIATE_URL,
            self._staff_payload(items=[self.line(quantity=-4)]),
            format='json',
        )
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assert_no_rows()

    def test_an_unauthorized_staff_source_claim_still_fails(self):
        outsider = User.objects.create_user(
            first_name='X', last_name='Y', email='d01-outsider@test.com',
            phone_number='256700031098', username='256700031098',
            country='Uganda', password='password', roles=[],
        )
        response = self._staff_client(outsider).post(
            INITIATE_URL, self._staff_payload(), format='json',
        )
        self.assertEqual(response.status_code, 404, response.content[:200])
        self.assert_no_rows()

    def test_a_foreign_tenant_substitution_in_the_body_still_fails(self):
        other_owner = User.objects.create_user(
            first_name='O', last_name='T', email='d01-other@test.com',
            phone_number='256700031097', username='256700031097',
            country='Uganda', password='password', roles=[],
        )
        other = Restaurant.objects.create(
            name='D01 Other', location='ot', owner=other_owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        response = self.post({
            'restaurant': str(other.pk),
            'items': [self.line(quantity=1)],
        })
        self.assertEqual(response.status_code, 400, response.content[:200])
        self.assert_no_rows()

    def test_ownership_fields_in_the_body_are_ignored_not_written(self):
        stranger = User.objects.create_user(
            first_name='S', last_name='R', email='d01-stranger@test.com',
            phone_number='256700031096', username='256700031096',
            country='Uganda', password='password', roles=[],
        )
        response = self.post({
            'items': [dict(self.line(quantity=1),
                           unit_price='1', total_cost='1', actual_cost='1',
                           created_by=str(stranger.pk))],
            'created_by': str(stranger.pk),
        })
        self.assertEqual(response.status_code, 200, response.content[:300])
        order = Order.objects.get()
        line = OrderItem.objects.get()
        self.assertIsNone(order.created_by, 'a body field set order ownership')
        self.assertIsNone(line.created_by)
        # Money is server-derived from the menu item, never from the request.
        self.assertEqual(line.unit_price, self.item.primary_price)
        self.assertEqual(line.actual_cost, self.item.primary_price)

    def test_the_error_envelope_keeps_a_usable_top_level_message(self):
        payload = self.assert_rejected({'items': [self.line(quantity=-1)]})
        self.assertIn('message', payload)
        self.assertIsInstance(payload['message'], str)
        # `errors` is additive, bounded, and never echoes a submitted value.
        self.assertIn('errors', payload)
        self.assertLessEqual(len(payload['errors']),
                             order_input.MAX_REPORTED_ERRORS)
        self.assertNotIn('-1', json.dumps(payload['errors']))
        # And it must NOT look like the ongoing-order block the diner app
        # special-cases on `data.order_id`.
        self.assertNotIn('data', payload)

    def test_validation_errors_are_bounded(self):
        many = [self.line(quantity=-1) for _ in range(40)]
        payload = self.assert_rejected({'items': many})
        self.assertLessEqual(len(payload['errors']),
                             order_input.MAX_REPORTED_ERRORS)


class D01ReplayTests(_D01Base):
    """A correctly shaped, authorized replay is unaffected by D01 and by later
    menu changes."""

    def test_a_correctly_shaped_replay_creates_nothing_after_a_menu_change(self):
        key = str(uuid.uuid4())
        body = {'client_order_id': key, 'items': [self.line(quantity=2)]}
        first = self.post(body)
        self.assertEqual(first.status_code, 200, first.content[:200])
        order_id = first.json()['data']['order_details']['id']
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()

        # The menu moves underneath the replay.
        self.item.available = False
        self.item.in_stock = False
        self.item.enabled = False
        self.item.save(update_fields=['available', 'in_stock', 'enabled'])

        replay = self.post(body)
        self.assertEqual(replay.status_code, 200, replay.content[:200])
        self.assertEqual(replay.json()['data']['order_details']['id'], order_id)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)

    def test_a_malformed_replay_is_refused_by_the_static_rule(self):
        # Documented, intentional: static checks precede the replay lookup, so a
        # body that is not a well-formed order is refused whether or not it
        # repeats a key. No compatibility is promised for a previously accepted
        # malformed body.
        key = str(uuid.uuid4())
        first = self.post(
            {'client_order_id': key, 'items': [self.line(quantity=2)]},
        )
        self.assertEqual(first.status_code, 200, first.content[:200])
        orders_before = Order.objects.count()
        replay = self.post(
            {'client_order_id': key, 'items': [self.line(quantity=-2)]},
        )
        self.assertEqual(replay.status_code, 400, replay.content[:200])
        self.assertEqual(Order.objects.count(), orders_before)


class D01LateRollbackTests(TransactionTestCase):
    """
    GENUINE late-rollback evidence: writes that really happened, then a failure
    after them, then proof they are gone.

    WHY THIS NEEDED A SPY. The service's earlier gates are thorough — static
    input validation, then `validate_order_selections` and
    `normalize_order_items` inside the transaction — so on correct production
    code almost every bad basket is refused BEFORE the counter is allocated.
    Instrumenting the two scenarios that previously claimed to test rollback
    showed both were rejected early (counter_allocated=False,
    add_order_item_calls=0). A test-only spy on `ConOrder.add_order_item` is
    therefore the only way to reach the late path; it DELEGATES the first call
    to the real writer and injects a controlled non-200 on the second.

    Nothing real is faked: `transaction.atomic`, the ORM, rollback, the
    catalogue checks and the admission check all run for real, and the
    production code carries no fault-injection hook.

    `TransactionTestCase`, deliberately. `TestCase` wraps the whole test in a
    transaction it rolls back at teardown, which would make framework cleanup
    indistinguishable from the service's own rollback — and would make an
    `in_atomic_block` assertion meaningless. Here each statement commits, so a
    post-return query reads genuinely committed state, and every assertion is
    made BEFORE teardown.
    """

    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='LR', last_name='Owner', email='d01-lr@test.com',
            phone_number='256700061001', username='256700061001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='LR R', location='lr', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='LR Section', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self.first_item = MenuItem.objects.create(
            name='LR First', section=self.section, primary_price=10000,
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.second_item = MenuItem.objects.create(
            name='LR Second', section=self.section, primary_price=4000,
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )
        self.spare_table = Table.objects.create(
            number=2, str_number='2', restaurant=self.restaurant,
            qr_mode='order_pay',
        )

    # -- helpers ---------------------------------------------------------
    def _items(self):
        return [
            {'item': str(self.first_item.id), 'quantity': 2},
            {'item': str(self.second_item.id), 'quantity': 1},
        ]

    def _counter(self):
        return (
            RestaurantDailyOrderCounter.objects
            .filter(restaurant=self.restaurant)
            .values_list('next_number', flat=True)
            .first()
        )

    def _run_with_failure_on_the_second_item(self, table=None):
        """Delegate the first item to the real writer, capture what is visible
        inside the transaction, then inject a controlled rejection."""
        real_add = ConOrder.add_order_item
        seen = {'calls': 0, 'inside': None}

        def spy(item, order_id, order=None):
            seen['calls'] += 1
            if seen['calls'] == 1:
                result = real_add(item=item, order_id=order_id, order=order)
                # The REAL writer has just run. Prove its rows exist right now,
                # inside the open transaction, before anything fails.
                seen['inside'] = {
                    'orders': Order.objects.filter(pk=order_id).count(),
                    'items': OrderItem.objects.filter(order_id=order_id).count(),
                    'counter': self._counter(),
                    'quantity': OrderItem.objects.filter(
                        order_id=order_id,
                    ).values_list('quantity', flat=True).first(),
                }
                return result
            return {'status': 400, 'message': 'injected failure on item 2'}

        with mock.patch.object(ConOrder, 'add_order_item', staticmethod(spy)):
            result = _create_order(
                restaurant=self.restaurant, table=table or self.table,
                items=self._items(),
            )
        return result, seen

    # -- the evidence ----------------------------------------------------
    def test_a_late_failure_unwinds_writes_that_really_happened(self):
        self.assertIsNone(self._counter(), 'precondition: no counter yet')
        orders_before = Order.objects.count()

        result, seen = self._run_with_failure_on_the_second_item()

        # (2)+(3) the first item was persisted by the REAL writer, and those
        # writes were visible inside the transaction immediately before the
        # injected failure.
        self.assertEqual(seen['calls'], 2, 'the second item was never attempted')
        self.assertIsNotNone(seen['inside'], 'the first item never persisted')
        self.assertEqual(seen['inside']['orders'], 1)
        self.assertEqual(seen['inside']['items'], 1)
        self.assertEqual(seen['inside']['quantity'], 2)
        self.assertEqual(seen['inside']['counter'], 2,
                         'the counter was allocated and advanced')

        # (4) a controlled rejection, not an exception
        self.assertEqual(result.get('status'), 400, result)
        self.assertIn('message', result)

        # (5) and after the service returned, none of it survives.
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertIsNone(self._counter(),
                          'the daily counter row survived the rollback')
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    def test_it_restores_a_pre_existing_counter_rather_than_removing_it(self):
        # A counter row that predates the failed attempt must come back to its
        # ORIGINAL value, not vanish and not stay advanced.
        first = _create_order(
            restaurant=self.restaurant, table=self.spare_table,
            items=[{'item': str(self.first_item.id), 'quantity': 1}],
        )
        self.assertEqual(first.get('status'), 200, first)
        counter_before = self._counter()
        self.assertIsNotNone(counter_before)
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()

        result, seen = self._run_with_failure_on_the_second_item()

        self.assertEqual(seen['inside']['counter'], counter_before + 1,
                         'the attempt did not advance the counter')
        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(self._counter(), counter_before,
                         'the counter was not restored to its original value')
        # (unrelated data preserved)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)
        self.assertTrue(Order.objects.filter(pk=first['order'].pk).exists(),
                        'the earlier, unrelated order was destroyed')

    def test_success_control_writes_everything_and_advances_the_counter(self):
        # The same input WITHOUT the injected failure must write the records and
        # advance the counter — so the test above is evidence of rollback, not
        # of the basket never being writable in the first place.
        self.assertIsNone(self._counter())
        result = _create_order(
            restaurant=self.restaurant, table=self.table, items=self._items(),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 2)
        self.assertEqual(self._counter(), 2)
        self.assertEqual(
            sorted(OrderItem.objects.values_list('quantity', flat=True)),
            [1, 2],
        )
        self.assertEqual(DinifyTransaction.objects.count(), 0)


class D01ServiceClientOrderIdTests(_D01Base):
    """
    The optional idempotency key, enforced at the SERVICE boundaries.

    The endpoint already validated it; these services did not, and each consumes
    it in ORM operations — the replay lookup, the INSERT, and the insert-race
    recovery. Before this, a malformed value raised `ValidationError` OUT of the
    service, and an integer or boolean was silently COERCED by
    `uuid.UUID(int=...)` into a key no caller ever issued.

    The two failure classes are asserted separately on purpose: "it errors
    somehow" would have been satisfied by the coercion cases too, and those were
    the dangerous ones.
    """

    # Values a client may legitimately send, or an in-process caller may pass.
    def _valid_forms(self):
        generated = uuid.uuid4()
        return [
            ('absent', None),
            ('canonical string', str(uuid.uuid4())),
            ('UPPERCASE string', str(uuid.uuid4()).upper()),
            ('uuid.UUID object (internal)', generated),
        ]

    # Everything else. Split by how it used to fail.
    def _coerced_forms(self):
        # These were the silent ones: uuid.UUID(int=...) accepted them and
        # manufactured a key. 0 and False both became the nil UUID, so two
        # unrelated callers collided on one idempotency key.
        return [('integer 5', 5), ('integer 0', 0),
                ('True', True), ('False', False)]

    def _raising_forms(self):
        return [('malformed string', 'not-a-uuid'), ('blank string', ''),
                ('float', 2.5), ('empty list', []),
                ('list of uuid', [str(uuid.uuid4())]),
                ('empty dict', {}), ('dict', {'a': 1})]

    def _invalid_forms(self):
        return self._coerced_forms() + self._raising_forms()

    def _assert_nothing_written(self):
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(RestaurantDailyOrderCounter.objects.count(), 0)
        self.assertEqual(DinifyTransaction.objects.count(), 0)

    # -- ConOrder.initiate_order -----------------------------------------
    def test_initiate_order_refuses_every_invalid_key(self):
        for label, key in self._invalid_forms():
            with self.subTest(key=label):
                result = ConOrder.initiate_order(
                    restaurant_id=str(self.restaurant.pk),
                    table_id=str(self.table.pk),
                    items=[self.line(quantity=1)],
                    client_order_id=key,
                )
                self.assertEqual(result.get('status'), 400, result)
                self.assertTrue(str(result.get('message', '')).strip())
                self.assertEqual(result.get('errors', {}).get('client_order_id'),
                                 ['A valid client_order_id is required.'])
                self._assert_nothing_written()

    def test_initiate_order_accepts_every_valid_form(self):
        for label, key in self._valid_forms():
            with self.subTest(key=label):
                result = ConOrder.initiate_order(
                    restaurant_id=str(self.restaurant.pk),
                    table_id=str(self.table.pk),
                    items=[self.line(quantity=1)],
                    client_order_id=key,
                )
                self.assertEqual(result.get('status'), 200, result)
                Order.objects.all().delete()
                OrderItem.objects.all().delete()
                RestaurantDailyOrderCounter.objects.all().delete()

    # -- _create_order ----------------------------------------------------
    def test_create_order_refuses_every_invalid_key(self):
        for label, key in self._invalid_forms():
            with self.subTest(key=label):
                result = _create_order(
                    restaurant=self.restaurant, table=self.table,
                    items=[self.line(quantity=1)], client_order_id=key,
                )
                self.assertEqual(result.get('status'), 400, result)
                self.assertTrue(str(result.get('message', '')).strip())
                self._assert_nothing_written()

    def test_create_order_accepts_every_valid_form(self):
        for label, key in self._valid_forms():
            with self.subTest(key=label):
                result = _create_order(
                    restaurant=self.restaurant, table=self.table,
                    items=[self.line(quantity=1)], client_order_id=key,
                )
                self.assertEqual(result.get('status'), 200, result)
                Order.objects.all().delete()
                OrderItem.objects.all().delete()
                RestaurantDailyOrderCounter.objects.all().delete()

    # -- the specific hazards --------------------------------------------
    def test_an_integer_never_becomes_a_key_through_uuid_int(self):
        # uuid.UUID(int=5) is a well-formed identifier no caller ever issued.
        for key in (5, 0, True, False):
            with self.subTest(key=repr(key)):
                result = _create_order(
                    restaurant=self.restaurant, table=self.table,
                    items=[self.line(quantity=1)], client_order_id=key,
                )
                self.assertEqual(result.get('status'), 400, result)
        self.assertFalse(
            Order.objects.exclude(client_order_id=None).exists(),
            'a fabricated client_order_id was persisted',
        )

    def test_a_falsy_key_is_refused_rather_than_read_as_absent(self):
        # The old truthiness gate treated 0/False/''/[] as "no key": they
        # skipped the replay lookup, persisted the nil UUID, and then skipped
        # the insert-race recovery too, so a second attempt surfaced an
        # uncaught IntegrityError.
        for key in (0, False, '', []):
            with self.subTest(key=repr(key)):
                result = _create_order(
                    restaurant=self.restaurant, table=self.table,
                    items=[self.line(quantity=1)], client_order_id=key,
                )
                self.assertEqual(result.get('status'), 400, result)
                self._assert_nothing_written()

    def test_only_None_means_absent(self):
        result = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=1)], client_order_id=None,
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertIsNone(Order.objects.get().client_order_id)

    # -- canonicalisation and stability -----------------------------------
    def test_both_supported_forms_canonicalise_to_the_same_stored_key(self):
        generated = uuid.uuid4()
        typed = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=1)], client_order_id=generated,
        )
        self.assertEqual(typed.get('status'), 200, typed)
        # The SAME key as an UPPERCASE string is the same order, not a new one.
        replay = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=1)],
            client_order_id=str(generated).upper(),
        )
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay.get('idempotent'))
        self.assertEqual(replay['order'].pk, typed['order'].pk)
        self.assertEqual(Order.objects.count(), 1)

    def test_validating_the_canonical_output_again_is_stable(self):
        once, ok = order_input.validate_service_client_order_id(uuid.uuid4())
        self.assertTrue(ok)
        twice, ok2 = order_input.validate_service_client_order_id(once)
        self.assertTrue(ok2)
        self.assertEqual(once, twice)
        self.assertIsInstance(twice, str)

    def test_the_public_rule_is_not_loosened_by_the_service_rule(self):
        # An HTTP caller may not send a uuid.UUID object — only a string.
        self.assertEqual(
            order_input.validate_public_client_order_id(uuid.uuid4()),
            (None, False),
        )
        # ...and the service rule reuses the public one for strings, so there
        # is one notion of what a UUID is.
        self.assertEqual(
            order_input.validate_service_client_order_id('not-a-uuid'),
            (None, False),
        )

    def test_there_is_no_trusted_or_skip_validation_switch(self):
        for function in (order_input.validate_public_client_order_id,
                         order_input.validate_service_client_order_id):
            parameters = set(inspect.signature(function).parameters)
            self.assertEqual(parameters, {'value'})

    # -- replay behaviour preserved ---------------------------------------
    def test_a_same_key_replay_still_returns_the_original_after_a_menu_change(self):
        key = uuid.uuid4()
        first = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=2)], client_order_id=key,
        )
        self.assertEqual(first.get('status'), 200, first)
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()

        self.item.available = False
        self.item.enabled = False
        self.item.save(update_fields=['available', 'enabled'])

        replay = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[self.line(quantity=2)], client_order_id=key,
        )
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay.get('idempotent'))
        self.assertEqual(replay['order'].pk, first['order'].pk)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)

    # -- the endpoint is unchanged ----------------------------------------
    def test_the_endpoint_still_refuses_a_malformed_key(self):
        self.assert_rejected(
            {'items': [self.line(quantity=1)], 'client_order_id': 'not-a-uuid'},
        )

    def test_the_endpoint_still_refuses_an_integer_key(self):
        self.assert_rejected(
            {'items': [self.line(quantity=1)], 'client_order_id': 5},
        )
