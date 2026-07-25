"""
Tests for the rebuilt Menu performance report (PR7).

These lock in the corrected semantics over the legacy bugs:
  * only OrderItems in a *sale* order ({served, paid}) are counted — a
    cancelled order's items are excluded (the legacy had no order_status
    filter),
  * revenue is ``Sum('actual_cost')`` — NOT ``total_cost`` (gross),
  * ``quantity_sold`` is ``Sum('quantity')`` and ``order_count`` is the count of
    OrderItem line-item rows in the group,
  * each grouping is ONE grouped query (no per-row N+1, no peak_hours /
    most_ordered_item sub-queries),
  * the ``groups`` grouping excludes items with no section_group,
  * ``average_rating`` is a null scaffold in the ITEMS grouping only.

Distinct money values (``total`` != ``actual``) let the assertions prove revenue
comes from ``actual_cost``, not ``total_cost``. ``time_created`` is
``auto_now_add``, so it is set via ``.update()`` after create; UTC instants are
used (EAT is UTC+3, so 09:00 UTC == 12:00 EAT, same calendar day).
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import (
    Restaurant, Table, MenuSection, SectionGroup, MenuItem,
)
from orders_app.models import Order, OrderItem
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    OrderStatus_Served, OrderStatus_Paid, OrderStatus_Cancelled,
)
from reports_app.controllers.restaurant.menu import (
    generate_restaurant_menu_summary,
)


def make_user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


def utc(year, month, day, hour=9, minute=0):
    """A timezone-aware UTC instant (Order.time_created is stored in UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


class MenuReportBase(TestCase):
    """Shared fixtures + Order / OrderItem seeding helpers.

    Menu tree: sections Mains + Drinks; group Combos (under Mains). Items:
    Burger (Mains / Combos), Fries (Mains, no group), Soda (Drinks, no group).
    """

    def setUp(self):
        self.owner = make_user('256700000500')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

        # A second tenant, to assert restaurant scoping.
        self.other_owner = make_user('256700000501')
        self.restaurant_b = Restaurant.objects.create(
            name='Other Restaurant', location='loc-b',
            status=RestaurantStatus_Live, owner=self.other_owner,
        )
        self.table_b = Table.objects.create(number=1, restaurant=self.restaurant_b)

        self.mains = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, listing_position=0,
        )
        self.drinks = MenuSection.objects.create(
            name='Drinks', restaurant=self.restaurant, listing_position=1,
        )
        self.combos = SectionGroup.objects.create(
            name='Combos', section=self.mains,
        )
        self.burger = MenuItem.objects.create(
            name='Burger', section=self.mains, section_group=self.combos,
            primary_price=Decimal('10.00'), listing_position=0,
        )
        self.fries = MenuItem.objects.create(
            name='Fries', section=self.mains,
            primary_price=Decimal('5.00'), listing_position=1,
        )
        self.soda = MenuItem.objects.create(
            name='Soda', section=self.drinks,
            primary_price=Decimal('3.00'), listing_position=0,
        )

    def make_order(self, status=OrderStatus_Paid, when=None, restaurant=None,
                   table=None):
        order = Order.objects.create(
            restaurant=restaurant or self.restaurant,
            table=table or self.table,
            order_status=status,
            total_cost=Decimal('0.00'), discounted_cost=Decimal('0.00'),
            savings=Decimal('0.00'), actual_cost=Decimal('0.00'),
        )
        if when is not None:
            Order.objects.filter(id=order.id).update(time_created=when)
        return order

    def add_item(self, order, item, quantity=1, total='100.00', actual='75.00',
                 parent_item=None):
        # Distinct total vs actual so revenue assertions can tell the columns
        # apart. discounted_cost mirrors actual; savings is the difference.
        total_d = Decimal(total)
        actual_d = Decimal(actual)
        return OrderItem.objects.create(
            order=order, item=item, quantity=quantity,
            unit_price=Decimal('10.00'), discounted_price=Decimal('10.00'),
            total_cost=total_d, discounted_cost=actual_d,
            savings=total_d - actual_d, actual_cost=actual_d,
            parent_item=parent_item,
        )

    def summary(self, grouping, date_from='2024-01-10', date_to='2024-01-10'):
        return generate_restaurant_menu_summary(
            restaurant_id=self.restaurant.id, grouping=grouping,
            date_from=date_from, date_to=date_to,
        )


class MenuSummaryShapeTests(MenuReportBase):

    def test_envelope_echoes_grouping_and_returns_rows(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger)

        result = self.summary('sections')
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['message'],
                         'Successfully retrieved the menu summary')
        self.assertEqual(result['data']['grouping'], 'sections')
        self.assertIsInstance(result['data']['rows'], list)

    def test_aggregates_use_actual_cost_quantity_and_row_count(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        # Two Mains line items in one order.
        self.add_item(order, self.burger, quantity=2, total='100.00', actual='75.00')
        self.add_item(order, self.fries, quantity=3, total='200.00', actual='50.00')

        data = self.summary('sections')['data']

        self.assertEqual(len(data['rows']), 1)  # only Mains
        row = data['rows'][0]
        self.assertEqual(row['name'], 'Mains')
        # order_count == number of line-item rows, not distinct orders.
        self.assertEqual(row['order_count'], 2)
        # quantity_sold == Sum(quantity) == 2 + 3.
        self.assertEqual(row['quantity_sold'], 5)
        # revenue == Sum(actual_cost) == 75 + 50 — NOT total_cost (100 + 200).
        self.assertEqual(row['revenue'], Decimal('125.00'))
        self.assertNotEqual(row['revenue'], Decimal('300.00'))

    def test_rows_ordered_by_quantity_sold_desc(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger, quantity=2)
        self.add_item(order, self.fries, quantity=5)
        self.add_item(order, self.soda, quantity=1)

        data = self.summary('items')['data']

        self.assertEqual([r['name'] for r in data['rows']],
                         ['Fries', 'Burger', 'Soda'])
        self.assertEqual([r['quantity_sold'] for r in data['rows']], [5, 2, 1])

    def test_empty_range_returns_empty_rows(self):
        data = self.summary('sections')['data']
        self.assertEqual(data['grouping'], 'sections')
        self.assertEqual(data['rows'], [])

    def test_invalid_grouping_returns_400(self):
        result = self.summary('fortnightly')
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], 'Invalid grouping')

    def test_range_over_31_days_is_allowed(self):
        # The 31-day cap was relaxed: this aggregate-only report must render at
        # any range (e.g. a full year). One served order mid-range gives every
        # grouping a row, proving data still aggregates over the long span and
        # the output stays bounded by menu size, not range length.
        order = self.make_order(OrderStatus_Served, when=utc(2024, 6, 15))
        self.add_item(order, self.burger, quantity=2)  # Mains / Combos / Burger

        expected_name = {
            'sections': 'Mains', 'groups': 'Combos', 'items': 'Burger',
        }
        for grouping, name in expected_name.items():
            result = self.summary(grouping,
                                  date_from='2024-01-01', date_to='2024-12-31')
            self.assertEqual(result['status'], 200)
            rows = result['data']['rows']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], name)
            self.assertEqual(rows[0]['quantity_sold'], 2)


class MenuSummarySaleSetTests(MenuReportBase):

    def test_cancelled_order_items_excluded(self):
        sale = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(sale, self.burger, quantity=1)
        # A cancelled (non-sale) order with a large quantity that must NOT leak.
        cancelled = self.make_order(OrderStatus_Cancelled, when=utc(2024, 1, 10))
        self.add_item(cancelled, self.burger, quantity=9)

        data = self.summary('items')['data']

        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['name'], 'Burger')
        self.assertEqual(data['rows'][0]['quantity_sold'], 1)  # not 1 + 9

    def test_paid_not_served_order_is_counted(self):
        order = self.make_order(OrderStatus_Paid, when=utc(2024, 1, 10))
        self.add_item(order, self.burger, quantity=4)

        data = self.summary('items')['data']

        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['quantity_sold'], 4)

    def test_restaurant_scoped(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger, quantity=2)

        # Another restaurant's order on the same day must not bleed in.
        section_b = MenuSection.objects.create(
            name='B-Mains', restaurant=self.restaurant_b, listing_position=0,
        )
        item_b = MenuItem.objects.create(
            name='B-Burger', section=section_b,
            primary_price=Decimal('10.00'), listing_position=0,
        )
        order_b = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10),
                                  restaurant=self.restaurant_b, table=self.table_b)
        self.add_item(order_b, item_b, quantity=7)

        data = self.summary('items')['data']

        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['name'], 'Burger')
        self.assertEqual(data['rows'][0]['quantity_sold'], 2)

    def test_extras_count_as_their_own_rows(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        # Parent line item + an extra (own item FK, own actual_cost, parent_item
        # set). Extras are NOT folded into the parent's cost, so each is its own
        # revenue row and there is no double-count.
        parent = self.add_item(order, self.burger, quantity=1, actual='75.00')
        self.add_item(order, self.soda, quantity=1, actual='25.00',
                      parent_item=parent)

        rows = {r['name']: r for r in self.summary('items')['data']['rows']}

        self.assertEqual(set(rows), {'Burger', 'Soda'})
        self.assertEqual(rows['Burger']['revenue'], Decimal('75.00'))
        self.assertEqual(rows['Soda']['revenue'], Decimal('25.00'))


class MenuSummaryGroupingTests(MenuReportBase):

    def test_groups_grouping_excludes_null_section_group(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger)  # Combos group
        self.add_item(order, self.fries)   # Mains, no group
        self.add_item(order, self.soda)    # Drinks, no group

        data = self.summary('groups')['data']

        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['name'], 'Combos')
        self.assertEqual(data['rows'][0]['order_count'], 1)

    def test_sections_grouping_splits_by_section(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger, quantity=1)  # Mains
        self.add_item(order, self.fries, quantity=1)   # Mains
        self.add_item(order, self.soda, quantity=4)    # Drinks

        rows = {r['name']: r for r in self.summary('sections')['data']['rows']}

        self.assertEqual(set(rows), {'Mains', 'Drinks'})
        self.assertEqual(rows['Mains']['order_count'], 2)
        self.assertEqual(rows['Drinks']['quantity_sold'], 4)


class MenuSummaryAverageRatingScaffoldTests(MenuReportBase):

    def test_items_grouping_has_null_average_rating_and_no_legacy_keys(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger)

        row = self.summary('items')['data']['rows'][0]

        self.assertIn('average_rating', row)
        self.assertIsNone(row['average_rating'])
        # The dropped legacy per-row sub-query columns are gone.
        self.assertNotIn('peak_hours', row)
        self.assertNotIn('most_order_item', row)
        self.assertNotIn('most_ordered_item', row)

    def test_sections_and_groups_have_no_average_rating(self):
        order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10))
        self.add_item(order, self.burger)

        for grouping in ('sections', 'groups'):
            row = self.summary(grouping)['data']['rows'][0]
            self.assertNotIn('average_rating', row)


class MenuSummaryQueryCountTests(MenuReportBase):

    def test_each_grouping_is_a_single_grouped_query(self):
        # Several orders, each with several line items. If the legacy per-row
        # N+1 were back, this would be many queries per grouping.
        for i in range(3):
            order = self.make_order(OrderStatus_Served, when=utc(2024, 1, 10, 8 + i))
            self.add_item(order, self.burger, quantity=1)
            self.add_item(order, self.fries, quantity=1)
            self.add_item(order, self.soda, quantity=1)

        for grouping in ('sections', 'groups', 'items'):
            with self.assertNumQueries(1):
                self.summary(grouping)
