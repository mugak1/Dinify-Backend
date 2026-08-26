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

━━ THE ONE CLASS THIS DEFAULT CANNOT COVER — CHECK BEFORE DEPLOYING ━━━━━━━━━━━━━

Step 2D shipped the ``mode=new`` owner creator BEFORE this gate existed. Any owner it
created in the window between that deploy and this one has an unresolved invitation
and an unusable password, and this migration will call them ``established`` — which is
the pre-2D.1 state, i.e. still reachable by generic password reset. The window is real
rather than hypothetical (Step 2D reached UAT on 2026-08-25), though creating such an
owner needs a platform-staff account, a live admin session, a fresh second factor, a
CSRF token and a hand-built POST: there is no Admin creation UI.

IT IS NOT BACKFILLED HERE, and the reason is not squeamishness about ``RunPython``.
The obvious rule — "everyone holding an unresolved invitation under ``admin_created``
onboarding" — is WRONG, and wrong in the direction that matters: the invitation is
minted unconditionally, so an ``mode=existing`` owner has one too. That owner is an
established account, very possibly already trading at another restaurant, and
demoting them would lock a live tenant out of its own restaurant. That is precisely
the multi-tenant false positive this whole design exists to avoid. NOTHING IN THE
SCHEMA DISTINGUISHES THE TWO MODES — only ``AdminAuditLog.after_state`` does, via
``owner_account_created``.

So the exposure is ENUMERATED, not guessed. One query names it exactly::

    SELECT after_state->>'owner_user_id' AS owner_user_id, created_at
    FROM admin_audit_log
    WHERE action = 'admin.restaurant.created'
      AND result = 'success'
      AND after_state->>'owner_account_created' = 'true';

Empty (the expected result) — nothing to do, and this migration is complete on its
own. Non-empty — set exactly those users to ``pending_initial_claim`` before or
immediately after deploying, as a deliberate, attributed operator action against a
named list, rather than by a heuristic baked into a migration that would then run
forever against every environment for a window that closed the moment it was applied.
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
