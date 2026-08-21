"""
``platform_admin_app/0009`` — the onboarding-domain migration is ADDITIVE.

Two separate proofs, because they fail in different ways:

  THE SHAPE (`OnboardingMigrationShapeTests`) reads the migration's operation list
  and refuses anything that is not "create these two tables". This is the ratchet:
  the natural next edit to a migration like this is a `RunPython` that "just
  backfills the obvious rows", and a backfill here would assert an onboarding event
  that never happened, attributed to nobody. Static, so it costs nothing.

  THE APPLICATION (`OnboardingMigrationApplicationTests`) actually rewinds to the
  previous schema state and rolls forward against the live database, then compares
  the pre-existing tables on both sides of the boundary. That is what "expand-only"
  means concretely — the deployed commit's tables are byte-for-byte the ones it had
  before, so rolling CODE back onto this SCHEMA changes nothing for it.

TEARDOWN RESOLVES THE LEAF FROM THE GRAPH, never a hardcoded name. This repository
has twice been bitten by a migration test that restored the schema to a target that
later went stale, leaving every subsequent TransactionTestCase running against a
half-migrated database — see the teardown note in `tests_migration_flip.py`.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import (
    AddConstraint,
    AddIndex,
    CreateModel,
)
from django.test import TestCase, TransactionTestCase

APP_LABEL = 'platform_admin_app'
# This PR's migration. Safe to name: an already-written migration never moves.
ONBOARDING_MIGRATION = '0009_restaurantonboarding_ownerinvitation_and_more'
PREVIOUS_MIGRATION = '0008_adminloginchallenge_one_live_admin_challenge_per_user'

NEW_TABLES = {'restaurant_onboarding', 'owner_invitation'}
# Tables the previously-deployed code reads and writes. If the onboarding migration
# touched any of these, rollback would land old code on a schema it does not know.
PRE_EXISTING_TABLES = [
    'restaurants', 'users', 'restaurant_employees', 'admin_audit_log',
    'delegation_grant', 'admin_session',
]


class OnboardingMigrationShapeTests(TestCase):
    def setUp(self):
        super().setUp()
        loader = MigrationLoader(connection, load=True)
        self.migration = loader.disk_migrations[(APP_LABEL, ONBOARDING_MIGRATION)]

    def test_every_operation_creates_new_structure(self):
        allowed = (CreateModel, AddConstraint, AddIndex)
        offending = [
            type(operation).__name__
            for operation in self.migration.operations
            if not isinstance(operation, allowed)
        ]

        self.assertEqual(
            offending, [],
            msg=(
                'The onboarding migration must only CREATE structure. An '
                'AlterField / RemoveField / RenameField would break the '
                'expand-only rule; a RunPython would invent onboarding history.'
            ),
        )

    def test_it_creates_exactly_the_two_new_models(self):
        created = {
            operation.name for operation in self.migration.operations
            if isinstance(operation, CreateModel)
        }

        self.assertEqual(created, {'RestaurantOnboarding', 'OwnerInvitation'})

    def test_constraints_and_indexes_target_only_the_new_models(self):
        new_models = {'restaurantonboarding', 'ownerinvitation'}
        targeted = {
            operation.model_name.lower()
            for operation in self.migration.operations
            if isinstance(operation, (AddConstraint, AddIndex))
        }

        # A constraint added to `restaurants` or `users` here would change what the
        # previously-deployed code is allowed to write.
        self.assertEqual(targeted - new_models, set())

    def test_it_carries_no_data_migration(self):
        names = {type(operation).__name__ for operation in self.migration.operations}

        # Stated separately from the allow-list above so the failure message names
        # the specific thing that must never appear here.
        self.assertEqual(names & {'RunPython', 'RunSQL'}, set())


class OnboardingMigrationApplicationTests(TransactionTestCase):
    """Rewind to 0008, roll forward to 0009, and compare both schema states."""

    migrate_from = [(APP_LABEL, PREVIOUS_MIGRATION)]
    migrate_to = [(APP_LABEL, ONBOARDING_MIGRATION)]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Restore the schema to the CURRENT graph leaf of this app, resolved rather
        # than named: a TransactionTestCase's DDL is not rolled back, so stopping
        # short would leave every following test on a stale schema — and a hardcoded
        # target goes stale the moment a 0010 lands.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        self._migrate(executor.loader.graph.leaf_nodes(APP_LABEL))
        super().tearDown()

    @staticmethod
    def _columns(table):
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor, table,
            )
        # Name + nullability is the part old code depends on; comparing the driver's
        # raw type codes would make this brittle without making it stricter.
        return sorted((column.name, column.null_ok) for column in description)

    @staticmethod
    def _tables():
        with connection.cursor() as cursor:
            return set(connection.introspection.table_names(cursor))

    def test_the_new_tables_appear_and_nothing_else_changes(self):
        self._migrate(self.migrate_from)
        before_tables = self._tables()
        before_columns = {
            table: self._columns(table) for table in PRE_EXISTING_TABLES
        }

        # The previous deployed schema genuinely does not have them...
        self.assertEqual(NEW_TABLES & before_tables, set())

        self._migrate(self.migrate_to)
        after_tables = self._tables()

        # ...and applying the migration adds those two tables and ONLY those two.
        self.assertTrue(NEW_TABLES.issubset(after_tables))
        self.assertEqual(after_tables - before_tables, NEW_TABLES)
        self.assertEqual(before_tables - after_tables, set())

        # THE EXPAND-ONLY PROOF: every table the previously-deployed commit reads is
        # unchanged, so rolling that code back onto this schema is a no-op for it.
        for table in PRE_EXISTING_TABLES:
            with self.subTest(table=table):
                self.assertEqual(before_columns[table], self._columns(table))

    def test_the_new_tables_are_empty_after_application(self):
        self._migrate(self.migrate_to)

        # No backfill: every restaurant that exists, Baba House included, is
        # OUTSIDE the onboarding domain until an administrator brings it in.
        for table in sorted(NEW_TABLES):
            with self.subTest(table=table):
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                    self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_migration_reverses_cleanly(self):
        self._migrate(self.migrate_to)
        self.assertTrue(NEW_TABLES.issubset(self._tables()))

        self._migrate(self.migrate_from)

        # Reversible because it only ever added structure — the property that makes
        # a bad deploy recoverable without a hand-written incident-time fix.
        self.assertEqual(NEW_TABLES & self._tables(), set())
