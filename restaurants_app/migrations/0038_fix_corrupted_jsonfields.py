"""
Data migration: heal JSONField values that were saved as JSON-encoded strings
due to the multipart/form-data parsing bug on SerializerPutMenuItem and
SerializerPutMenuSection. For each affected row, parse the string back into
its intended list/dict and re-save.
"""
import json

from django.db import migrations, transaction


MENUITEM_FIELDS = [
    ('options', dict),
    ('allergens', list),
    ('tags', list),
    ('discount_details', dict),
    ('extras_applicable', list),
]
MENUSECTION_FIELDS = [('schedules', list)]


def _coerce(parsed, expected_type):
    """
    Return parsed if it matches expected_type, otherwise return the empty
    form of that type. This protects against rows where the string-encoded
    JSON parsed to a value of the wrong shape (e.g. 'null' parsing to
    Python None on a column with default=dict and no null=True).
    """
    if isinstance(parsed, expected_type):
        return parsed
    return expected_type()


def _heal(apps, model_label, field_specs):
    Model = apps.get_model('restaurants_app', model_label)

    planned = []
    for row in Model.objects.all().iterator():
        row_changes = {}
        for field_name, expected_type in field_specs:
            value = getattr(row, field_name)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    row_changes[field_name] = expected_type()
                    continue
                row_changes[field_name] = _coerce(parsed, expected_type)
            elif value is None and expected_type is dict:
                # NOT NULL violations: a None sitting in a default=dict column
                # (which has no null=True) needs healing too. Coerce to {}.
                # We don't do the same for list columns because some have
                # null=True; coercing those would change behaviour.
                row_changes[field_name] = {}
        if row_changes:
            planned.append((row.pk, row_changes))

    for pk, changes in planned[:5]:
        print(f"[0038 migration] {model_label} pk={pk} preview:")
        for field_name, new_value in changes.items():
            print(f"  {field_name}: {type(new_value).__name__}")

    for pk, changes in planned:
        row = Model.objects.get(pk=pk)
        for field_name, new_value in changes.items():
            setattr(row, field_name, new_value)
        row.save(update_fields=list(changes.keys()))

    print(f"[0038 migration] {model_label}: fixed {len(planned)} row(s)")


def heal_corrupted(apps, schema_editor):
    with transaction.atomic():
        _heal(apps, 'MenuItem', MENUITEM_FIELDS)
        _heal(apps, 'MenuSection', MENUSECTION_FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0037_add_calories_to_menuitem'),
    ]

    operations = [
        migrations.RunPython(heal_corrupted, migrations.RunPython.noop),
    ]
