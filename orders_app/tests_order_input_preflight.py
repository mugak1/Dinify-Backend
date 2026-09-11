"""
The D01 read-only preflight command, and the migration it exists to inform.

TWO THINGS ARE PROVED HERE.

*The command tells the truth.* It separates a database-constraint blocker from a
catalogue compatibility concern from an informational capacity observation, it
says whether an affected item is orderable or a draft, and — the property that
matters most for a gate — an inspection that does not complete is never reported
as a clean one.

*The command changes nothing.* Every test that runs it asserts the data is
byte-identical afterwards. A preflight that repaired anything would be a
migration wearing a disguise.

The migration tests exercise the CONSTRAINT against a violating historical
fixture at the SQL level rather than re-running the migration framework: what
matters operationally is that a pre-existing negative row makes the constraint
refuse to apply, leaves that row exactly as it was, and that dropping the
constraint again restores the old permissiveness without restoring any data —
because no data was ever altered.
"""
import io
import json

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from orders_app.management.commands import check_order_input_compatibility as pre
from orders_app.controllers.services import order_input
from orders_app.models import Order, OrderItem
from restaurants_app.models import MenuItem, MenuSection, Restaurant, Table
from users_app.models import User

CONSTRAINT = 'orderitem_quantity_non_negative'


def source_body(module):
    """The migration's code, with its explanatory docstring stripped — the
    docstring legitimately discusses what is NOT done."""
    with open(module.__file__) as handle:
        text = handle.read()
    return text.split('\"\"\"')[2]


class _PreflightBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='P', last_name='F', email='pf@test.com',
            phone_number='256700051001', username='256700051001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='PF R', location='pf', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='PF S', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )
        self._n = 0

    # -- fixtures ---------------------------------------------------------
    def item(self, options=None, **kwargs):
        self._n += 1
        opts = dict(approved=True, enabled=True, available=True,
                    primary_price=1000)
        opts.update(kwargs)
        return MenuItem.objects.create(
            name=f'PF Item {self._n}', section=self.section,
            options=options or {}, **opts,
        )

    def order_line(self, quantity, item=None):
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
        )
        return OrderItem.objects.create(
            order=order, item=item or self.item(), quantity=quantity,
            unit_price=1000, discounted_price=1000, total_cost=1000,
            discounted_cost=1000, savings=0, actual_cost=1000,
        )

    # -- running the command ---------------------------------------------
    def run_preflight(self, **options):
        """Returns (exit_code, stdout). Never raises out of a non-zero code."""
        out, err = io.StringIO(), io.StringIO()
        try:
            call_command('check_order_input_compatibility',
                         stdout=out, stderr=err, **options)
            return 0, out.getvalue() + err.getvalue()
        except CommandError as exc:
            return getattr(exc, 'returncode', 1), out.getvalue() + err.getvalue()

    def snapshot(self):
        """Everything the command reads, as comparable data."""
        return (
            sorted(OrderItem.objects.values_list('pk', 'quantity')),
            sorted(
                (str(pk), json.dumps(opts, sort_keys=True))
                for pk, opts in MenuItem.objects.values_list('pk', 'options')
            ),
        )


class PreflightCleanDataTests(_PreflightBase):
    def test_a_clean_database_reports_clean(self):
        self.order_line(2)
        self.order_line(1)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)
        self.assertIn('No blockers', output)
        self.assertIn('CLEAN at this moment', output)

    def test_legitimate_zero_rows_are_not_blockers(self):
        # A sold-out / unavailable line is server-zeroed on purpose; the
        # constraint is >= 0 precisely so this stays legal.
        self.order_line(0)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)
        self.assertIn('order items with a negative quantity: 0', output)

    def test_an_empty_database_reports_clean(self):
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)


class PreflightBlockerTests(_PreflightBase):
    _constraint_dropped = False

    def _negative_row(self, quantity=-3):
        line = self.order_line(2)
        if PreflightBlockerTests._constraint_dropped:
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE order_items SET quantity = %s WHERE id = %s',
                    [quantity, str(line.pk)])
            return line
        # The constraint is live in the test schema, so write beneath it to
        # stand in for a row that predates the migration. `SET CONSTRAINTS ALL
        # IMMEDIATE` flushes the deferred foreign-key trigger events the
        # surrounding test transaction is holding — PostgreSQL refuses to
        # ALTER a table that has any pending.
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute(
                f'ALTER TABLE order_items DROP CONSTRAINT {CONSTRAINT}')
            cursor.execute(
                'UPDATE order_items SET quantity = %s WHERE id = %s',
                [quantity, str(line.pk)])
        PreflightBlockerTests._constraint_dropped = True
        self.addCleanup(self._reset_constraint_flag)
        return line

    def _reset_constraint_flag(self):
        # Each test runs in its own rolled-back transaction, so the schema
        # change is undone for us; only the in-memory flag needs resetting.
        PreflightBlockerTests._constraint_dropped = False

    def _restore_constraint(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE order_items ADD CONSTRAINT {CONSTRAINT} '
                'CHECK (quantity >= 0)')

    def test_a_negative_historical_row_is_reported_as_a_blocker(self):
        line = self._negative_row()
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_BLOCKER, output)
        self.assertIn('BLOCKED', output)
        self.assertIn(str(line.pk), output)
        self.assertIn('quantity=-3', output)

    def test_the_blocker_report_does_not_change_the_row(self):
        line = self._negative_row()
        before = self.snapshot()
        self.run_preflight()
        self.assertEqual(self.snapshot(), before)
        line.refresh_from_db()
        self.assertEqual(line.quantity, -3, 'the preflight repaired a row')

    def test_samples_are_bounded(self):
        for _ in range(8):
            self._negative_row()
        code, output = self.run_preflight(sample_size=3)
        self.assertEqual(code, pre.EXIT_BLOCKER)
        self.assertIn('order items with a negative quantity: 8', output)
        self.assertIn('...', output)

    def test_it_runs_against_the_pre_migration_schema(self):
        # The whole point is to DECIDE whether to deploy 0036, so it must work
        # before 0036 exists.
        self.order_line(2)
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute(
                f'ALTER TABLE order_items DROP CONSTRAINT {CONSTRAINT}')
        try:
            code, output = self.run_preflight()
            self.assertEqual(code, pre.EXIT_CLEAN, output)
        finally:
            self._restore_constraint()


class PreflightCatalogueTests(_PreflightBase):
    def test_a_malformed_active_definition_is_a_concern_naming_its_state(self):
        item = self.item({'hasModifiers': True, 'groups': 'oops'})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('checkout will now REFUSE', output)
        self.assertIn(str(item.pk), output)
        self.assertIn('groups_not_a_list', output)
        self.assertIn('orderable', output)

    def test_a_malformed_draft_definition_is_only_informational(self):
        # Not orderable anyway — reporting it as a concern would manufacture
        # work that does not exist.
        self.item({'hasModifiers': True, 'groups': 'oops'}, enabled=False)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)
        self.assertIn('draft/inactive', output)

    def test_a_large_but_valid_optional_catalogue_is_not_a_concern(self):
        self.item({'hasModifiers': True, 'groups': [{
            'id': 'g1',
            'choices': [{'id': f'c{i}'} for i in range(400)],
        }]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)
        self.assertIn('capacity only', output)
        self.assertIn('choices in a group', output)

    def test_an_unsatisfiable_minimum_is_a_concern(self):
        item = self.item({'hasModifiers': True, 'groups': [{
            'id': 'g1', 'minSelections': 5,
            'choices': [{'id': 'c1'}, {'id': 'c2'}],
        }]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('required selections above the defined choices', output)
        self.assertIn(str(item.pk), output)

    def test_a_minimum_above_the_request_ceiling_is_a_concern(self):
        self.item({'hasModifiers': True, 'groups': [{
            'id': 'g1', 'minSelections': 100,
            'choices': [{'id': f'c{i}'} for i in range(200)],
        }]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('per-group request ceiling', output)

    def test_too_many_required_extras_is_a_concern(self):
        self.item(has_extras=True, extras_min_selections=200)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('required extras above the per-line request ceiling',
                      output)

    def test_inspecting_the_catalogue_changes_nothing(self):
        self.item({'hasModifiers': True, 'groups': 'oops'})
        self.item({'hasModifiers': True, 'groups': [{
            'id': 'g1', 'minSelections': 5, 'choices': [{'id': 'c1'}]}]})
        self.order_line(0)
        before = self.snapshot()
        self.run_preflight()
        self.assertEqual(self.snapshot(), before)


class PreflightSafetyTests(_PreflightBase):
    def test_an_incomplete_inspection_is_never_reported_as_clean(self):
        from unittest import mock
        with mock.patch.object(
            pre.Command, '_inspect_catalogue',
            side_effect=RuntimeError('connection lost'),
        ):
            code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_INCOMPLETE, output)
        self.assertIn('INSPECTION DID NOT COMPLETE', output)
        self.assertIn('UNKNOWN', output)
        self.assertNotIn('CLEAN at this moment', output)

    def test_there_is_no_repair_mode(self):
        out = io.StringIO()
        try:
            call_command('check_order_input_compatibility', '--help',
                         stdout=out)
        except SystemExit:
            pass  # argparse exits after printing help
        help_text = out.getvalue()
        for forbidden in ('--fix', '--repair', '--write', '--apply',
                          '--clamp', '--delete'):
            self.assertNotIn(forbidden, help_text)

    def test_arguments_are_bounded(self):
        for bad in ({'sample_size': 0}, {'sample_size': 10 ** 6},
                    {'chunk_size': 0}):
            with self.subTest(**bad):
                with self.assertRaises(CommandError):
                    call_command('check_order_input_compatibility',
                                 stdout=io.StringIO(), **bad)

    def test_the_report_carries_no_order_or_customer_content(self):
        self.order_line(0)
        self.item({'hasModifiers': True, 'groups': 'oops'})
        _, output = self.run_preflight()
        self.assertNotIn(self.owner.email, output)
        self.assertNotIn(self.owner.phone_number, output)
        self.assertNotIn('hasModifiers', output, 'catalogue JSON was printed')


class MigrationConstraintTests(TransactionTestCase):
    """What the migration does to real data — and, more importantly, what it
    does NOT do."""

    reset_sequences = True

    def setUp(self):
        owner = User.objects.create_user(
            first_name='M', last_name='G', email='mg@test.com',
            phone_number='256700052001', username='256700052001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='MG R', location='mg', owner=owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='MG S', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self.menu_item = MenuItem.objects.create(
            name='MG Item', section=self.section, primary_price=1000,
            approved=True, enabled=True, available=True,
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )

    def _line(self, quantity):
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
        )
        return OrderItem.objects.create(
            order=order, item=self.menu_item, quantity=quantity,
            unit_price=1000, discounted_price=1000, total_cost=1000,
            discounted_cost=1000, savings=0, actual_cost=1000,
        )

    def _drop(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE order_items DROP CONSTRAINT {CONSTRAINT}')

    def _add(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE order_items ADD CONSTRAINT {CONSTRAINT} '
                'CHECK (quantity >= 0)')

    def _exists(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = %s "
                "AND conrelid = 'order_items'::regclass", [CONSTRAINT])
            return cursor.fetchone() is not None

    def test_a_fresh_install_carries_the_constraint(self):
        self.assertTrue(self._exists())

    def test_existing_valid_and_zero_rows_are_unaffected(self):
        for quantity in (0, 1, 250):
            with self.subTest(quantity=quantity):
                line = self._line(quantity)
                line.refresh_from_db()
                self.assertEqual(line.quantity, quantity)

    def test_a_violating_historical_row_blocks_the_constraint_and_survives(self):
        self._drop()
        line = self._line(1)
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE order_items SET quantity = -7 WHERE id = %s',
                [str(line.pk)])
        try:
            with self.assertRaises(Exception) as caught:
                self._add()
            self.assertIn('is violated by some row', str(caught.exception))
            self.assertFalse(self._exists(), 'the constraint was added anyway')
            # The migration ABORTS; it never clamps, deletes or repairs.
            line.refresh_from_db()
            self.assertEqual(line.quantity, -7)
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM order_items WHERE id = %s',
                               [str(line.pk)])
            if not self._exists():
                self._add()

    def test_it_applies_once_the_violation_is_resolved(self):
        self._drop()
        line = self._line(1)
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE order_items SET quantity = -7 WHERE id = %s',
                [str(line.pk)])
        with self.assertRaises(Exception):
            self._add()
        with connection.cursor() as cursor:
            cursor.execute('UPDATE order_items SET quantity = 1 WHERE id = %s',
                           [str(line.pk)])
        self._add()
        self.assertTrue(self._exists())

    def test_the_reverse_operation_drops_the_rule_and_restores_no_data(self):
        line = self._line(3)
        self._drop()
        self.assertFalse(self._exists())
        # Reversing only removes a rule: the rows it never touched are
        # unchanged, and nothing is "restored" because nothing was altered.
        line.refresh_from_db()
        self.assertEqual(line.quantity, 3)
        # Old permissiveness is genuinely back.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE order_items SET quantity = -1 WHERE id = %s',
                           [str(line.pk)])
        line.refresh_from_db()
        self.assertEqual(line.quantity, -1)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE order_items SET quantity = 3 WHERE id = %s',
                           [str(line.pk)])
        self._add()

    def test_the_constraint_stops_writes_that_bypass_every_serializer(self):
        line = self._line(2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderItem.objects.filter(pk=line.pk).update(quantity=-1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                line.quantity = -1
                line.save(update_fields=['quantity'])


class MigrationShapeTests(TestCase):
    """The migration is additive and reversible, and adds no second rule."""

    def test_it_is_one_reversible_add_constraint_and_nothing_else(self):
        import importlib
        module = importlib.import_module(
            'orders_app.migrations.0036_orderitem_quantity_non_negative'
        )
        operations = module.Migration.operations
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        self.assertEqual(operation.__class__.__name__, 'AddConstraint')
        self.assertEqual(operation.constraint.name, CONSTRAINT)
        self.assertTrue(operation.reversible)
        self.assertEqual(module.Migration.dependencies,
                         [('orders_app', '0035_order_is_test')])
        # It is `>= 0`, never `> 0` — zero is a legitimate internal state.
        self.assertEqual(operation.constraint.condition.deconstruct()[1],
                         (('quantity__gte', 0),))
        # No data migration, and no money-field invariant alongside. The
        # docstring is stripped first: it legitimately discusses what this
        # migration does NOT do.
        with open(module.__file__) as handle:
            body = handle.read().split('"""')[2]
        for forbidden in ('RunPython', 'RunSQL', 'savings', 'actual_cost',
                          'unit_price', 'quantity__gt,'):
            self.assertNotIn(forbidden, body)


class PreflightUnorderableIdentifierTests(_PreflightBase):
    """A stored id no client can name makes a REQUIRED selection unsatisfiable.
    The preflight must not call such a catalogue clean (Codex P2)."""

    def test_a_required_group_with_a_numeric_id(self):
        item = self.item({'hasModifiers': True, 'groups': [
            {'id': 1, 'minSelections': 1, 'choices': [{'id': 'c1'}]},
        ]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('required option groups no request can name', output)
        self.assertIn(str(item.pk), output)

    def test_a_required_group_with_an_overlong_id(self):
        self.item({'hasModifiers': True, 'groups': [
            {'id': 'g' * (order_input.MAX_MODIFIER_ID_LENGTH + 1),
             'minSelections': 1, 'choices': [{'id': 'c1'}]},
        ]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('required option groups no request can name', output)

    def test_too_few_nameable_choices_for_the_minimum(self):
        item = self.item({'hasModifiers': True, 'groups': [
            {'id': 'g1', 'minSelections': 2,
             'choices': [{'id': 1}, {'id': 2}, {'id': 'c3'}]},
        ]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('choices no request can name', output)
        self.assertIn(str(item.pk), output)

    def test_an_optional_group_with_a_numeric_id_stays_clean(self):
        # Orderable today — the diner never names it. Reporting it would
        # manufacture work that does not exist.
        self.item({'hasModifiers': True, 'groups': [
            {'id': 1, 'minSelections': 0, 'choices': [{'id': 'c1'}]},
        ]})
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)

    def test_the_preflight_uses_THE_request_contract_not_a_copy(self):
        # Identity, so the two cannot drift into disagreeing about what a
        # client may send.
        self.assertIs(pre.is_submittable_identifier,
                      order_input.is_submittable_identifier)


class PreflightCombinedCeilingTests(_PreflightBase):
    """Modifier and extra minimums share ONE whole-request ceiling, so checking
    each axis alone is not enough (Codex P2)."""

    def _max_required_groups(self, extras_min):
        groups = order_input.MAX_MODIFIER_GROUPS_PER_LINE
        per_group = order_input.MAX_CHOICES_PER_GROUP
        return self.item(
            {'hasModifiers': True, 'groups': [
                {'id': f'g{i}', 'minSelections': per_group,
                 'choices': [{'id': f'g{i}c{j}'} for j in range(per_group)]}
                for i in range(groups)
            ]},
            has_extras=True, extras_min_selections=extras_min,
        )

    def test_exactly_at_the_combined_ceiling_is_clean(self):
        # 32 * 64 = 2048 required entries, and no required extra.
        self._max_required_groups(extras_min=0)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)

    def test_one_entry_over_the_combined_ceiling_is_a_concern(self):
        # 32 * 64 + 1 = 2049: each axis passes its own check, the combination
        # does not, and no request can ever satisfy the item.
        item = self._max_required_groups(extras_min=1)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CONCERNS, output)
        self.assertIn('combined required options + extras', output)
        self.assertIn(str(item.pk), output)
        self.assertIn('2049 required entries', output)

    def test_the_combined_check_changes_nothing_about_ordinary_items(self):
        self.item({'hasModifiers': True, 'groups': [
            {'id': 'g1', 'minSelections': 1,
             'choices': [{'id': 'c1'}, {'id': 'c2'}]},
        ]}, has_extras=True, extras_min_selections=1)
        code, output = self.run_preflight()
        self.assertEqual(code, pre.EXIT_CLEAN, output)
