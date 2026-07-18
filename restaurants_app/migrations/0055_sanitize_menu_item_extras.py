"""
Data-only repair of persisted MenuItem relationship state (PR3).

PR #233 made the runtime read/order paths fail closed against bad data; this
migration deterministically REPAIRS the persisted rows so the write-time
invariants (restaurants_app/controllers/menu_relationships.py) hold for the
existing corpus. No schema change.

For every MenuItem it:
  * parses ``extras_applicable`` tolerantly (real list; JSON-encoded string;
    legacy Python-literal string via ``ast.literal_eval``; anything else -> []),
    canonicalises members to lowercase-UUID strings, preserves first-occurrence
    order, and DROPS duplicates, malformed members, self-references, and any id
    that is not a same-restaurant, non-deleted, ``is_extra`` MenuItem (covers
    foreign / non-extra / deleted / missing). Approval/enablement/availability/
    stock are deliberately NOT required — an unpublished extra is still valid;
  * coerces dependent state: a soft-deleted parent OR a ``has_extras=False``
    parent -> ``[]`` / has_extras False / min 0 / max None; an active
    ``has_extras=True`` parent keeps the repaired list but CLAMPS the limits to
    it (empty -> min 0/max None; else min<=len, max null/0 -> None else <=len,
    and min<=max);
  * clears a ``section_group`` whose section no longer matches the item's own
    section (incoherent group -> NULL; the item's section is preserved).

The cleanup is deterministic and IDEMPOTENT (a second pass makes no change) but
LOSSY — rejected ids cannot be reconstructed — so the reverse is a documented
no-op (``RunPython.noop``): this migration is intentionally irreversible.
"""
import ast
import json
import uuid
from collections import defaultdict

from django.db import migrations


# Fields written by the sanitizer (bulk_update writes all of them for any changed
# row; unchanged fields are rewritten to their current value — a harmless no-op).
SANITIZE_FIELDS = [
    'extras_applicable',
    'has_extras',
    'extras_min_selections',
    'extras_max_selections',
    'section_group',
]


def _parse_stored_extras(raw):
    """Tolerantly coerce a persisted extras_applicable value to a Python list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError, TypeError):
                return []
        return parsed if isinstance(parsed, list) else []
    return []


def _canonical_uuid(value):
    """Return the canonical lowercase-UUID string for ``value`` or None."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _sanitize_row(row, item_restaurant, valid_extras_by_restaurant, group_section):
    """Pure, deterministic, idempotent sanitizer for a single historical MenuItem.

    Mutates ``row`` in place and returns the set of changed field names (empty if
    nothing changed). ``item_restaurant`` maps item id -> restaurant id;
    ``valid_extras_by_restaurant`` maps restaurant id -> set of valid extra id
    strings (is_extra, not deleted); ``group_section`` maps section-group id ->
    section id."""
    changed = set()

    restaurant_id = item_restaurant.get(row.id)
    valid = valid_extras_by_restaurant.get(restaurant_id, set())
    self_id = str(row.id)

    cleaned = []
    seen = set()
    for member in _parse_stored_extras(row.extras_applicable):
        canonical = _canonical_uuid(member)
        if canonical is None or canonical in seen or canonical == self_id:
            continue
        if canonical not in valid:
            continue
        seen.add(canonical)
        cleaned.append(canonical)

    if row.deleted or not row.has_extras:
        target_extras, target_has, target_min, target_max = [], False, 0, None
    else:
        target_has = True
        count = len(cleaned)
        if count == 0:
            target_extras, target_min, target_max = [], 0, None
        else:
            target_extras = cleaned
            target_min = min(row.extras_min_selections or 0, count)
            if row.extras_max_selections in (None, 0):
                target_max = None
            else:
                target_max = min(row.extras_max_selections, count)
            if target_max is not None and target_min > target_max:
                target_min = target_max

    if row.extras_applicable != target_extras:
        row.extras_applicable = target_extras
        changed.add('extras_applicable')
    if bool(row.has_extras) != target_has:
        row.has_extras = target_has
        changed.add('has_extras')
    if (row.extras_min_selections or 0) != target_min:
        row.extras_min_selections = target_min
        changed.add('extras_min_selections')
    if row.extras_max_selections != target_max:
        row.extras_max_selections = target_max
        changed.add('extras_max_selections')

    if (
        row.section_group_id is not None
        and group_section.get(row.section_group_id) != row.section_id
    ):
        row.section_group_id = None
        changed.add('section_group')

    return changed


def sanitize(apps, schema_editor):
    MenuItem = apps.get_model('restaurants_app', 'MenuItem')
    SectionGroup = apps.get_model('restaurants_app', 'SectionGroup')

    # Global maps built with bounded queries and no image/file columns loaded.
    item_restaurant = {
        iid: rid
        for iid, rid in MenuItem.objects.values_list('id', 'section__restaurant_id')
    }
    valid_extras_by_restaurant = defaultdict(set)
    for iid, rid in (
        MenuItem.objects
        .filter(is_extra=True, deleted=False)
        .values_list('id', 'section__restaurant_id')
    ):
        valid_extras_by_restaurant[rid].add(str(iid))
    group_section = {
        gid: sid
        for gid, sid in SectionGroup.objects.values_list('id', 'section_id')
    }

    batch = []
    rows = (
        MenuItem.objects
        .only(
            'id', 'section_id', 'section_group_id', 'is_extra', 'deleted',
            'has_extras', 'extras_applicable',
            'extras_min_selections', 'extras_max_selections',
        )
        .iterator(chunk_size=500)
    )
    for row in rows:
        if _sanitize_row(row, item_restaurant, valid_extras_by_restaurant, group_section):
            batch.append(row)
            if len(batch) >= 500:
                MenuItem.objects.bulk_update(batch, SANITIZE_FIELDS)
                batch = []
    if batch:
        MenuItem.objects.bulk_update(batch, SANITIZE_FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0054_table_qr_version'),
    ]

    operations = [
        # Intentionally irreversible: the repair is lossy (rejected ids cannot be
        # reconstructed), so the reverse is a documented no-op.
        migrations.RunPython(sanitize, migrations.RunPython.noop),
    ]
