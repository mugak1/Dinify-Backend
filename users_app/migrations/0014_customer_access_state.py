"""
Add ``User.customer_access_state`` — the Step-2D.1 pre-claim customer-plane gate.

ADDITIVE ONLY: one ``AddField`` plus the vocabulary ``AddConstraint``. There is NO
``RunPython`` and no inference of any kind — no reading of invitations, onboarding
rows, password state, login history or memberships. The rule this migration applies
is the only truthful one available:

    every identity that predates this gate is ``established``.

The ``AddField`` default does that for the whole existing corpus in one statement,
which is also why the corpus cannot be locked out: `established` means exactly what
those accounts already had, namely "subject to the ordinary customer rules and
nothing more". The only thing that ever writes `pending_initial_claim` is
``platform_admin_app.onboarding_creation`` creating a brand-new owner, from this
release onwards.

WHY ``db_default`` AS WELL AS ``default`` — and it is load-bearing, not decoration.
Django manages defaults in Python: ``AddField`` adds the column WITH a default and
then immediately drops it, so a NOT NULL column ends up with no database default at
all. That is fine for reads (Django emits explicit column lists, so old code simply
never names the column) but NOT for writes, and ``users`` is a table old code
INSERTS into — ``self_register`` and ``determine-customers`` both create rows. Under
the expand-only rule a rollback lands OLD CODE ON NEW SCHEMA, and old code inserting
a user without naming this column would hit a NOT NULL violation. ``db_default``
(Django 5.0+) keeps a real database-level default, so those inserts succeed and land
on ``established`` — the correct value for an identity created by code that predates
the gate. This strengthens, for INSERT, the property migration
``restaurants_app/0057``'s docstring asserts for SELECT.

LOCK PROFILE. On PostgreSQL 11+ adding a column with a non-volatile default is a
metadata-only change — no table rewrite. There is deliberately no index: nothing
queries on this column alone, it is only ever read for an already-resolved row.
``AddConstraint`` validates the existing rows, which at this scale is instantaneous
and, since every row was just set to ``established``, cannot fail.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("users_app", "0013_close_ambient_admin_authority"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="customer_access_state",
            field=models.CharField(
                choices=[
                    ("established", "established"),
                    ("pending_initial_claim", "pending_initial_claim"),
                ],
                db_default="established",
                default="established",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "customer_access_state__in",
                        ("established", "pending_initial_claim"),
                    )
                ),
                name="user_customer_access_state_vocabulary",
            ),
        ),
    ]
