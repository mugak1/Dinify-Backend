"""
Data migration: assign a sensible listing_position to every existing MenuItem,
so the new ordering field has meaningful values for already-created data.

Strategy: per section, order items alphabetically by name (matching the
previous Meta.ordering behaviour) and assign incrementing positions 0..N-1.
Result: existing data renders in the same visual order it did before this
PR. Reordering becomes possible going forward but doesn't disrupt the past.
"""
from django.db import migrations, transaction


def backfill_listing_position(apps, schema_editor):
    MenuSection = apps.get_model('restaurants_app', 'MenuSection')
    MenuItem = apps.get_model('restaurants_app', 'MenuItem')

    sections = MenuSection.objects.all().iterator()
    total_items = 0
    total_sections = 0

    with transaction.atomic():
        for section in sections:
            items = list(
                MenuItem.objects.filter(section=section).order_by('name', 'time_created')
            )
            if not items:
                continue
            total_sections += 1
            for index, item in enumerate(items):
                if item.listing_position != index:
                    item.listing_position = index
                    item.save(update_fields=['listing_position'])
                total_items += 1

    print(f'[0040 migration] backfilled {total_items} items across {total_sections} sections')


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0039_menuitem_listing_position'),
    ]

    operations = [
        migrations.RunPython(backfill_listing_position, migrations.RunPython.noop),
    ]
