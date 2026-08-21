"""
Create the onboarding domain: ``RestaurantOnboarding`` + ``OwnerInvitation``.

PURELY ADDITIVE, AND THAT IS THE WHOLE POINT. Two ``CreateModel`` operations plus
the constraints and index those new tables need — no ``AlterField`` on Restaurant,
User or RestaurantEmployee, no ``RemoveField``, no ``RenameField``, no
``RunPython``, no data migration and no backfill. Nothing existing is touched.

EXPAND-ONLY / ROLLBACK REASONING (see ## Database in CLAUDE.md). A rollback moves
CODE backwards but never SCHEMA: the deploy script runs a bare forward ``migrate``
and has no reverse step, so old code has to keep working against the new schema.
It does here, trivially — the preceding deployed commit does not know these tables
exist, never names them in a query, and the tables have no FK pointing INTO an
existing table's write path that could constrain an existing insert. They simply
sit there, empty.

EMPTY IS THE CORRECT STATE. There is deliberately no backfill: every restaurant
that exists today, Baba House included, ends this migration with ZERO onboarding
rows. A row here asserts that an administrator brought a tenant into the Admin
onboarding domain, with provenance and an actor attached; manufacturing one from a
migration would assert an event that never happened and attribute it to nobody. The
first real row is written by the adoption service in the next PR.

LOCK PROFILE. Creating new tables takes locks only on those tables, which no other
session can be holding — nothing blocks, and no existing table is rewritten.
"""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "platform_admin_app",
            "0008_adminloginchallenge_one_live_admin_challenge_per_user",
        ),
        ("restaurants_app", "0057_restaurant_is_test"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RestaurantOnboarding",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("admin_created", "admin_created"),
                            ("legacy_adopted", "legacy_adopted"),
                        ],
                        max_length=32,
                    ),
                ),
                ("adopted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "owner_control_attested_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "adopted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="restaurant_onboardings_adopted",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="restaurant_onboardings_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner_control_attested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="restaurant_onboardings_attested",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner_control_attested_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="restaurant_onboardings_attested_as_owner",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "restaurant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="admin_onboarding",
                        to="restaurants_app.restaurant",
                    ),
                ),
            ],
            options={
                "db_table": "restaurant_onboarding",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="OwnerInvitation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("issued_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("superseded_at", models.DateTimeField(blank=True, null=True)),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="owner_invitations_cancelled",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invited_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="owner_invitations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "issued_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="owner_invitations_issued",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "onboarding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="owner_invitations",
                        to="platform_admin_app.restaurantonboarding",
                    ),
                ),
            ],
            options={
                "db_table": "owner_invitation",
                "ordering": ["-issued_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="restaurantonboarding",
            constraint=models.CheckConstraint(
                condition=models.Q(("source__in", ["admin_created", "legacy_adopted"])),
                name="restaurant_onboarding_source_vocabulary",
            ),
        ),
        migrations.AddConstraint(
            model_name="restaurantonboarding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("source", "admin_created"), _negated=True),
                    models.Q(
                        ("adopted_at__isnull", True),
                        ("adopted_by__isnull", True),
                        ("created_by__isnull", False),
                        ("owner_control_attested_at__isnull", True),
                        ("owner_control_attested_by__isnull", True),
                        ("owner_control_attested_user__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="restaurant_onboarding_admin_created_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="restaurantonboarding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("source", "legacy_adopted"), _negated=True),
                    models.Q(
                        ("adopted_at__isnull", False),
                        ("adopted_by__isnull", False),
                        ("created_by__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="restaurant_onboarding_legacy_adopted_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="restaurantonboarding",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("owner_control_attested_at__isnull", True),
                        ("owner_control_attested_by__isnull", True),
                        ("owner_control_attested_user__isnull", True),
                    ),
                    models.Q(
                        ("owner_control_attested_at__isnull", False),
                        ("owner_control_attested_by__isnull", False),
                        ("owner_control_attested_user__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="restaurant_onboarding_attestation_triple",
            ),
        ),
        migrations.AddIndex(
            model_name="ownerinvitation",
            index=models.Index(
                fields=["onboarding", "issued_at"],
                name="owner_invit_onboard_b39eeb_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("cancelled_at__isnull", True),
                    ("consumed_at__isnull", True),
                    ("superseded_at__isnull", True),
                ),
                fields=("onboarding",),
                name="one_unresolved_owner_invitation_per_onboarding",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("consumed_at__isnull", True),
                    ("cancelled_at__isnull", True),
                    _connector="OR",
                ),
                name="owner_invitation_not_consumed_and_cancelled",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("consumed_at__isnull", True),
                    ("superseded_at__isnull", True),
                    _connector="OR",
                ),
                name="owner_invitation_not_consumed_and_superseded",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("cancelled_at__isnull", True),
                    ("superseded_at__isnull", True),
                    _connector="OR",
                ),
                name="owner_invitation_not_cancelled_and_superseded",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cancelled_at__isnull", True), ("cancelled_by__isnull", True)
                    ),
                    models.Q(
                        ("cancelled_at__isnull", False), ("cancelled_by__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="owner_invitation_cancellation_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="ownerinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("expires_at__gt", models.F("issued_at"))),
                name="owner_invitation_expires_after_issue",
            ),
        ),
    ]
