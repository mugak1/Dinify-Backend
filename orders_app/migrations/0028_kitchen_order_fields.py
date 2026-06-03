from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    """
    Phase 2 schema additions (non-destructive):
      - the kitchen fulfilment axis + provenance/idempotency fields on Order,
      - the OrderItem snapshot fields,
      - the RestaurantDailyOrderCounter model,
      - the client_order_id unique constraint (safe: every existing row is NULL).

    The (restaurant, order_date, order_number) constraint and the KitchenTicket
    table drops are deferred to 0030 so the data backfill in 0029 can run first.
    """

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("restaurants_app", "0048_restaurant_menu_item_sort_mode"),
        ("orders_app", "0027_kitchenticket_kitchenticketitem"),
    ]

    operations = [
        migrations.CreateModel(
            name="RestaurantDailyOrderCounter",
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
                ("time_created", models.DateTimeField(auto_now_add=True)),
                ("time_last_updated", models.DateTimeField(auto_now=True)),
                ("time_deleted", models.DateTimeField(blank=True, null=True)),
                ("deleted", models.BooleanField(default=False)),
                (
                    "deletion_reason",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("archived", models.BooleanField(default=False)),
                ("vacuumed", models.BooleanField(default=False)),
                ("eod_last_date", models.DateField(db_index=True, null=True)),
                ("eod_record_date", models.DateField(db_index=True, null=True)),
                ("order_date", models.DateField()),
                ("next_number", models.PositiveIntegerField(default=1)),
            ],
            options={
                "db_table": "restaurant_daily_order_counters",
            },
        ),
        migrations.AddField(
            model_name="order",
            name="client_order_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="fulfilment_status",
            field=models.CharField(
                choices=[
                    ("new", "New"),
                    ("preparing", "Preparing"),
                    ("ready", "Ready"),
                    ("served", "Served"),
                ],
                db_index=True,
                default="new",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="fulfilment_status_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="fulfilment_status_updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="orders_fulfilment_updated",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="order_date",
            field=models.DateField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="order_source",
            field=models.CharField(
                choices=[
                    ("diner_self_service", "Diner self-service"),
                    ("server_assisted", "Server assisted"),
                ],
                db_index=True,
                default="diner_self_service",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="priority",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="served_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="allergen_tags_snapshot",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="item_name_snapshot",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="modifiers_snapshot",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="restaurantdailyordercounter",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="%(class)s_created_by",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="restaurantdailyordercounter",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="%(class)s_deleted_by",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="restaurantdailyordercounter",
            name="restaurant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="restaurants_app.restaurant",
            ),
        ),
        migrations.AddConstraint(
            model_name="restaurantdailyordercounter",
            constraint=models.UniqueConstraint(
                fields=("restaurant", "order_date"), name="uniq_daily_counter"
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                condition=models.Q(("client_order_id__isnull", False)),
                fields=("restaurant", "client_order_id"),
                name="uniq_order_restaurant_client_order_id",
            ),
        ),
    ]
