"""
D02 — ONE coherent catalogue read and ONE pricing instant for a whole order.

WHY THIS EXISTS. Before it, building a four-line order read the same rows three
times in three separate statements (``validate_order_selections``' batch,
``normalize_order_items``' batch, and ``add_order_item``'s per-line scoped guard),
fetched every line's allergen tags in its own query, and read the clock 28 times
(measured: 9 ``localdate`` + 19 ``localtime`` for four lines). Under READ COMMITTED
every one of those statements takes a fresh snapshot, so a menu edit committing
mid-build could have line 1 validated against one version of a row and line 3
priced against another, and a discount window closing mid-build could apply to one
line and not the next. ``transaction.atomic()`` does not prevent any of that.

WHAT IS MATERIALIZED, AND FROM WHICH STATEMENT. Exactly ONE statement runs:

  (1) SELECT FROM menu_items
        INNER JOIN menu_sections
        LEFT  JOIN section_groups
        LEFT  JOIN menu_sections (the group's own section)
        LEFT  JOIN menu_item_tags JOIN restaurant_tags  (aggregated)
      WHERE menu_items.id IN (...) AND menu_sections.restaurant_id = %s
      GROUP BY the four joined primary keys

      It materializes every field any decision here reads: pricing
      (``primary_price``, ``discount_details``, ``options``), availability
      (``available``, ``in_stock``), publication (``approved``/``enabled``/
      ``deleted`` on the item, its section and its group, plus the section
      schedule fields), extras configuration (``is_extra``, ``has_extras``,
      ``extras_applicable``, ``extras_min_selections``,
      ``extras_max_selections``), the name snapshot AND the allergen labels.
      The three entity joins are what remove the lazy accesses —
      ``item.section``, ``item.section_group`` and, easy to miss,
      ``item.section_group.section``, which ``group_operationally_visible``
      dereferences.

WHY THE ALLERGEN LABELS ARE IN THAT STATEMENT AND NOT A SECOND ONE. They used to
be a separate batched read, argued safe because a tag edit could move only the
label text — true for a LABEL-ONLY edit, and not the statement that was needed.
One operator transaction that rewrites a recipe changes the dish DEFINITION and
its allergen LINKS TOGETHER, and under READ COMMITTED each statement takes its own
snapshot, so that single atomic edit could be observed HALF APPLIED: the old dish
name beside the new allergen labels, written onto a kitchen ticket describing a
catalogue state that existed at no instant. That is a preparation fact, not a
display detail, and it is the one an allergic diner is handed. Folding the labels
into the item statement makes coherence STRUCTURAL — one statement sees one
committed state by definition — and it costs a query rather than adding one.
``tests_order_snapshot.py`` drives the exact interleaving with a
``connection.execute_wrapper`` rather than a sleep, and keeps the label-only and
edit-after-the-snapshot cases as controls.

POSTGRESQL IS REQUIRED for that aggregate, deliberately and without a fallback. A
second, portable two-statement path would be a second opinion about the same
question, and the one it gives is the incoherent one — the failure mode would
reappear on exactly the deployment that took the fallback. This repository already
requires PostgreSQL for the order path (``JSONField`` ``__contains`` in the
deletion-blocker predicates), CI runs it, and production is RDS.

WHAT IT DELIBERATELY DOES NOT DO. It takes NO lock on ``menu_items``. Locking the
catalogue on every checkout would serialise every diner behind the menu editor and
would add a level to the documented lock ordering (advisory -> Table -> Counter ->
Order -> OrderItem) that nothing else takes. It would also buy nothing here: a price
committed a microsecond before this read is indistinguishable from one committed a
microsecond after, so a lock cannot make the read "more correct" — it can only
decide which of two equally valid instants wins. What protects the DINER from a
price they never saw is not a lock but the server-priced quote they acknowledge
before the order is accepted.
"""
import uuid

from django.contrib.postgres.aggregates import JSONBAgg
from django.db.models import F, Func, Q, TextField, Value

from restaurants_app.models import MenuItem


class ResolvedItem:
    """One catalogue row, fully materialized at the snapshot's instant."""

    __slots__ = ('menu_item', 'verdict', 'allergen_tags')

    def __init__(self, menu_item, verdict, allergen_tags):
        self.menu_item = menu_item
        self.verdict = verdict
        self.allergen_tags = allergen_tags

    @property
    def name(self):
        return self.menu_item.name


class CatalogueSnapshot:
    """Immutable view of every catalogue row ONE order references, at ONE instant.

    ``now`` is captured by the caller AFTER the blocking admission and table
    acquisition, so the instant an order is priced at is the instant it was
    actually admitted — not one sampled before the request waited on a lock.
    """

    __slots__ = ('now', 'restaurant', '_items')

    def __init__(self, *, now, restaurant, items):
        self.now = now
        self.restaurant = restaurant
        self._items = items

    def get(self, item_id):
        """Resolved row for ``item_id``, or ``None`` when it is not in scope.

        ``None`` means the id did not resolve inside this restaurant — the
        caller's existing opaque menu refusal, never a fallback.
        """
        key = _as_uuid(item_id)
        return self._items.get(key) if key is not None else None

    def menu_item(self, item_id):
        resolved = self.get(item_id)
        return resolved.menu_item if resolved is not None else None

    def as_map(self):
        """``{pk: MenuItem}`` for the publication validator, which is written
        against that shape. Same rows, same statement — supplying it is what
        stops publication taking a second, independent read."""
        return {
            item_id: resolved.menu_item
            for item_id, resolved in self._items.items()
        }

    def __contains__(self, item_id):
        return self.get(item_id) is not None

    def __len__(self):
        return len(self._items)


def _as_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def referenced_item_ids(items):
    """Every menu-item id one validated order request refers to — parents AND
    extras — so the whole order resolves in one statement rather than per line."""
    wanted = []
    for line in items:
        if not isinstance(line, dict):
            continue
        parsed = _as_uuid(line.get('item'))
        if parsed is not None:
            wanted.append(parsed)
        for extra_id in (line.get('extras') or []):
            parsed_extra = _as_uuid(extra_id)
            if parsed_extra is not None:
                wanted.append(parsed_extra)
    return wanted


def build_snapshot(restaurant, items, now):
    """Resolve the whole order's catalogue in two statements (see module doc)."""
    wanted = set(referenced_item_ids(items))
    if not wanted:
        return CatalogueSnapshot(now=now, restaurant=restaurant, items={})

    rows = list(
        MenuItem.objects
        .filter(pk__in=wanted, section__restaurant=restaurant)
        # section_group__section is NOT redundant: group_operationally_visible
        # dereferences the group's own section, which would otherwise be a lazy
        # load per grouped line and a read outside this snapshot.
        .select_related('section', 'section_group', 'section_group__section')
        .annotate(allergen_labels=_ALLERGEN_LABELS)
    )

    resolved = {}
    for row in rows:
        resolved[row.pk] = ResolvedItem(
            menu_item=row,
            # The price verdict is computed HERE, once, from columns that came
            # out of the single statement, at the snapshot's captured instant.
            verdict=row.price_verdict(now),
            # `default=None` rather than an empty array: an item with no
            # allergen tags aggregates to SQL NULL, and normalising it here
            # keeps every consumer reading a list.
            allergen_tags=row.allergen_labels or [],
        )
    return CatalogueSnapshot(now=now, restaurant=restaurant, items=resolved)


#: ONE allergen label, built in SQL so the aggregate carries whole labels rather
#: than three parallel arrays a reader would have to trust are aligned. The keys
#: are typed TEXT explicitly: an untyped parameter inside ``jsonb_build_object``
#: is ambiguous to PostgreSQL's polymorphic resolution.
def _label_key(name):
    return Value(name, output_field=TextField())


_ALLERGEN_LABEL = Func(
    _label_key('name'), F('tags__name'),
    _label_key('icon'), F('tags__icon'),
    _label_key('colour'), F('tags__colour'),
    function='JSONB_BUILD_OBJECT',
)

#: The allergen labels for one menu item, aggregated inside the item statement.
#: ``filter`` keeps the join a LEFT one for items with no allergen tags — they
#: must still appear in the result — and ``order_by`` reproduces the display
#: order the separate read used to apply in Python.
_ALLERGEN_LABELS = JSONBAgg(
    _ALLERGEN_LABEL,
    filter=Q(tags__category='allergen'),
    order_by=('tags__display_order', 'tags__name'),
    default=None,
)
