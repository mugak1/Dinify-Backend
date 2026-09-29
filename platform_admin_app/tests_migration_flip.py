"""
Migration test for `users_app/0011_flip_admin_account_type`: existing platform-role
holders flip to account_type='platform_staff'; non-holders don't; reverse reverts.

Mirrors the `restaurants_app/tests_migration_0055_sanitize.py` harness
(`MigrationExecutor` + `migrate_from`/`migrate_to` + drift-safe historical create +
a re-migrate-forward `tearDown` so the rest of the suite keeps the head schema).
0010→0011 is a data-only migration (no schema change), so this runs on SQLite.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FlipAccountTypeMigrationTests(TransactionTestCase):
    migrate_from = [('users_app', '0010_user_account_type')]
    migrate_to = [('users_app', '0011_flip_admin_account_type')]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    @staticmethod
    def _make_user(User, phone, roles):
        # Filter to fields present on the historical model (schema drift-safe).
        names = {f.name for f in User._meta.get_fields()}
        kw = dict(
            first_name='T', last_name=phone[-3:], email=f'{phone}@t.com',
            phone_number=phone, username=phone, country='Uganda',
            password='x', roles=roles,
        )
        return User.objects.create(**{k: v for k, v in kw.items() if k in names})

    def tearDown(self):
        # Leave the schema/state fully migrated forward for the rest of the suite —
        # to the HEAD of users_app, not merely to `migrate_to`. This is a
        # TransactionTestCase, so its DDL is not rolled back for the tests that
        # follow: stopping at 0011 left `phone_number` at its pre-0012 definition
        # (NOT NULL), and the next TransactionTestCase to create a platform-staff
        # account — which legitimately has no phone number — died on the constraint.
        # The target is RESOLVED from the graph rather than named, so it cannot drift
        # again when a 0014 lands. (`None` would migrate correctly but `_migrate`
        # also builds a project_state, which needs a real node.)
        #
        # The WHOLE project graph's leaves, not users_app's: rewinding users_app also
        # unapplies every migration in other apps that depends on it, and restoring
        # one app would leave those rolled back for the rest of the suite. Pinned by
        # `dinify_backend/tests_migration_test_isolation.py`.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        self._migrate(executor.loader.graph.leaf_nodes())

    def test_forward_flips_only_role_holders(self):
        old_apps = self._migrate(self.migrate_from)   # 0010: account_type exists, all default
        User = old_apps.get_model('users_app', 'User')
        admin = self._make_user(User, '256702000001', ['dinify_admin'])
        acct_mgr = self._make_user(User, '256702000002', ['dinify_account_manager'])
        both = self._make_user(User, '256702000003', ['dinify_admin', 'owner'])
        regular = self._make_user(User, '256702000004', ['owner'])
        empty = self._make_user(User, '256702000005', [])

        new_apps = self._migrate(self.migrate_to)     # runs flip_forward
        User2 = new_apps.get_model('users_app', 'User')

        def account_type(pk):
            return User2.objects.get(pk=pk).account_type

        self.assertEqual(account_type(admin.pk), 'platform_staff')
        self.assertEqual(account_type(acct_mgr.pk), 'platform_staff')
        self.assertEqual(account_type(both.pk), 'platform_staff')
        self.assertEqual(account_type(regular.pk), 'restaurant_user')
        self.assertEqual(account_type(empty.pk), 'restaurant_user')

    def test_reverse_reverts_role_holders(self):
        old_apps = self._migrate(self.migrate_from)
        User = old_apps.get_model('users_app', 'User')
        admin = self._make_user(User, '256702000011', ['dinify_admin'])
        regular = self._make_user(User, '256702000012', ['owner'])

        new_apps = self._migrate(self.migrate_to)     # forward
        User2 = new_apps.get_model('users_app', 'User')
        self.assertEqual(User2.objects.get(pk=admin.pk).account_type, 'platform_staff')

        back_apps = self._migrate(self.migrate_from)  # runs flip_reverse
        User3 = back_apps.get_model('users_app', 'User')
        self.assertEqual(User3.objects.get(pk=admin.pk).account_type, 'restaurant_user')
        self.assertEqual(User3.objects.get(pk=regular.pk).account_type, 'restaurant_user')
