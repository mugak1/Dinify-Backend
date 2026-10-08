"""
D12 B2 — deleting a catalogue row must not delete the order lines that name it.

WHAT THIS PINS. ``OrderItem.item`` was ``on_delete=CASCADE``, so an ORM delete
of a menu item that an order had bought deleted that order's lines with it. So
did deleting the item's section group or section, whose own CASCADE edges reach
the item. The order row and its ``OrderAcceptance`` survived, which made it
worse: the order still stated what the diner paid while the lines that made up
that amount were gone, a saved draft's recomputed ``quote_ref`` moved (so its
next submit was refused as stale), and a deleted parent line orphaned its extras
(``parent_item`` is SET_NULL), which then read as main dishes. ``OrderItem.item``
is now PROTECT: the collector raises ``ProtectedError`` while it is still
collecting, before any statement is written.

WHAT THIS DOES NOT PIN, deliberately. Deleting an ORDER or a LINE directly is a
different operation and keeps working (``order`` is CASCADE and ``parent_item``
is SET_NULL, both unchanged). Raw SQL bypasses the collector and meets the
database constraint, which this change does not alter. Code rolled back to
CASCADE brings the old behaviour back. The supported HTTP deletion path is a
soft delete with an inline rename and never reaches the collector, so its
responses are unchanged; one control pins that.

THREE KINDS OF TEST:
  * ``REGRESSION`` — behaviour the CASCADE edge violated.
  * ``CONTROL`` — what held before and must still hold.
  * ``STRUCTURE`` — the relation and migration shape this depends on.

The fixture is the accepted B1 fixture (``_HistoryFixture``): a real order
placed through the diner endpoints and served by the kitchen, plus a DRAFT at a
second table whose reviewed quote has not yet been submitted. Exception message
text is never asserted; it names the outer cascade edge, not the line.

PostgreSQL is required, as for every other order-path test.
"""
import importlib
import json
import uuid

from django.db import connection, models, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.purchase_integrity import (
    REASON_PURCHASE_NEEDS_REVIEW,
)
from orders_app.models import (
    Order, OrderAcceptance, OrderItem, OrderQuoteClosure,
)
from orders_app.tests_order_history_names import (
    SUBMIT_URL, _HistoryFixture, decode_wire,
)
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.models import MenuItem, MenuSection, SectionGroup

WRITE_VERBS = ('INSERT', 'UPDATE', 'DELETE')


class _RetentionFixture(_HistoryFixture):
    """An accepted, served order and an unsubmitted draft, both naming every
    catalogue row the fixture holds."""

    def lines(self):
        # Frozen at first use: a successful ``Model.delete()`` sets
        # ``instance.pk = None``, and rebuilding the request from the fixture's
        # instances afterwards would send ``"None"`` as an item id. That only
        # happens when a deletion SUCCEEDS, i.e. in a CASCADE mutation run, and
        # there the replay must fail on the history, not on a malformed body.
        if not hasattr(self, '_frozen_lines'):
            self._frozen_lines = super().lines()
        return json.loads(json.dumps(self._frozen_lines))

    def setUp(self):
        super().setUp()
        self.order, self.table, self.coid = self.place()
        self.draft_table = self.tables[1]
        self.draft_coid = str(uuid.uuid4())
        response = self.initiate(self.draft_table, self.draft_coid)
        self.assertEqual(response.status_code, 200, response.content[:300])
        details = decode_wire(response)['data']['order_details']
        self.draft = Order.objects.get(pk=details['id'])
        self.draft_quote_ref = details['quote_ref']
        # Premises the scenarios rely on, checked rather than assumed.
        self.assertEqual(self.draft.order_status, 'initiated')
        self.assertTrue(OrderAcceptance.objects.filter(order=self.order).exists())
        for order in (self.order, self.draft):
            self.assertEqual(OrderItem._base_manager.filter(order=order).count(), 5)
            bacon = OrderItem._base_manager.get(order=order, item=self.bacon)
            self.assertEqual(bacon.quantity, 0, 'the sold-out extra is a qty-0 line')
            self.assertIsNotNone(bacon.parent_item_id)

    # ------------------------------------------------------------ the state
    def catalogue_state(self):
        sections = MenuSection._base_manager.filter(restaurant=self.restaurant)
        return {
            'sections': list(sections.order_by('id').values()),
            'groups': list(SectionGroup._base_manager
                           .filter(section__in=sections).order_by('id').values()),
            'items': list(MenuItem._base_manager
                          .filter(section__in=sections).order_by('id').values()),
        }

    def history_state(self):
        """Every stored column of both orders, their lines, their acceptance
        and closure rows, and the quote reference recomputed from the lines."""
        out = {}
        for label, order in (('accepted', self.order), ('draft', self.draft)):
            row = Order._base_manager.filter(pk=order.pk)
            out[label] = {
                'order': list(row.values()),
                'lines': list(OrderItem._base_manager.filter(order=order)
                              .order_by('id').values()),
                'acceptance': list(OrderAcceptance.objects
                                   .filter(order=order).values()),
                'closure': list(OrderQuoteClosure.objects
                                .filter(order=order).values()),
                'quote_ref': (quote_ref(row.get()) if row.exists() else None),
            }
        return out

    def referencing_lines(self, items):
        return {('orders_app.OrderItem', pk) for pk in OrderItem._base_manager
                .filter(item__in=items).values_list('pk', flat=True)}

    # --------------------------------------------------------- the attempt
    def refused(self, delete, protected_items):
        """Run one catalogue deletion and assert that it was refused before
        anything was written, naming exactly the lines that reference it."""
        expected = self.referencing_lines(protected_items)
        self.assertTrue(expected, 'premise: some order line names the target')
        catalogue, history = self.catalogue_state(), self.history_state()
        with CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(ProtectedError) as caught:
                with transaction.atomic():
                    delete()
        protected = {(o._meta.label, o.pk)
                     for o in caught.exception.protected_objects}
        self.assertEqual(protected, expected)
        writes = [q['sql'] for q in ctx.captured_queries
                  if q['sql'].lstrip().upper().startswith(WRITE_VERBS)]
        self.assertEqual(writes, [], 'a refused deletion wrote nothing')
        self.assertEqual(self.catalogue_state(), catalogue)
        self.assertEqual(self.history_state(), history)

    def fresh(self, model, obj):
        # A fresh instance, so the fixture's own instance is never the one a
        # successful (mutation-run) Model.delete() empties.
        return model._base_manager.get(pk=obj.pk)


class CatalogueDeletionIsRefusedTests(_RetentionFixture):
    """REGRESSION: under CASCADE each of these deleted order lines."""

    def test_REGRESSION_parent_dish_model_delete(self):
        self.refused(lambda: self.fresh(MenuItem, self.dish).delete(),
                     [self.dish])

    def test_REGRESSION_parent_dish_queryset_delete(self):
        self.refused(lambda: MenuItem.objects.filter(pk=self.dish.pk).delete(),
                     [self.dish])

    def test_REGRESSION_ordered_extra_model_delete(self):
        self.refused(lambda: self.fresh(MenuItem, self.cheese).delete(),
                     [self.cheese])

    def test_REGRESSION_ordered_extra_queryset_delete(self):
        self.refused(
            lambda: MenuItem.objects.filter(pk=self.cheese.pk).delete(),
            [self.cheese])

    def test_REGRESSION_sold_out_extra_model_delete(self):
        self.refused(lambda: self.fresh(MenuItem, self.bacon).delete(),
                     [self.bacon])

    def test_REGRESSION_sold_out_extra_queryset_delete(self):
        self.refused(lambda: MenuItem.objects.filter(pk=self.bacon.pk).delete(),
                     [self.bacon])

    def test_REGRESSION_section_group_model_delete(self):
        # The group holds the dish only; the extras sit directly in the section.
        self.refused(lambda: self.fresh(SectionGroup, self.group).delete(),
                     [self.dish])

    def test_REGRESSION_section_group_queryset_delete(self):
        self.refused(
            lambda: SectionGroup.objects.filter(pk=self.group.pk).delete(),
            [self.dish])

    def test_REGRESSION_section_model_delete(self):
        self.refused(
            lambda: self.fresh(MenuSection, self.section).delete(),
            [self.dish, self.fish, self.pasta, self.cheese, self.bacon])

    def test_REGRESSION_section_queryset_delete(self):
        self.refused(
            lambda: MenuSection.objects.filter(pk=self.section.pk).delete(),
            [self.dish, self.fish, self.pasta, self.cheese, self.bacon])

    def test_REGRESSION_a_mixed_queryset_delete_is_all_or_nothing(self):
        unordered = MenuItem.objects.create(
            name='Unordered Salad', section=self.section,
            primary_price='100.00', approved=True, enabled=True,
            available=True, in_stock=True)
        self.refused(
            lambda: MenuItem.objects.filter(
                pk__in=[unordered.pk, self.pasta.pk]).delete(),
            [self.pasta])
        self.assertTrue(MenuItem.objects.filter(pk=unordered.pk).exists())

    def test_REGRESSION_soft_deleted_lines_still_protect(self):
        # The collector reads related rows through the base manager, so a line
        # flagged deleted=True is still history. The flag is a direct write:
        # no supported writer produces it. The comparison is against the state
        # immediately before the attempt; the draft's later fate is not
        # asserted, because flagging its line is itself a change to it.
        flagged = OrderItem._base_manager.filter(item=self.pasta).update(
            deleted=True)
        self.assertEqual(flagged, 2)
        self.refused(lambda: self.fresh(MenuItem, self.pasta).delete(),
                     [self.pasta])


class HistoryAfterARefusalTests(_RetentionFixture):
    """REGRESSION: after a refused deletion the orders read, replay and submit
    exactly as before. Under CASCADE the reader lost name sites, the replay
    returned a different quote and the draft's submit was refused as stale."""

    def assert_history_survives(self, delete):
        accepted_before = self.details(self.order, self.table)
        draft_before = self.details(self.draft, self.draft_table)
        acceptance_before = list(OrderAcceptance.objects
                                 .filter(order=self.order).values())
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                delete()

        # The diner's reads say exactly what they said before.
        accepted_after = self.details(self.order, self.table)
        self.assertEqual(accepted_after, accepted_before)
        self.assertEqual(self.details(self.draft, self.draft_table),
                         draft_before)
        sites = self.detail_sites(accepted_after)
        self.assertEqual(len(sites), len(self.detail_sites(accepted_before)))
        self.assert_names_are_saved(self.order, sites)
        self.assert_provenance(self.order, sites)
        self.assertTrue(all(prov == 'snapshot' for *_, prov in sites))

        # The accepted order's evidence did not move.
        self.assertEqual(list(OrderAcceptance.objects
                              .filter(order=self.order).values()),
                         acceptance_before)

        # A replay of each key returns the same order and the same quote.
        for table, coid, order in ((self.table, self.coid, self.order),
                                   (self.draft_table, self.draft_coid,
                                    self.draft)):
            replayed = self.replay(table, coid)
            self.assertEqual(replayed['order_details']['id'], str(order.pk))
            rows, nested = self.replay_sites(replayed)
            self.assert_names_are_saved(order, rows, nested)
            self.assert_provenance(order, rows, nested)
        replayed = self.replay(self.draft_table, self.draft_coid)
        self.assertEqual(replayed['order_details']['quote_ref'],
                         self.draft_quote_ref)

        # The draft is accepted against the quote it was given.
        submitted = self.diner.put(
            SUBMIT_URL,
            data=json.dumps({'order': str(self.draft.pk),
                             'quote_ref': self.draft_quote_ref}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(self.draft_table))
        self.assertEqual(submitted.status_code, 200, submitted.content[:300])
        self.assertEqual(
            OrderAcceptance.objects.get(order=self.draft).quote_ref,
            self.draft_quote_ref)

    def test_REGRESSION_after_a_refused_section_delete(self):
        self.assert_history_survives(
            lambda: self.fresh(MenuSection, self.section).delete())

    def test_REGRESSION_after_a_refused_parent_dish_delete(self):
        self.assert_history_survives(
            lambda: MenuItem.objects.filter(pk=self.dish.pk).delete())


class RetentionControlTests(_RetentionFixture):
    """CONTROL: the catalogue is not frozen, and the supported paths behave
    exactly as they did."""

    def test_CONTROL_an_unordered_item_deletes(self):
        history = self.history_state()
        first = MenuItem.objects.create(
            name='Unordered One', section=self.section, primary_price='100.00',
            approved=True, enabled=True, available=True, in_stock=True)
        second = MenuItem.objects.create(
            name='Unordered Two', section=self.section, primary_price='100.00',
            approved=True, enabled=True, available=True, in_stock=True)
        self.fresh(MenuItem, first).delete()
        MenuItem.objects.filter(pk=second.pk).delete()
        self.assertFalse(MenuItem._base_manager
                         .filter(pk__in=[first.pk, second.pk]).exists())
        self.assertEqual(self.history_state(), history)

    def test_CONTROL_an_unordered_section_group_and_item_delete(self):
        history = self.history_state()
        section = MenuSection.objects.create(
            name='Desserts', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always')
        group = SectionGroup.objects.create(
            name='Cakes', section=section, approved=True, enabled=True)
        cake = MenuItem.objects.create(
            name='Cake', section=section, section_group=group,
            primary_price='100.00', approved=True, enabled=True,
            available=True, in_stock=True)
        section.delete()
        self.assertFalse(MenuSection._base_manager.filter(pk=section.pk).exists())
        self.assertFalse(SectionGroup._base_manager.filter(pk=group.pk).exists())
        self.assertFalse(MenuItem._base_manager.filter(pk=cake.pk).exists())
        self.assertEqual(self.history_state(), history)

    def test_CONTROL_the_http_soft_delete_path_is_unchanged(self):
        lines_before = self.row_state(self.order)
        draft_before = self.row_state(self.draft)
        # The supported path: soft delete with the inline vacuum's rename,
        # parents before the extras they offer, then the group and section.
        self.delete_everything()
        self.setup_delete('sectiongroups', self.group.id)
        self.setup_delete('menusections', self.section.id)
        dish = MenuItem._base_manager.get(pk=self.dish.pk)
        self.assertTrue(dish.deleted)
        self.assertTrue(dish.name.endswith('_autodel1'))
        self.assertEqual(self.row_state(self.order), lines_before)
        self.assertEqual(self.row_state(self.draft), draft_before)
        body = self.details(self.order, self.table)
        self.assert_names_are_saved(self.order, self.detail_sites(body))
        # D06, unchanged: a draft whose dishes were withdrawn is refused for
        # review rather than accepted.
        submitted = self.diner.put(
            SUBMIT_URL,
            data=json.dumps({'order': str(self.draft.pk),
                             'quote_ref': self.draft_quote_ref}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(self.draft_table))
        self.assertEqual(submitted.status_code, 400, submitted.content[:300])
        self.assertEqual(json.loads(submitted.content)['reason'],
                         REASON_PURCHASE_NEEDS_REVIEW)

    def test_CONTROL_a_rename_is_unchanged(self):
        lines_before = self.row_state(self.order)
        self.rename_everything()
        self.assertEqual(MenuItem.objects.get(pk=self.dish.pk).name,
                         'Chicken Burger')
        self.assertEqual(self.row_state(self.order), lines_before)
        body = self.details(self.order, self.table)
        self.assert_names_are_saved(self.order, self.detail_sites(body))

    def test_CONTROL_deleting_an_order_still_removes_its_lines(self):
        # Out of scope, and stated as the limit rather than as a promise: the
        # change protects lines from CATALOGUE deletion, not from deleting the
        # order itself.
        catalogue = self.catalogue_state()
        Order.objects.get(pk=self.order.pk).delete()
        self.assertFalse(OrderItem._base_manager.filter(order_id=self.order.pk)
                         .exists())
        self.assertEqual(self.catalogue_state(), catalogue)


class RetentionStructureTests(TestCase):
    """STRUCTURE: the relation, the migration and the database constraint."""

    def test_STRUCTURE_orderitem_item_is_protect_and_otherwise_unchanged(self):
        field = OrderItem._meta.get_field('item')
        self.assertIs(field.remote_field.on_delete, models.PROTECT)
        self.assertIs(field.related_model, MenuItem)
        self.assertEqual(field.remote_field.related_name, 'item')
        self.assertFalse(field.null)
        self.assertTrue(field.db_constraint)
        self.assertTrue(field.db_index)

    def test_STRUCTURE_the_neighbouring_relations_are_unchanged(self):
        self.assertIs(OrderItem._meta.get_field('order').remote_field.on_delete,
                      models.CASCADE)
        self.assertIs(
            OrderItem._meta.get_field('parent_item').remote_field.on_delete,
            models.SET_NULL)

    def test_STRUCTURE_the_migration_is_one_state_only_alter_field(self):
        module = importlib.import_module(
            'orders_app.migrations.0042_alter_orderitem_item')
        ops = module.Migration.operations
        self.assertEqual([type(op).__name__ for op in ops], ['AlterField'])
        (op,) = ops
        self.assertEqual((op.model_name, op.name), ('orderitem', 'item'))
        self.assertIs(op.field.remote_field.on_delete, models.PROTECT)
        self.assertFalse(op.field.null)

    def test_STRUCTURE_the_database_constraint_is_unchanged(self):
        # PROTECT is enforced by Django's collector. The constraint the
        # migrations created stays NO ACTION, deferrable, initially deferred.
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT c.confdeltype::text, c.condeferrable, c.condeferred
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid
                                   AND a.attnum = ANY(c.conkey)
                WHERE c.contype = 'f'
                  AND c.conrelid = 'order_items'::regclass
                  AND a.attname = 'item_id'""")
            self.assertEqual(cursor.fetchall(), [('a', True, True)])
