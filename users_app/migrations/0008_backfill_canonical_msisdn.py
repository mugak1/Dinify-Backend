"""
Backfill User.phone_number / User.username to the canonical 256XXXXXXXXX form.

Idempotent and re-runnable: already-canonical rows are no-ops. Collisions,
diverged usernames, invalid values and non-UG countries are detected and SKIPPED
(never half-applied), and reported in a summary. Full before->after output is
gated behind MSISDN_BACKFILL_DEBUG (dev-only, off by default); the default
summary prints counts plus defensively-masked before->after examples.
"""
from django.db import migrations, transaction, IntegrityError


def _emit(label, row_id, orig_phone, after, debug):
    from misc_app.controllers.msisdn import mask_msisdn
    if debug:
        shown_orig = orig_phone
        shown_after = after if after is not None else '(unchanged)'
    else:
        shown_orig = mask_msisdn(orig_phone)
        shown_after = mask_msisdn(after) if after is not None else '(unchanged)'
    print(f"[msisdn-backfill] {label} id={row_id}: {shown_orig} -> {shown_after}")


def backfill_forward(apps, schema_editor):
    from decouple import config
    from misc_app.controllers.msisdn import plan_msisdn_backfill

    User = apps.get_model('users_app', 'User')
    debug = config('MSISDN_BACKFILL_DEBUG', default=False, cast=bool)

    rows = list(User.objects.all().values_list('id', 'phone_number', 'username', 'country'))
    plan = plan_msisdn_backfill(rows)

    converted = 0
    already_canonical = 0
    for w in plan.writes:
        if not w['changed']:
            already_canonical += 1
            continue
        try:
            # Per-row savepoint so an unforeseen unique-constraint hit rolls back
            # only this row and the outer migration transaction survives.
            with transaction.atomic():
                User.objects.filter(id=w['id']).update(
                    phone_number=w['canonical'],
                    username=w['canonical'],
                )
            converted += 1
            _emit('converted', w['id'], w['orig_phone'], w['canonical'], debug)
        except IntegrityError as exc:
            plan.collision.append(
                {'id': w['id'], 'orig_phone': w['orig_phone'], 'canonical': w['canonical']}
            )
            print(f"[msisdn-backfill] UNEXPECTED collision, skipped id={w['id']}: {exc}")

    print(
        "[msisdn-backfill] summary: "
        f"{converted} converted, {already_canonical} already-canonical, "
        f"{len(plan.invalid)} skipped-invalid, "
        f"{len(plan.unsupported)} skipped-unsupported-country, "
        f"{len(plan.diverged)} skipped-diverged, "
        f"{len(plan.collision)} skipped-collision "
        f"(of {len(rows)} rows)"
    )

    for item in plan.invalid:
        _emit('invalid', item['id'], item['orig_phone'], None, debug)
    for item in plan.unsupported:
        _emit('unsupported-country', item['id'], item['orig_phone'], None, debug)
    for item in plan.diverged:
        _emit('diverged', item['id'], item['orig_phone'], None, debug)
    for item in plan.collision:
        _emit('collision', item['id'], item['orig_phone'], item.get('canonical'), debug)


class Migration(migrations.Migration):

    dependencies = [
        ('users_app', '0007_alter_user_prompt_password_change'),
    ]

    operations = [
        migrations.RunPython(backfill_forward, migrations.RunPython.noop),
    ]
