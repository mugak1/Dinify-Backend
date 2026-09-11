"""
D02 — the pricing-provenance discriminator.

ONE additive ``AddField``. No ``RunPython``, no backfill, no recalculation of any
historical price, and no inference of a version from a timestamp or from anything
else. Every row that predates this column is LEGACY, which is the only truthful
statement that can be made about it: it was priced by the pre-D02 convention.

WHY ``db_default`` AS WELL AS ``default``. Django manages defaults in Python —
``AddField`` adds the column with a default and immediately DROPS it — so a NOT
NULL column normally ends up with no database default. That is fine for reads and
NOT fine here: under the expand-only rule a rollback lands OLD CODE ON NEW SCHEMA,
and an older application version INSERTing an order without naming this column
would hit a NOT NULL violation. ``db_default`` keeps a real database default, so
such an insert succeeds AND lands on LEGACY — which is exactly the classification
it deserves, because old code is precisely the code that prices the old way. The
same reasoning covers any future writer that forgets to set it: forgetting cannot
certify an order as corrected.

LOCK AND DURATION PROFILE. ``ALTER TABLE ... ADD COLUMN`` with a non-volatile
default takes ``ACCESS EXCLUSIVE`` on ``orders`` but does NOT rewrite the table on
PostgreSQL 11+ (the default is stored in the catalogue), so the ALTER itself is
brief. The ``db_index=True`` then builds an index over every existing row, and THAT
cost is proportional to the table's row count, which this repository cannot
observe. Do not describe this deployment as instantaneous. If the measured row
count makes the index build unacceptable, the supported alternative is to split it:
ship the column here and add the index in a separate migration using
``CREATE INDEX CONCURRENTLY`` (via ``AddIndexConcurrently``, which must run
non-atomically). That decision needs a measurement this repository does not have,
so it is documented rather than pre-empted.

REVERSING drops the column and restores nothing, because nothing was altered. An
order's stored amounts are untouched in both directions.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders_app", "0036_orderitem_quantity_non_negative"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="pricing_version",
            field=models.PositiveSmallIntegerField(
                db_default=0, db_index=True, default=0
            ),
        ),
    ]
