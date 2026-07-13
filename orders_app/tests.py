from uuid import uuid4
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.db.models.deletion import ProtectedError
from rest_framework.test import APIClient

from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from orders_app.controllers.con_orders import (
    ConOrder, handle_add_order_items, NOT_ON_MENU_MESSAGE,
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
from restaurants_app.models import Restaurant, Table, MenuItem, MenuSection
from dinify_backend.configss.messages import OK_ORDER_UPDATED
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, OrderStatus_Pending,
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
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        discounted = MenuItem.objects.get(name=TEST_DISCOUNTED_MENU_ITEM_NAME)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        # first attempt has the options item with no selected_modifiers → rejected
        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {
                'item': str(options_item.pk),
                'quantity': 1,
                'extras': [str(menu_item1.pk), str(menu_item2.pk)],
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
            'extras': [str(menu_item1.pk), str(menu_item2.pk)],
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

    def test_handle_add_order_items(self):
        order_record = Order.objects.get(
            table=Table.objects.get(number=TEST_TABLE_NUMBER3)
        )
        old_total_cost = order_record.total_cost

        menu_item1 = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)
        menu_item2 = MenuItem.objects.get(name=TEST_MENU_ITEM2_NAME)
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        items = [
            {'item': str(menu_item1.pk), 'quantity': 2},
            {
                'item': str(options_item.pk),
                'quantity': 1,
                'selected_modifiers': {TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
                'extras': [str(menu_item1.pk), str(menu_item2.pk)],
            },
        ]
        response = handle_add_order_items(order_id=str(order_record.pk), items=items)
        self.assertEqual(response['status'], 200)

        order_record.refresh_from_db()
        self.assertGreater(order_record.total_cost, old_total_cost)

    def test_add_order_item_merges_when_same_item_has_two_lines(self):
        # BUG-P2-5 regression: the same menu item can legitimately sit on an order
        # as two lines (e.g. Small vs Large). The bump path used to re-look-up the
        # line with OrderItem.objects.get(order, item) — non-unique here, so it
        # raised MultipleObjectsReturned -> 500. It must now bump the matched line
        # in place and never crash.
        order = Order.objects.get(table=Table.objects.get(number=TEST_TABLE_NUMBER3))
        options_item = MenuItem.objects.get(name=TEST_OPTION_MENU_ITEM_NAME)

        line_defaults = dict(
            quantity=1,
            unit_price=Decimal('10.00'),
            discounted_price=Decimal('10.00'),
            unit_cost_of_options=Decimal('0.00'),
            total_cost=Decimal('10.00'),
            discounted_cost=Decimal('10.00'),
            savings=Decimal('0.00'),
            cost_of_options=Decimal('0.00'),
            actual_cost=Decimal('10.00'),
        )
        small_line = OrderItem.objects.create(
            order=order, item=options_item,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_SMALL_ID]},
            **line_defaults,
        )
        large_line = OrderItem.objects.create(
            order=order, item=options_item,
            selected_modifiers={TEST_OPTION_GROUP_ID: [TEST_OPTION_CHOICE_LARGE_ID]},
            **line_defaults,
        )
        # The matcher inspects existing_items[0], ordered by '-time_created'. Pin
        # the Small line as the most recent so it is deterministically [0] — the
        # row the incoming Small selection must merge onto.
        now = timezone.now()
        OrderItem.objects.filter(pk=large_line.pk).update(
            time_created=now - timedelta(minutes=1)
        )
        OrderItem.objects.filter(pk=small_line.pk).update(time_created=now)
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
        )
        self.assertEqual(response.status_code, 200)
        return str(response.json()['data']['order_details']['id'])

    def test_anonymous_diner_initiate_then_submit_succeeds(self):
        order_id = self._initiate_anonymous_order()

        response = self.client.put(
            '/api/v1/orders/submit/',
            {'order': order_id},
            format='json',
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
        # .get(id=None), rather than surfacing a 500.
        response = self.client.put('/api/v1/orders/submit/', {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_submit_malformed_order_id_returns_400(self):
        # A non-UUID order id raises ValidationError in the ORM lookup and is
        # converted to a clean 400, not a 500.
        response = self.client.put(
            '/api/v1/orders/submit/', {'order': 'not-a-uuid'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_submit_nonexistent_order_id_returns_404(self):
        # A well-formed but unknown order id raises DoesNotExist -> 404, not 500.
        response = self.client.put(
            '/api/v1/orders/submit/', {'order': str(uuid4())}, format='json',
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
                'extras': [str(self.item_a2.pk)],
            }],
        )
        self.assertEqual(response['status'], 200)
        self.assertEqual(Order.objects.count(), orders_before + 1)
        order_id = str(response['data']['order_details']['id'])
        parent = OrderItem.objects.get(
            order__id=order_id, item=self.item_a, parent_item__isnull=True
        )
        child = OrderItem.objects.get(
            order__id=order_id, item=self.item_a2, parent_item__isnull=False
        )
        self.assertEqual(child.parent_item_id, parent.pk)
        self.assertEqual(child.quantity, 1)
        # priced server-side from A's menu item, never from client input
        self.assertEqual(child.unit_price, Decimal('1000.00'))
        self.assertEqual(child.actual_cost, Decimal('1000.00'))

    def test_chokepoint_rolls_back_whole_transaction(self):
        # Drive the service directly (bypassing initiate_order's batch gate)
        # so the rejection fires INSIDE the transaction at the SECOND item's
        # extras: the first, valid item's already-written rows must unwind too.
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
                'extras': [str(self.item_a2.pk)],
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
                    'extras': [str(self.item_a2.pk)]}],
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

    def test_duplicate_extras_accepted_as_two_child_rows(self):
        # The set-based batch gate must validate duplicates without collapsing
        # them: two occurrences of the same valid extra still produce two
        # child rows (pre-existing duplicate semantics are unchanged).
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant_a.pk),
            table_id=str(self.table_a.pk),
            items=[{
                'item': str(self.item_a.pk),
                'quantity': 1,
                'extras': [str(self.item_a2.pk), str(self.item_a2.pk)],
            }],
        )
        self.assertEqual(response['status'], 200)
        order_id = str(response['data']['order_details']['id'])
        self.assertEqual(
            OrderItem.objects.filter(
                order__id=order_id, item=self.item_a2, parent_item__isnull=False
            ).count(),
            2,
        )


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
        # A plain parent item: check_extras_requirements only enforces min/max
        # when has_extras=True, so no extras config is needed to carry an extra.
        self.parent = MenuItem.objects.get(name=TEST_MENU_ITEM1_NAME)

    def _make_extra(self, name, discount_details):
        # discounted_price is set to a stale 8000 on purpose — neither the flag
        # nor the charge reads that stored column; the window predicate decides
        # both (mirrors TestDiscountActivationPricing._make_item).
        return MenuItem.objects.create(
            name=name,
            section=self.section,
            primary_price=Decimal('10000'),
            discounted_price=Decimal('8000'),
            running_discount=True,
            consider_discount_object=True,
            discount_details=discount_details,
        )

    def _place_order_with_extra(self, extra_item):
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
            primary_price=Decimal('5000'),
            running_discount=False,
        )
        row = self._place_order_with_extra(extra)

        self.assertFalse(row.discounted)
        self.assertEqual(row.actual_cost, Decimal('5000.00'))
        self.assertEqual(row.savings, Decimal('0.00'))
        self.assertFalse(serialize_order_item_details(item=row)['discounted'])
