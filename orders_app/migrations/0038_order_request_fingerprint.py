"""
D04 — bind the idempotency key to the purchase it was used for.

ADDITIVE AND NULLABLE, with NO ``RunPython`` and no backfill. That is not
laziness about old rows; it is the only truthful option. A pre-D04 order has no
record of the request that created it — an unavailable line is persisted at
quantity 0 and identical configurations are merged, so the saved rows cannot be
read back into the request they came from. Populating this column from a retry
that arrives later would certify an equivalence nobody observed, which is worse
than admitting it is unknown. NULL therefore means EXACTLY "this order predates
D04", and `order_intent.is_supported` treats it as undecidable rather than as a
mismatch or a match.

ROLLBACK. Nullable with no database default is sufficient for the expand-only
rule here: old code INSERTs into `orders` without naming this column, and a
nullable column accepts that. It needs no `db_default` — unlike
`users_app/0014`, where the column was NOT NULL and an old INSERT would have
violated it. A test exercises a raw INSERT that omits the column.

Reverting the MIGRATION drops the evidence; reverting only the CODE leaves the
column populated and simply unread, so a re-deploy resumes with the bindings it
already wrote. Neither direction repairs or invents data.

LOCK COST. `AddField` of a NULLABLE column with no default is metadata-only on
PostgreSQL 11+ and does not rewrite the table. The `db_index=True` that
accompanies it is NOT free: `CREATE INDEX` takes a `SHARE` lock and its
duration is proportional to the row count of `orders`, which this repository
cannot observe. Do not describe the deploy as instantaneous on that basis.
`AddIndexConcurrently` (outside a transaction, `atomic = False`) is the
alternative if a measurement against the target says the plain build is too
long; it is deliberately NOT used here, because it cannot run in the same
atomic migration and the trade is only worth making against real numbers.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders_app", "0037_order_pricing_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="request_fingerprint",
            field=models.CharField(
                blank=True, db_index=True, max_length=128, null=True
            ),
        ),
    ]
