"""
Write-time menu relationship integrity.

The COMPANION to ``restaurants_app/controllers/menu_publication.py``: where that
module answers "what may a diner SEE and ORDER at runtime" (and fails closed
against bad persisted data), THIS module answers "what relationships may an
operator PERSIST". It is the write-time authority behind ``SerializerPutMenuItem``
and the ``deletion_blockers()`` on ``MenuItem`` / ``MenuSection`` / ``SectionGroup``.

Invariants enforced here:

* ``extras_applicable`` is a canonical ordered list of unique lowercase-UUID
  strings, each referring to an existing, non-deleted, same-restaurant
  ``MenuItem`` with ``is_extra=True``, and never the parent item itself.
* ``has_extras=False`` cannot retain an allowlist or non-zero selection limits.
* Extras selection limits are validated against the COMPLETE effective state
  (persisted values merged with the partial update), never merely incoming attrs.
* A non-null ``section_group`` always belongs to the item's EXACT section — even
  when a partial update moves the section but OMITS ``section_group`` (the group
  is retained and re-validated against the new section, not silently kept).
* A referenced extra cannot be demoted (``is_extra`` True->False) or soft-deleted
  while an active same-restaurant parent still depends on it.

Unlike ``menu_publication`` (domain-only, DRF-free), this module is write-time and
raises ``rest_framework.serializers.ValidationError`` so messages bind to the
right field. It reuses ``menu_publication.normalize_extras_applicable`` ONLY to
parse an already-PERSISTED allowlist (deliberately tolerant); fresh caller input
is validated strictly and REJECTED on any invalid member, never silently dropped.

Concurrency: the tenancy resolver and the lifecycle guard take ``select_for_update``
row locks on the EXTRA item's own row, so a parent assigning an extra and an
operation demoting/deleting that same extra serialize on one row and can never
both commit an invalid final relationship. Callers must run inside a transaction
(``Secretary.create``/``update`` already are; the menu-item delete path is wrapped
explicitly at the endpoint).
"""
from dataclasses import dataclass

from django.db.models import Q
from rest_framework import serializers

from restaurants_app.models import MenuItem
from restaurants_app.controllers.menu_publication import normalize_extras_applicable


# One opaque, non-enumerating rejection for any extra that is foreign / unknown /
# not an is_extra / soft-deleted / a self-reference — the response never reveals
# whether an id exists on another tenant (no enumeration). Identical create+update.
INVALID_EXTRAS_MESSAGE = (
    "One or more selected extras do not belong to this restaurant or are not "
    "valid extras."
)
DUPLICATE_EXTRAS_MESSAGE = "Duplicate extra selections are not allowed."
EXTRAS_NOT_ACCEPTED_MESSAGE = "Cannot set extras when the item does not accept extras."
SECTION_GROUP_MISMATCH_MESSAGE = "A section group must belong to the menu item's section."
CROSS_RESTAURANT_MOVE_MESSAGE = "Cannot move a menu item to another restaurant's section."
MIN_EXCEEDS_MAX_MESSAGE = "Minimum extras cannot exceed maximum extras."
MIN_EXCEEDS_COUNT_MESSAGE = "Minimum extras cannot exceed the number of applicable extras."
MAX_EXCEEDS_COUNT_MESSAGE = "Maximum extras cannot exceed the number of applicable extras."
EXTRA_STILL_REFERENCED_MESSAGE = (
    "This extra is still used by one or more menu items. Remove it from those "
    "items before changing or deleting it."
)


@dataclass
class EffectiveMenuItemState:
    """The complete post-write state of a MenuItem: the partial update merged over
    the persisted instance. ``*_supplied`` records whether the client actually sent
    the field (present in ``attrs``) — the ONLY way to tell "omitted" (leave alone)
    from an explicit ``null`` (clear), which ``attrs.get()`` collapses."""
    instance: object
    restaurant_id: object
    section_supplied: bool
    effective_section: object
    effective_section_id: object
    group_supplied: bool
    group_cleared: bool
    effective_group: object
    has_extras_supplied: bool
    effective_has_extras: bool
    extras_supplied: bool
    effective_extras: list
    min_supplied: bool
    effective_min: int
    max_supplied: bool
    effective_max: object
    effective_is_extra: bool
    effective_deleted: bool


def build_effective_state(*, instance, attrs) -> EffectiveMenuItemState:
    """Merge the partial ``attrs`` over ``instance`` into an effective-state view.
    Membership (``'field' in attrs``) — NOT ``attrs.get()`` — distinguishes omitted
    from explicit-null. ``restaurant_id`` is resolved ONCE from the item's stable
    home (its current section on update; the supplied section on create); a
    cross-restaurant move is rejected separately by ``validate_section_move``."""
    inst = instance

    section_supplied = 'section' in attrs
    effective_section = attrs['section'] if section_supplied else (inst.section if inst else None)
    effective_section_id = getattr(effective_section, 'id', None) if effective_section is not None else None

    if inst is not None:
        restaurant_id = inst.section.restaurant_id
    elif effective_section is not None:
        restaurant_id = effective_section.restaurant_id
    else:
        restaurant_id = None

    group_supplied = 'section_group' in attrs
    if group_supplied:
        group_value = attrs.get('section_group')      # SectionGroup instance or None
        group_cleared = group_value is None
        effective_group = group_value
    else:
        group_cleared = False
        effective_group = inst.section_group if inst is not None else None

    has_extras_supplied = 'has_extras' in attrs
    effective_has_extras = attrs['has_extras'] if has_extras_supplied else (inst.has_extras if inst else False)

    extras_supplied = 'extras_applicable' in attrs
    if extras_supplied:
        # list[uuid.UUID] straight from the typed field; canonicalised by the caller
        # before this list is consulted for the min/max count.
        effective_extras = attrs['extras_applicable']
    elif inst is not None:
        effective_extras = normalize_extras_applicable(inst.extras_applicable)
    else:
        effective_extras = []

    min_supplied = 'extras_min_selections' in attrs
    effective_min = attrs['extras_min_selections'] if min_supplied else (inst.extras_min_selections if inst else 0)
    max_supplied = 'extras_max_selections' in attrs
    effective_max = attrs['extras_max_selections'] if max_supplied else (inst.extras_max_selections if inst else None)

    effective_is_extra = attrs['is_extra'] if 'is_extra' in attrs else (inst.is_extra if inst else False)
    effective_deleted = attrs['deleted'] if 'deleted' in attrs else (inst.deleted if inst else False)

    return EffectiveMenuItemState(
        instance=inst,
        restaurant_id=restaurant_id,
        section_supplied=section_supplied,
        effective_section=effective_section,
        effective_section_id=effective_section_id,
        group_supplied=group_supplied,
        group_cleared=group_cleared,
        effective_group=effective_group,
        has_extras_supplied=has_extras_supplied,
        effective_has_extras=bool(effective_has_extras),
        extras_supplied=extras_supplied,
        effective_extras=effective_extras,
        min_supplied=min_supplied,
        effective_min=effective_min or 0,
        max_supplied=max_supplied,
        effective_max=effective_max,
        effective_is_extra=bool(effective_is_extra),
        effective_deleted=bool(effective_deleted),
    )


def validate_section_move(state) -> None:
    """UPDATE only: a menu item may not be moved to another restaurant's section.
    On create the endpoint gate already authorised the submitted section."""
    if state.instance is not None and state.section_supplied and state.effective_section is not None:
        if state.effective_section.restaurant_id != state.restaurant_id:
            raise serializers.ValidationError({'section': CROSS_RESTAURANT_MOVE_MESSAGE})


def validate_section_group_cohesion(state) -> None:
    """The effective (post-write) group, when non-null, must belong to the effective
    (post-write) section. This single rule covers every case: a supplied group is
    checked against the effective section; a group RETAINED across a section move
    (omitted, so kept from the instance) is re-checked against the NEW section and
    fails if it belonged to the old one; a cleared (explicit-null) group is a no-op."""
    group = state.effective_group
    if group is None or state.effective_section_id is None:
        return
    if group.section_id != state.effective_section_id:
        raise serializers.ValidationError({'section_group': SECTION_GROUP_MISMATCH_MESSAGE})


def canonicalize_submitted_extras(uuid_list) -> list:
    """Fresh caller input (``list[uuid.UUID]`` from the typed field) -> a canonical
    ordered list of lowercase-UUID strings. REJECTS a duplicate submission rather
    than silently deduping (write-time strictness; the runtime normalizer dedups
    persisted data instead). Order is preserved."""
    canonical = []
    seen = set()
    for value in uuid_list:
        member = str(value)                    # uuid.UUID str form is canonical lowercase
        if member in seen:
            raise serializers.ValidationError({'extras_applicable': DUPLICATE_EXTRAS_MESSAGE})
        seen.add(member)
        canonical.append(member)
    return canonical


def resolve_and_validate_extras_tenancy(canonical_ids, *, restaurant_id, self_id):
    """Resolve every submitted extra id in ONE bounded, restaurant-scoped, locking
    query and prove each is a same-restaurant, non-deleted ``is_extra`` item that is
    not the parent itself. Deliberately does NOT require approved/enabled/available/
    in_stock — an operator may configure an extra before publishing it (PR2's runtime
    policy decides diner visibility). Any failure -> one opaque, non-enumerating 400.

    Locks the extra rows (``select_for_update(of='self')``, pk order) so a concurrent
    demotion/deletion of a referenced extra serialises against this assignment."""
    if not canonical_ids:
        return {}
    locked = {
        str(item.id): item
        for item in (
            MenuItem.objects
            .select_for_update(of=('self',))
            .filter(pk__in=sorted(canonical_ids), section__restaurant_id=restaurant_id)
            .only('id', 'is_extra', 'deleted', 'section_id')
            .order_by('pk')
        )
    }
    self_id_str = str(self_id) if self_id is not None else None
    for cid in canonical_ids:
        item = locked.get(cid)
        if (
            item is None                       # foreign / unknown (never resolved in this restaurant)
            or cid == self_id_str              # self-reference
            or not item.is_extra               # not an extra
            or item.deleted                    # soft-deleted
        ):
            raise serializers.ValidationError({'extras_applicable': INVALID_EXTRAS_MESSAGE})
    return locked


def apply_has_extras_effective_state(state, attrs) -> None:
    """Enforce the has_extras/limit invariants on the effective state, mutating
    ``attrs`` to persist the canonical dependent state.

    has_extras=False: a deliberately non-empty allowlist is a conflicting
    instruction -> reject; otherwise the dependent config is coerced to the empty
    canonical form (``[]`` / 0 / None). Stale ``min``/``max`` resent by a frontend
    that only cleared the IDs are coerced silently (not rejected) so that flow 200s.

    has_extras=True: validate the full effective state — min<=positive-max,
    min<=count, positive-max<=count — REJECTING on violation (distinct from the
    repair migration, which CLAMPS)."""
    if not state.effective_has_extras:
        if state.extras_supplied and len(state.effective_extras) > 0:
            raise serializers.ValidationError({'extras_applicable': EXTRAS_NOT_ACCEPTED_MESSAGE})
        if (state.has_extras_supplied or state.extras_supplied
                or state.min_supplied or state.max_supplied):
            attrs['extras_applicable'] = []
            attrs['extras_min_selections'] = 0
            attrs['extras_max_selections'] = None
        return

    emin = state.effective_min or 0
    emax = state.effective_max
    if emax in (None, 0):
        emax = None
        # normalise a stored/submitted 0 to the canonical unlimited sentinel (None)
        if state.max_supplied or state.effective_max == 0:
            attrs['extras_max_selections'] = None
    count = len(state.effective_extras)
    if emax is not None and emin > emax:
        raise serializers.ValidationError({'extras_min_selections': MIN_EXCEEDS_MAX_MESSAGE})
    if emin > count:
        raise serializers.ValidationError({'extras_min_selections': MIN_EXCEEDS_COUNT_MESSAGE})
    if emax is not None and emax > count:
        raise serializers.ValidationError({'extras_max_selections': MAX_EXCEEDS_COUNT_MESSAGE})


def find_blocking_inbound_references(*, item_ids, restaurant_id,
                                     exclude_section=None, exclude_group=None):
    """Active same-restaurant parents (``has_extras=True``, not deleted) whose
    persisted ``extras_applicable`` references ANY of ``item_ids``. Restaurant-scoped
    (never an all-tenant scan) and bounded to a single query (a Q-OR of JSON
    containment). ``exclude_section``/``exclude_group`` drop parents inside the
    subtree being deleted (they are cascaded away and cannot be orphaned)."""
    ids = [str(i) for i in item_ids]
    if not ids:
        return MenuItem.objects.none()
    overlap = Q()
    for member in ids:
        overlap |= Q(extras_applicable__contains=[member])
    qs = (
        MenuItem.objects
        .filter(section__restaurant_id=restaurant_id, has_extras=True, deleted=False)
        .filter(overlap)
        .exclude(pk__in=ids)
        .only('id')
    )
    if exclude_section is not None:
        qs = qs.exclude(section=exclude_section)
    if exclude_group is not None:
        qs = qs.exclude(section_group=exclude_group)
    return qs


def lock_item_row(item_id):
    """Lock the item's OWN row before evaluating inbound references, so a concurrent
    assignment of this item as an extra serialises against its demotion/deletion."""
    return (
        MenuItem.objects
        .select_for_update(of=('self',))
        .filter(pk=item_id)
        .only('id', 'is_extra', 'deleted')
        .first()
    )


def assert_extra_not_referenced(*, item_id, restaurant_id, field) -> None:
    """Block a demotion/soft-delete while any active parent still depends on the
    extra. The error attaches to the triggering field (``is_extra`` or ``deleted``)
    and never discloses foreign-tenant parent data."""
    if find_blocking_inbound_references(item_ids=[item_id], restaurant_id=restaurant_id).exists():
        raise serializers.ValidationError({field: EXTRA_STILL_REFERENCED_MESSAGE})


def validate_menu_item_relationships(*, instance, attrs):
    """Ordered write-time relationship pipeline for ``SerializerPutMenuItem.validate``.
    Mutates and returns ``attrs`` (canonical extras + coerced dependent state) or
    raises ``serializers.ValidationError``. MUST run inside the caller's transaction
    so the row locks it takes are held through ``save()``."""
    state = build_effective_state(instance=instance, attrs=attrs)

    # A row that will be (or already is) soft-deleted needs no coherent forward
    # relationships — skip the structural checks so a delete (or an edit of an
    # already-deleted row) never trips on legacy-incoherent state; the lifecycle
    # guard below still runs so a referenced extra cannot be deleted.
    if not state.effective_deleted:
        validate_section_move(state)
        validate_section_group_cohesion(state)

        if state.extras_supplied:
            canonical = canonicalize_submitted_extras(attrs['extras_applicable'])
            # Only resolve/lock when the item will actually accept a non-empty
            # allowlist; a non-empty list while effective has_extras is False is
            # rejected by apply_has_extras_effective_state below.
            if state.effective_has_extras and canonical:
                resolve_and_validate_extras_tenancy(
                    canonical, restaurant_id=state.restaurant_id,
                    self_id=getattr(instance, 'id', None),
                )
            attrs['extras_applicable'] = canonical
            state.effective_extras = canonical

        apply_has_extras_effective_state(state, attrs)

    # Lifecycle guard: only an item that IS currently an extra can strand a
    # dependent parent, so a normal item's edit/delete never takes a lock or trips
    # on stale references. Demote = is_extra True->False; soft-delete of an extra.
    if instance is not None and bool(getattr(instance, 'is_extra', False)):
        demoting = 'is_extra' in attrs and attrs['is_extra'] is False
        deleting = attrs.get('deleted') is True and not getattr(instance, 'deleted', False)
        if demoting or deleting:
            lock_item_row(instance.id)
            assert_extra_not_referenced(
                item_id=instance.id, restaurant_id=state.restaurant_id,
                field='is_extra' if demoting else 'deleted',
            )

    return attrs
