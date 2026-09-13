"""
D05 — the kitchen-order concurrency token.

ONE additive column. ``Order.fulfilment_revision`` versions the KITCHEN-ORDER
STATE a command acts on — fulfilment status, cancellation and priority together,
not merely the text of ``fulfilment_status``. Every kitchen command names the
revision it believes it is acting on (``if_revision``), and the transition
service refuses when that no longer matches the locked row.

WHY A COLUMN AND NOT AN EXISTING FIELD. ``time_last_updated`` is ``auto_now`` on
``BaseModel``, so it moves on every save including ones no kitchen command made,
it is not exposed on the kitchen contract, and its uniqueness is not a property
this repository can demonstrate. ``quote_ref`` answers a different question (what
the DINER agreed to pay) and ``checkout_protocol`` / ``pricing_version`` are D04
and D02 contracts — overloading any of them would make one value answer two
questions, which is the mistake #661 records.

IT IS NOT HISTORY. The number counts nothing and proves nothing about the past:
it is a compare-and-set token. A value of 0 on an existing row is the ADOPTION
BASELINE — it does not claim the order has never been touched and it is not
evidence that the order was ever accepted (that is ``OrderAcceptance``'s job, and
D04's meaning of missing evidence is unchanged).

NO BACKFILL, AND NONE IS POSSIBLE. Nothing recorded how many kitchen commands a
historical order received, so every existing row starts at 0 by definition rather
than by inference.

BOTH DEFAULTS ARE ZERO, AND ``db_default`` IS LOAD-BEARING. Django manages
defaults in Python: ``AddField`` adds the column with a default and immediately
DROPS it, leaving a NOT NULL column with no database default. Under the
expand-only rule a rollback lands OLD CODE ON NEW SCHEMA, and old code INSERTs
orders without naming this column — which would then violate NOT NULL.
``db_default`` keeps a real database default so those inserts succeed and land on
the adoption baseline. (The same reasoning as ``users_app/0014``.)

DDL COST. ``ADD COLUMN ... DEFAULT 0 NOT NULL`` does not rewrite the table on
PostgreSQL 11+ — the default is stored in the catalogue and materialised on read.
It still takes a brief ACCESS EXCLUSIVE lock to update the catalogue, so it waits
behind any open transaction touching ``orders`` and blocks new ones while it
waits. The duration of that WAIT is a property of the deployment, not of this
migration, and this repository cannot observe the target table. Do not describe
it as instantaneous. There is deliberately NO index: nothing filters or orders by
this column — it is only ever read on a row already located by primary key.

REVERSE drops the column and restores nothing, because nothing was altered.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders_app', '0039_order_acceptance'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='fulfilment_revision',
            field=models.PositiveIntegerField(default=0, db_default=0),
        ),
    ]
