"""
Data backfill for the Phase 2 fulfilment axis.

Runs after 0028 (fields added) and before 0030 (which adds the
(restaurant, order_date, order_number) unique constraint):

  1. order_date = local date of time_created for every order — required before
     the partial unique constraint can hold on historical rows.
  2. fulfilment_status derived from order_status (NOT from payment): 'served'
     -> 'served' (served_at = time_last_updated); 'preparing' -> 'preparing';
     everything else (paid/pending/initiated/...) -> 'new'.
  3. dedup any (restaurant, order_date, order_number) collisions left by the
     retired race-prone numbering signal, so the constraint can be added.
  4. seed RestaurantDailyOrderCounter from the existing max(order_number) per
     (restaurant, order_date) so the first order created after deploy does not
     re-allocate a number that already exists for the same day.
"""
from collections import defaultdict

from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Order = apps.get_model("orders_app", "Order")
    Counter = apps.get_model("orders_app", "RestaurantDailyOrderCounter")

    # 1 + 2. order_date / fulfilment_status / served_at
    to_update = []
    for order in Order.objects.all().only(
        "id", "time_created", "time_last_updated", "order_status",
        "order_date", "fulfilment_status", "served_at",
    ):
        if order.time_created is not None:
            order.order_date = timezone.localtime(order.time_created).date()
        if order.order_status == "served":
            order.fulfilment_status = "served"
            order.served_at = order.time_last_updated
        elif order.order_status == "preparing":
            order.fulfilment_status = "preparing"
        else:
            order.fulfilment_status = "new"
        to_update.append(order)
    if to_update:
        Order.objects.bulk_update(
            to_update,
            ["order_date", "fulfilment_status", "served_at"],
            batch_size=500,
        )

    # 3. dedup duplicate order_numbers within a (restaurant, order_date) group.
    #    Process oldest-first so the earliest order keeps its original number.
    used_by_key = defaultdict(set)
    dupes = []
    for order in (
        Order.objects.filter(order_number__isnull=False)
        .order_by("time_created")
        .only("id", "restaurant_id", "order_date", "order_number")
    ):
        key = (order.restaurant_id, order.order_date)
        used = used_by_key[key]
        if order.order_number in used:
            candidate = (max(used) + 1) if used else 1
            while candidate in used:
                candidate += 1
            order.order_number = candidate
            dupes.append(order)
        used.add(order.order_number)
    if dupes:
        Order.objects.bulk_update(dupes, ["order_number"], batch_size=500)

    # 4. seed the daily counters from the (now unique) max order_number per day.
    max_by_key = {}
    for order in Order.objects.filter(
        order_number__isnull=False, order_date__isnull=False
    ).only("restaurant_id", "order_date", "order_number"):
        key = (order.restaurant_id, order.order_date)
        if order.order_number > max_by_key.get(key, 0):
            max_by_key[key] = order.order_number
    counters = [
        Counter(restaurant_id=restaurant_id, order_date=order_date, next_number=highest + 1)
        for (restaurant_id, order_date), highest in max_by_key.items()
    ]
    if counters:
        Counter.objects.bulk_create(counters, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("orders_app", "0028_kitchen_order_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=migrations.RunPython.noop),
    ]
