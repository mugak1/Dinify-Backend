"""
Dining model, Step 1 — the SCHEMA (PR 1): visits, bills, round placement and the
restaurant's business-day cut-off.

NOTHING WRITES THESE YET. What this file pins is what the schema alone must
guarantee, so the writer that follows (behind ``DINING_VISIT_WRITES_ENABLED``) lands
on a foundation that is already proven:

* A ROLLBACK IS SAFE. Old code inserts restaurants without naming
  ``business_day_cutoff``; the column's DATABASE default is what makes that work.
* THE VOCABULARIES ARE DATABASE FACTS. ``payment_timing`` on a visit and on a
  placement is ``pay_first`` / ``pay_after`` / ``not_chosen`` and nothing else, and
  a visit has exactly one bill.
* NO RESPONSE MOVES except the owner's restaurant-detail read, which gains exactly
  one key. Every other serializer's field set is pinned to what it was at
  ``0588a1d``, captured there rather than re-derived here.
* DELETION BEHAVES AS IT DID. A restaurant that has an order still cannot be
  hard-deleted, with or without Step 1 rows beside the order, and the new foreign
  keys mirror the ones on ``Order`` / ``OrderAcceptance``.

No test here rewinds migrations (``dinify_backend/tests_migration_test_isolation.py``).
"""
import ast
import uuid
from datetime import date, time
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, connection, models, transaction
from django.db.models import ProtectedError, RestrictedError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app.models import PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.models import (
    DINING_PAYMENT_TIMINGS, PAYMENT_TIMING_NOT_CHOSEN,
    Bill, Order, OrderAcceptance, OrderPlacement, Visit,
)
from restaurants_app.models import Restaurant, RestaurantEmployee, Table
from users_app.models import User

DETAIL_URL = '/api/v1/restaurant-setup/details/'
CUTOFF = time(4, 0)

#: The field set of every serializer this change could reach, CAPTURED AT
#: 0588a1d (the commit this branch starts from) by instantiating each class there.
#: A literal, deliberately: deriving the expectation from the code under test
#: would pin nothing.
HEAD_SERIALIZER_FIELDS = {
    'restaurants_app.serializers.SerializerGetRestaurantDetail': frozenset({
        'accepting_orders', 'allow_deliveries', 'allow_pickups', 'archived',
        'branding_configuration', 'contact_email', 'contact_phone', 'country',
        'cover_photo', 'created_by', 'cuisine_types', 'deleted', 'deleted_by',
        'deletion_reason', 'eod_last_date', 'eod_record_date',
        'eod_restaurant_last_date', 'eod_restaurant_status',
        'expose_order_ratings', 'first_time_menu_approval',
        'first_time_menu_approval_decision', 'first_time_menu_submitted_by',
        'flat_fee', 'id', 'is_test', 'landmark', 'location', 'logo',
        'menu_item_sort_mode', 'name', 'opening_hours',
        'order_surcharge_cap_amount', 'order_surcharge_min_amount',
        'order_surcharge_percentage', 'owner', 'preferred_subscription_method',
        'preset_tags', 'receipt_footer', 'require_order_prepayments', 'socials',
        'status', 'subscription_expiry_date', 'subscription_validity', 'tagline',
        'time_created', 'time_deleted', 'time_last_updated', 'tin', 'vacuumed',
        'vat_rate', 'vat_registered',
    }),
    'restaurants_app.serializers.SerializerPublicGetRestaurant': frozenset({
        'cover_photo', 'id', 'location', 'logo', 'name', 'owner', 'preset_tags',
        'status',
    }),
    'restaurants_app.serializers.SerializerPutRestaurant': frozenset({
        'accepting_orders', 'allow_deliveries', 'allow_pickups',
        'branding_configuration', 'contact_email', 'contact_phone', 'country',
        'cover_photo', 'created_by', 'cuisine_types', 'deleted', 'deleted_by',
        'deletion_reason', 'expose_order_ratings', 'first_time_menu_approval',
        'first_time_menu_approval_decision', 'flat_fee', 'id', 'landmark',
        'location', 'logo', 'menu_item_sort_mode', 'name', 'opening_hours',
        'order_surcharge_cap_amount', 'order_surcharge_min_amount',
        'order_surcharge_percentage', 'owner', 'preferred_subscription_method',
        'preset_tags', 'receipt_footer', 'require_order_prepayments', 'socials',
        'status', 'subscription_expiry_date', 'subscription_validity', 'tagline',
        'time_created', 'time_deleted', 'time_last_updated', 'tin', 'vat_rate',
        'vat_registered',
    }),
    'restaurants_app.models.SerArcRestaurant': frozenset({
        'accepting_orders', 'allow_deliveries', 'allow_pickups', 'archived',
        'branding_configuration', 'contact_email', 'contact_phone', 'country',
        'cover_photo', 'created_by', 'cuisine_types', 'deleted', 'deleted_by',
        'deletion_reason', 'eod_last_date', 'eod_record_date',
        'eod_restaurant_last_date', 'eod_restaurant_status',
        'expose_order_ratings', 'first_time_menu_approval',
        'first_time_menu_approval_decision', 'first_time_menu_submitted_by',
        'flat_fee', 'id', 'is_test', 'landmark', 'location', 'logo',
        'menu_item_sort_mode', 'name', 'opening_hours',
        'order_surcharge_cap_amount', 'order_surcharge_min_amount',
        'order_surcharge_percentage', 'owner', 'preferred_subscription_method',
        'preset_tags', 'receipt_footer', 'require_order_prepayments', 'socials',
        'status', 'subscription_expiry_date', 'subscription_validity', 'tagline',
        'time_created', 'time_deleted', 'time_last_updated', 'tin', 'vacuumed',
        'vat_rate', 'vat_registered',
    }),
    'restaurants_app.serializers.SerializerPublicGetTable': frozenset({
        'archived', 'created_by', 'deleted', 'deleted_by', 'deletion_reason',
        'dining_area', 'display_name', 'enabled', 'eod_last_date',
        'eod_record_date', 'floor_height', 'floor_width', 'floor_x', 'floor_y',
        'has_qr', 'id', 'is_active', 'max_capacity', 'min_capacity', 'number',
        'outdoor_seating', 'prepayment_required', 'qr_mode', 'qr_regenerated_at',
        'qr_version', 'reserved', 'restaurant', 'room_name', 'shape',
        'smoking_zone', 'status', 'str_number', 'tags', 'time_created',
        'time_deleted', 'time_last_updated', 'vacuumed',
    }),
    'restaurants_app.serializers.SerializerPublicGetTableDetails': frozenset({
        'available', 'current_order', 'dining_area', 'display_name', 'id',
        'max_capacity', 'min_capacity', 'number', 'prepayment_required', 'qr_mode',
        'reserved', 'restaurant', 'room_name', 'shape', 'status', 'tags',
    }),
    'restaurants_app.serializers.SerializerPutTable': frozenset({
        'created_by', 'deleted', 'deleted_by', 'deletion_reason', 'dining_area',
        'display_name', 'enabled', 'floor_height', 'floor_width', 'floor_x',
        'floor_y', 'has_qr', 'id', 'is_active', 'max_capacity', 'min_capacity',
        'number', 'outdoor_seating', 'prepayment_required', 'qr_mode',
        'qr_regenerated_at', 'qr_version', 'reserved', 'restaurant', 'room_name',
        'shape', 'smoking_zone', 'status', 'str_number', 'tags', 'time_created',
        'time_deleted', 'time_last_updated',
    }),
    'restaurants_app.models.SerArcTable': frozenset({
        'archived', 'created_by', 'deleted', 'deleted_by', 'deletion_reason',
        'dining_area', 'display_name', 'enabled', 'eod_last_date',
        'eod_record_date', 'floor_height', 'floor_width', 'floor_x', 'floor_y',
        'has_qr', 'id', 'is_active', 'max_capacity', 'min_capacity', 'number',
        'outdoor_seating', 'prepayment_required', 'qr_mode', 'qr_regenerated_at',
        'qr_version', 'reserved', 'restaurant', 'room_name', 'shape',
        'smoking_zone', 'status', 'str_number', 'tags', 'time_created',
        'time_deleted', 'time_last_updated', 'vacuumed',
    }),
    'orders_app.serializers.SerializerPublicOrderDetails': frozenset({
        'accepted', 'accepted_at', 'actual_cost', 'balance_payable', 'checkout',
        'checkout_protocol', 'discounted_cost', 'id', 'items', 'order_number',
        'order_status', 'payment_status', 'prepayment_required', 'quote',
        'quote_closure', 'quote_complete', 'quote_policy', 'quote_protocol',
        'quote_total', 'savings', 'table', 'time_last_updated', 'total_cost',
        'total_paid',
    }),
    'orders_app.serializers.SerializerListOrderItem': frozenset({
        'actual_cost', 'available', 'cost_of_options', 'deleted',
        'deletion_reason', 'discounted_price', 'extra_items', 'id', 'item',
        'options', 'quantity', 'savings', 'status', 'time_last_updated',
        'unit_price',
    }),
    'orders_app.serializers.SerializerPutOrderItem': frozenset({
        'actual_cost', 'allergen_tags_snapshot', 'available', 'cost_of_options',
        'created_by', 'deleted', 'deleted_by', 'deletion_reason', 'discounted',
        'discounted_cost', 'discounted_price', 'id', 'item', 'item_name_snapshot',
        'last_updated_by', 'modifiers_snapshot', 'option', 'option_choice',
        'option_cost', 'options', 'order', 'parent_item', 'quantity', 'savings',
        'selected_modifiers', 'status', 'time_created', 'time_deleted',
        'time_last_updated', 'total_cost', 'unit_cost_of_options', 'unit_price',
    }),
    'orders_app.serializers_kitchen.ActiveKitchenOrderSerializer': frozenset({
        'created_at', 'fulfilment_revision', 'fulfilment_status', 'id', 'items',
        'order_number', 'order_source', 'order_status', 'priority', 'served_at',
        'table_label',
    }),
    'orders_app.serializers_kitchen.KitchenMenuItemSerializer': frozenset({
        'available', 'id', 'in_stock', 'name', 'section_name',
    }),
    'reports_app.serializers.SerializerOrderListingReport': frozenset({
        'discount', 'gross', 'item_count', 'order_number', 'payment_mode',
        'payment_status', 'revenue', 'time_created',
    }),
}


#: The ONE key Step 1 adds, and the only two serializers allowed to gain it. Both
#: return every model field (`fields='__all__'`): the owner's restaurant-detail read
#: and the MongoDB archive copy.
ADDED_KEY = 'business_day_cutoff'
GAINS_THE_KEY = {
    'restaurants_app.serializers.SerializerGetRestaurantDetail',
    'restaurants_app.models.SerArcRestaurant',
}


def _user(phone):
    return User.objects.create_user(
        first_name='Dining', last_name='Schema', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda', password='password',
    )


def _order(restaurant, table):
    return Order.objects.create(
        restaurant=restaurant, table=table,
        total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
    )


def _round(order, timing=PAYMENT_TIMING_NOT_CHOSEN):
    """A visit, its bill and the order's placement, as the Step 1 writer will
    create them. Written directly: nothing in production writes them yet."""
    now = timezone.now()
    visit = Visit.objects.create(
        restaurant=order.restaurant, table=order.table, opened_at=now,
        payment_timing=timing,
    )
    bill = Bill.objects.create(visit=visit)
    placement = OrderPlacement.objects.create(
        order=order, visit=visit, bill=bill, payment_timing=timing,
        released_at=now, business_date=date(2026, 11, 2), round_number=1,
    )
    return visit, bill, placement


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = _user('256700430001')
        cls.restaurant = Restaurant.objects.create(
            name='Dining Schema', location='Kampala', owner=cls.owner,
            status=RestaurantStatus_Live,
        )
        cls.table = Table.objects.create(number=1, restaurant=cls.restaurant)


# --- the cut-off and its database default ----------------------------------------

class BusinessDayCutoffTests(_Fixture):
    def test_a_new_restaurant_turns_over_at_four(self):
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.business_day_cutoff, CUTOFF)

    def test_an_insert_that_never_names_the_column_gets_four(self):
        """THE ROLLBACK CASE, exercised rather than reasoned about.

        Old code's INSERT names every restaurant column it knows and none it does
        not. This copies an existing row through raw SQL naming every column EXCEPT
        the new one, so the value can only come from the database default.
        Django drops a Python-only default after ``AddField``; without
        ``db_default`` this INSERT is a NOT NULL violation.
        """
        new_id = uuid.uuid4()
        columns = [
            field.column for field in Restaurant._meta.concrete_fields
            if field.column != ADDED_KEY
        ]
        quote = connection.ops.quote_name
        selected = [
            '%s' if column in ('id', 'name') else quote(column)
            for column in columns
        ]
        params = [
            models.UUIDField().get_db_prep_value(new_id, connection)
            if column == 'id' else 'Old Code Cafe'
            for column in columns if column in ('id', 'name')
        ]
        source_id = models.UUIDField().get_db_prep_value(
            self.restaurant.pk, connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO {quote("restaurants")} '
                f'({", ".join(quote(c) for c in columns)}) '
                f'SELECT {", ".join(selected)} FROM {quote("restaurants")} '
                f'WHERE {quote("id")} = %s',
                params + [source_id],
            )
        self.assertNotIn(ADDED_KEY, columns)
        self.assertEqual(
            Restaurant.objects.get(pk=new_id).business_day_cutoff, CUTOFF)

    def test_the_cutoff_has_no_writer(self):
        """No tenant path can change it in Step 1: absent from EDIT_INFORMATION and
        from the restaurant write serializer."""
        from dinify_backend.configss.edit_information import EDIT_INFORMATION
        from restaurants_app.serializers import SerializerPutRestaurant
        self.assertNotIn(ADDED_KEY, EDIT_INFORMATION['restaurants'])
        self.assertNotIn(ADDED_KEY, SerializerPutRestaurant().fields)


# --- the vocabularies and the one-bill rule ---------------------------------------

class PaymentTimingVocabularyTests(_Fixture):
    def setUp(self):
        self.order = _order(self.restaurant, self.table)

    def test_the_vocabulary_is_commercial_apps_plus_one(self):
        """Built from commercial_app's own constants, so the spellings cannot
        drift, plus the one value Dinify adds."""
        self.assertEqual(
            DINING_PAYMENT_TIMINGS,
            (PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_AFTER, 'not_chosen'),
        )
        self.assertEqual(PAYMENT_TIMING_NOT_CHOSEN, 'not_chosen')

    def test_every_value_in_the_vocabulary_is_accepted(self):
        for value in DINING_PAYMENT_TIMINGS:
            with self.subTest(value=value):
                order = _order(self.restaurant, self.table)
                visit, _bill, placement = _round(order, timing=value)
                self.assertEqual(visit.payment_timing, value)
                self.assertEqual(placement.payment_timing, value)

    def test_a_visit_refuses_a_timing_outside_the_vocabulary(self):
        visit, _bill, _placement = _round(self.order)
        for bad in ('cash', '', 'PAY_FIRST', 'unset'):
            with self.subTest(value=bad):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Visit.objects.filter(pk=visit.pk).update(payment_timing=bad)

    def test_a_placement_refuses_a_timing_outside_the_vocabulary(self):
        _visit, _bill, placement = _round(self.order)
        for bad in ('cash', '', 'PAY_FIRST', 'unset'):
            with self.subTest(value=bad):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        OrderPlacement.objects.filter(pk=placement.pk).update(
                            payment_timing=bad)


class OneBillPerVisitTests(_Fixture):
    def test_a_second_bill_for_a_visit_is_refused(self):
        visit, _bill, _placement = _round(_order(self.restaurant, self.table))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Bill.objects.create(visit=visit)
        self.assertEqual(Bill.objects.filter(visit=visit).count(), 1)

    def test_one_order_has_at_most_one_placement(self):
        order = _order(self.restaurant, self.table)
        visit, bill, _placement = _round(order)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderPlacement.objects.create(
                    order=order, visit=visit, bill=bill,
                    payment_timing=PAYMENT_TIMING_NOT_CHOSEN,
                    business_date=date(2026, 11, 2), round_number=1,
                )

    def test_released_at_may_be_empty_for_a_held_round(self):
        """Step 2's held rounds have no release time yet."""
        order = _order(self.restaurant, self.table)
        visit, bill, placement = _round(order)
        OrderPlacement.objects.filter(pk=placement.pk).update(released_at=None)
        placement.refresh_from_db()
        self.assertIsNone(placement.released_at)

    def test_no_model_carries_a_money_column(self):
        """Non-custodial: bill totals are computed, never stored."""
        for model in (Visit, Bill, OrderPlacement):
            for field in model._meta.concrete_fields:
                with self.subTest(field=f'{model.__name__}.{field.name}'):
                    self.assertNotIsInstance(
                        field, (models.DecimalField, models.FloatField))


# --- what responses show ----------------------------------------------------------

class RestaurantDetailResponseTests(_Fixture):
    def setUp(self):
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.client = APIClient()

    def test_the_owner_detail_read_gains_exactly_one_key(self):
        token = RefreshToken.for_user(self.owner).access_token
        response = self.client.get(
            f'{DETAIL_URL}?record=restaurants&id={self.restaurant.pk}',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200, response.content)
        keys = set(response.json()['data'])
        before = HEAD_SERIALIZER_FIELDS[
            'restaurants_app.serializers.SerializerGetRestaurantDetail']
        self.assertEqual(keys - before, {ADDED_KEY})
        self.assertEqual(before - keys, set())
        self.assertEqual(response.json()['data'][ADDED_KEY], '04:00:00')


class SerializerFieldSetTests(TestCase):
    """Every serializer that reads Restaurant, Table, Order or an order line keeps
    the fields it had at 0588a1d. The new reverse relations (``Table.visits``,
    ``Order.placement``) must not surface through a ``fields='__all__'``."""

    def test_field_sets_are_unchanged_but_for_the_one_key(self):
        import importlib
        for dotted, before in HEAD_SERIALIZER_FIELDS.items():
            with self.subTest(serializer=dotted):
                module, _, name = dotted.rpartition('.')
                fields = set(getattr(importlib.import_module(module), name)().fields)
                expected = set(before) | ({ADDED_KEY} if dotted in GAINS_THE_KEY else set())
                self.assertEqual(fields, expected)


# --- deletion -----------------------------------------------------------------------

class DeletionMirrorsTheExistingGraphTests(TestCase):
    """Deleting a restaurant behaves exactly as it did before this schema existed.

    TODAY a restaurant that has an order cannot be hard-deleted: the cascade
    collects its tables and ``Order.table`` (PROTECT) refuses
    (``orders_app/tests.py`` pins it). With Step 1 rows beside the order the
    outcome must be the same — refused, and nothing deleted.
    """

    def _tenant(self, phone, with_order=True, with_round=False):
        owner = _user(phone)
        restaurant = Restaurant.objects.create(
            name=f'Delete {phone}', owner=owner, status=RestaurantStatus_Live)
        table = Table.objects.create(number=1, restaurant=restaurant)
        order = _order(restaurant, table) if with_order else None
        if with_round:
            _round(order)
        return restaurant, table, order

    def _delete_result(self, restaurant):
        """What deleting it does: the exception class (or None) and every row that
        survives, by table."""
        try:
            with transaction.atomic():
                restaurant.delete()
            raised = None
        except (ProtectedError, RestrictedError) as error:
            raised = type(error)
        survivors = {
            'restaurant': Restaurant.objects.filter(pk=restaurant.pk).count(),
            'table': Table.objects.filter(restaurant_id=restaurant.pk).count(),
            'order': Order.objects.filter(restaurant_id=restaurant.pk).count(),
        }
        return raised, survivors

    def test_a_restaurant_with_an_order_is_refused_with_or_without_step_1_rows(self):
        today, _t, _o = self._tenant('256700430101')
        with_rows, _t2, _o2 = self._tenant('256700430102', with_round=True)

        before = self._delete_result(today)
        after = self._delete_result(with_rows)

        self.assertEqual(before, (ProtectedError,
                                  {'restaurant': 1, 'table': 1, 'order': 1}))
        self.assertEqual(after, before)
        # And the Step 1 rows themselves are untouched by the refused delete.
        self.assertEqual(Visit.objects.filter(restaurant=with_rows).count(), 1)
        self.assertEqual(Bill.objects.filter(visit__restaurant=with_rows).count(), 1)
        self.assertEqual(
            OrderPlacement.objects.filter(order__restaurant=with_rows).count(), 1)

    def test_a_restaurant_without_orders_still_deletes(self):
        restaurant, _t, _o = self._tenant('256700430103', with_order=False)
        self.assertEqual(
            self._delete_result(restaurant),
            (None, {'restaurant': 0, 'table': 0, 'order': 0}),
        )

    def test_the_new_keys_mirror_the_ones_on_order(self):
        """The deletion rules are COPIED from the existing graph, not invented. In
        restaurant deletion ``Visit.restaurant`` cannot be told apart behaviourally
        (``Visit.table`` PROTECT refuses first, as ``Order.table`` does), so the
        mirror itself is what is pinned."""
        def rule(model, name):
            return model._meta.get_field(name).remote_field.on_delete

        self.assertIs(rule(Visit, 'restaurant'), rule(Order, 'restaurant'))
        self.assertIs(rule(Visit, 'table'), rule(Order, 'table'))
        self.assertIs(rule(Visit, 'opened_by'), rule(Order, 'created_by'))
        self.assertIs(rule(OrderPlacement, 'order'), rule(OrderAcceptance, 'order'))
        self.assertIs(rule(Bill, 'visit'), models.CASCADE)
        self.assertIs(rule(OrderPlacement, 'visit'), models.RESTRICT)
        self.assertIs(rule(OrderPlacement, 'bill'), models.RESTRICT)

    def test_deleting_an_order_takes_its_placement_and_keeps_the_visit(self):
        _r, _t, order = self._tenant('256700430104', with_round=True)
        visit = order.placement.visit
        order.delete()
        self.assertFalse(OrderPlacement.objects.filter(visit=visit).exists())
        self.assertTrue(Visit.objects.filter(pk=visit.pk).exists())

    def test_a_visit_or_bill_with_a_round_cannot_be_deleted_on_its_own(self):
        _r, _t, order = self._tenant('256700430105', with_round=True)
        placement = order.placement
        for target in (placement.visit, placement.bill):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(RestrictedError):
                    with transaction.atomic():
                        target.delete()
        self.assertTrue(OrderPlacement.objects.filter(pk=placement.pk).exists())

    def test_a_visit_that_outlives_its_orders_still_holds_its_table(self):
        """A DELIBERATE CONSEQUENCE, recorded so it is not rediscovered.

        ``Visit.table`` is PROTECT, as ``Order.table`` is. So if a restaurant's
        orders are hard-deleted first, a leftover visit still blocks the restaurant
        delete, where without Step 1 rows it would now succeed. No production path
        hard-deletes an order (orders are soft-deleted), so this changes nothing
        that runs today.
        """
        restaurant, _t, _o = self._tenant('256700430106', with_round=True)
        Order.objects.filter(restaurant=restaurant).delete()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                restaurant.delete()


# --- nothing writes these yet --------------------------------------------------------

class NothingWritesTheseYetTests(TestCase):
    """PR 1 is the schema alone. The writer arrives behind
    ``DINING_VISIT_WRITES_ENABLED`` in its own pull request, which updates this
    inventory deliberately."""

    NAMES = {'Visit', 'Bill', 'OrderPlacement'}
    ALLOWED = {Path('orders_app/models.py')}

    def test_no_production_module_references_the_new_models(self):
        root = Path(settings.BASE_DIR)
        offenders = []
        for path in root.rglob('*.py'):
            relative = path.relative_to(root)
            parts = relative.parts
            if parts[0] in {'venv', '.venv', 'node_modules'}:
                continue
            if 'migrations' in parts or any(p.startswith('test') for p in parts):
                continue
            if relative in self.ALLOWED:
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                name = (
                    node.id if isinstance(node, ast.Name)
                    else node.attr if isinstance(node, ast.Attribute)
                    else None
                )
                if name in self.NAMES:
                    offenders.append(f'{relative}:{node.lineno} {name}')
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in self.NAMES:
                            offenders.append(f'{relative}:{node.lineno} import {alias.name}')
        self.assertEqual(offenders, [])
