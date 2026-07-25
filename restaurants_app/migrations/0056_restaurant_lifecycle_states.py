"""
Restaurant lifecycle: constrain ``status`` and migrate the free-text vocabulary.

FAIL CLOSED. The old field was an unconstrained CharField, so the only honest
assumption about what is in it is "we do not know". This migration maps the three
values whose meaning is unambiguous and RAISES on everything else rather than
defaulting — a row silently landing in the wrong state would be a permission bug
that nobody notices until a diner or an owner is affected.

``inactive`` and ``rejected`` are deliberately NOT mapped. They appear in the
codebase with no defined semantics, and the plausible targets differ (an
``inactive`` restaurant might be onboarding-but-paused, suspended, or finished;
``rejected`` never became a tenant at all). Guessing per row is a commercial
decision, not a schema one — so the migration stops and names the rows.

Verified production state at the time of writing: ONE restaurant, status
``active``. The volume is trivial; the defensiveness is the point.
"""
from django.db import migrations, models


# Forward: old value -> new state. Unambiguous cases only.
FORWARD_MAP = {
    'active': 'live',          # trading today
    'pending': 'onboarding',   # signed, not yet trading
    'blocked': 'suspended',    # stopped, recoverable (typically for non-payment)
}

# Values that must not be guessed. Listed separately from "unknown" so the operator
# gets the real reason rather than a generic parse failure.
UNDECIDED = ('inactive', 'rejected')

NEW_STATES = ('onboarding', 'live', 'suspended', 'offboarded')

# Reverse of FORWARD_MAP. ``offboarded`` is absent on purpose: it has no
# pre-migration equivalent, so a row in that state cannot be reversed and the
# operator is told rather than silently handed a wrong value.
REVERSE_MAP = {new: old for old, new in FORWARD_MAP.items()}


def _distinct_statuses(Restaurant):
    return sorted(
        value for value in
        Restaurant.objects.values_list('status', flat=True).distinct()
        if value is not None
    )


def forward(apps, schema_editor):
    Restaurant = apps.get_model('restaurants_app', 'Restaurant')

    present = _distinct_statuses(Restaurant)

    undecided = [value for value in present if value in UNDECIDED]
    if undecided:
        counts = {
            value: Restaurant.objects.filter(status=value).count()
            for value in undecided
        }
        raise RuntimeError(
            "restaurants_app.0056: refusing to guess a lifecycle state for "
            f"{counts}. `inactive` and `rejected` have no defined meaning in the "
            "new vocabulary, so each row needs a decision. Resolve them first, e.g. "
            "UPDATE restaurants SET status='suspended' WHERE status='inactive'; "
            "(valid interim values: 'active', 'pending', 'blocked'), then re-run "
            "the migration."
        )

    # Idempotent: a value already in the new vocabulary is left alone, so a partial
    # or re-run migration converges rather than double-mapping.
    unknown = [
        value for value in present
        if value not in FORWARD_MAP and value not in NEW_STATES
    ]
    if unknown:
        raise RuntimeError(
            f"restaurants_app.0056: unrecognised Restaurant.status value(s) {unknown}. "
            "The field was free text, so this migration maps only values it can map "
            "safely and refuses the rest. Set each affected row to one of "
            f"{sorted(FORWARD_MAP)} (or directly to one of {list(NEW_STATES)}) and "
            "re-run."
        )

    for old_value, new_value in FORWARD_MAP.items():
        Restaurant.objects.filter(status=old_value).update(status=new_value)

    # Data-integrity check: prove every row landed on a valid choice before the
    # migration is allowed to commit.
    remaining = [
        value for value in _distinct_statuses(Restaurant) if value not in NEW_STATES
    ]
    if remaining:
        raise RuntimeError(
            f"restaurants_app.0056: post-migration integrity check failed — rows "
            f"remain on invalid status value(s) {remaining}."
        )


def backward(apps, schema_editor):
    Restaurant = apps.get_model('restaurants_app', 'Restaurant')

    present = _distinct_statuses(Restaurant)

    irreversible = [value for value in present if value == 'offboarded']
    if irreversible:
        count = Restaurant.objects.filter(status='offboarded').count()
        raise RuntimeError(
            f"restaurants_app.0056 (reverse): {count} restaurant(s) are `offboarded`, "
            "a state that did not exist before this migration and therefore has no "
            "prior value to restore. Decide what each should become in the old "
            "vocabulary (e.g. 'inactive') and set it before reversing."
        )

    unknown = [
        value for value in present
        if value not in REVERSE_MAP and value not in FORWARD_MAP
    ]
    if unknown:
        raise RuntimeError(
            f"restaurants_app.0056 (reverse): unrecognised status value(s) {unknown}."
        )

    for new_value, old_value in REVERSE_MAP.items():
        Restaurant.objects.filter(status=new_value).update(status=old_value)


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0055_sanitize_menu_item_extras'),
    ]

    operations = [
        # Schema first, then data. Django reverses operations in reverse order, so
        # unwinding runs `backward` (restoring the old values) BEFORE the field is
        # widened back — which is the order that keeps the data valid at every step.
        migrations.AlterField(
            model_name='restaurant',
            name='status',
            field=models.CharField(
                choices=[
                    ('onboarding', 'Onboarding'),
                    ('live', 'Live'),
                    ('suspended', 'Suspended'),
                    ('offboarded', 'Offboarded'),
                ],
                db_index=True,
                default='onboarding',
                max_length=255,
            ),
        ),
        migrations.RunPython(forward, backward),
    ]
