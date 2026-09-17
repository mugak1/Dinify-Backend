"""
D04 — the checkout intent: what one deliberate attempt to buy is, and what a
server may answer with an order that already exists.

WHAT THESE TESTS ARE ABOUT. The idempotency key used to identify an ATTEMPT and
be bound to NOTHING. `_create_order` selected an existing order by
`(restaurant, key)` and returned it, so a request naming three burgers received
the one-burger order that key had been used for, and a request naming a
DIFFERENT TABLE received the first table's order — both at HTTP 200, with no
conflict reported and no way for the diner to notice. Every test in the
`IntentBindingTests` class below FAILS on that code.

THE SPLIT WITH `tests_order_intent_concurrency.py`. This file is single
threaded: it pins the RULE — what counts as the same purchase, and what each
non-matching outcome answers — plus the rollback boundary, using deterministic
seams on one connection. The races that need two committed transactions (a
winner committing while the loser waits on the table lock, a winner whose
lifecycle or menu context changed in between) are proved there, against
independent PostgreSQL connections.

ONE THING THESE TESTS DO NOT CLAIM. Equal fingerprints under DIFFERENT keys are
not the same intent and are never deduplicated: two diners deliberately ordering
the same meal are two purchases. The fingerprint answers "is this the same
purchase?" only once the key has already said "is this the same attempt?".
"""
import uuid
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.orders import serializers as serializers_module
from orders_app.controllers.services import create_order as create_order_module
from orders_app.controllers.services.checkout_protocol import (
    CHECKOUT_PROTOCOL, CHECKOUT_PROTOCOL_BINDING,
    CHECKOUT_PROTOCOL_RECOVERABLE,
)
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED,
)
from orders_app.controllers.services.create_order import (
    _create_order, allocate_daily_order_number,
)
from orders_app.controllers.services.order_input import (
    MAX_QUANTITY_PER_LINE, validate_order_items,
)
from orders_app.controllers.services.order_eligibility import (
    REASON_RESTAURANT_PAUSED,
)
from orders_app.controllers.services.order_intent import (
    ABSENT, FINGERPRINT_VERSION, MATCH, MISMATCH, OUT_OF_SCOPE, UNSUPPORTED,
    REASON_INTENT_BINDING_UNAVAILABLE, REASON_INTENT_MISMATCH,
    REASON_INTENT_UNUSABLE,
    fingerprint, is_supported, resolve_intent,
)
from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


def _validated(items):
    """The D01-validated lines — the ONLY legitimate input to `fingerprint`."""
    result = validate_order_items(items)
    assert result.get('status') == 200, result
    return result['items']


def _fp(items):
    return fingerprint(_validated(items))


class IntentFixture(TestCase):
    """One restaurant, two orderable tables, two dishes and one extra."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Int', last_name='Owner', email='int_owner@test.com',
            phone_number='256700000901', username='256700000901',
            country='Uganda', password='password', roles=[],
        )
        self.staff = User.objects.create_user(
            first_name='Int', last_name='Staff', email='int_staff@test.com',
            phone_number='256700000902', username='256700000902',
            country='Uganda', password='password', roles=[],
        )
        self.diner = User.objects.create_user(
            first_name='Int', last_name='Diner', email='int_diner@test.com',
            phone_number='256700000903', username='256700000903',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Intent R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live,
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
        self.table_b = Table.objects.create(
            number=2, restaurant=self.restaurant, dining_area=self.area,
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
        self.other_item = MenuItem.objects.create(
            name='Katogo', section=self.section, primary_price=Decimal('7000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )

    # -- shorthand ---------------------------------------------------------

    def _line(self, item=None, quantity=1, **extra):
        line = {'item': str((item or self.item).pk), 'quantity': quantity}
        line.update(extra)
        return line

    def _create(self, items=None, table=None, key=None, **kwargs):
        return _create_order(
            restaurant=self.restaurant,
            table=table or self.table,
            items=items if items is not None else [self._line()],
            client_order_id=key,
            **kwargs,
        )

    def _orders(self, **filters):
        return Order.objects.filter(restaurant=self.restaurant, **filters)


# ---------------------------------------------------------------------------
# A. WHAT MAKES TWO REQUESTS THE SAME PURCHASE
# ---------------------------------------------------------------------------

class CanonicalPurchaseTests(TestCase):
    """The fingerprint, exercised as a pure function over validated lines.

    No database: the canonical purchase is deliberately independent of the
    catalogue, which is the whole reason it is taken from the D01-validated
    request rather than from the priced rows.
    """

    ITEM_A = '11111111-1111-4111-8111-111111111111'
    ITEM_B = '22222222-2222-4222-8222-222222222222'
    EXTRA_A = '33333333-3333-4333-8333-333333333333'
    EXTRA_B = '44444444-4444-4444-8444-444444444444'

    def test_quantity_three_equals_one_plus_two(self):
        """One line of 3 and two lines of 1 and 2 are ONE purchase.

        The server merges identical configurations into a single stored row, so
        both spellings produce the same order. Treating them as different
        intents would hand a spurious conflict to any client that tidied its
        basket between attempts.
        """
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 3}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1},
                 {'item': self.ITEM_A, 'quantity': 2}]),
        )

    def test_coalescing_happens_after_the_raw_ceilings_not_before(self):
        """The D01 ceilings count RAW entries; only then does this coalesce.

        A raw per-line quantity of 100 is refused whether or not an equivalent
        99 + 1 spelling would be accepted. If coalescing ran first the forbidden
        request would enter through the equivalent spelling.
        """
        over = validate_order_items(
            [{'item': self.ITEM_A, 'quantity': MAX_QUANTITY_PER_LINE + 1}])
        self.assertEqual(over.get('status'), 400)

        split = validate_order_items(
            [{'item': self.ITEM_A, 'quantity': MAX_QUANTITY_PER_LINE},
             {'item': self.ITEM_A, 'quantity': 1}])
        self.assertEqual(split.get('status'), 200)
        # ...and the accepted spelling really does coalesce past the ceiling.
        self.assertEqual(
            fingerprint(split['items']),
            fingerprint([{'item': self.ITEM_A,
                          'quantity': MAX_QUANTITY_PER_LINE + 1}]),
        )

    def test_parent_line_order_does_not_distinguish_a_purchase(self):
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1},
                 {'item': self.ITEM_B, 'quantity': 2}]),
            _fp([{'item': self.ITEM_B, 'quantity': 2},
                 {'item': self.ITEM_A, 'quantity': 1}]),
        )

    def test_the_order_choices_were_tapped_in_does_not_distinguish(self):
        """"Cheese then Bacon" and "Bacon then Cheese" are one purchase."""
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g1': ['cheese', 'bacon']}}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g1': ['bacon', 'cheese']}}]),
        )

    def test_group_order_does_not_distinguish(self):
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g1': ['c1'], 'g2': ['c2']}}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g2': ['c2'], 'g1': ['c1']}}]),
        )

    def test_a_repeated_modifier_choice_is_not_a_second_selection(self):
        """Matching the canonicalisation the server itself performs."""
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g1': ['c1', 'c1']}}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'selected_modifiers': {'g1': ['c1']}}]),
        )

    def test_extras_are_sorted_but_never_de_duplicated(self):
        """The asymmetry with modifier choices is deliberate.

        A duplicate extra is REFUSED upstream rather than collapsed, so
        collapsing one here would turn a request the server rejects into a
        valid replay of a request it accepted.
        """
        self.assertEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'extras': [self.EXTRA_A, self.EXTRA_B]}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'extras': [self.EXTRA_B, self.EXTRA_A]}]),
        )
        self.assertNotEqual(
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'extras': [self.EXTRA_A, self.EXTRA_A]}]),
            _fp([{'item': self.ITEM_A, 'quantity': 1,
                  'extras': [self.EXTRA_A]}]),
        )

    def test_modifier_ids_are_opaque(self):
        """Never case-folded, trimmed or parsed as UUIDs.

        The `options` column is unvalidated and this repository's own fixtures
        use `'g-req'` / `'c1'`. Two ids differing only in case are two ids to
        the catalogue, and they must stay two here.
        """
        base = {'item': self.ITEM_A, 'quantity': 1,
                'selected_modifiers': {'g1': ['c1']}}
        for variant in ({'G1': ['c1']}, {'g1': ['C1']}, {' g1': ['c1']},
                        {'g1': ['c1 ']}):
            self.assertNotEqual(
                _fp([base]),
                _fp([{'item': self.ITEM_A, 'quantity': 1,
                      'selected_modifiers': variant}]),
                variant,
            )

    def test_absent_null_and_empty_selections_are_one_purchase(self):
        """They mean the same thing to the order path, so they mean the same
        thing here — a client that sends `extras: []` has not ordered extras."""
        plain = _fp([{'item': self.ITEM_A, 'quantity': 1}])
        for spelling in (
            {'item': self.ITEM_A, 'quantity': 1, 'extras': []},
            {'item': self.ITEM_A, 'quantity': 1, 'selected_modifiers': {}},
            {'item': self.ITEM_A, 'quantity': 1, 'selected_modifiers': None,
             'extras': None},
        ):
            self.assertEqual(plain, _fp([spelling]), spelling)

    def test_a_different_purchase_is_a_different_fingerprint(self):
        base = [{'item': self.ITEM_A, 'quantity': 1}]
        for other in (
            [{'item': self.ITEM_B, 'quantity': 1}],
            [{'item': self.ITEM_A, 'quantity': 2}],
            [{'item': self.ITEM_A, 'quantity': 1},
             {'item': self.ITEM_B, 'quantity': 1}],
            [{'item': self.ITEM_A, 'quantity': 1, 'extras': [self.EXTRA_A]}],
            [{'item': self.ITEM_A, 'quantity': 1,
              'selected_modifiers': {'g1': ['c1']}}],
        ):
            self.assertNotEqual(_fp(base), _fp(other), other)

    def test_the_encoding_carries_its_version(self):
        """A bare digest would be silently reinterpreted the day the encoding
        changes; a separate column would be a second thing to keep in step."""
        value = _fp([{'item': self.ITEM_A, 'quantity': 1}])
        self.assertTrue(value.startswith(f'{FINGERPRINT_VERSION}:'), value)
        self.assertTrue(is_supported(value))

    def test_only_this_encoding_is_comparable(self):
        self.assertFalse(is_supported(None))
        self.assertFalse(is_supported(''))
        self.assertFalse(is_supported('v2:deadbeef'))
        self.assertFalse(is_supported('deadbeef'))
        self.assertFalse(is_supported(12345))


class TheFingerprintIgnoresTheCatalogueTests(IntentFixture):
    """A menu edit between two attempts must not change the purchase.

    This is why the value is taken from the D01-validated request and BEFORE
    `normalize_order_items`, which reorders choices into menu-definition order
    against the live catalogue. A diner recovering a lost response must not be
    told their purchase is different because the restaurant edited a dish.
    """

    def test_a_price_change_between_attempts_is_still_the_same_purchase(self):
        key = uuid.uuid4()
        first = self._create(key=key)
        self.assertEqual(first.get('status'), 200, first)

        MenuItem.objects.filter(pk=self.item.pk).update(
            primary_price=Decimal('99000'))

        retry = self._create(key=key)
        self.assertEqual(retry.get('status'), 200, retry)
        self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['order'].id, first['order'].id)

    def test_a_renamed_dish_between_attempts_is_still_the_same_purchase(self):
        key = uuid.uuid4()
        first = self._create(key=key)
        self.assertEqual(first.get('status'), 200, first)

        MenuItem.objects.filter(pk=self.item.pk).update(name='Rolex Special')

        retry = self._create(key=key)
        self.assertEqual(retry.get('status'), 200, retry)
        self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['order'].id, first['order'].id)


# ---------------------------------------------------------------------------
# B. THE BINDING POLICY — one answer per outcome, and nothing mutated
# ---------------------------------------------------------------------------

class IntentBindingTests(IntentFixture):
    """Every test here fails on the pre-D04 `(restaurant, key)` lookup."""

    def test_an_identical_retry_returns_the_same_order(self):
        key = uuid.uuid4()
        first = self._create(key=key)
        self.assertEqual(first.get('status'), 200, first)

        retry = self._create(key=key)
        self.assertEqual(retry.get('status'), 200, retry)
        self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['order'].id, first['order'].id)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_a_successful_replay_allocates_no_new_order_number(self):
        """A replay produces NO new creation effect, and the daily counter is
        a creation effect. Two numbers for one order is what the shared
        rollback boundary at steps 3+4 exists to prevent."""
        key = uuid.uuid4()
        first = self._create(key=key)
        self.assertEqual(first.get('status'), 200, first)
        number = first['order'].order_number
        counter = RestaurantDailyOrderCounter.objects.get(
            restaurant=self.restaurant)
        after_first = counter.next_number

        self._create(key=key)

        counter.refresh_from_db()
        self.assertEqual(counter.next_number, after_first)
        first['order'].refresh_from_db()
        self.assertEqual(first['order'].order_number, number)

    def test_the_same_key_for_a_different_purchase_is_refused(self):
        """The headline defect: three burgers used to receive the one-burger
        order that key had been used for, at HTTP 200."""
        key = uuid.uuid4()
        first = self._create(items=[self._line(quantity=1)], key=key)
        self.assertEqual(first.get('status'), 200, first)

        conflict = self._create(items=[self._line(quantity=3)], key=key)
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertEqual(conflict.get('reason'), REASON_INTENT_MISMATCH)
        self.assertNotIn('order', conflict)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_a_conflict_is_409_and_never_the_table_occupied_shape(self):
        """`data.order_id` on this endpoint is the established table-occupied
        signal; carrying it here would send a client down an unrelated
        recovery."""
        key = uuid.uuid4()
        self._create(items=[self._line(quantity=1)], key=key)
        conflict = self._create(items=[self._line(quantity=2)], key=key)

        self.assertEqual(conflict.get('status'), 409)
        self.assertNotIn('data', conflict)
        self.assertIn('message', conflict)

    def test_the_same_key_at_another_table_is_refused_opaquely(self):
        """The second defect: a request naming a DIFFERENT TABLE used to
        receive the first table's order.

        The refusal says the key cannot be used. It does not say another table
        holds it, which table, or anything about that order — the caller has no
        relationship with it.
        """
        key = uuid.uuid4()
        first = self._create(key=key, table=self.table)
        self.assertEqual(first.get('status'), 200, first)

        elsewhere = self._create(key=key, table=self.table_b)
        self.assertEqual(elsewhere.get('status'), 409, elsewhere)
        self.assertEqual(elsewhere.get('reason'), REASON_INTENT_UNUSABLE)
        self.assertNotIn('order', elsewhere)
        self.assertNotIn('data', elsewhere)
        body = str(elsewhere)
        self.assertNotIn(str(first['order'].id), body)
        self.assertNotIn(str(self.table.pk), body)
        # and no replacement order was created for the taken key
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_the_scope_check_runs_before_the_fingerprint(self):
        """A caller at another table gets no diagnostic about a purchase that
        is not theirs, even when the purchase differs too."""
        key = uuid.uuid4()
        self._create(items=[self._line(quantity=1)], key=key, table=self.table)
        elsewhere = self._create(
            items=[self._line(quantity=7)], key=key, table=self.table_b)
        self.assertEqual(elsewhere.get('reason'), REASON_INTENT_UNUSABLE)

    def test_a_different_creator_is_a_different_command(self):
        """Staff-versus-diner is part of the command, not decoration: the order
        was written with that attribution and nothing here may rewrite it."""
        key = uuid.uuid4()
        first = self._create(key=key)          # diner: created_by is None
        self.assertEqual(first.get('status'), 200, first)

        as_staff = self._create(key=key, created_by=self.staff)
        self.assertEqual(as_staff.get('status'), 409, as_staff)
        self.assertEqual(as_staff.get('reason'), REASON_INTENT_MISMATCH)
        first['order'].refresh_from_db()
        self.assertIsNone(first['order'].created_by_id)

    def test_a_different_attributed_customer_is_a_different_command(self):
        key = uuid.uuid4()
        first = self._create(key=key, customer=self.diner)
        self.assertEqual(first.get('status'), 200, first)

        anonymous = self._create(key=key)
        self.assertEqual(anonymous.get('status'), 409, anonymous)
        self.assertEqual(anonymous.get('reason'), REASON_INTENT_MISMATCH)
        first['order'].refresh_from_db()
        self.assertEqual(first['order'].customer_id, self.diner.pk)

    def test_a_pre_d04_order_cannot_be_matched_or_dismissed(self):
        """NULL means "this order predates D04" and nothing else.

        Equivalence is not decidable, which is a distinct outcome with its own
        honest answer: returning it would hand back a purchase nobody matched,
        and creating a replacement would ignore an occupied key.
        """
        key = uuid.uuid4()
        first = self._create(key=key)
        self.assertEqual(first.get('status'), 200, first)
        Order.objects.filter(pk=first['order'].pk).update(
            request_fingerprint=None)

        retry = self._create(key=key)
        self.assertEqual(retry.get('status'), 409, retry)
        self.assertEqual(retry.get('reason'),
                         REASON_INTENT_BINDING_UNAVAILABLE)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_an_unreadable_encoding_cannot_be_matched_or_dismissed(self):
        key = uuid.uuid4()
        first = self._create(key=key)
        Order.objects.filter(pk=first['order'].pk).update(
            request_fingerprint='v2:' + 'a' * 64)

        retry = self._create(key=key)
        self.assertEqual(retry.get('status'), 409, retry)
        self.assertEqual(retry.get('reason'),
                         REASON_INTENT_BINDING_UNAVAILABLE)

    def test_a_refusal_mutates_nothing(self):
        key = uuid.uuid4()
        first = self._create(items=[self._line(quantity=1)], key=key)
        order = first['order']
        before = Order.objects.filter(pk=order.pk).values().get()
        lines_before = OrderItem.objects.filter(order=order).count()
        counter_before = RestaurantDailyOrderCounter.objects.get(
            restaurant=self.restaurant).next_number

        conflict = self._create(items=[self._line(quantity=4)], key=key)
        self.assertEqual(conflict.get('status'), 409)

        self.assertEqual(Order.objects.filter(pk=order.pk).values().get(),
                         before)
        self.assertEqual(OrderItem.objects.filter(order=order).count(),
                         lines_before)
        self.assertEqual(
            RestaurantDailyOrderCounter.objects.get(
                restaurant=self.restaurant).next_number,
            counter_before,
        )
        self.assertEqual(self._orders().count(), 1)

    def test_a_new_order_records_the_binding_in_the_same_statement(self):
        """A row with a key and no binding would be indistinguishable from a
        pre-D04 row and would fall into the undecidable branch forever."""
        key = uuid.uuid4()
        items = [self._line(quantity=2)]
        result = self._create(items=items, key=key)
        self.assertEqual(result.get('status'), 200, result)
        order = result['order']
        self.assertEqual(order.request_fingerprint, _fp(items))
        self.assertTrue(is_supported(order.request_fingerprint))


class KeylessCallersKeepTheirWeakerGuaranteesTests(IntentFixture):
    """A request with no key is not an intent and never a replay.

    Keyless callers are RETAINED deliberately — several in-process ones exist —
    but they get exactly the protection they always had: the table lock and the
    occupancy gate, and no recovery.
    """

    def test_two_keyless_requests_at_different_tables_are_two_orders(self):
        first = self._create(table=self.table)
        second = self._create(table=self.table_b)
        self.assertEqual(first.get('status'), 200, first)
        self.assertEqual(second.get('status'), 200, second)
        self.assertNotEqual(first['order'].id, second['order'].id)

    def test_a_keyless_repeat_at_one_table_hits_the_ordinary_gate(self):
        """The ordinary table-occupied 400, not an intent refusal — the two
        answers mean different things and must not be conflated."""
        first = self._create()
        self.assertEqual(first.get('status'), 200, first)
        # an `initiated` draft does not occupy its table (PR #210); the table
        # is claimed at submit, so this is what the gate actually sees.
        Order.objects.filter(pk=first['order'].pk).update(
            order_status='pending')

        again = self._create()
        self.assertEqual(again.get('status'), 400, again)
        self.assertEqual(str(again.get('data', {}).get('order_id')),
                         str(first['order'].id))

    def test_the_resolver_reports_absent_for_a_keyless_request(self):
        self.assertEqual(
            resolve_intent(
                restaurant_id=self.restaurant.pk, client_order_id=None,
                table_id=self.table.pk, request_fingerprint=_fp([self._line()]),
                created_by_id=None, customer_id=None,
            ).outcome,
            ABSENT,
        )


class TheResolverIsOneSharedPolicyTests(IntentFixture):
    """Four sites ask it — the controller preflight, the service lookup, the
    post-wait recheck and the unique-conflict recovery — and they used to
    interpret the key independently."""

    def _resolve(self, **overrides):
        kwargs = dict(
            restaurant_id=self.restaurant.pk,
            client_order_id=self.key,
            table_id=self.table.pk,
            request_fingerprint=self.fingerprint,
            created_by_id=None,
            customer_id=None,
        )
        kwargs.update(overrides)
        return resolve_intent(**kwargs)

    def setUp(self):
        super().setUp()
        self.key = uuid.uuid4()
        self.items = [self._line(quantity=2)]
        self.fingerprint = _fp(self.items)
        created = self._create(items=self.items, key=self.key)
        self.assertEqual(created.get('status'), 200, created)
        self.order = created['order']

    def test_match(self):
        verdict = self._resolve()
        self.assertEqual(verdict.outcome, MATCH)
        self.assertTrue(verdict.is_match)
        self.assertEqual(verdict.order.id, self.order.id)

    def test_absent_for_an_unused_key(self):
        self.assertEqual(self._resolve(client_order_id=uuid.uuid4()).outcome,
                         ABSENT)

    def test_absent_in_another_restaurant(self):
        """The namespace is per restaurant, so the same key elsewhere is free."""
        other = Restaurant.objects.create(
            name='Other R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        self.assertEqual(self._resolve(restaurant_id=other.pk).outcome, ABSENT)

    def test_mismatch(self):
        self.assertEqual(
            self._resolve(request_fingerprint=_fp([self._line(quantity=5)])
                          ).outcome,
            MISMATCH,
        )

    def test_out_of_scope(self):
        self.assertEqual(self._resolve(table_id=self.table_b.pk).outcome,
                         OUT_OF_SCOPE)

    def test_unsupported(self):
        Order.objects.filter(pk=self.order.pk).update(request_fingerprint=None)
        self.assertEqual(self._resolve().outcome, UNSUPPORTED)

    def test_the_lookup_is_restaurant_wide_not_table_scoped(self):
        """Narrowing the lookup to the table would make an occupied key look
        FREE and drive a second INSERT into the restaurant-wide unique
        constraint. The table is compared AFTER the row is found."""
        verdict = self._resolve(table_id=self.table_b.pk)
        self.assertEqual(verdict.outcome, OUT_OF_SCOPE)
        self.assertIsNotNone(verdict.order)

    def test_uuid_objects_and_their_string_spellings_agree(self):
        self.assertEqual(self._resolve(client_order_id=str(self.key)).outcome,
                         MATCH)
        self.assertEqual(self._resolve(table_id=str(self.table.pk)).outcome,
                         MATCH)


# ---------------------------------------------------------------------------
# C. A MATCHED REPLAY IS NOT NEW WORK
# ---------------------------------------------------------------------------

class AReplaySkipsTheNewOrderGatesTests(IntentFixture):
    """Each of those gates answers "may a NEW order be created here, now?".

    An order that already exists is not created again, and refusing to hand it
    back because a new one would now be disallowed is a lifecycle or menu
    change retroactively hiding a diner's own draft. Authority is NOT in that
    block and is not skipped — for a diner it is the table session, resolved at
    the endpoint before any of this runs.
    """

    def _initiate(self, key, items=None):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=items if items is not None else [self._line()],
            client_order_id=str(key),
        )

    def test_a_replay_survives_the_restaurant_pausing_orders(self):
        key = uuid.uuid4()
        first = self._initiate(key)
        self.assertEqual(first.get('status'), 200, first)

        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        retry = self._initiate(key)
        self.assertEqual(retry.get('status'), 200, retry)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_a_replay_survives_the_dish_leaving_the_menu(self):
        key = uuid.uuid4()
        first = self._initiate(key)
        self.assertEqual(first.get('status'), 200, first)

        MenuItem.objects.filter(pk=self.item.pk).update(enabled=False)

        retry = self._initiate(key)
        self.assertEqual(retry.get('status'), 200, retry)

    def test_a_conflicting_key_is_refused_before_those_gates_run(self):
        """The refusal is about the key, not about the paused restaurant — a
        409 rather than the availability 400."""
        key = uuid.uuid4()
        self._initiate(key, items=[self._line(quantity=1)])
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        conflict = self._initiate(key, items=[self._line(quantity=6)])
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertEqual(conflict.get('reason'), REASON_INTENT_MISMATCH)

    def test_a_new_order_is_still_refused_at_a_paused_restaurant(self):
        """The negative control: skipping the gates for a REPLAY must not
        weaken them for a genuinely new request.

        It used to assert `reason` was ABSENT, as a proxy for "this is the
        availability refusal, not a D04 intent conflict". D06 gives every
        refusal on this path a machine code, so the discriminator is now stated
        POSITIVELY — which is what the assertion was reaching for anyway, and is
        a stronger claim: it pins WHICH refusal answered, not merely that the
        answer came from somewhere other than the intent policy.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        refused = self._initiate(uuid.uuid4())
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(refused.get('reason'), REASON_RESTAURANT_PAUSED)
        self.assertNotEqual(refused.get('reason'), REASON_INTENT_MISMATCH)


# ---------------------------------------------------------------------------
# D. THE ROLLBACK BOUNDARY — a losing request leaves no creation effect
# ---------------------------------------------------------------------------

class _Loser:
    """Inject a committed-looking winner at a seam INSIDE the transaction but
    OUTSIDE the rollback boundary that wraps the daily number and the INSERT.

    `normalize_order_items` is that seam: it runs after the step-1 lookup and
    after the post-wait recheck, and before steps 3+4. A winner created at the
    ALLOCATION seam (where this used to be injected) would be unwound along
    with the loser, and the recovery would find nothing — which is precisely
    the coupling the shared boundary introduced.
    """

    def __init__(self, test, key, items, table=None, fingerprint_value=None):
        self.test = test
        self.key = key
        self.items = items
        self.table = table
        self.fingerprint_value = fingerprint_value
        self.winner = None

    def __enter__(self):
        real = ConOrder.normalize_order_items

        def _inject(restaurant, order_items, **kwargs):
            if self.winner is None:
                self.winner = self.test._winner_row(
                    key=self.key,
                    table=self.table or self.test.table,
                    fingerprint_value=(
                        self.fingerprint_value
                        if self.fingerprint_value is not None
                        else _fp(self.items)
                    ),
                )
            return real(restaurant, order_items, **kwargs)

        self._patch = mock.patch.object(
            ConOrder, 'normalize_order_items', side_effect=_inject)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class CreationEffectsUnwindTogetherTests(IntentFixture):
    """The daily number and the INSERT share ONE rollback boundary.

    The allocation used to sit OUTSIDE the savepoint wrapping the INSERT, so a
    losing request rolled the failed INSERT back and COMMITTED the number it
    had already taken: one order, two numbers consumed. A successful replay
    must produce NO new creation effect, and the counter is a creation effect.
    """

    def _winner_row(self, key, table, fingerprint_value):
        """A racing request's committed order. `order_number` stays NULL so
        ONLY the intent-key constraint can trip."""
        return Order.objects.create(
            restaurant=self.restaurant, table=table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status='initiated', payment_status='pending',
            fulfilment_status='new',
            client_order_id=key, request_fingerprint=fingerprint_value,
            order_number=None,
        )

    def test_a_losing_insert_leaves_no_counter_row_behind(self):
        """The absent-counter case: the loser's `get_or_create` is inside the
        boundary too, so the row it would have created is unwound with it."""
        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant).exists())
        key = uuid.uuid4()
        items = [self._line()]

        with _Loser(self, key, items) as race:
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['order'].id, race.winner.id)
        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant).exists())
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_a_losing_insert_does_not_consume_an_existing_days_number(self):
        """The existing-counter case, which is the ordinary one after the
        first order of the day."""
        earlier = self._create()
        self.assertEqual(earlier.get('status'), 200, earlier)
        counter = RestaurantDailyOrderCounter.objects.get(
            restaurant=self.restaurant)
        before = counter.next_number

        key = uuid.uuid4()
        items = [self._line()]
        with _Loser(self, key, items) as race:
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['order'].id, race.winner.id)
        counter.refresh_from_db()
        self.assertEqual(counter.next_number, before)

    def test_a_losing_insert_against_a_different_purchase_is_refused(self):
        """The recovery applies the SAME policy as every other site: a winner
        that is a different purchase is a conflict, never adopted."""
        key = uuid.uuid4()
        items = [self._line(quantity=1)]
        other_fingerprint = _fp([self._line(quantity=9)])

        with _Loser(self, key, items,
                    fingerprint_value=other_fingerprint) as race:
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 409, result)
        self.assertEqual(result.get('reason'), REASON_INTENT_MISMATCH)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)
        self.assertEqual(self._orders().get().id, race.winner.id)

    def test_a_losing_insert_against_another_table_is_refused_opaquely(self):
        key = uuid.uuid4()
        items = [self._line()]

        with _Loser(self, key, items, table=self.table_b):
            result = self._create(items=items, key=key, table=self.table)

        self.assertEqual(result.get('status'), 409, result)
        self.assertEqual(result.get('reason'), REASON_INTENT_UNUSABLE)

    def test_an_unrelated_integrity_error_is_not_relabelled_a_replay(self):
        """The dangerous variant: a row for this key EXISTS, so the recovery
        path is reachable — and must still re-raise rather than hand back an
        order the failure had nothing to do with."""
        key = uuid.uuid4()
        items = [self._line()]

        with _Loser(self, key, items):
            with mock.patch.object(
                create_order_module, '_insert_order',
                side_effect=IntegrityError('some other constraint'),
            ):
                with self.assertRaises(IntegrityError):
                    self._create(items=items, key=key)

    def test_a_failure_after_the_first_line_unwinds_the_whole_order(self):
        """§ the order, its first written line, and the daily number all go.

        The rejection is raised on the SECOND line, so the first has genuinely
        been written by the time the transaction unwinds — a guard assertion
        records that, because a test that never reached the first write would
        prove nothing.
        """
        counter_before = RestaurantDailyOrderCounter.objects.filter(
            restaurant=self.restaurant).count()
        key = uuid.uuid4()
        items = [self._line(quantity=1),
                 self._line(item=self.other_item, quantity=1)]

        real_add = ConOrder.add_order_item
        seen = {'lines': 0, 'rows_at_second_call': None}

        def _fail_second(**kwargs):
            seen['lines'] += 1
            if seen['lines'] == 1:
                return real_add(**kwargs)
            seen['rows_at_second_call'] = OrderItem.objects.filter(
                order__restaurant=self.restaurant).count()
            return {'status': 400, 'message': 'rejected for the test'}

        with mock.patch.object(ConOrder, 'add_order_item',
                               side_effect=_fail_second):
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 400, result)
        # the guard: the first line really was written before the unwind
        self.assertEqual(seen['rows_at_second_call'], 1)
        self.assertEqual(self._orders().count(), 0)
        self.assertEqual(
            OrderItem.objects.filter(order__restaurant=self.restaurant).count(),
            0,
        )
        self.assertEqual(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant).count(),
            counter_before,
        )


class ThePostWaitRecheckIsWiredTests(IntentFixture):
    """A competing request may commit while this one waits on the TABLE lock,
    so the step-1 lookup can be stale by the time the lock is granted.

    This asserts the recheck EXISTS and short-circuits: the winner is injected
    between the first lookup and the table lock, and the loser must return it
    without doing any new-order work at all — no admission refusal, no
    occupancy rejection, no daily number, no INSERT. The genuine
    cross-connection race is proved in `tests_order_intent_concurrency.py`;
    this one is deterministic and runs everywhere.
    """

    def _winner_row(self, key, table, fingerprint_value):
        return Order.objects.create(
            restaurant=self.restaurant, table=table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status='initiated', payment_status='pending',
            fulfilment_status='new',
            client_order_id=key, request_fingerprint=fingerprint_value,
            order_number=None,
        )

    def _race_at_admission(self, key, items, **winner):
        """Inject at `admit` — between the step-1 lookup and the recheck."""
        real_admit = create_order_module.admit
        box = {}

        def _inject(**kwargs):
            verdict = real_admit(**kwargs)
            if 'winner' not in box:
                box['winner'] = self._winner_row(
                    key=key,
                    table=winner.get('table', self.table),
                    fingerprint_value=winner.get(
                        'fingerprint_value', _fp(items)),
                )
            return verdict

        return mock.patch.object(
            create_order_module, 'admit', side_effect=_inject), box

    def test_a_winner_that_appears_during_the_wait_is_returned(self):
        key = uuid.uuid4()
        items = [self._line()]
        patch, box = self._race_at_admission(key, items)
        with patch, mock.patch.object(
            ConOrder, 'any_present_ongoing_order',
            side_effect=AssertionError('occupancy gate must not be reached'),
        ):
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['order'].id, box['winner'].id)
        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant).exists())

    def test_the_binding_is_re_validated_not_assumed(self):
        """Same key, different contents conflicts here rather than adopting
        whatever happens to exist."""
        key = uuid.uuid4()
        items = [self._line(quantity=1)]
        patch, _box = self._race_at_admission(
            key, items, fingerprint_value=_fp([self._line(quantity=8)]))
        with patch:
            result = self._create(items=items, key=key)

        self.assertEqual(result.get('status'), 409, result)
        self.assertEqual(result.get('reason'), REASON_INTENT_MISMATCH)
        self.assertEqual(self._orders(client_order_id=key).count(), 1)

    def test_the_recheck_is_skipped_for_a_keyless_request(self):
        """There is nothing to re-resolve, and asking would cost a query per
        order for every caller that never opted into recovery."""
        with mock.patch.object(
            create_order_module, 'resolve_intent',
            wraps=create_order_module.resolve_intent,
        ) as resolver:
            result = self._create()
        self.assertEqual(result.get('status'), 200, result)
        # exactly one call (the step-1 lookup, which short-circuits on a
        # None key) and none from the recheck
        self.assertEqual(resolver.call_count, 1)


class TheServerStatesWhatItCanPromiseTests(IntentFixture):
    """The capability level, on the response a checkout client actually reads.

    A client that wants to retry an uncertain mutation safely has to know
    whether the server it is talking to can answer that retry safely, and
    every value already on the wire answers a different question:
    `pricing_version` describes how the MONEY was calculated, a present total
    says a field exists, a fulfilment state says what the kitchen is doing.
    #661 is the standing lesson about collapsing two contract introductions
    into one flag — it cost every checkout for the width of a deploy.
    """

    def _details(self, response):
        return response['data']['order_details']

    def test_the_initiate_response_states_the_level(self):
        result = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=[self._line()],
            client_order_id=str(uuid.uuid4()),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(self._details(result)['checkout_protocol'],
                         CHECKOUT_PROTOCOL)

    def test_a_replay_states_the_same_level(self):
        key = str(uuid.uuid4())
        first = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk), items=[self._line()],
            client_order_id=key,
        )
        retry = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk), items=[self._line()],
            client_order_id=key,
        )
        self.assertEqual(self._details(retry)['checkout_protocol'],
                         self._details(first)['checkout_protocol'])

    def test_the_level_is_only_as_high_as_what_is_built(self):
        """A level is raised ONLY by the change that makes it true.

        D04/B shipped level 1 and D04/C raised it to 2, and 2 promises TWO
        things: durable acceptance evidence, and a scoped read that resolves
        an intent key. Asserting the number alone would let a future edit
        raise it past what exists — which is the #661 mistake in a new place,
        a client told it may recover an acceptance it has no way to look up.
        So the promise is checked rather than the integer.
        """
        self.assertGreaterEqual(CHECKOUT_PROTOCOL, CHECKOUT_PROTOCOL_BINDING)

        if CHECKOUT_PROTOCOL >= CHECKOUT_PROTOCOL_RECOVERABLE:
            from orders_app.models import OrderAcceptance
            from restaurants_app.controllers.handle_diner_journey import (
                handle_show_order_details,
            )
            import inspect
            # durable acceptance evidence exists, and survives an Order save
            self.assertTrue(hasattr(OrderAcceptance, 'accepted_at'))
            self.assertTrue(hasattr(OrderAcceptance, 'quote_ref'))
            # ...and the read can be resolved by an intent key
            self.assertIn(
                "'intent'", inspect.getsource(handle_show_order_details))

    def test_the_level_is_not_derived_from_the_pricing_version(self):
        """Two keys, two independent sources — a server can price correctly
        and still be unable to bind a key.

        They happen to share the integer 1 today, so asserting they DIFFER
        would prove nothing and would break on the next bump of either. What
        is asserted instead is independence: move one and the other stays
        exactly where it was.
        """
        with mock.patch.object(
            serializers_module, 'CHECKOUT_PROTOCOL',
            CHECKOUT_PROTOCOL_RECOVERABLE,
        ):
            result = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant.pk),
                table_id=str(self.table.pk), items=[self._line()],
                client_order_id=str(uuid.uuid4()),
            )
        details = self._details(result)
        self.assertEqual(details['checkout_protocol'],
                         CHECKOUT_PROTOCOL_RECOVERABLE)
        self.assertEqual(details['pricing_version'],
                         PRICING_VERSION_CORRECTED)
