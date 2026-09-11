"""
D02 — ONE coherent catalogue read and ONE pricing instant.

A COUNT OF CLOCK CALLS IS NOT EVIDENCE. These are real concurrent edits,
committed from a second database connection at the exact point between what used
to be the separate read phases, with the order build parked there. Under READ
COMMITTED each of the old statements took its own snapshot, so an edit landing
between them could have validation, canonicalisation and pricing reading three
different versions of one row.

``TransactionTestCase`` and PostgreSQL are required: the edit must genuinely
COMMIT on another connection, which a wrapped ``TestCase`` cannot express.
"""
import threading
from decimal import Decimal

from django.db import connection, connections
from django.test import TransactionTestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.services import catalogue_snapshot
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantTag, Table,
)
from users_app.models import User

D = Decimal


def _postgres():
    return connection.vendor == 'postgresql'


class SnapshotCoherenceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='S', last_name='O', email='s@t.com',
            phone_number='256700066001', username='256700066001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Snap R', location='sr', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.items = [
            MenuItem.objects.create(
                name=f'Dish {n}', section=self.section, primary_price=D('1000'),
                approved=True, enabled=True, available=True,
            )
            for n in range(4)
        ]
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant, qr_mode='order_pay')
            for n in range(1, 4)
        ]

    def _lines(self):
        return [{'item': str(item.id), 'quantity': 1} for item in self.items]

    def _commit_on_another_connection(self, mutate):
        """Run ``mutate`` on a SEPARATE connection and commit it."""
        done = threading.Event()
        error = {}

        def worker():
            try:
                mutate()
            except Exception as exc:                # pragma: no cover
                error['exc'] = exc
            finally:
                connections.close_all()
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(done.wait(timeout=20), 'the concurrent edit never ran')
        thread.join(timeout=20)
        if 'exc' in error:
            raise error['exc']

    def _place(self, table_index=0):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.tables[table_index].pk),
            items=self._lines(),
        )

    def test_a_price_change_between_the_old_read_phases_cannot_split_an_order(self):
        """THE DEFECT THIS CLOSES, reproduced as a real committed write.

        The build is parked immediately AFTER the catalogue snapshot is taken.
        A price change then commits from another connection. Every line must be
        priced from the snapshot — at the ONE version the order was admitted
        against — so all four lines carry the pre-edit price and the order's
        total is exactly four of it. Before the snapshot, ``add_order_item``
        re-read the row per line and the later lines would have taken the new
        price.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        real_build = catalogue_snapshot.build_snapshot

        def build_then_let_an_edit_land(restaurant, items, now):
            snapshot = real_build(restaurant, items, now)
            self._commit_on_another_connection(lambda: MenuItem.objects.filter(
                pk__in=[i.pk for i in self.items],
            ).update(primary_price=D('9999')))
            return snapshot

        from orders_app.controllers.services import create_order as service
        original = service.build_snapshot
        service.build_snapshot = build_then_let_an_edit_land
        try:
            response = self._place(0)
        finally:
            service.build_snapshot = original

        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = OrderItem.objects.filter(order=order, parent_item__isnull=True)
        self.assertEqual(rows.count(), 4)
        for row in rows:
            self.assertEqual(row.unit_price, D('1000.00'))
            self.assertEqual(row.actual_cost, D('1000.00'))
        self.assertEqual(order.actual_cost, D('4000.00'))
        # The edit really did commit.
        self.assertEqual(
            MenuItem.objects.get(pk=self.items[0].pk).primary_price, D('9999.00'),
        )

    def test_an_availability_change_after_the_snapshot_does_not_split_an_order(self):
        """Deliverability is decided from the snapshot too, so the same order
        cannot call one line available and the next sold out for the same
        concurrent edit."""
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        real_build = catalogue_snapshot.build_snapshot

        def build_then_sell_out(restaurant, items, now):
            snapshot = real_build(restaurant, items, now)
            self._commit_on_another_connection(lambda: MenuItem.objects.filter(
                pk__in=[i.pk for i in self.items],
            ).update(in_stock=False))
            return snapshot

        from orders_app.controllers.services import create_order as service
        original = service.build_snapshot
        service.build_snapshot = build_then_sell_out
        try:
            response = self._place(1)
        finally:
            service.build_snapshot = original

        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = list(OrderItem.objects.filter(order=order, parent_item__isnull=True))
        self.assertEqual(len(rows), 4)
        self.assertEqual({row.available for row in rows}, {True},
                         'every line saw the SAME committed availability')
        self.assertEqual(order.actual_cost, D('4000.00'))

    def test_a_tag_edit_between_the_two_statements_changes_only_the_labels(self):
        """THE ARGUED EXCEPTION, exercised rather than asserted in prose.

        The allergen labels come from a SECOND statement. A tag edit committing
        between the two can change only the label text written onto the line —
        exactly the field it describes — and cannot move an amount, a merge
        decision or a deliverability flag.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        tag = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
            icon='nut', colour='amber',
        )
        for item in self.items:
            item.sync_tag_links([tag.id])

        real_tags = catalogue_snapshot._allergen_tags

        def edit_then_read(rows):
            self._commit_on_another_connection(lambda: RestaurantTag.objects.filter(
                pk=tag.pk,
            ).update(name='Peanuts'))
            return real_tags(rows)

        catalogue_snapshot._allergen_tags = edit_then_read
        try:
            response = self._place(2)
        finally:
            catalogue_snapshot._allergen_tags = real_tags

        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = list(OrderItem.objects.filter(order=order, parent_item__isnull=True))
        self.assertEqual(len(rows), 4)
        # The labels moved with the edit — that is the field the second
        # statement owns — and NOTHING ELSE did.
        for row in rows:
            self.assertEqual(
                [t['name'] for t in row.allergen_tags_snapshot], ['Peanuts'],
            )
            self.assertEqual(row.unit_price, D('1000.00'))
            self.assertEqual(row.actual_cost, D('1000.00'))
            self.assertTrue(row.available)
        self.assertEqual(order.actual_cost, D('4000.00'))


class SnapshotReadShapeTests(TransactionTestCase):
    """The snapshot's statement inventory, asserted rather than described."""

    def setUp(self):
        owner = User.objects.create_user(
            first_name='S', last_name='R', email='sr@t.com',
            phone_number='256700066002', username='256700066002',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Shape R', location='shr', owner=owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.items = [
            MenuItem.objects.create(
                name=f'D{n}', section=self.section, primary_price=D('100'),
                approved=True, enabled=True, available=True,
            )
            for n in range(6)
        ]

    def test_it_is_two_statements_however_many_lines(self):
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone

        def measure(count):
            lines = [{'item': str(i.id), 'quantity': 1}
                     for i in self.items[:count]]
            with CaptureQueriesContext(connection) as captured:
                catalogue_snapshot.build_snapshot(
                    self.restaurant, lines, timezone.localtime(),
                )
            return len(captured.captured_queries)

        measure(1)                       # warm
        self.assertEqual(measure(1), 2)
        self.assertEqual(measure(6), 2, 'the read does not grow with the order')

    def test_it_joins_the_group_and_the_group_s_own_section(self):
        """``group_operationally_visible`` dereferences the GROUP's section — a
        lazy load that is easy to miss and would be a read outside the
        snapshot. Touching every publication attribute must issue no query."""
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from restaurants_app.models import SectionGroup

        group = SectionGroup.objects.create(
            name='G', section=self.section, approved=True, enabled=True,
            available=True,
        )
        for item in self.items:
            item.section_group = group
            item.save(update_fields=['section_group'])

        lines = [{'item': str(i.id), 'quantity': 1} for i in self.items]
        snapshot = catalogue_snapshot.build_snapshot(
            self.restaurant, lines, timezone.localtime(),
        )
        with CaptureQueriesContext(connection) as captured:
            for item in self.items:
                resolved = snapshot.get(item.id)
                _ = resolved.menu_item.section.available
                _ = resolved.menu_item.section_group.available
                _ = resolved.menu_item.section_group.section.available
                _ = resolved.verdict.usable
                _ = resolved.allergen_tags
        self.assertEqual(len(captured.captured_queries), 0, [
            q['sql'] for q in captured.captured_queries
        ])

    def test_it_takes_no_row_lock_on_the_catalogue(self):
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone

        lines = [{'item': str(i.id), 'quantity': 1} for i in self.items]
        with CaptureQueriesContext(connection) as captured:
            catalogue_snapshot.build_snapshot(
                self.restaurant, lines, timezone.localtime(),
            )
        for query in captured.captured_queries:
            self.assertNotIn('FOR UPDATE', query['sql'].upper())
            self.assertNotIn('FOR SHARE', query['sql'].upper())

    def test_it_is_restaurant_scoped(self):
        """The snapshot is the per-line tenant guard, so it must not resolve a
        foreign id — that is what lets add_order_item read from it."""
        from django.utils import timezone

        other_owner = User.objects.create_user(
            first_name='O', last_name='O', email='oo@t.com',
            phone_number='256700066003', username='256700066003',
            country='Uganda', password='password', roles=[],
        )
        other = Restaurant.objects.create(
            name='Other', location='o', owner=other_owner,
            status=RestaurantStatus_Live,
        )
        other_section = MenuSection.objects.create(
            name='OS', restaurant=other, approved=True, enabled=True,
        )
        foreign = MenuItem.objects.create(
            name='Foreign', section=other_section, primary_price=D('100'),
            approved=True, enabled=True,
        )
        snapshot = catalogue_snapshot.build_snapshot(
            self.restaurant,
            [{'item': str(foreign.id), 'quantity': 1}],
            timezone.localtime(),
        )
        self.assertIsNone(snapshot.get(foreign.id))
