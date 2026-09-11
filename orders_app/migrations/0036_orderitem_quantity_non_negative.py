"""
Add the D01 persisted-quantity invariant: ``OrderItem.quantity >= 0``.

ADDITIVE AND REVERSIBLE: one ``AddConstraint``. No ``RunPython``, no backfill, no
data repair, no touch of any other table or column, and no money-field
constraint (D02 owns pricing invariants).

WHAT IT IS, AND WHAT IT IS NOT. It validates ONE persisted fact. It is NOT a
claim that the database validates the incoming JSON order request — request
shape, quantity type and the collection ceilings are enforced in
``orders_app/controllers/services/order_input.py``, above this. This is the
backstop for the paths that rule cannot reach: a direct ORM ``save()``, a bulk
``queryset.update()``, a management command, or a future writer nobody has
written yet. A negative stored quantity silently REDUCES what a diner is
charged, which is why it is worth a database rule and not only an application
one.

``>= 0`` AND NEVER ``> 0``. Zero is a legitimate internal representation, not an
absence of validation: ``ConOrder.add_order_item`` and ``ConOrder.process_item_extras``
set a line to zero when its menu item is unavailable or sold out, so the line is
neither prepared nor charged while still appearing in the diner's
reconciliation. ``orders_app/tests_checkout_policy.py`` and
``dinify_backend/tenancy/tests_tenant_isolation_closure.py`` both pin that
behaviour. A positive-only constraint would break it on the next order placed
for a sold-out item.

NO UPPER BOUND. The per-line ceiling in the input validator bounds what a client
may SUBMIT; it is not a bound on the stored value. Several valid lines for the
same item merge into one row through ``update_item_quantity``, so a row may
legitimately exceed the per-line ceiling. Adding a maximum here would reject
correct orders.

EXPAND-ONLY / ROLLBACK. Adding a constraint is backward-compatible with the
immediately preceding deployed commit: older code writes non-negative quantities
on every path it has (the pre-D01 defect wrote a negative only from a malformed
customer request, which the deployed constraint then refuses with an
``IntegrityError`` rather than accepting silently). The reverse operation drops
the constraint. IT RESTORES NOTHING, because nothing was altered — this
migration only ever adds a rule; it neither rewrites nor deletes a historical
row, and a rollback cannot bring back data that was never changed.

LOCK AND SCAN PROFILE — READ BEFORE DEPLOYING.
``ALTER TABLE ... ADD CONSTRAINT ... CHECK`` takes an ``ACCESS EXCLUSIVE`` lock on
``order_items`` and validates EVERY existing row, including soft-deleted,
archived and vacuumed ones, before it commits. For the duration of that lock all
reads and writes to ``order_items`` block — which on this table means order
creation, the kitchen board and the reports queries.

**The duration is proportional to the row count of the TARGET table, which this
repository cannot observe.** Nothing here should be read as a claim that the
table is small or the lock brief; that is exactly what the preflight command
exists to establish (``manage.py check_order_input_compatibility``).

Two operational precautions for the deploy session, neither of which this
migration can set for itself (the deploy script runs a bare ``migrate``):

* bound the wait with ``lock_timeout`` (a few seconds) so the migration fails
  fast instead of queueing behind a long-running transaction and holding every
  order write behind it while it waits;
* bound the scan with ``statement_timeout`` sized from the preflight's row
  count.

IF IT TIMES OUT, the statement aborts, the transaction rolls back, no constraint
is added and no row is touched — it is safe to retry in a quieter window.

IF IT FINDS A VIOLATION, PostgreSQL raises and the migration ABORTS the same
way: no constraint, no data change. That is deliberate. A pre-existing negative
quantity is a real historical order that was under-charged, and deciding what to
do about it is a business decision with an owner — never something a migration
should clamp, delete or quietly repair. Run the preflight, get an explicit
remediation decision, then deploy.

STAGED ALTERNATIVE, IF THE EVIDENCE WARRANTS IT. If the preflight shows
``order_items`` is large enough that an exclusive scan is unacceptable, the same
invariant can be reached in two steps — ``ADD CONSTRAINT ... NOT VALID`` (brief
``ACCESS EXCLUSIVE``, no scan, enforced for all NEW rows immediately) followed by
``VALIDATE CONSTRAINT`` (``SHARE UPDATE EXCLUSIVE``, which does not block reads or
writes). That needs hand-written ``RunSQL`` and is deliberately NOT adopted here:
it is a real cost in complexity and it should be paid against evidence, not
against a guess. The simple form is the right default until a measured row count
says otherwise.

A PREFLIGHT IS A POINT-IN-TIME OBSERVATION. It tells you whether the constraint
can be added right now; it is not a substitute for the constraint, which is what
keeps the invariant true afterwards.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders_app", "0035_order_is_test"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gte", 0)),
                name="orderitem_quantity_non_negative",
            ),
        ),
    ]
