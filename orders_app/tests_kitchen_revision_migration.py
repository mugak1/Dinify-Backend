"""
D05 — the revision column's SCHEMA contract.

The expand-only rule means a rollback lands OLD CODE ON NEW SCHEMA, so what the
DATABASE does when a writer never mentions this column is the thing that has to
be right. These tests exercise the column as PostgreSQL actually holds it, not
as Django describes it.
"""
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from orders_app.models import Order
from orders_app.controllers.services import kitchen_transition as kt
from restaurants_app.models import DiningArea, Restaurant, Table
from users_app.models import User
from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, PaymentStatus_Pending, RestaurantStatus_Live,
)


class RevisionColumnTests(TestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('the column contract is a PostgreSQL fact')
        self.owner = User.objects.create_user(
            first_name='O', last_name='W', email='o-d05mig@test.com',
            phone_number='256900030001', username='256900030001',
            country='Uganda', password='pw', roles=[])
        self.restaurant = Restaurant.objects.create(
            name='D05 Migration', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live, country='UG')
        self.area = DiningArea.objects.create(
            restaurant=self.restaurant, name='Main')
        self.table = Table.objects.create(
            restaurant=self.restaurant, dining_area=self.area, number=1)

    def test_the_column_carries_a_real_database_default(self):
        """`db_default`, not just `default`.

        Django manages defaults in Python: `AddField` adds the column with a
        default and immediately DROPS it, leaving a NOT NULL column with no
        database default. That is fine until a rollback puts OLD CODE on the NEW
        schema — and old code INSERTs orders without naming this column.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'orders' AND column_name = 'fulfilment_revision'")
            row = cursor.fetchone()
        self.assertIsNotNone(row, 'the column is missing')
        default, nullable = row
        self.assertEqual(nullable, 'NO')
        self.assertIsNotNone(
            default, 'no database default: a rolled-back writer would violate NOT NULL')
        self.assertIn('0', default)

    def test_an_insert_that_never_names_the_column_succeeds_at_the_baseline(self):
        """THE ROLLBACK CASE, exercised rather than reasoned about: raw SQL that
        does not mention the column at all, exactly as older code would."""
        import uuid
        order_id = uuid.uuid4()
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO orders ('
                '  id, time_created, time_last_updated, deleted, archived,'
                '  vacuumed, restaurant_id, table_id, total_cost, discounted_cost,'
                '  savings, actual_cost, prepayment_required, total_paid,'
                '  balance_payable, payment_status, order_status, order_source,'
                '  customer_match_attempted, is_test, pricing_version,'
                '  fulfilment_status, priority'
                ') VALUES ('
                '  %s, %s, %s, false, false, false, %s, %s, 0, 0, 0, 0, false, 0, 0,'
                '  %s, %s, %s, false, false, 0, %s, false)',
                [order_id, now, now, self.restaurant.pk, self.table.pk,
                 PaymentStatus_Pending, OrderStatus_Pending,
                 'diner_self_service', 'new'])
        saved = Order.objects.get(pk=order_id)
        self.assertEqual(saved.fulfilment_revision, 0)

    def test_zero_is_the_adoption_baseline_and_claims_nothing(self):
        """A pre-D05 row starts at 0. That is a starting point for
        compare-and-set, NOT a claim that nothing has ever happened to the order
        and NOT evidence that it was accepted."""
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=OrderStatus_Pending, payment_status=PaymentStatus_Pending,
            fulfilment_status='ready', order_date=timezone.localdate())
        self.assertEqual(order.fulfilment_revision, 0)
        # It is usable immediately as a precondition — no backfill required.
        from orders_app.models import OrderItem
        from restaurants_app.models import MenuItem, MenuSection
        section = MenuSection.objects.create(
            restaurant=self.restaurant, name='S', approved=True, enabled=True,
            available=True)
        item = MenuItem.objects.create(
            section=section, name='D', primary_price=1, available=True,
            in_stock=True, approved=True, enabled=True)
        OrderItem.objects.create(
            order=order, item=item, quantity=1, available=True, unit_price=0,
            discounted_price=0, unit_cost_of_options=0, total_cost=0,
            discounted_cost=0, savings=0, cost_of_options=0, actual_cost=0,
            item_name_snapshot='D')
        from restaurants_app.models import RestaurantEmployee
        from dinify_backend.configss.string_definitions import RESTAURANT_OWNER
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        result = kt.execute(
            order.pk, self.owner,
            kt.KitchenCommand(action=kt.ACTION_SERVE, if_revision=0))
        self.assertEqual(result['outcome'], 'applied')
        self.assertEqual(result['state']['fulfilment_revision'], 1)

    def test_the_field_is_absent_from_every_write_serializer(self):
        """Server-owned, like `is_test` and `pricing_version`: protected by
        ABSENCE rather than by a read_only flag somebody can remove."""
        from dinify_backend.configss.edit_information import EDIT_INFORMATION
        for section, fields in EDIT_INFORMATION.items():
            keys = {f['key'] for f in fields}
            self.assertNotIn('fulfilment_revision', keys, msg=section)

    def test_the_revision_is_not_part_of_the_quote_fingerprint(self):
        """The diner's reference must not move when the kitchen acts. Asserted
        against the fingerprint's SOURCE, so a future field added there is
        caught rather than assumed safe."""
        import inspect
        from orders_app.controllers.services import order_quote
        source = inspect.getsource(order_quote)
        self.assertNotIn('fulfilment_revision', source)
