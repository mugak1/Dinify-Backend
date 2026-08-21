"""
Add ``Restaurant.is_test`` — platform-owned metadata marking a non-trading tenant.

ADDITIVE AND EXPAND-ONLY. One ``AddField`` with a literal default and no data
migration, so the immediately preceding deployed commit keeps working against the
new schema: Django emits explicit column lists rather than ``SELECT *``, and old
code simply never names the column. That is the property the expand-only rule in
CLAUDE.md exists to preserve, and it holds here without a contract step.

NO BACKFILL, DELIBERATELY. Every existing row becomes ``False``, which is the
honest answer: whether a tenant is a test tenant is a commercial decision, and this
migration has no basis on which to make it. In particular there is NO name-based
heuristic — a restaurant is not a test tenant because its name looks like one, and
mislabelling the single live production restaurant would silently delete it from
every revenue figure. Marking one is an explicit operator decision, and Step 2's
creation flow decides how it is assigned.

LOCK PROFILE. On PostgreSQL 11+ adding a column with a non-volatile default is a
metadata-only change — no table rewrite. The ``db_index=True`` does emit a plain
``CREATE INDEX``, which holds a SHARE lock blocking writes for its duration; at the
current scale (a single-digit number of restaurants) that is effectively
instantaneous. The index is added now rather than later because the field's whole
purpose is exclusion from portfolio and financial queries, which filter on it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants_app", "0056_restaurant_lifecycle_states"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="is_test",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
