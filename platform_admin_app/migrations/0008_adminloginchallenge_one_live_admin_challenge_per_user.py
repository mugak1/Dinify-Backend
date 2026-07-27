"""
One live login challenge per administrator, enforced by the database.

``challenges.create_challenge`` documented that a fresh password submission
invalidates the previous half-finished attempt, but it consumed and inserted in two
separate autocommitted statements with no lock — so two simultaneous correct-password
logins could interleave and leave two live challenges. The application side is now
serialised on the ``User`` row; this migration adds the partial unique index so the
invariant survives a future caller that forgets.

The data repair has to run FIRST: the index cannot be created while any user already
holds two unconsumed rows. It is idempotent and re-runnable — on a clean table it
writes nothing.

REVERSIBILITY. Dropping the constraint is clean, so the migration reverses. The repair
reverses as a no-op, deliberately: the rows it stamps were duplicates that the
application had already promised were invalid, and resurrecting them would recreate
exactly the state the constraint forbids.
"""
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def consume_duplicate_live_challenges(apps, schema_editor):
    """
    Stamp every surplus unconsumed challenge consumed, keeping the newest per user.

    Newest wins because that is the one the application would have handed to the
    browser last, and it matches the semantics of the code path this backfills:
    a later password submission supersedes an earlier one.
    """
    AdminLoginChallenge = apps.get_model('platform_admin_app', 'AdminLoginChallenge')

    duplicated_user_ids = (
        AdminLoginChallenge.objects
        .filter(consumed_at__isnull=True)
        .values('user_id')
        .annotate(live=Count('id'))
        .filter(live__gt=1)
        .values_list('user_id', flat=True)
    )

    now = timezone.now()
    for user_id in list(duplicated_user_ids):
        keep = (
            AdminLoginChallenge.objects
            .filter(user_id=user_id, consumed_at__isnull=True)
            .order_by('-created_at', '-id')
            .values_list('id', flat=True)
            .first()
        )
        AdminLoginChallenge.objects.filter(
            user_id=user_id, consumed_at__isnull=True,
        ).exclude(id=keep).update(consumed_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin_app", "0007_adminloginchallenge_recovery_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            consume_duplicate_live_challenges,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="adminloginchallenge",
            constraint=models.UniqueConstraint(
                condition=models.Q(("consumed_at__isnull", True)),
                fields=("user",),
                name="one_live_admin_challenge_per_user",
            ),
        ),
    ]
