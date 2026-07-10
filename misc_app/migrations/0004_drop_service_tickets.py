"""
Drop the orphaned `service_tickets` table left behind by the removed `crm_app`.

`crm_app` (the legacy `ServiceTicket` ticketing app, superseded by
`support_app.SupportIssue`) was deleted in full, including its migrations, so
this drop lives in a surviving app. The table has NO inbound foreign keys, so
the drop is referentially clean.
"""
from django.db import migrations


def drop_service_tickets(apps, schema_editor):
    # Postgres (CI + prod RDS) honours CASCADE; SQLite (local test runs) has no
    # CASCADE keyword on DROP TABLE, so use the plain form there — equivalent
    # here since there are no dependents. IF EXISTS keeps it idempotent: on a
    # fresh test DB, crm_app is uninstalled so the table was never created and
    # this is a no-op; on prod it drops the existing empty table.
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute("DROP TABLE IF EXISTS service_tickets CASCADE;")
    else:
        schema_editor.execute("DROP TABLE IF EXISTS service_tickets;")


class Migration(migrations.Migration):

    dependencies = [
        ('misc_app', '0003_sysactivityconfig_config_datetime_value_and_more'),
    ]

    operations = [
        migrations.RunPython(drop_service_tickets, migrations.RunPython.noop),
    ]
