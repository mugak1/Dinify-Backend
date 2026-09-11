from uuid import uuid4
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.db.models.deletion import ProtectedError
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from orders_app.controllers.con_orders import (
    ConOrder, NOT_ON_MENU_MESSAGE,
)
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.orders.serializers import serialize_order_item_details
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from restaurants_app.tests import (
    seed_restaurant, seed_menu_section, seed_menu_items, seed_tables,
    TEST_RESTAURANT_NAME, TEST_MENU_SECTION_NAME,
    TEST_MENU_ITEM1_NAME, TEST_MENU_ITEM2_NAME,
    TEST_DISCOUNTED_MENU_ITEM_NAME,
    TEST_TABLE_NUMBER1,
    TEST_TABLE_NUMBER3,
    TEST_TABLE_NUMBER4,
    TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME,
    TEST_OPTION_MENU_ITEM_NAME,
    TEST_OPTION_GROUP_ID,
    TEST_OPTION_CHOICE_SMALL_ID,
    TEST_OPTION_CHOICE_LARGE_ID,
    TEST_OPTION_CHOICE_SMALL_COST,
)
from platform_admin_app.testing import give_legacy_platform_role
from restaurants_app.models import (
    Restaurant, Table, MenuItem, MenuSection, SectionGroup, RestaurantEmployee,
)
from orders_app.controllers.services.order_quote import quote_ref
from dinify_backend.configss.messages import OK_ORDER_UPDATED
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, OrderStatus_Pending,
    RestaurantStatus_Live, RESTAURANT_OWNER, RESTAURANT_STAFF,
)


def seed_order():
    restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
    table = Table.objects.get(number=TEST_TABLE_NUMBER1)
    table3 = Table.objects.get(number=TEST_TABLE_NUMBER3)
    user = User.objects.get(username=TEST_PHONE)

    Order.objects.create(
        restaurant=restaurant,
        table=table,
        customer=user,
        total_cost=10000,
        discounted_cost=9000,
        savings=1000,
        actual_cost=9000,
        prepayment_required=True,
        payment_status='paid',
        order_status='completed'
    )

    Order.objects.create(
        restaurant=restaurant,
        table=table3,
        customer=user,
        total_cost=0,
        discounted_cost=0,
        savings=0,
        actual_cost=0,
        prepayment_required=True,
        payment_status='paid',
        order_status='completed'
    )


class TestOrderFunctions(TestCase):
    print("\n===TESTING ORDERS===\n")

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

    def test_determine_effective_unit_price_no_modifiers(self):
        menu_item = MenuItem.objects.get(name=TEST_EXTRA_DISCOUNTED_MENU_ITEM_NAME)
        result = ConOrder.determine_effective_unit_price(menu_item=menu_item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('800.00'))
        self.assertEqual(result['cost_of_options'], Decimal('0.00'))

    def test_determine_effective_unit_price_with_modifiers(self):
        menu_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        result = ConOrder.determine_effective_unit_price(
            menu_item=menu_item,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]}
        )
        self.assertEqual(result['status'], 200)
        # discounted_price 900 + small choice 1100 = 2000
        self.assertEqual(result['price'], Decimal('2000.00'))
        self.assertEqual(result['cost_of_options'], Decimal(str(TEST_OPTION_CHOICE_SMALL_COST)) + Decimal('0'))

    def test_check_options_requirements(self):
        menu_item_with_options = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)
        menu_item_without_options = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

        ok_items = [{
            'item': str(menu_item_without_options.pk),
            'quantity': 2,
        }]
        self.assertEqual(ConOrder.check_options_requirements(ok_items)['status'], 200)

        missing_selection = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {}
        }]
        self.assertEqual(ConOrder.check_options_requirements(missing_selection)['status'], 400)

        too_many = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {
                TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID, TEST_OPTION_CHOICE_LARGE_ID],
            }
        }]
        self.assertEqual(ConOrder.check_options_requirements(too_many)['status'], 400)

        valid = [{
            'item': str(menu_item_with_options.pk),
            'quantity': 1,
            'selected_modifiers': {
                TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID],
            }
        }]
        self.assertEqual(ConOrder.check_options_requirements(valid)['status'], 200)

    def test_check_extras_requirements(self):
        # No fixture is pre-seeded with extras, so configure one inline
        # (mirrors how the options test reuses a pre-configured option item).
        mi = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        mi.has_extras = True
        mi.extras_min_selections = 1
        mi.extras_max_selections = 2
        mi.save()
        pk = str(mi.pk)

        # check_extras_requirements only counts len(item['extras']),
        # so placeholder values of the right length are sufficient.
        under_min = [{'item': pk, 'quantity': 1, 'extras': []}]
        self.assertEqual(ConOrder.check_extras_requirements(under_min)['status'], 400)

        at_min = [{'item': pk, 'quantity': 1, 'extras': ['x']}]
        self.assertEqual(ConOrder.check_extras_requirements(at_min)['status'], 200)

        over_max = [{'item': pk, 'quantity': 1, 'extras': ['x', 'y', 'z']}]
        self.assertEqual(ConOrder.check_extras_requirements(over_max)['status'], 400)

        # Unlimited maximum when extras_max_selections is null.
        mi.extras_max_selections = None
        mi.save()
        unlimited = [{'item': pk, 'quantity': 1, 'extras': ['a', 'b', 'c', 'd']}]
        self.assertEqual(ConOrder.check_extras_requirements(unlimited)['status'], 200)

    def test_con_order_initiate_and_item_lifecycle(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        table = Table.objects.get(number=TEST_TABLE_NUMBER4)

        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        discounted = MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        # Wire two dedicated published is_extra items into the options item's
        # allowlist so the extras it carries pass the extra-applicability gate.
        extra1 = MenuItem.objects.create(
            name='Lifecycle Extra 1', section=options_item.section,
            primary_price=Decimal('500'), approved=True, enabled=True, is_extra=True,
        )
        extra2 = MenuItem.objects.create(
            name='Lifecycle Extra 2', section=options_item.section,
            primary_price=Decimal('500'), approved=True, enabled=True, is_extra=True,
        )
        options_item.has_extras = True
        options_item.extras_applicable = [str(extra1.pk), str(extra2.pk)]
        options_item.save(update_fields=['has_extras', 'extras_applicable'])

        # first attempt has the options item with no selected_modifiers → rejected
        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {
                'item': str(options_item.pk),
                'quantity': 1,
                'extras': [str(extra1.pk), str(extra2.pk)],
            },
        ]
        response = ConOrder.initiate_order(
            restaurant_id=str(restaurant.pk),
            table_id=str(table.pk),
            items=items,
        )
        self.assertEqual(response['status'], 400)

        # second attempt supplies grouped modifier selections
        order_item_payload = {
            'item': str(options_item.pk),
            'quantity': 1,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
            'extras': [str(extra1.pk), str(extra2.pk)],
        }
        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {'item': str(discounted.pk), 'quantity': 3},
            order_item_payload,
        ]
        response = ConOrder.initiate_order(
            restaurant_id=str(restaurant.pk),
            table_id=str(table.pk),
            items=items,
        )
        self.assertEqual(response['status'], 200)

        order_id = str(response['data']['order_details']['id'])
        # dedup check: re-submitting the same grouped modifier selection finds the existing row
        self.assertIsNotNone(
            ConOrder.find_existing_order_item(item=order_item_payload, order_id=order_id)
        )
        # but a different choice is a distinct row
        different_choice = dict(order_item_payload)
        different_choice['selected_modifiers'] = {
            TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_LARGE_ID]
        }
        self.assertIsNone(
            ConOrder.find_existing_order_item(item=different_choice, order_id=order_id)
        )

        # confirm stored fields on the created item
        saved_option_item = OrderItem.objects.get(
            order__id=order_id,
            item=options_item,
            parent_item__isnull=True,
        )
        self.assertEqual(
            saved_option_item.selected_modifiers,
            {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
        )
        self.assertEqual(len(saved_option_item.options), 1)
        self.assertEqual(saved_option_item.options[0]['name'], 'Size')
        self.assertEqual(saved_option_item.options[0]['choices'], 'Small')

    def test_handle_add_order_items_is_deleted(self):
        """`handle_add_order_items` is gone and must not come back.

        It had no production caller — the `add-items` route was retired, and
        nothing (including the action-string dispatch in V2OrdersEndpoint) reached
        it. Re-mounting it as written would be worse than dead code: it locked
        `OrderItem` and only then touched `Order`, the INVERSE of the live create
        path's `Order -> OrderItem`, so the two together are a deadlock cycle. It
        also took no admission advisory lock and did no lifecycle re-check, so it
        would append items to an order at a suspended restaurant.
        """
        import orders_app.controllers.con_orders as con_orders

        self.assertFalse(hasattr(con_orders, 'handle_add_order_items'))

    def test_add_order_item_merges_when_same_item_has_two_lines(self):
        # BUG-P2-5 regression: the same menu item can legitimately sit on an order
        # as two lines (e.g. Small vs Large). The bump path used to re-look-up the
        # line with OrderItem.objects.get(order, item) — non-unique here, so it
        # raised MultipleObjectsReturned -> 500. It must now bump the matched line
        # in place and never crash.
        order = Order.objects.get(table=Table.objects.get(number=TEST_TABLE_NUMBER3))
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        # Both lines are written by the REAL writer, so their immutable unit and
        # preparation snapshots are the ones the pricer actually produces. A
        # hand-built row with invented amounts is a DIFFERENT line under the D02
        # identity rule (same selections, incompatible pricing snapshot) and
        # would not — and should not — merge.
        for choice in (TEST_OPTION_CHOICE_SMALL_ID, TEST_OPTION_CHOICE_LARGE_ID):
            created = ConOrder.add_order_item(
                item={
                    'item': str(options_item.pk), 'quantity': 1,
                    'selected_modifiers': {TEST_OPTION_GROUP_ID: [choice]},
                },
                order_id=str(order.pk),
            )
            self.assertEqual(created['status'], 200, created)
        small_line = OrderItem.objects.get(
            order__id=order.pk, item=options_item, deleted=False,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
        )
        large_line = OrderItem.objects.get(
            order__id=order.pk, item=options_item, deleted=False,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_LARGE_ID]},
        )
        # The Large line is the most recent, so a matcher that examined only the
        # newest candidate would miss the Small one entirely — which is the D03
        # defect this now also covers, alongside the original crash.
        self.assertEqual(
            OrderItem.objects.filter(
                order__id=order.pk, item=options_item, deleted=False
            ).count(),
            2,
        )

        payload = {
            'item': str(options_item.pk),
            'quantity': 2,
            'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
        }
        # Previously raised OrderItem.MultipleObjectsReturned; now returns the 200 dict.
        result = ConOrder.add_order_item(item=payload, order_id=str(order.pk))
        self.assertEqual(result['status'], 200)

        small_line.refresh_from_db()
        large_line.refresh_from_db()
        self.assertEqual(small_line.quantity, 3)   # 1 + 2 merged onto the matched [0] line
        self.assertEqual(large_line.quantity, 1)   # the non-matching line is untouched
        # merged in place, not appended as a third line
        self.assertEqual(
            OrderItem.objects.filter(
                order__id=order.pk, item=options_item, deleted=False
            ).count(),
            2,
        )

    def test_add_order_item_creates_then_merges_single_line(self):
        # A genuinely-new item creates a line; re-adding the same item merges into
        # that one line (the simple single-line dedup path) instead of duplicating.
        order = Order.objects.get(table=Table.objects.get(number=TEST_TABLE_NUMBER3))
        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

        # new item -> create path (returns the uniform 200 envelope, so the
        # _create_order chokepoint can status-check every call), exactly one
        # line at quantity 1
        create_result = ConOrder.add_order_item(
            item={'item': str(menu_item1.pk), 'quantity': 1}, order_id=str(order.pk)
        )
        self.assertEqual(create_result['status'], 200)
        line = OrderItem.objects.get(
            order__id=order.pk, item=menu_item1, deleted=False
        )
        self.assertEqual(line.quantity, 1)

        # same item again -> merge path bumps the same line, no duplicate created
        merge_result = ConOrder.add_order_item(
            item={'item': str(menu_item1.pk), 'quantity': 2}, order_id=str(order.pk)
        )
        self.assertEqual(merge_result['status'], 200)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 3)
        self.assertEqual(
            OrderItem.objects.filter(
                order__id=order.pk, item=menu_item1, deleted=False
            ).count(),
            1,
        )


class TestOrderTableProtect(TestCase):
    """
    Leg 1 of the deletion model: Order.table is on_delete=PROTECT, so a Table
    (or, by chain, a Restaurant) that has orders can never be hard-deleted —
    financial/order history is protected instead of being silently
    cascade-destroyed.
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()  # creates orders on tables 1 and 3

    def test_deleting_table_with_orders_raises_protected_error(self):
        table_with_order = Table.objects.get(number=TEST_TABLE_NUMBER1)
        with self.assertRaises(ProtectedError):
            table_with_order.delete()
        # the table and its orders survive the blocked delete
        self.assertTrue(Table.objects.filter(number=TEST_TABLE_NUMBER1).exists())
        self.assertTrue(Order.objects.filter(table=table_with_order).exists())

    def test_deleting_table_without_orders_succeeds(self):
        table_without_order = Table.objects.get(number=TEST_TABLE_NUMBER4)
        self.assertFalse(Order.objects.filter(table=table_without_order).exists())
        table_without_order.delete()
        self.assertFalse(Table.objects.filter(number=TEST_TABLE_NUMBER4).exists())

    def test_deleting_restaurant_with_orders_raises_protected_error(self):
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        # PROTECT propagates up the Restaurant -> Table -> Order chain: the
        # cascade collects the tables, then the Order.table PROTECT blocks.
        with self.assertRaises(ProtectedError):
            restaurant.delete()
        self.assertTrue(Restaurant.objects.filter(name=TEST_RESTAURANT_NAME).exists())


class TestAnonymousOrderPaths(TestCase):
    """
    Regression coverage for the AnonymousUser order-path bug: DRF hands
    unauthenticated requests an AnonymousUser (not None), which used to slip past
    `if user is None` guards and get assigned to User FK fields, raising a
    ValueError that surfaced as a generic failure (submit) or a 500 (delete-item).
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        # APIClient with no force_authenticate → request.user is AnonymousUser,
        # exactly reproducing an unauthenticated diner.
        self.client = APIClient()
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER1)
        self.menu_item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

    def _diner_session(self, table=None):
        # The opaque diner table-session capability (PR 7A) an anonymous diner
        # presents on every write. Minted for self.table by default and sent in
        # the X-Diner-Session header the endpoints read.
        from restaurants_app.controllers.diner_capability import (
            issue_table_session,
        )
        return issue_table_session(table or self.table)

    def _initiate_anonymous_order(self) -> str:
        """Place an order as an anonymous diner and return its id."""
        response = self.client.post(
            '/api/v2/orders/initiate/',
            {
                'restaurant': str(self.restaurant.pk),
                'table': str(self.table.pk),
                'items': [{'item': str(self.menu_item.pk), 'quantity': 1}],
            },
            format='json',
            HTTP_X_DINER_SESSION=self._diner_session(),
        )
        self.assertEqual(response.status_code, 200)
        return str(response.json()['data']['order_details']['id'])

    def test_anonymous_diner_initiate_then_submit_succeeds(self):
        order_id = self._initiate_anonymous_order()

        response = self.client.put(
            '/api/v1/orders/submit/',
            {'order': order_id, 'quote_ref': quote_ref(Order.objects.get(id=order_id))},
            format='json',
            HTTP_X_DINER_SESSION=self._diner_session(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['message'], OK_ORDER_UPDATED)

        order = Order.objects.get(id=order_id)
        self.assertEqual(order.order_status, OrderStatus_Pending)
        # attribution is left null rather than crashing on AnonymousUser
        self.assertIsNone(order.last_updated_by)

    def test_order_management_actions_are_retired(self):
        # prepare / cancel / update-item (v1) and add-items (v2, POST & DELETE)
        # were orphaned and unscoped (any authenticated user could transition
        # another restaurant's order by id) — they are retired and now 404
        # instead of falling through to a 500. submit / initiate stay live
        # (covered above). Mirrors tests_kitchen.test_kds_routes_are_retired.
        order_id = self._initiate_anonymous_order()
        item = OrderItem.objects.filter(order__id=order_id).first()
        self.assertIsNotNone(item)

        retired = [
            ('put', '/api/v1/orders/cancel/'),
            ('put', '/api/v1/orders/prepare/'),
            ('put', '/api/v1/orders/update-item/'),
            ('post', '/api/v2/orders/add-items/'),
            ('delete', '/api/v2/orders/add-items/'),
        ]
        for method, url in retired:
            response = getattr(self.client, method)(
                url,
                {'order': order_id, 'item': str(item.pk)},
                format='json',
            )
            self.assertEqual(
                response.status_code, 404,
                msg=f'expected 404 for retired {method.upper()} {url}',
            )

        # the retired actions touched nothing: order still initiated, item live
        order = Order.objects.get(id=order_id)
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        item.refresh_from_db()
        self.assertFalse(item.deleted)

    def test_details_read_endpoint_is_retired(self):
        # api/v2/orders/details/ was an orphaned, unauthenticated full-order
        # read (C1) — no caller; the diner gets its order from the initiate
        # response + nav-state, and the scoped diner view uses the journey path.
        # It is retired and now 404s. Mirrors tests_kitchen's
        # test_kds_routes_are_retired.
        order_id = self._initiate_anonymous_order()
        response = self.client.get(f'/api/v2/orders/details/?order={order_id}')
        self.assertEqual(response.status_code, 404)

    def test_submit_missing_order_returns_400(self):
        # No 'order' in the body: the guard returns 400 without calling
        # .get(id=None), rather than surfacing a 500. The order-id guard fires
        # before any session logic, so no diner session is needed here.
        response = self.client.put('/api/v1/orders/submit/', {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_submit_without_session_or_staff_auth_returns_400(self):
        # A well-formed order id but neither a diner session nor a staff JWT:
        # the caller has presented no authority, so submit refuses with a clean
        # 400 (never a 500, and deliberately not 401/403 which would trip the
        # diner app's logout interceptor).
        order_id = self._initiate_anonymous_order()
        response = self.client.put(
            '/api/v1/orders/submit/', {'order': order_id}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        # The draft is untouched — it was never transitioned.
        self.assertEqual(
            Order.objects.get(id=order_id).order_status, OrderStatus_Initiated,
        )

    def test_submit_malformed_order_id_under_session_returns_404(self):
        # Under a valid diner session a non-UUID order id raises ValidationError
        # in the scoped ORM lookup and is folded into ONE non-disclosing 404
        # (not a 500, and no longer a distinct 400 — a junk id is simply "not
        # found on your table").
        response = self.client.put(
            '/api/v1/orders/submit/', {'order': 'not-a-uuid'}, format='json',
            HTTP_X_DINER_SESSION=self._diner_session(),
        )
        self.assertEqual(response.status_code, 404)

    def test_submit_nonexistent_order_id_returns_404(self):
        # A well-formed but unknown order id under a session raises DoesNotExist
        # in the scoped lookup -> 404, not 500.
        response = self.client.put(
            '/api/v1/orders/submit/', {'order': str(uuid4())}, format='json',
            HTTP_X_DINER_SESSION=self._diner_session(),
        )
        self.assertEqual(response.status_code, 404)

    def test_submit_unknown_action_still_404(self):
        # Any action other than 'submit' still 404s (unchanged dispatch).
        response = self.client.put(
            '/api/v1/orders/frobnicate/', {'order': str(uuid4())}, format='json',
        )
        self.assertEqual(response.status_code, 404)


class TestDiscountActivationPricing(TestCase):
    """The effective unit price honours the single, timezone-aware discount
    window predicate: an inactive window charges primary_price even when
    running_discount=True and a discounted_price is stored (that column is no
    longer consulted — discount_details is the source of truth)."""

    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)

    def _make_item(self, name, discount_details):
        # discounted_price is deliberately set to a stale 8000 to prove the
        # window predicate — not the stored column — decides the charge.
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=Decimal('8000'),
            running_discount=True,
            consider_discount_object=True,
            discount_details=discount_details,
        )

    def test_expired_window_charges_primary_price(self):
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        item = self._make_item('Expired Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': yesterday,
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('10000.00'))

    def test_wrong_recurring_day_charges_primary_price(self):
        today_iso = timezone.localdate().isoweekday()
        other_days = [d for d in range(1, 8) if d != today_iso]
        item = self._make_item('Wrong Day Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': other_days,
            'start_date': '', 'end_date': '',
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('10000.00'))

    def test_active_window_charges_discounted_price(self):
        item = self._make_item('Active Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': '',
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))

    def test_empty_recurring_days_means_every_day(self):
        # Empty recurring_days == "every day". Locks the chosen semantic so a
        # future switch to "never" is a deliberate change.
        today = timezone.localdate()
        item = self._make_item('Empty Recurring Discount Item', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [],
            'start_date': (today - timedelta(days=5)).isoformat(),
            'end_date': (today + timedelta(days=5)).isoformat(),
            'start_time': '', 'end_time': '',
        })
        result = ConOrder.determine_effective_unit_price(menu_item=item)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['price'], Decimal('8000.00'))


class TestOrderTenantConsistency(TestCase):
    """
    BUG-P1-1: order creation must reject a table or menu items that do not
    belong to the order's restaurant. The diner app always sends a consistent
    restaurant/table/items triple (QR scan -> that restaurant's own menu), so
    these guards only ever reject crafted cross-tenant submissions.
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        seed_order()

        # Restaurant A — the legitimate tenant. seed_order() occupies tables 1
        # and 3, so table 4 is free for a clean creation.
        self.restaurant_a = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.table_a = Table.objects.get(number=TEST_TABLE_NUMBER4)
        self.item_a = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        self.item_a2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        # A dedicated published is_extra item for restaurant A, wired into
        # item_a's allowlist so applicability tests submit a real, valid extra.
        self.extra_a = MenuItem.objects.create(
            name='Tenant Extra A', section=self.item_a.section,
            primary_price=Decimal('1000'), approved=True, enabled=True,
            is_extra=True,
        )
        self.item_a.has_extras = True
        self.item_a.extras_applicable = [str(self.extra_a.pk)]
        self.item_a.save(update_fields=['has_extras', 'extras_applicable'])

        # Restaurant B — a second tenant with its own section, item and table.
        owner = User.objects.get(username=TEST_PHONE)
        self.restaurant_b = Restaurant.objects.create(
            name='Other Tenant Restaurant',
            location='elsewhere',
            owner=owner,
        )
        section_b = MenuSection.objects.create(
            name='Other Tenant Section',
            restaurant=self.restaurant_b,
        )
        self.item_b = MenuItem.objects.create(
            name='Other Tenant Item',
            section=section_b,
            primary_price=1000.0,
            discounted_price=900.0,
            running_discount=False,
        )
        self.table_b = Table.objects.create(
            number=99,
            restaurant=self.restaurant_b,
            prepayment_required=False,
        )

    def test_table_from_other_restaurant_rejected(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_b.pk),
            items=[{'item': str(self.item_a.pk), 'quantity': 1}],
        )
        self.assertEqual(response['status'], 400)
        # nothing created, and B's table is untouched
        self.assertEqual(Order.objects.count(), before)
        self.assertFalse(Order.objects.filter(table=self.table_b).exists())

    def test_item_from_other_restaurant_rejected(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[
                {'item': str(self.item_a.pk), 'quantity': 1},
                {'item': str(self.item_b.pk), 'quantity': 1},
            ],
        )
        self.assertEqual(response['status'], 400)
        # the whole order is rejected — a single foreign item poisons it
        self.assertEqual(Order.objects.count(), before)
        self.assertFalse(Order.objects.filter(table=self.table_a).exists())

    def test_nonexistent_table_returns_400(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(uuid4()),
            items=[{'item': str(self.item_a.pk), 'quantity': 1}],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(Order.objects.count(), before)

    def test_nonexistent_item_returns_400(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{'item': str(uuid4()), 'quantity': 1}],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(Order.objects.count(), before)

    def test_item_missing_quantity_returns_400(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{'item': str(self.item_a.pk)}],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(Order.objects.count(), before)

    def test_valid_same_restaurant_order_succeeds(self):
        before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[
                {'item': str(self.item_a.pk), 'quantity': 2},
                {'item': str(self.item_a2.pk), 'quantity': 1},
            ],
        )
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)
        order_id = str(response['data']['order_details']['id'])
        self.assertTrue(
            OrderItem.objects.filter(order__id=order_id, item=self.item_a).exists()
        )
        self.assertTrue(
            OrderItem.objects.filter(order__id=order_id, item=self.item_a2).exists()
        )

    # ---- extras tenant boundary (BUG-P1-1 follow-up) --------------------
    # Extras are MenuItems referenced by bare UUID strings inside each item
    # entry's `extras` list; #198 closed the boundary for the table and the
    # parent items but missed them. Every rejection below must be the SAME
    # opaque 400 (NOT_ON_MENU_MESSAGE) regardless of whether the id is
    # foreign, nonexistent, malformed or of the wrong type, and must leave
    # NOTHING persisted — the whole order aborts, never a partial write.

    def test_extra_from_other_restaurant_rejected_atomically(self):
        orders_before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 1,
                'extras': [str(self.item_b.pk)],
            }],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        # nothing persisted anywhere — no order, no items, table A untouched
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertFalse(Order.objects.filter(table=self.table_a).exists())

    def test_mixed_valid_and_foreign_extras_rejected(self):
        orders_before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 1,
                'extras': [str(self.item_a2.pk), str(self.item_b.pk)],
            }],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        # all or nothing: the valid sibling extra did not persist either
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_malformed_extra_uuid_returns_400_not_500(self):
        # a raise here would fail the test with an error — pinning "never 500"
        orders_before = Order.objects.count()
        for bad_member in ('not-a-uuid', None, 123):
            response = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant_a.pk),
                table_id=str(self.table_a.pk),
                items=[{
                    'item': str(self.item_a.pk),
                    'quantity': 1,
                    'extras': [bad_member],
                }],
            )
            self.assertEqual(response['status'], 400)
            self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_nonexistent_extra_indistinguishable_from_foreign(self):
        orders_before = Order.objects.count()
        foreign = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{'item': str(self.item_a.pk), 'quantity': 1,
                    'extras': [str(self.item_b.pk)]}],
        )
        nonexistent = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{'item': str(self.item_a.pk), 'quantity': 1,
                    'extras': [str(uuid4())]}],
        )
        self.assertEqual(foreign['status'], 400)
        self.assertEqual(nonexistent['status'], 400)
        # identical outward response — a prober cannot learn whether an id
        # exists on another tenant's menu
        self.assertEqual(nonexistent['message'], foreign['message'])
        self.assertEqual(Order.objects.count(), orders_before)

    def test_valid_same_tenant_extra_creates_parent_and_child(self):
        orders_before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 2,
                'extras': [str(self.extra_a.pk)],
            }],
        )
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), orders_before + 1)
        order_id = str(response['data']['order_details']['id'])
        parent = OrderItem.objects.get(
            order__id=order_id, item=self.item_a, parent_item__isnull=True
        )
        child = OrderItem.objects.get(
            order__id=order_id, item=self.extra_a, parent_item__isnull=False
        )
        self.assertEqual(child.parent_item_id, parent.pk)
        # D02 (P1): ONE selected extra PER UNIT of its parent dish. The parent is
        # quantity 2, so two of this extra are charged and two are prepared. It
        # was hardcoded to 1 however many dishes it was attached to.
        self.assertEqual(child.quantity, 2)
        # priced server-side from A's menu item, never from client input
        self.assertEqual(child.unit_price, Decimal('1000.00'))
        self.assertEqual(child.actual_cost, Decimal('2000.00'))

    def test_a_foreign_extra_rejects_the_whole_order_before_any_write(self):
        # Renamed and re-described: the old name and comment claimed the
        # rejection fired at the SECOND item's extras so that "the first, valid
        # item's already-written rows must unwind too". Instrumenting the
        # service shows otherwise — `validate_order_selections` at step 2b
        # resolves parents AND extras in one restaurant-scoped query, so the
        # foreign extra is refused with counter_allocated=False and
        # add_order_item_calls=0. Nothing is ever written, so nothing unwinds.
        #
        # The assertions below were always correct; only the story was wrong.
        # This is a tenant-boundary test over the extras axis. Genuine
        # late-rollback evidence is in
        # orders_app/tests_order_input.py::D01LateRollbackTests.
        orders_before = Order.objects.count()
        result = _create_order(
            restaurant=self.restaurant_a,
            table=self.table_a,
            items=[
                {'item': str(self.item_a.pk), 'quantity': 1},
                {'item': str(self.item_a2.pk), 'quantity': 1,
                 'extras': [str(self.item_b.pk)]},
            ],
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), 0)
        # even the daily-number allocation unwound with the transaction
        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant_a
            ).exists()
        )

    def test_valid_retry_same_client_order_id_still_one_order(self):
        orders_before = Order.objects.count()
        key = uuid4()
        payload = dict(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 1,
                'extras': [str(self.extra_a.pk)],
            }],
            client_order_id=str(key),
        )
        first = ConOrder.initiate_order(**payload)
        second = ConOrder.initiate_order(**payload)
        self.assertEqual(first['status'], 200)
        self.assertEqual(second['status'], 200)
        self.assertEqual(Order.objects.count(), orders_before + 1)
        order = Order.objects.get(client_order_id=key)
        # the replay re-fetched the draft; it did not re-add the items
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 2)

    def test_rejected_draft_leaves_no_poisoned_row_for_client_order_id(self):
        # Post-#210 _create_order writes a DRAFT row that idempotent retries
        # re-fetch by client_order_id. A rejected attempt must leave NO such
        # row behind, and the valid retry must build a fresh clean order —
        # never resurrect anything from the rejected attempt.
        key = uuid4()
        first = _create_order(
            restaurant=self.restaurant_a,
            table=self.table_a,
            items=[{'item': str(self.item_a.pk), 'quantity': 1,
                    'extras': [str(self.item_b.pk)]}],
            client_order_id=key,
        )
        self.assertEqual(first['status'], 400)
        self.assertEqual(Order.objects.filter(client_order_id=key).count(), 0)

        second = _create_order(
            restaurant=self.restaurant_a,
            table=self.table_a,
            items=[{'item': str(self.item_a.pk), 'quantity': 1,
                    'extras': [str(self.extra_a.pk)]}],
            client_order_id=key,
        )
        self.assertEqual(second['status'], 200)
        self.assertFalse(second['idempotent'])  # fresh creation, not a replay
        self.assertEqual(Order.objects.filter(client_order_id=key).count(), 1)
        order_items = OrderItem.objects.filter(order=second['order'])
        self.assertEqual(order_items.count(), 2)
        self.assertFalse(order_items.filter(item=self.item_b).exists())

    def test_direct_add_order_item_foreign_extra_defense_in_depth(self):
        # The guard holds standalone, without initiate_order's batch gate in
        # front: the foreign extra is rejected with the same opaque 400 and no
        # cross-tenant row is ever written. (Whole-order atomicity on
        # rejection is the service's contract — _create_order — not this
        # helper's.)
        order = Order.objects.get(
            table=Table.objects.get(number=TEST_TABLE_NUMBER3)
        )
        result = ConOrder.add_order_item(
            item={'item': str(self.item_a.pk), 'quantity': 1,
                  'extras': [str(self.item_b.pk)]},
            order_id=str(order.pk),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], NOT_ON_MENU_MESSAGE)
        self.assertFalse(OrderItem.objects.filter(item=self.item_b).exists())

    def test_extras_not_a_list_returns_400(self):
        orders_before = Order.objects.count()
        for bad_extras in (5, 'abc', {'k': 'v'}):
            response = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant_a.pk),
                table_id=str(self.table_a.pk),
                items=[{'item': str(self.item_a.pk), 'quantity': 1,
                        'extras': bad_extras}],
            )
            self.assertEqual(response['status'], 400)
            self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), orders_before)

        # an empty list ("no extras selected") and an explicit null both stay
        # valid — the boundary only rejects what it cannot resolve. Drafts do
        # not occupy the table (post-#210), so both creations share table A.
        for valid_extras in ([], None):
            response = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant_a.pk),
                table_id=str(self.table_a.pk),
                items=[{'item': str(self.item_a.pk), 'quantity': 1,
                        'extras': valid_extras}],
            )
            self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), orders_before + 2)

    def test_duplicate_extras_rejected(self):
        # PR2: a duplicate submitted extra id is rejected — the same extra may not
        # be attached to one parent twice. The whole order fails with the opaque
        # menu message and no row is committed. (This reverses the pre-PR2
        # "accepted as two child rows" behaviour.)
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 1,
                'extras': [str(self.extra_a.pk), str(self.extra_a.pk)],
            }],
        )
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)


class TestOrderingAvailabilityGates(TestCase):
    """
    BUG-P2-1: order creation must enforce ordering availability for DINER orders
    (created_by is None). A paused restaurant (accepting_orders=False), a
    view-only table (qr_mode='menu_only'), or a table that is not available for a
    scan (soft-deleted / disabled / inactive / out of service) must reject the
    order without creating a row. Staff/admin orders (created_by set) are a
    management action and bypass every one of these gates.
    """

    def setUp(self) -> None:
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        # NB: seed_order() is deliberately NOT called, so every seeded table is
        # free — the double-seating gate never interferes with the success cases.

        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        # A real User to stand in for a staff/admin order's created_by.
        self.staff = User.objects.get(username=TEST_PHONE)
        # A free table carrying the ordering-friendly defaults (qr_mode
        # 'order_pay', is_active True, status 'available').
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER4)

    def _order(self, created_by=None):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=created_by,
        )

    # --- Gate 1: restaurant accepting_orders -------------------------------

    def test_diner_order_blocked_when_not_accepting_orders(self):
        self.restaurant.accepting_orders = False
        self.restaurant.save(update_fields=['accepting_orders'])
        before = Order.objects.count()
        response = self._order()
        self.assertEqual(response['status'], 400)
        self.assertEqual(
            response['message'], 'This restaurant is not currently accepting orders'
        )
        self.assertEqual(Order.objects.count(), before)

    def test_staff_order_succeeds_when_not_accepting_orders(self):
        self.restaurant.accepting_orders = False
        self.restaurant.save(update_fields=['accepting_orders'])
        before = Order.objects.count()
        response = self._order(created_by=self.staff)
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    # --- Gate 2: table qr_mode ---------------------------------------------

    def test_diner_order_blocked_at_menu_only_table(self):
        self.table.qr_mode = 'menu_only'
        self.table.save(update_fields=['qr_mode'])
        before = Order.objects.count()
        response = self._order()
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], 'Ordering is not available at this table')
        self.assertEqual(Order.objects.count(), before)

    def test_diner_order_succeeds_at_order_pay_table(self):
        self.table.qr_mode = 'order_pay'
        self.table.save(update_fields=['qr_mode'])
        response = self._order()
        self.assertEqual(response['status'], 200)

    def test_diner_order_succeeds_at_order_only_table(self):
        self.table.qr_mode = 'order_only'
        self.table.save(update_fields=['qr_mode'])
        response = self._order()
        self.assertEqual(response['status'], 200)

    def test_staff_order_bypasses_menu_only_gate(self):
        self.table.qr_mode = 'menu_only'
        self.table.save(update_fields=['qr_mode'])
        before = Order.objects.count()
        response = self._order(created_by=self.staff)
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    # --- Gate 3: table availability (is_available_for_scan) ----------------

    def test_diner_order_blocked_at_inactive_table(self):
        self.table.is_active = False
        self.table.save(update_fields=['is_active'])
        before = Order.objects.count()
        response = self._order()
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], 'This table is not available for ordering')
        self.assertEqual(Order.objects.count(), before)

    def test_diner_order_blocked_at_out_of_service_table(self):
        self.table.status = 'out_of_service'
        self.table.save(update_fields=['status'])
        before = Order.objects.count()
        response = self._order()
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], 'This table is not available for ordering')
        self.assertEqual(Order.objects.count(), before)

    def test_staff_order_bypasses_inactive_table_gate(self):
        self.table.is_active = False
        self.table.save(update_fields=['is_active'])
        before = Order.objects.count()
        response = self._order(created_by=self.staff)
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    # --- Happy path --------------------------------------------------------

    def test_normal_diner_order_succeeds_end_to_end(self):
        # accepting_orders True, qr_mode 'order_pay', active/available table are
        # all fixture defaults, so there is nothing to mutate here.
        before = Order.objects.count()
        response = self._order()
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)
        order_id = str(response['data']['order_details']['id'])
        self.assertTrue(
            OrderItem.objects.filter(order__id=order_id, item=self.item).exists()
        )


class TestExtrasDiscountedFlag(TestCase):
    """BUG-P3-4: an extra's persisted/serialized `discounted` flag must reflect
    the live-discount predicate (is_discount_active()), not the raw
    running_discount column — mirroring the parent-item derivation at
    con_orders.py:408. The extra's CHARGE is already gated on the predicate
    (determine_effective_unit_price -> effective_base_price), so an out-of-window
    discount charges primary_price; the flag must agree, so a full-price extra is
    never badged "discounted"."""

    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        # seed_order() deliberately NOT called → TEST_TABLE_NUMBER4 stays free.
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER4)
        # The parent accepts extras; each test wires its specific extra into the
        # allowlist in _place_order_with_extra. Extra applicability (has_extras +
        # extras_applicable membership + is_extra) is enforced on the order path.
        self.parent = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        self.parent.has_extras = True
        self.parent.save(update_fields=['has_extras'])

    def _make_extra(self, name, discount_details):
        # discounted_price is set to a stale 8000 on purpose — neither the flag
        # nor the charge reads that stored column; the window predicate decides
        # both (mirrors TestDiscountActivationPricing._make_item).
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            # Published + is_extra so the diner-path order clears the publication
            # AND extra-applicability gates; this suite is about the discount flag.
            approved=True,
            enabled=True,
            is_extra=True,
            primary_price=Decimal('10000'),
            discounted_price=Decimal('8000'),
            running_discount=True,
            consider_discount_object=True,
            discount_details=discount_details,
        )

    def _place_order_with_extra(self, extra_item):
        # Wire this specific extra into the parent's allowlist so it is an
        # applicable, published, same-restaurant is_extra item at order time.
        self.parent.extras_applicable = [str(extra_item.pk)]
        self.parent.save(update_fields=['extras_applicable'])
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=[{
                'item': str(self.parent.pk),
                'quantity': 1,
                'extras': [str(extra_item.pk)],
            }],
        )
        self.assertEqual(response['status'], 200)
        order_id = response['data']['order_details']['id']
        return OrderItem.objects.get(
            order__id=order_id,
            item=extra_item,
            parent_item__isnull=False,
        )

    def test_lapsed_discount_extra_is_not_flagged_and_charged_full(self):
        # THE REPRO: a window that ended yesterday. The extra is charged the full
        # primary_price (unchanged) and — after the fix — reads discounted=False
        # both persisted and serialized (it previously read True off
        # running_discount).
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        extra = self._make_extra('Lapsed Discount Extra', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': yesterday,
            'start_time': '', 'end_time': '',
        })
        row = self._place_order_with_extra(extra)

        # persisted flag is now truthful (was extra_item.running_discount=True)
        self.assertFalse(row.discounted)
        # charge is the full price — the fix moves no money
        self.assertEqual(row.actual_cost, Decimal('10000.00'))
        self.assertEqual(row.total_cost, Decimal('10000.00'))
        self.assertEqual(row.savings, Decimal('0.00'))
        # serialized to the diner: no false "discounted" badge
        self.assertFalse(serialize_order_item_details(item=row)['discounted'])

    def test_active_discount_extra_is_flagged_and_charged_discounted(self):
        # An active window: unchanged behaviour — discounted=True and the 20%
        # discounted price applies.
        extra = self._make_extra('Active Discount Extra', {
            'discount_type': 'percentage', 'discount_percentage': 20.0,
            'discount_amount': 0.0, 'recurring_days': [1, 2, 3, 4, 5, 6, 7],
            'start_date': '', 'end_date': '',
            'start_time': '', 'end_time': '',
        })
        row = self._place_order_with_extra(extra)

        self.assertTrue(row.discounted)
        self.assertEqual(row.actual_cost, Decimal('8000.00'))  # 20% off 10000
        self.assertEqual(row.savings, Decimal('2000.00'))
        self.assertTrue(serialize_order_item_details(item=row)['discounted'])

    def test_extra_with_no_discount_is_not_flagged_and_charged_full(self):
        # No discount configured at all: is_discount_active() subsumes the
        # presence check → False, and the full price is charged.
        extra = MenuItem.objects.create(
            name='Plain Extra',
            section=self.section,
            approved=True,
            enabled=True,
            is_extra=True,
            primary_price=Decimal('5000'),
            running_discount=False,
        )
        row = self._place_order_with_extra(extra)

        self.assertFalse(row.discounted)
        self.assertEqual(row.actual_cost, Decimal('5000.00'))
        self.assertEqual(row.savings, Decimal('0.00'))
        self.assertFalse(serialize_order_item_details(item=row)['discounted'])


class AdminSourceOrderInitiationAuthTests(TestCase):
    """
    source='admin' order initiation must be authorized at the target restaurant
    (TENANT-P2-02).

    V2OrdersEndpoint is AllowAny (anonymous QR diner ordering). The source=='admin'
    branch previously only AUTHENTICATED — any authenticated principal (a
    self-registered diner) could set created_by and thereby skip all three diner
    availability gates in initiate_order (accepting_orders, qr_mode,
    is_available_for_scan) at ANY restaurant, then submit to occupy a foreign table
    and push a ticket to a foreign kitchen. The gate now requires
    can_user_access_module(user, restaurant, MODULE_TABLES) before created_by is
    set, returning 404 (non-disclosure) for a non-member. Anonymous/authenticated
    DINER ordering (source != 'admin') is untouched, and genuine staff still
    legitimately bypass the availability gates (a staff feature, not a diner one).

    There is no platform-wide exception: the dinify-admin bypass that used to
    admit a role-holder at every restaurant is gone, so "authorized at the target
    restaurant" now means employment, with no second door.
    """

    def _make_user(self, phone, roles=None):
        return User.objects.create_user(
            first_name='OSrc', last_name='User',
            email=f'{phone}@test.com', phone_number=phone,
            username=phone, country='Uganda', password='password',
            roles=roles or [],
        )

    def _seed_tenant(self, tag, owner_phone):
        owner = self._make_user(owner_phone)
        restaurant = Restaurant.objects.create(
            name=f'OSrc Restaurant {tag}', location=f'loc-{tag}',
            status=RestaurantStatus_Live, owner=owner, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=owner, restaurant=restaurant, roles=[RESTAURANT_OWNER],
        )
        # Published section + item so the DINER-path tests in this suite clear
        # the publication gate; the staff/admin-source tests bypass it anyway.
        section = MenuSection.objects.create(
            name=f'OSrc Section {tag}', restaurant=restaurant,
            approved=True, enabled=True,
        )
        item = MenuItem.objects.create(
            name=f'OSrc Item {tag}', section=section, primary_price=1000,
            approved=True, enabled=True,
        )
        table = Table.objects.create(
            number=1, str_number='1', restaurant=restaurant,
        )
        return owner, restaurant, item, table

    def setUp(self):
        self.owner_a, self.restaurant_a, self.item_a, self.table_a = \
            self._seed_tenant('A', '256700000610')
        self.owner_b, self.restaurant_b, self.item_b, self.table_b = \
            self._seed_tenant('B', '256700000620')
        # Genuine B staff with the minimal tables-only role (grants MODULE_TABLES).
        self.staff_b = self._make_user('256700000631')
        RestaurantEmployee.objects.create(
            user=self.staff_b, restaurant=self.restaurant_b, roles=[RESTAURANT_STAFF],
        )
        # Role-less authenticated diner (employed nowhere).
        self.diner = self._make_user('256700000640')
        # An account carrying the RETIRED platform role string. It used to reach
        # any restaurant's tables module through the dinify-admin bypass; it is now
        # a stranger. Written through the ORM, since the write paths refuse it.
        self.legacy_role_holder = give_legacy_platform_role(
            self._make_user('256700000650'))

    # --- helpers --------------------------------------------------------
    def _client(self, user=None):
        client = APIClient()
        if user is not None:
            from rest_framework_simplejwt.tokens import RefreshToken
            token = str(RefreshToken.for_user(user).access_token)
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def _initiate(self, user, restaurant, table, item, source='admin'):
        body = {
            'restaurant': str(restaurant.pk),
            'table': str(table.pk),
            'items': [{'item': str(item.pk), 'quantity': 1}],
        }
        if source is not None:
            body['source'] = source
        extra = {}
        # The anonymous QR path now requires an opaque diner table session bound
        # to the table (PR 7A); the admin path authorises via staff JWT instead.
        # The body still carries restaurant/table, which must MATCH the session.
        if source != 'admin':
            from restaurants_app.controllers.diner_capability import (
                issue_table_session,
            )
            extra['HTTP_X_DINER_SESSION'] = issue_table_session(table)
        return self._client(user).post(
            '/api/v2/orders/initiate/', body, format='json', **extra,
        )

    def _order_id(self, resp):
        return resp.json()['data']['order_details']['id']

    # --- 1. role-less diner denied -------------------------------------
    def test_diner_admin_source_denied_no_order(self):
        before = Order.objects.count()
        resp = self._initiate(self.diner, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    # --- 2. cross-tenant employee denied -------------------------------
    def test_cross_tenant_employee_admin_source_denied(self):
        # owner_a is an authorized employee of A, but has no access to B.
        resp = self._initiate(self.owner_a, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertFalse(Order.objects.filter(restaurant=self.restaurant_b).exists())

    # --- 3. target staff with MODULE_TABLES allowed, created_by set ----
    def test_target_staff_admin_source_succeeds(self):
        resp = self._initiate(self.staff_b, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=self._order_id(resp))
        self.assertEqual(order.created_by_id, self.staff_b.id)
        self.assertIsNone(order.customer_id)

    # --- 4. gate bypass closed for non-members; staff bypass intact ----
    def test_paused_restaurant_diner_admin_source_denied_no_order(self):
        self.restaurant_b.accepting_orders = False
        self.restaurant_b.save()
        before = Order.objects.count()
        resp = self._initiate(self.diner, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_paused_restaurant_staff_admin_source_still_bypasses(self):
        self.restaurant_b.accepting_orders = False
        self.restaurant_b.save()
        resp = self._initiate(self.staff_b, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Order.objects.get(id=self._order_id(resp)).created_by_id, self.staff_b.id)

    # --- 5. same for menu_only + out_of_service tables -----------------
    def test_menu_only_table_diner_admin_source_denied(self):
        self.table_b.qr_mode = 'menu_only'
        self.table_b.save()
        before = Order.objects.count()
        resp = self._initiate(self.diner, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_menu_only_table_staff_admin_source_still_bypasses(self):
        self.table_b.qr_mode = 'menu_only'
        self.table_b.save()
        resp = self._initiate(self.staff_b, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Order.objects.get(id=self._order_id(resp)).created_by_id, self.staff_b.id)

    def test_out_of_service_table_diner_admin_source_denied(self):
        self.table_b.status = 'out_of_service'
        self.table_b.save()
        before = Order.objects.count()
        resp = self._initiate(self.diner, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_out_of_service_table_staff_admin_source_still_bypasses(self):
        self.table_b.status = 'out_of_service'
        self.table_b.save()
        resp = self._initiate(self.staff_b, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Order.objects.get(id=self._order_id(resp)).created_by_id, self.staff_b.id)

    # --- 6. the legacy platform role reaches no restaurant --------------
    def test_legacy_platform_role_admin_source_denied(self):
        # Was `test_dinify_admin_admin_source_succeeds`: the role string used to
        # authorise source='admin' ordering at ANY restaurant, skipping every diner
        # availability gate. It now 404s like any other non-member, and writes
        # nothing.
        before = Order.objects.count()
        resp = self._initiate(
            self.legacy_role_holder, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    def test_legacy_platform_role_is_indistinguishable_from_a_cross_tenant_employee(self):
        holder = self._initiate(
            self.legacy_role_holder, self.restaurant_b, self.table_b, self.item_b)
        stranger = self._initiate(
            self.owner_a, self.restaurant_b, self.table_b, self.item_b)
        self.assertEqual(holder.status_code, stranger.status_code)
        self.assertEqual(holder.content, stranger.content)

    # --- 7. diner flow unregressed -------------------------------------
    def test_anonymous_diner_initiate_still_works(self):
        resp = self._initiate(None, self.restaurant_a, self.table_a, self.item_a, source=None)
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=self._order_id(resp))
        self.assertIsNone(order.created_by_id)
        self.assertIsNone(order.customer_id)

    def test_authenticated_diner_initiate_sets_customer_not_created_by(self):
        resp = self._initiate(self.diner, self.restaurant_a, self.table_a, self.item_a, source=None)
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(id=self._order_id(resp))
        self.assertIsNone(order.created_by_id)
        self.assertEqual(order.customer_id, self.diner.id)

    def test_diner_still_rejected_at_paused_restaurant(self):
        self.restaurant_a.accepting_orders = False
        self.restaurant_a.save()
        resp = self._initiate(None, self.restaurant_a, self.table_a, self.item_a, source=None)
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_diner_still_rejected_at_menu_only_table(self):
        self.table_a.qr_mode = 'menu_only'
        self.table_a.save()
        resp = self._initiate(None, self.restaurant_a, self.table_a, self.item_a, source=None)
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_diner_still_rejected_at_out_of_service_table(self):
        # An out-of-service table is now rejected EARLIER — at the diner
        # capability layer (the session can't resolve an unavailable table) with a
        # non-disclosing 404 — rather than at the initiate availability gate (400).
        # Either way the diner is blocked and no order is created.
        self.table_a.status = 'out_of_service'
        self.table_a.save()
        before = Order.objects.count()
        resp = self._initiate(None, self.restaurant_a, self.table_a, self.item_a, source=None)
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(Order.objects.count(), before)

    # --- 8. unauthenticated source='admin' → 401 (not 404) -------------
    def test_unauthenticated_admin_source_is_401(self):
        resp = self._initiate(None, self.restaurant_b, self.table_b, self.item_b, source='admin')
        self.assertEqual(resp.status_code, 401, resp.content)


class TestOrderPublicationGate(TestCase):
    """PR 1A (write side): an anonymous diner (created_by is None) may order
    ONLY published, diner-orderable records. An unpublished parent item, an
    unpublished parent SECTION, an item beneath a soft-deleted group, or an
    unpublished extra must fail — atomically, before any Order row is written.
    Malformed ids and foreign-tenant ids keep their existing clean-400
    handling, and the authorised staff/admin path (created_by set) is
    unaffected. available / in_stock are deliberately NOT gated here (they stay
    on the existing zero-and-flag reconciliation)."""

    def setUp(self):
        seed_user()
        seed_restaurant(seed_owner=True)
        seed_menu_section()
        seed_menu_items()
        seed_tables()
        # seed_order() deliberately NOT called → TEST_TABLE_NUMBER4 stays free.
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)
        self.section = MenuSection.objects.get(name=TEST_MENU_SECTION_NAME)
        self.table = Table.objects.get(number=TEST_TABLE_NUMBER4)
        self.staff = User.objects.get(username=TEST_PHONE)
        self.published_item = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

        # Unpublished parent variants in the (published) seeded section.
        self.unapproved_item = MenuItem.objects.create(
            name='Gate Unapproved', section=self.section, primary_price=1000,
            approved=False, enabled=True,
        )
        self.disabled_item = MenuItem.objects.create(
            name='Gate Disabled', section=self.section, primary_price=1000,
            approved=True, enabled=False,
        )
        self.deleted_item = MenuItem.objects.create(
            name='Gate Deleted', section=self.section, primary_price=1000,
            approved=True, enabled=True, deleted=True,
        )

        # A published-looking item under an UNPUBLISHED section.
        self.unapproved_section = MenuSection.objects.create(
            name='Gate Unapproved Section', restaurant=self.restaurant,
            approved=False, enabled=True, available=True,
        )
        self.item_in_unapproved_section = MenuItem.objects.create(
            name='Gate Item In Unapproved Section',
            section=self.unapproved_section, primary_price=1000,
            approved=True, enabled=True,
        )

        # A published-looking item beneath a soft-deleted group.
        self.deleted_group = SectionGroup.objects.create(
            name='Gate Deleted Group', section=self.section,
            approved=True, enabled=True, deleted=True,
        )
        self.item_under_deleted_group = MenuItem.objects.create(
            name='Gate Item Under Deleted Group', section=self.section,
            section_group=self.deleted_group, primary_price=1000,
            approved=True, enabled=True,
        )

        # Extras (published + unpublished).
        self.published_extra = MenuItem.objects.create(
            name='Gate Published Extra', section=self.section, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )
        self.unpublished_extra = MenuItem.objects.create(
            name='Gate Unpublished Extra', section=self.section,
            primary_price=500, approved=False, enabled=False, is_extra=True,
        )
        # The published parent accepts extras and lists ONLY the published extra
        # as applicable. The unpublished extra is not in the allowlist, so it is
        # rejected on both the applicability and the publication axes.
        self.published_item.has_extras = True
        self.published_item.extras_applicable = [str(self.published_extra.pk)]
        self.published_item.save(
            update_fields=['has_extras', 'extras_applicable']
        )

        # A second tenant with its own published item (foreign-tenant control).
        other_owner = User.objects.create_user(
            first_name='Other', last_name='Owner',
            email='gate_other@example.com', phone_number='256700000970',
            username='256700000970', country='Uganda', password='password',
            roles=[],
        )
        self.other_restaurant = Restaurant.objects.create(
            name='Gate Other Tenant', location='gate-other', owner=other_owner,
        )
        other_section = MenuSection.objects.create(
            name='Gate Other Section', restaurant=self.other_restaurant,
            approved=True, enabled=True, available=True,
        )
        self.foreign_item = MenuItem.objects.create(
            name='Gate Foreign Item', section=other_section, primary_price=1000,
            approved=True, enabled=True,
        )

    def _order(self, items, created_by=None):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=items,
            created_by=created_by,
        )

    def _assert_rejected_no_row(self, items, created_by=None):
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        response = self._order(items, created_by=created_by)
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)
        # Atomic: no Order and no OrderItem row committed.
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)
        return response

    # --- 10. happy path ---------------------------------------------------
    def test_published_diner_order_succeeds(self):
        before = Order.objects.count()
        response = self._order(
            [{'item': str(self.published_item.pk), 'quantity': 1}]
        )
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    # --- 11-12 (+). unpublished / deleted parent -------------------------
    def test_unapproved_parent_rejected_no_row(self):
        self._assert_rejected_no_row(
            [{'item': str(self.unapproved_item.pk), 'quantity': 1}]
        )

    def test_disabled_parent_rejected_atomically(self):
        self._assert_rejected_no_row(
            [{'item': str(self.disabled_item.pk), 'quantity': 1}]
        )

    def test_soft_deleted_parent_rejected(self):
        self._assert_rejected_no_row(
            [{'item': str(self.deleted_item.pk), 'quantity': 1}]
        )

    def test_item_in_unapproved_section_rejected(self):
        self._assert_rejected_no_row(
            [{'item': str(self.item_in_unapproved_section.pk), 'quantity': 1}]
        )

    def test_item_under_soft_deleted_group_rejected(self):
        self._assert_rejected_no_row(
            [{'item': str(self.item_under_deleted_group.pk), 'quantity': 1}]
        )

    # --- 13. unpublished extra -------------------------------------------
    def test_unpublished_extra_rejected_atomically(self):
        self._assert_rejected_no_row([{
            'item': str(self.published_item.pk), 'quantity': 1,
            'extras': [str(self.unpublished_extra.pk)],
        }])

    def test_published_extra_accepted(self):
        before = Order.objects.count()
        response = self._order([{
            'item': str(self.published_item.pk), 'quantity': 1,
            'extras': [str(self.published_extra.pk)],
        }])
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    # --- 14. mixed array fails atomically --------------------------------
    def test_mixed_published_and_unpublished_parents_rejected_atomically(self):
        self._assert_rejected_no_row([
            {'item': str(self.published_item.pk), 'quantity': 1},
            {'item': str(self.unapproved_item.pk), 'quantity': 1},
        ])

    # --- 15. foreign tenant ----------------------------------------------
    def test_foreign_tenant_item_rejected(self):
        self._assert_rejected_no_row(
            [{'item': str(self.foreign_item.pk), 'quantity': 1}]
        )

    # --- 16. malformed uuids ---------------------------------------------
    def test_malformed_parent_uuid_rejected(self):
        response = self._order([{'item': 'not-a-uuid', 'quantity': 1}])
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)

    def test_malformed_extra_uuid_rejected(self):
        response = self._order([{
            'item': str(self.published_item.pk), 'quantity': 1,
            'extras': ['not-a-uuid'],
        }])
        self.assertEqual(response['status'], 400)
        self.assertEqual(response['message'], NOT_ON_MENU_MESSAGE)

    # --- 17. authorised staff bypass -------------------------------------
    def test_staff_can_order_unapproved_item(self):
        # Staff/admin (created_by set) keep pre-existing behaviour: an
        # unpublished item is orderable via the authorised management path.
        before = Order.objects.count()
        response = self._order(
            [{'item': str(self.unapproved_item.pk), 'quantity': 1}],
            created_by=self.staff,
        )
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)
