from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Post-backfill schema changes:
      - add the (restaurant, order_date, order_number) unique constraint now that
        0029 has populated order_date and deduped historical order numbers,
      - retire the dead KitchenTicket KDS (drops kitchen_tickets and
        kitchen_ticket_items).
    """

    dependencies = [
        ("orders_app", "0029_backfill_order_date_and_fulfilment"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                condition=models.Q(("order_number__isnull", False)),
                fields=("restaurant", "order_date", "order_number"),
                name="uniq_order_restaurant_date_number",
            ),
        ),
        migrations.RemoveField(
            model_name="kitchenticket",
            name="created_by",
        ),
        migrations.RemoveField(
            model_name="kitchenticket",
            name="deleted_by",
        ),
        migrations.RemoveField(
            model_name="kitchenticket",
            name="order",
        ),
        migrations.RemoveField(
            model_name="kitchenticket",
            name="restaurant",
        ),
        migrations.RemoveField(
            model_name="kitchenticket",
            name="table",
        ),
        migrations.RemoveField(
            model_name="kitchenticketitem",
            name="created_by",
        ),
        migrations.RemoveField(
            model_name="kitchenticketitem",
            name="deleted_by",
        ),
        migrations.RemoveField(
            model_name="kitchenticketitem",
            name="order_item",
        ),
        migrations.RemoveField(
            model_name="kitchenticketitem",
            name="ticket",
        ),
        migrations.DeleteModel(
            name="KitchenTicket",
        ),
        migrations.DeleteModel(
            name="KitchenTicketItem",
        ),
    ]
