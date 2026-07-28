"""
The deploy check that refuses a non-PostgreSQL production database (DB-VENDOR-00).

These tests hit ``check_production_database_vendor`` directly rather than running
``check --deploy``, and that is the point of the seam: the check's decision is a pure
function of ``(vendor, debug)``, so every combination — including the production one
this suite can never actually be in — is exercisable here. The same split
``dinify_backend/diner_cap_config.py`` uses for the diner-capability key.

It matters more than usual for this check. ``check --deploy`` is not invoked by
``ci.yml`` or ``scripts/verify.sh`` — only by ``deploy-uat.yml``, twice, before the
Apache restart. So this module is the ONLY place the check's body runs in the PR loop,
and the registration test below is what stops a refactor silently demoting it out of
the deploy set, where nothing would then run it at all.
"""
from django.core.checks import ERROR, Tags, registry
from django.test import SimpleTestCase, tag

from restaurants_app.checks import (
    CHECK_ID,
    REQUIRED_DB_VENDOR,
    check_production_database_vendor,
    production_requires_postgresql,
)


@tag('tenant_closure')
class ProductionDatabaseVendorCheckTests(SimpleTestCase):

    def test_a_deployed_non_postgresql_database_is_an_error(self):
        messages = check_production_database_vendor('sqlite', debug=False)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, CHECK_ID)
        # An Error, not a Warning: `manage.py check` exits non-zero only at ERROR and
        # above, and `deploy-uat.yml` runs under `set -e`. A Warning would print into
        # the deploy log and let the restart proceed.
        self.assertEqual(messages[0].level, ERROR)

    def test_a_deployed_postgresql_database_passes(self):
        self.assertEqual(
            check_production_database_vendor(REQUIRED_DB_VENDOR, debug=False), [],
        )

    def test_it_is_silent_in_local_development(self):
        # The configuration `scripts/verify.sh` runs in with no DATABASE_* exported:
        # test_settings sets DEBUG=True and falls back to SQLite in-memory. The check
        # must not fire there, or every local run and every CI leg would be red.
        self.assertEqual(
            check_production_database_vendor('sqlite', debug=True), [],
        )

    def test_debug_suppresses_every_vendor(self):
        # DEBUG is the gate, not a vendor allowlist — an unknown backend under DEBUG is
        # still a development environment.
        for vendor in ('sqlite', 'mysql', 'oracle', REQUIRED_DB_VENDOR):
            with self.subTest(vendor=vendor):
                self.assertEqual(
                    check_production_database_vendor(vendor, debug=True), [],
                )

    def test_the_error_never_echoes_connection_details(self):
        # It names the vendor and the setting to change. Messages reach deploy logs, so
        # they must not carry a host, a user or a password — the discipline
        # diner_cap_config states for its own ImproperlyConfigured text.
        message = check_production_database_vendor('sqlite', debug=False)[0]
        rendered = f'{message.msg} {message.hint}'

        for secret in ('PASSWORD', 'DATABASE_PASSWORD', 'DATABASE_HOST', 'USER'):
            self.assertNotIn(secret, rendered)

    def test_the_hint_points_at_the_primitive_it_protects(self):
        # A deploy is aborting; the operator needs to know what silently stops working,
        # not just that a preference was violated.
        hint = check_production_database_vendor('sqlite', debug=False)[0].hint

        self.assertIn('admission_lock', hint)
        self.assertIn('DATABASE_ENGINE', hint)

    def test_the_check_is_registered_as_a_deploy_check(self):
        # Registration is the whole delivery mechanism: `deploy-uat.yml` runs
        # `check --deploy`, so a check that lost `deploy=True` would stop running
        # anywhere at all rather than merely running less often.
        self.assertIn(
            production_requires_postgresql,
            registry.registry.get_checks(include_deployment_checks=True),
        )
        self.assertIn(Tags.database, production_requires_postgresql.tags)

    def test_the_check_is_not_in_the_ordinary_check_set(self):
        # The complement of the test above, and the reason the plain `django check` in
        # `ci.yml` and `verify.sh` stays quiet.
        self.assertNotIn(
            production_requires_postgresql, registry.registry.get_checks(),
        )
