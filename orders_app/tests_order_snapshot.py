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

    def test_a_label_only_edit_landing_mid_read_is_simply_not_observed(self):
        """THE CONTROL THAT USED TO BE THE ARGUED EXCEPTION.

        This test previously patched the separate allergen read by name and
        asserted that a tag edit landing between the two statements moved the
        LABEL and nothing else. That was true of the mechanism then and it
        certified more than it proved: it showed a label-only edit is harmless,
        which is not the same statement as "the snapshot is coherent" — see the
        combined-edit regression below, which the old mechanism failed.

        With the labels folded into the item statement there is no window to land
        in, so the edit is simply not in the snapshot. Kept as a control: a
        mechanism that read labels LATE would show 'Peanuts' here.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        tag = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
            icon='nut', colour='amber',
        )
        for item in self.items:
            item.sync_tag_links([tag.id])

        fired = {'count': 0}

        def rename_the_tag_mid_read(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if fired['count'] == 0 and 'FROM "menu_items"' in sql:
                fired['count'] += 1
                self._commit_on_another_connection(
                    lambda: RestaurantTag.objects.filter(pk=tag.pk).update(
                        name='Peanuts'),
                )
            return result

        real_build = catalogue_snapshot.build_snapshot

        def build_under_the_wrapper(restaurant, items, now):
            with connection.execute_wrapper(rename_the_tag_mid_read):
                return real_build(restaurant, items, now)

        from orders_app.controllers.services import create_order as service
        original = service.build_snapshot
        service.build_snapshot = build_under_the_wrapper
        try:
            response = self._place(2)
        finally:
            service.build_snapshot = original

        self.assertEqual(fired['count'], 1, 'the concurrent edit never ran')
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        rows = list(OrderItem.objects.filter(order=order, parent_item__isnull=True))
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(
                [t['name'] for t in row.allergen_tags_snapshot], ['Nuts'],
                'a label edit committed after the read reached the snapshot',
            )
            self.assertEqual(row.unit_price, D('1000.00'))
            self.assertEqual(row.actual_cost, D('1000.00'))
            self.assertTrue(row.available)
        self.assertEqual(order.actual_cost, D('4000.00'))
        # The edit really did commit.
        self.assertEqual(RestaurantTag.objects.get(pk=tag.pk).name, 'Peanuts')

    def test_a_combined_definition_and_tag_edit_is_never_half_observed(self):
        """THE REGRESSION THE LABEL-ONLY TEST ABOVE DOES NOT COVER.

        The test before this one proves that a LABEL-ONLY edit can move nothing
        but the label — true, and useful, and NOT the same statement as "the
        snapshot is coherent". One operator transaction that changes the dish
        DEFINITION and its allergen LINKS together is the case that matters: a
        reader taking two statements can have that edit commit between them and
        write a line carrying the OLD dish name with the NEW allergen labels — a
        preparation instruction that existed in the catalogue at no instant, and
        the one an allergic diner is handed.

        THE INTERLEAVING IS EXACT, NOT TIMED. A ``connection.execute_wrapper``
        installed around the snapshot fires the competing commit the moment the
        FIRST ``menu_items`` SELECT returns, so the edit lands precisely in the
        window between the phases if a window exists. The seam is the snapshot
        builder, which survives whatever the read is made of — a test that
        patched the second phase by name could not outlive folding the two into
        one statement, which is the fix.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        dish = self.items[0]
        nuts = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
            icon='nut', colour='amber',
        )
        shellfish = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Shellfish', category='allergen',
            icon='shell', colour='blue',
        )
        dish.sync_tag_links([nuts.id])

        def operator_rewrites_the_dish():
            """ONE transaction: the recipe changed, so the name AND the
            allergens changed with it."""
            def mutate():
                from django.db import transaction
                with transaction.atomic():
                    row = MenuItem.objects.select_for_update().get(pk=dish.pk)
                    row.name = 'Dish 0 (shellfish)'
                    row.save(update_fields=['name'])
                    row.sync_tag_links([shellfish.id])
            self._commit_on_another_connection(mutate)

        fired = {'count': 0}

        def fire_between_statements(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if fired['count'] == 0 and 'FROM "menu_items"' in sql:
                fired['count'] += 1
                operator_rewrites_the_dish()
            return result

        real_build = catalogue_snapshot.build_snapshot

        def build_under_the_wrapper(restaurant, items, now):
            with connection.execute_wrapper(fire_between_statements):
                return real_build(restaurant, items, now)

        from orders_app.controllers.services import create_order as service
        original = service.build_snapshot
        service.build_snapshot = build_under_the_wrapper
        try:
            response = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant.pk),
                table_id=str(self.tables[0].pk),
                items=[{'item': str(dish.id), 'quantity': 1}],
            )
        finally:
            service.build_snapshot = original

        self.assertEqual(fired['count'], 1, 'the competing edit never interleaved')
        self.assertEqual(response.get('status'), 200, response)

        order = Order.objects.get(pk=response['data']['order_details']['id'])
        row = OrderItem.objects.get(order=order, parent_item__isnull=True)
        labels = [tag['name'] for tag in (row.allergen_tags_snapshot or [])]

        # COHERENT means the saved line describes ONE committed catalogue state.
        # Either is correct; the hybrid is not.
        self.assertIn(
            (row.item_name_snapshot, tuple(labels)),
            {('Dish 0', ('Nuts',)), ('Dish 0 (shellfish)', ('Shellfish',))},
            f'half-applied snapshot: name={row.item_name_snapshot!r} '
            f'labels={labels!r}',
        )

    def test_an_edit_after_the_snapshot_is_simply_not_in_it(self):
        """The control that keeps the regression above honest.

        With the competing transaction committing AFTER the whole snapshot
        rather than inside it, the line must carry the pre-edit state in both
        fields. A mechanism that passed the regression by reading everything
        LATE would fail here.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        dish = self.items[1]
        nuts = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
            icon='nut', colour='amber',
        )
        sesame = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Sesame', category='allergen',
            icon='seed', colour='tan',
        )
        dish.sync_tag_links([nuts.id])

        real_build = catalogue_snapshot.build_snapshot

        def build_then_let_the_edit_land(restaurant, items, now):
            snapshot = real_build(restaurant, items, now)

            def mutate():
                from django.db import transaction
                with transaction.atomic():
                    row = MenuItem.objects.select_for_update().get(pk=dish.pk)
                    row.name = 'Dish 1 (sesame)'
                    row.save(update_fields=['name'])
                    row.sync_tag_links([sesame.id])
            self._commit_on_another_connection(mutate)
            return snapshot

        from orders_app.controllers.services import create_order as service
        original = service.build_snapshot
        service.build_snapshot = build_then_let_the_edit_land
        try:
            response = ConOrder.initiate_order(
                restaurant_id=str(self.restaurant.pk),
                table_id=str(self.tables[1].pk),
                items=[{'item': str(dish.id), 'quantity': 1}],
            )
        finally:
            service.build_snapshot = original

        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        row = OrderItem.objects.get(order=order, parent_item__isnull=True)
        self.assertEqual(row.item_name_snapshot, 'Dish 1')
        self.assertEqual(
            [tag['name'] for tag in (row.allergen_tags_snapshot or [])], ['Nuts'],
        )
        # And the edit really did commit.
        self.assertEqual(
            MenuItem.objects.get(pk=dish.pk).name, 'Dish 1 (sesame)',
        )


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

    def test_it_is_ONE_statement_however_many_lines(self):
        """The measured breakdown, not a round number.

        It was TWO — one for the items, one batched allergen read — and the
        second is now an aggregate inside the first. The count went DOWN, and
        the flatness assertion is what keeps it from becoming per-line.
        """
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
        self.assertEqual(measure(1), 1)
        self.assertEqual(measure(6), 1, 'the read became per-line')

    def test_the_one_statement_still_carries_the_allergen_labels(self):
        """A single query is only the right answer if it still answers.

        Folding a read away and losing what it read would pass a query-count
        assertion perfectly.
        """
        if not _postgres():
            self.skipTest('requires PostgreSQL')

        from django.utils import timezone
        tag = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
            icon='nut', colour='amber',
        )
        other = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Chef pick', category='promo',
            icon='star', colour='gold',
        )
        self.items[0].sync_tag_links([tag.id, other.id])

        snapshot = catalogue_snapshot.build_snapshot(
            self.restaurant,
            [{'item': str(self.items[0].id), 'quantity': 1},
             {'item': str(self.items[1].id), 'quantity': 1}],
            timezone.localtime(),
        )
        tagged = snapshot.get(self.items[0].pk)
        self.assertEqual(
            tagged.allergen_tags,
            [{'name': 'Nuts', 'icon': 'nut', 'colour': 'amber'}],
            'a non-allergen tag leaked in, or the labels were lost',
        )
        # An item with NO allergen tags aggregates to SQL NULL and must still be
        # present, carrying an empty list rather than None.
        untagged = snapshot.get(self.items[1].pk)
        self.assertIsNotNone(untagged, 'a tagless item fell out of the join')
        self.assertEqual(untagged.allergen_tags, [])
