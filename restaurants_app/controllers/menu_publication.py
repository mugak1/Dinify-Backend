"""
Canonical diner menu-publication & checkout-eligibility policy.

ONE server-owned definition of "what may an anonymous diner SEE, and what may an
anonymous diner ORDER", shared by the public menu read path (``handle_show_menu``
+ the public serializers) and the order-creation path (``ConOrder.initiate_order``
/ ``_create_order``). Centralising it here removes the accidental predicate drift
that previously let a record hidden by one path re-enter through another, or let a
menu-item UUID make an unpublished / foreign / structurally-hidden record orderable.

Two layers of visibility:

* **Structural publication** — ``approved`` & ``enabled`` & not soft-``deleted``.
  Identical for the read and the order paths (a structurally-unpublished record is
  neither shown nor orderable, ever).
* **Operational visibility** — additionally ``available`` and, for sections, the
  schedule window at a captured evaluation time. For SECTIONS and GROUPS this is
  enforced on BOTH paths (a paused/scheduled-off section hides its items and blocks
  ordering). For an ITEM's OWN ``available``/``in_stock`` the two paths deliberately
  differ: the read path hides an unavailable item, the order path lets it through
  the established zero-and-flag reconciliation (it is neither prepared nor charged).
  That single field is the ONLY read-vs-checkout difference.

Nested extras are a distinct role but never a tenant-isolation exception: an extra
must be an ``is_extra`` item of the SAME restaurant, structurally published, and
explicitly listed in its parent's ``extras_applicable``. Per the product contract
the extra catalogue is derived from ``is_extra`` items restaurant-wide, so an extra
inherits restaurant + structural publication but NOT the section schedule / group
operational-availability of wherever it happens to live.

This module is DOMAIN-ONLY: it imports models + the schedule helper + constants and
NEVER imports serializers, DRF, HTTP request objects, or ``orders_app`` (so it stays
free of import cycles). ``validate_order_selections`` RETURNS a status dict; the
order service translates a rejection into its own ``OrderItemRejected`` sentinel.
"""
import uuid

from restaurants_app.models import Restaurant, MenuItem
from restaurants_app.controllers.utils.schedule_utils import (
    is_section_currently_active,
)
from restaurants_app.controllers.lifecycle_policy import (
    DINER_MENU_ALLOWED, DINER_MENU_UNAVAILABLE, diner_menu_visibility,
)
from dinify_backend.configss.messages import (
    MESSAGES, ERR_RESTAURANT_REFERENCE_REQUIRED, ERR_RESTAURANT_UNAVAILABLE,
)

# One canonical, opaque rejection for anything that cannot be resolved / ordered on
# this restaurant's menu — foreign, nonexistent, malformed, wrong-type (not an
# extra), unpublished, or not applicable to the submitted parent. A single string
# by design so the response never reveals whether an id exists on another tenant
# (no enumeration). Lives here as the policy's canonical message; ``con_orders``
# re-exports it so existing importers keep working.
NOT_ON_MENU_MESSAGE = "One or more items are not on this restaurant's menu."


# --- restaurant ------------------------------------------------------------

def restaurant_can_serve_menu(restaurant) -> bool:
    """A restaurant may serve an anonymous diner menu only when it exists, is not
    soft-deleted, and is in a lifecycle state that serves the menu (``onboarding``
    or ``live`` — the diner surface is needed during onboarding for the go-live
    test order). ``accepting_orders`` is NOT a visibility condition (a restaurant
    may pause new orders yet show its menu)."""
    return (
        restaurant is not None
        and not restaurant.deleted
        and diner_menu_visibility(restaurant.status) == DINER_MENU_ALLOWED
    )


def resolve_public_restaurant(restaurant_ref):
    """
    Resolve a caller-supplied restaurant reference for the public menu, failing
    closed. Returns ``(restaurant, None)`` on success or ``(None, error_dict)``:

    * missing / blank                          -> 400 (clean "reference required")
    * malformed UUID                           -> 404 (generic, non-disclosing)
    * unknown / soft-deleted / offboarded      -> same 404
    * suspended                                -> 503 "temporarily unavailable"

    DISCLOSURE, DELIBERATE. Everything above collapses to one non-disclosing 404
    except ``suspended``, which is answered with a graceful 503 — so that response
    does admit "a restaurant exists at this id and is currently stopped". That is
    the point of the state: a diner standing at a table with a printed QR needs to
    be told the place is temporarily unavailable rather than that it never existed.
    The tenant chose to be findable when it printed the code. Every other failure —
    including ``offboarded``, where the relationship is over — stays a flat 404 and
    reveals nothing.
    """
    if restaurant_ref is None or str(restaurant_ref).strip() == '':
        return None, {'status': 400, 'message': ERR_RESTAURANT_REFERENCE_REQUIRED}
    try:
        rid = uuid.UUID(str(restaurant_ref))
    except (ValueError, TypeError, AttributeError):
        return None, {'status': 404, 'message': MESSAGES.get('RESTAURANT_NOT_FOUND')}
    restaurant = Restaurant.objects.filter(id=rid).first()
    if restaurant is None or restaurant.deleted:
        return None, {'status': 404, 'message': MESSAGES.get('RESTAURANT_NOT_FOUND')}

    visibility = diner_menu_visibility(restaurant.status)
    if visibility == DINER_MENU_UNAVAILABLE:
        return None, {'status': 503, 'message': ERR_RESTAURANT_UNAVAILABLE}
    if visibility != DINER_MENU_ALLOWED:
        return None, {'status': 404, 'message': MESSAGES.get('RESTAURANT_NOT_FOUND')}
    return restaurant, None


# --- liveness: the half of structural publication that is not policy --------
#
# D06 completion, G2-C. Structural publication is two different kinds of fact
# wearing one name. `deleted` is LIVENESS — a soft-deleted section is not a
# place a dish can exist, which is true whoever is asking, exactly as D06 says
# of a soft-deleted table. `approved` / `enabled` are PUBLICATION POLICY FOR THE
# QR PUBLIC, and an authorized member of staff taking an order on a diner's
# behalf walks past them (`enforce_publication=False` on the create path).
#
# Callers that must bind every provenance ask for LIVENESS; callers deciding
# what the public may see or order ask for PUBLICATION. `section_structurally_
# published` is composed from the liveness predicate rather than restating
# `not deleted`, so the two cannot drift into disagreeing about what deleted
# means.

def section_live(section) -> bool:
    """Not soft-deleted. Binds EVERY provenance."""
    return not bool(section.deleted)


def group_live(group) -> bool:
    """Not soft-deleted. Binds EVERY provenance."""
    return not bool(group.deleted)


# --- structural publication (identical read + order) -----------------------

def section_structurally_published(section) -> bool:
    return bool(section.approved and section.enabled and section_live(section))


def group_structurally_published(group) -> bool:
    return bool(group.approved and group.enabled and group_live(group))


def item_structurally_published(item) -> bool:
    return bool(item.approved and item.enabled and not item.deleted)


# --- operational visibility (section/group: read + order alike) ------------

def section_operationally_visible(section, now) -> bool:
    """Structurally published AND available AND schedule-active at ``now``."""
    return bool(
        section_structurally_published(section)
        and section.available
        and is_section_currently_active(section, now=now)
    )


def group_operationally_visible(group, now) -> bool:
    """Structurally published AND available AND its parent section is visible."""
    return bool(
        group_structurally_published(group)
        and group.available
        and section_operationally_visible(group.section, now)
    )


# --- top-level item visibility ---------------------------------------------

def item_priceable(item, now) -> bool:
    """Can this item's stored price configuration actually be read at ``now``?

    D02/R22. ``primary_price`` and ``discount_details`` are unvalidated columns,
    and a malformed value used to raise ``InvalidOperation`` out of
    ``MenuItem.is_discount_active`` — which the PUBLIC MENU serializer calls — so
    ONE bad row returned HTTP 500 for an entire restaurant's menu. Making
    priceability part of publication contains the failure to its own item: every
    valid neighbour stays readable, and the affected item is simply not published
    and not orderable.

    It is time-dependent on purpose. Only an ACTIVE incoherent discount makes an
    item unpriceable; the same item outside that discount's window prices
    normally from ``primary_price`` and stays on the menu.

    There is deliberately NO fallback price. Showing the undiscounted price for
    an item whose discount cannot be read presents an unearned charge as valid,
    and showing zero makes it free — the pre-fix ``price if price > 0 else
    Decimal('0')`` clamp did exactly that for a discount larger than the price.
    """
    return item.price_verdict(now).usable


def item_visible_in_menu(item, now) -> bool:
    """
    READ path: a normal top-level item is shown only when it is structurally
    published, ``available`` (an out-of-stock ``in_stock=False`` item is still
    shown — sold-out is a passthrough flag, not a filter), sits in a currently
    visible section, and is group-less or under a currently visible group.
    """
    if not item_structurally_published(item):
        return False
    if not item.available:
        return False
    if not item_priceable(item, now):
        return False
    if not section_operationally_visible(item.section, now):
        return False
    if item.section_group_id is not None and not group_operationally_visible(
        item.section_group, now
    ):
        return False
    return True


def item_published_now(item, now) -> bool:
    """Is this item PUBLISHED to the ordering public at ``now``?

    Publication and scheduling only: structurally published, in a currently
    visible section, and group-less or under a currently visible group. It asks
    NOTHING about money.

    D06 completion, G2-C split this out of ``item_orderable`` for one caller:
    acceptance re-checks whether a saved quote may still be PREPARED, and that
    question is answered at the amounts the diner already agreed to. Folding
    priceability into it meant an operator mistyping a discount AFTER the review
    destroyed the quote over a figure nobody was going to charge. Creating a NEW
    order is the opposite case — it has to price the line — so ``item_orderable``
    below is unchanged and is still what the create path asks.

    The item's OWN ``available``/``in_stock`` are DELIBERATELY excluded — those
    route to the established zero-and-flag reconciliation (the line is neither
    prepared nor charged) rather than a hard publication rejection.
    """
    if not item_structurally_published(item):
        return False
    if not section_operationally_visible(item.section, now):
        return False
    if item.section_group_id is not None and not group_operationally_visible(
        item.section_group, now
    ):
        return False
    return True


def item_orderable(item, now) -> bool:
    """
    ORDER path (publication only): a parent item is orderable when it is
    published to the ordering public at ``now`` AND its stored price can be read.
    """
    return item_published_now(item, now) and item_priceable(item, now)


# --- nested extras ---------------------------------------------------------

def extra_publishable(extra, parent, restaurant_id) -> bool:
    """
    Whether ``extra`` may be attached to ``parent`` as a nested extra. Structural
    inheritance ONLY (the documented product contract): the extra must be a real
    ``is_extra`` item of the same restaurant, structurally published, under a
    structurally-published (non-deleted) section, group-less or under a
    structurally-published group, and not a self-reference. It does NOT inherit the
    section schedule or group operational-availability (an extra legitimately sits
    in a scheduled-off/unavailable section yet remains attachable). ``available`` /
    ``in_stock`` are handled by zero-and-flag, never here. Membership in the
    parent's ``extras_applicable`` is checked separately by the caller.
    """
    if extra is None:
        return False
    if not extra.is_extra:
        return False
    if not item_structurally_published(extra):
        return False
    if str(extra.section.restaurant_id) != str(restaurant_id):
        return False
    if str(extra.id) == str(parent.id):
        return False
    if not section_structurally_published(extra.section):
        return False
    if extra.section_group_id is not None and not group_structurally_published(
        extra.section_group
    ):
        return False
    return True


def normalize_extras_applicable(raw):
    """
    Defensive runtime normalization of a persisted ``extras_applicable`` allowlist
    (PR3 owns write-time integrity). Accepts ONLY an actual list; parses valid-UUID
    members; drops malformed members and duplicates while preserving configured
    order. A non-list value normalizes to ``[]`` (fail closed). Returns a list of
    canonical lowercase-UUID strings.
    """
    if isinstance(raw, str):
        # a legacy string-encoded list is not trusted here — PR3 fixes the writer.
        import json
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for member in raw:
        try:
            canonical = str(uuid.UUID(str(member)))
        except (ValueError, TypeError, AttributeError):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def build_safe_extras_map(items, restaurant_id):
    """
    Batch-resolve every extra referenced by the given menu ``items`` into a
    ``{str(id): MenuItem}`` map of records that are safe to surface as nested
    extras (same restaurant, ``is_extra``, structurally published). ONE query,
    ``select_related`` for the tenant/publication checks. Availability-agnostic and
    schedule-agnostic on purpose (see ``extra_publishable``) — do NOT derive this
    from the visible-items set, which filters ``available=True`` and would drop
    valid extras. Callers filter this map by each parent's normalized allowlist.
    """
    wanted = set()
    for item in items:
        for eid in normalize_extras_applicable(item.extras_applicable):
            wanted.add(eid)
    if not wanted:
        return {}
    resolved = {}
    for extra in (
        MenuItem.objects
        .filter(pk__in=wanted, section__restaurant_id=restaurant_id, is_extra=True)
        .select_related('section', 'section_group')
    ):
        if (
            item_structurally_published(extra)
            and section_structurally_published(extra.section)
            and (
                extra.section_group_id is None
                or group_structurally_published(extra.section_group)
            )
        ):
            resolved[str(extra.id)] = extra
    return resolved


# --- order-selection validation (read/write parity, one captured `now`) ----

def _parse_selection(items):
    """Parse the submitted order items into (per_item, all_ids) or an error dict.

    per_item is a list of (parent_uuid, [extra_uuid, ...]); all_ids is the set of
    every referenced id. Malformed/missing item|quantity or non-UUID/non-list
    members reject with the opaque NOT_ON_MENU message (no enumeration)."""
    per_item = []
    all_ids = set()
    for entry in items:
        if (
            not isinstance(entry, dict)
            or entry.get('item') is None
            or entry.get('quantity') is None
        ):
            return None, None, {
                'status': 400,
                'message': 'Each order item must include an item and a quantity.',
            }
        try:
            parent_id = uuid.UUID(str(entry['item']))
        except (ValueError, TypeError):
            return None, None, {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
        extra_ids = []
        extras_raw = entry.get('extras')
        if extras_raw is not None:
            if not isinstance(extras_raw, list):
                return None, None, {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
            for member in extras_raw:
                try:
                    extra_id = uuid.UUID(str(member))
                except (ValueError, TypeError):
                    return None, None, {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
                extra_ids.append(extra_id)
                all_ids.add(extra_id)
        all_ids.add(parent_id)
        per_item.append((parent_id, extra_ids))
    return per_item, all_ids, None


def validate_order_selections(restaurant, items, now, *, enforce_publication,
                              resolved=None):
    """
    Authoritative validation of an order's parent + extra selections against the
    canonical policy at a single captured ``now``. Returns ``{'status': 200}`` or a
    ``{'status': 400, 'message': ...}`` rejection. Creates NO rows — the order
    service translates a rejection into ``OrderItemRejected`` so the whole
    transaction rolls back before the daily counter is allocated.

    Order of checks (all opaque ``NOT_ON_MENU_MESSAGE`` except the final min/max,
    which surface only after every id has passed tenant + publication + role):
      1. parse ids (malformed → reject)
      2. batch resolve, restaurant-scoped tenant gate (every id must belong here)
      3. per parent: publication (when ``enforce_publication``); then for a
         non-empty extra list — parent ``has_extras`` gate, each extra publishable +
         in the normalized ``extras_applicable`` + not a duplicate submission; then
         extras min/max on the validated unique count.

    ``enforce_publication`` is True for anonymous diners and False for staff/admin
    (who already passed module authorization). Tenant + all extras-integrity checks
    apply to EVERY caller — management authorization is not license to create a
    structurally invalid order graph.

    ``resolved`` is the order's single coherent catalogue read (D02): a mapping of
    ``pk -> MenuItem`` already fetched restaurant-scoped, with ``section`` /
    ``section_group`` / the group's own section joined in. Supplying it means this
    function issues NO query and decides publication from the SAME row versions
    that price the order — it was one of three independent reads of the same rows
    inside one transaction. The batch fetch below is preserved for every caller
    that has no snapshot, and the tenant gate is identical either way: an id that
    is not in the mapping did not resolve inside this restaurant.
    """
    per_item, all_ids, parse_error = _parse_selection(items)
    if parse_error is not None:
        return parse_error

    if enforce_publication and not restaurant_can_serve_menu(restaurant):
        return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

    if not all_ids:
        return {'status': 200}

    if resolved is None:
        fetched = {
            item.pk: item
            for item in (
                MenuItem.objects
                .filter(pk__in=all_ids, section__restaurant=restaurant)
                .select_related('section', 'section_group',
                                'section_group__section')
            )
        }
    else:
        fetched = {
            item_id: resolved[item_id]
            for item_id in all_ids if item_id in resolved
        }
    # Tenant gate (every caller): a foreign / nonexistent id resolves to nothing.
    if any(_id not in fetched for _id in all_ids):
        return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

    for parent_id, extra_ids in per_item:
        parent = fetched[parent_id]

        if enforce_publication and not item_orderable(parent, now):
            return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

        if extra_ids:
            # A non-empty extra list requires a parent that accepts extras.
            if not parent.has_extras:
                return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
            allowlist = set(normalize_extras_applicable(parent.extras_applicable))
            seen = set()
            for extra_id in extra_ids:
                canonical = str(extra_id)
                if canonical in seen:
                    # duplicate submitted extra id
                    return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
                seen.add(canonical)
                extra = fetched[extra_id]
                if not extra_publishable(extra, parent, restaurant.id):
                    return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
                if canonical not in allowlist:
                    return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

        # extras min/max on the validated UNIQUE selection (only meaningful when the
        # parent accepts extras). Surfaced as a distinct message AFTER id validation.
        if parent.has_extras:
            unique_count = len(extra_ids)  # duplicates already rejected above
            min_extras = parent.extras_min_selections or 0
            max_extras = parent.extras_max_selections  # None/0 => unlimited
            if unique_count < min_extras:
                return {
                    'status': 400,
                    'message': (
                        f"Item {parent.name} requires at least {min_extras} "
                        "extra selection(s)."
                    ),
                }
            if max_extras and unique_count > max_extras:
                return {
                    'status': 400,
                    'message': (
                        f"Item {parent.name} allows a maximum of {max_extras} "
                        "extra selection(s)."
                    ),
                }

    return {'status': 200}
