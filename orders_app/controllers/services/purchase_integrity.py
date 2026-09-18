"""
D06 — IS THE PURCHASE THE DINER REVIEWED STILL THE PURCHASE WE WOULD PREPARE?

THE QUESTION THIS ANSWERS, AND THE ONE IT DOES NOT. It asks whether the quoted
lines can still be prepared as quoted. It does NOT ask what they would cost now:
an unexpired, otherwise-valid quote is honoured at the saved amounts, and nothing
here reprices, re-reads a discount window for money, or replaces a saved figure.
There is exactly one pricing engine and this is not a second one.

WHAT IT REPLACED. Acceptance re-read no catalogue fact at all. A dish sold out,
unpublished, soft-deleted, or redefined after the diner reviewed it was accepted
with the saved rows and sent to the kitchen — measured over the real routes on
unmodified 32fe4f9, including a modifier group whose selected choice had been
renamed and repriced. The create path already re-reads the catalogue under its
lock; acceptance did not, and acceptance is the moment the order becomes real.

THE CANDIDATE PURCHASE IS THE QUOTED LIVE POPULATION, NOT THE ORIGINAL REQUEST.
It is the same population ``order_quote`` fingerprints and the serializer renders:
undeleted ``OrderItem`` rows, parents grouped with their children. The raw basket
is NOT reconstructed — merging and zero-and-flag mean the saved rows cannot say
what was typed, and the D04 fingerprint is a digest, not a decoder.

ONLY DELIVERABLE LINES ARE EXAMINED, AND ZEROED ONES STAY ZEROED. A line the
create path already reconciled to quantity 0 / ``available=False`` is not part of
what is being bought: it is not checked, and it is never resurrected because the
dish came back into stock. The diner reviewed a quote without it.

WHAT COUNTS AS A CHANGE — the approved decision table, in the order applied:

  liveness          the item, its section or its group has been
                    soft-deleted, or the item is sold out /
                    unavailable (EVERY provenance)                   -> review
  publication       (diner-origin only) no longer published to the
                    ordering public at `now` — the item, its section
                    or its group unapproved, disabled, marked
                    unavailable, or out of schedule                  -> review
  extras            an extra is no longer attachable, or the
                    parent's extras minimum/maximum no longer holds  -> review
  selection         a selected modifier group or choice no longer
                    exists, or the group's own rules no longer hold  -> review
  allergens         the declared allergen set changed                -> review
  price/discount    honoured at the saved amount, INCLUDING a price
                    or discount that has since become unreadable     -> accept
  labels, order     a rename, a recolour, a reorder, an added
                    OPTIONAL choice, an unrelated item               -> accept

LIVENESS AND PUBLICATION ARE DIFFERENT KINDS OF FACT, and `section_structurally_
published` used to wear both names at once: it is `approved AND enabled AND not
deleted`. Calling it unconditionally here bound two thirds of a publication rule
to every provenance, which stranded the exemption the paragraph below promises —
a member of staff ordering against a menu that has not been approved yet created
the draft happily and then could never place it, because the refusal CLOSES the
quote. Deletion binds everyone (a soft-deleted section is not a place a dish can
exist, exactly as D06 says of a table); `approved` / `enabled` / `available` /
the schedule bind the diner path, which is the line the create path already
draws.

AND PRICE READABILITY IS NEITHER. `item_orderable` requires `item_priceable` —
a live read of `primary_price` and `discount_details` — so an operator mistyping
a discount AFTER the review destroyed the quote over a figure nobody was going
to charge. Acceptance asks `item_published_now` instead. Creating a NEW order
still asks `item_orderable`, because the create path has to price the line.

IDENTITY, NOT LABELS — and the limit that leaves. A selection is identified by its
GROUP AND CHOICE IDS, not by the words shown beside them, because the approved
rule keeps a pure presentation edit from invalidating a purchase ("keep the saved
description"). The consequence is stated rather than hidden: an operator who
edits a choice's LABEL so that the same id now means something else has changed
the preparation in a way no stored field records, and this check cannot see it.
Allergens are the one declaration compared by value, because that is the one
whose silent change is dangerous rather than untidy. Nothing here verifies real
ingredients; it verifies that the restaurant's own recorded declaration has not
moved.

A RENAMED ALLERGEN TAG IS A FALSE POSITIVE, ON PURPOSE. The saved snapshot holds
label text, not tag ids, so renaming a tag reads as a changed declaration and
sends the order for review. That is the safe direction of the only error this
comparison can make, and within a thirty-minute window it can affect only the
drafts open at that moment.

STAFF ORIGIN CHANGES EXACTLY ONE THING. Publication and scheduling are skipped for
a staff-origin order, mirroring ``enforce_publication`` on the create path, because
those orders never passed that gate in the first place. Tenant scope, soft-delete,
stock, extras relationships, selection validity and allergens apply to every
provenance: authorization to take an order is not authorization to prepare a dish
that no longer exists.

COST. ONE statement, via the same ``build_snapshot`` the create path uses, so the
whole purchase resolves — items, sections, groups and allergen labels — in a
single coherent read. There is no per-line query and no loop that grows with the
basket.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from restaurants_app.controllers.menu_publication import (
    group_live,
    item_published_now,
    normalize_extras_applicable,
    section_live,
    extra_publishable,
)
from restaurants_app.controllers.modifier_definition import (
    inspect_modifier_definition,
)
from orders_app.controllers.services.catalogue_snapshot import build_snapshot

logger = logging.getLogger(__name__)

#: The one machine code. DEFINITIVE for this attempt in the sense that the saved
#: quote can no longer be honoured as it stands — but NOT monotone the way expiry
#: is: stock can come back. That difference is why closing a quote for this
#: reason is a durable, recorded decision (see ``quote_closure``) rather than
#: something a client may infer from a refusal it happens to have seen.
REASON_PURCHASE_NEEDS_REVIEW = 'purchase_needs_review'

MESSAGE_PURCHASE_NEEDS_REVIEW = (
    'Something on this order has changed since you reviewed it. Please review '
    'the updated order and place it again.'
)

#: Bounded classifications, for the server log only. They never reach a response:
#: one controlled message goes to the diner, exactly as the menu path answers
#: every unorderable id with one opaque message.
CLASS_ITEM_MISSING = 'item_missing'
CLASS_ITEM_DELETED = 'item_deleted'
CLASS_ITEM_SOLD_OUT = 'item_sold_out'
CLASS_ITEM_UNPUBLISHED = 'item_unpublished'
CLASS_SECTION_GONE = 'section_or_group_deleted'
CLASS_EXTRA_NOT_ATTACHABLE = 'extra_not_attachable'
CLASS_EXTRAS_BOUNDS = 'extras_bounds_no_longer_met'
CLASS_MODIFIERS_REMOVED = 'modifier_definition_removed'
CLASS_MODIFIERS_INVALID = 'modifier_definition_invalid'
CLASS_GROUP_MISSING = 'modifier_group_missing'
CLASS_CHOICE_MISSING = 'modifier_choice_missing'
CLASS_GROUP_BOUNDS = 'modifier_group_bounds_no_longer_met'
CLASS_ALLERGENS_CHANGED = 'allergen_declaration_changed'


@dataclass(frozen=True)
class IntegrityRefusal:
    """Why the quoted purchase can no longer be prepared as quoted."""

    classification: str
    #: The offending item id, for the log line only.
    item_id: Optional[str] = None

    def as_refusal(self, *, extra=None):
        body = {
            'status': 400,
            'message': MESSAGE_PURCHASE_NEEDS_REVIEW,
            'reason': REASON_PURCHASE_NEEDS_REVIEW,
        }
        if extra:
            body.update(extra)
        return body


def _deliverable(row) -> bool:
    """The same predicate ``deliverable_parent_count`` applies, one row at a time.

    A row is part of the purchase when the create path left it orderable: flagged
    available AND carrying a positive quantity. Zero-and-flag always writes both
    together, so either alone would be a contradiction; requiring both means a
    contradictory row is excluded rather than half-examined.
    """
    return bool(row.available) and (row.quantity or 0) > 0


def _allergen_names(labels):
    """The declared allergen set, as comparable names.

    Order, colour and icon are presentation and are deliberately excluded: a
    reorder or a recolour is not a change of declaration. A malformed stored
    label contributes its repr rather than being skipped, so a declaration that
    became unreadable registers as different instead of silently matching.
    """
    names = set()
    for label in (labels or []):
        if isinstance(label, dict):
            names.add(str(label.get('name')))
        else:
            names.add(repr(label))
    return names


def _check_selection(row, menu_item):
    """Do the saved modifier selections still name real, currently valid choices?"""
    selected = row.selected_modifiers or {}
    verdict = inspect_modifier_definition(menu_item.options)

    if verdict.is_invalid:
        # The stored definition cannot be read at all. We cannot establish that
        # the saved selection still means anything, and a broken definition is
        # never downgraded to "no modifiers required" — that is the bypass the
        # D01 modifier work exists to prevent.
        return CLASS_MODIFIERS_INVALID

    groups = {group.group_id: group for group in verdict.groups}

    if selected and not verdict.is_active:
        # The diner chose something and the item now declares no modifiers at
        # all: the choice cannot be prepared.
        return CLASS_MODIFIERS_REMOVED

    for group_id, choice_ids in selected.items():
        group = groups.get(group_id)
        if group is None:
            return CLASS_GROUP_MISSING
        chosen = list(choice_ids or [])
        for choice_id in chosen:
            if choice_id not in group.choices_by_id:
                return CLASS_CHOICE_MISSING
        unique = len(set(chosen))
        if unique < (group.min_selections or 0):
            return CLASS_GROUP_BOUNDS
        # 0 means unlimited — the established contract.
        if group.max_selections and unique > group.max_selections:
            return CLASS_GROUP_BOUNDS

    # A group that has BECOME required since the quote: the saved selection does
    # not name it, so the dish can no longer be prepared as ordered.
    for group_id, group in groups.items():
        if (group.min_selections or 0) > 0 and not (selected.get(group_id) or []):
            return CLASS_GROUP_BOUNDS

    return None


def _check_parent(parent, children, snapshot, *, restaurant_id, now,
                  enforce_publication):
    """Every rule, for one quoted parent line and its quoted extras."""
    resolved = snapshot.get(parent.item_id)
    if resolved is None:
        # Not in this restaurant's catalogue any more, or never was. The tenant
        # gate is the same one the create path applies.
        return IntegrityRefusal(CLASS_ITEM_MISSING, str(parent.item_id))
    menu_item = resolved.menu_item

    # --- liveness, every provenance -----------------------------------------
    # DELETION ONLY. `approved` / `enabled` / `available` and the schedule are
    # publication policy and belong below; this block asks whether the dish and
    # the containers it lives in still EXIST, which binds every provenance for
    # the same reason a soft-deleted table does.
    if menu_item.deleted:
        return IntegrityRefusal(CLASS_ITEM_DELETED, str(parent.item_id))
    if not section_live(menu_item.section):
        return IntegrityRefusal(CLASS_SECTION_GONE, str(parent.item_id))
    if menu_item.section_group_id is not None and not group_live(
        menu_item.section_group
    ):
        return IntegrityRefusal(CLASS_SECTION_GONE, str(parent.item_id))
    if not (menu_item.available and menu_item.in_stock):
        # The quoted line was deliverable; it is not any more. The create path
        # would have zeroed it, and zeroing it HERE would change the amount the
        # diner agreed to — so the whole acceptance goes back for review.
        return IntegrityRefusal(CLASS_ITEM_SOLD_OUT, str(parent.item_id))

    # --- publication, diner-origin only -------------------------------------
    # `item_published_now`, NOT `item_orderable`: the latter also requires the
    # CURRENT price to be readable, and this boundary honours the price it
    # SAVED. A discount mistyped after the diner reviewed their order changes
    # neither what is prepared nor what is charged, so destroying the quote over
    # it would refuse a diner for a data fault they did not cause — and closing
    # a quote cannot be undone.
    if enforce_publication and not item_published_now(menu_item, now):
        return IntegrityRefusal(CLASS_ITEM_UNPUBLISHED, str(parent.item_id))

    # --- the extras relationship, every provenance --------------------------
    deliverable_children = [child for child in children if _deliverable(child)]
    if deliverable_children:
        if not menu_item.has_extras:
            return IntegrityRefusal(CLASS_EXTRA_NOT_ATTACHABLE, str(parent.item_id))
        allowlist = set(normalize_extras_applicable(menu_item.extras_applicable))
        for child in deliverable_children:
            child_resolved = snapshot.get(child.item_id)
            if child_resolved is None:
                return IntegrityRefusal(CLASS_ITEM_MISSING, str(child.item_id))
            extra = child_resolved.menu_item
            if extra.deleted:
                return IntegrityRefusal(CLASS_ITEM_DELETED, str(child.item_id))
            if not (extra.available and extra.in_stock):
                return IntegrityRefusal(CLASS_ITEM_SOLD_OUT, str(child.item_id))
            if not extra_publishable(extra, menu_item, restaurant_id):
                return IntegrityRefusal(
                    CLASS_EXTRA_NOT_ATTACHABLE, str(child.item_id))
            if str(extra.id) not in allowlist:
                return IntegrityRefusal(
                    CLASS_EXTRA_NOT_ATTACHABLE, str(child.item_id))

    # The extras bounds are checked against the QUOTED deliverable selection,
    # whether or not there are any: a minimum introduced since the quote makes a
    # plain line unorderable, and that is exactly the case a count of zero has
    # to be able to fail.
    if menu_item.has_extras:
        unique_extras = len({str(child.item_id) for child in deliverable_children})
        minimum = menu_item.extras_min_selections or 0
        maximum = menu_item.extras_max_selections  # None/0 => unlimited
        if unique_extras < minimum:
            return IntegrityRefusal(CLASS_EXTRAS_BOUNDS, str(parent.item_id))
        if maximum and unique_extras > maximum:
            return IntegrityRefusal(CLASS_EXTRAS_BOUNDS, str(parent.item_id))

    # --- preparation meaning, every provenance ------------------------------
    for row in [parent] + deliverable_children:
        row_resolved = snapshot.get(row.item_id)
        if row_resolved is None:                      # pragma: no cover - guarded above
            return IntegrityRefusal(CLASS_ITEM_MISSING, str(row.item_id))
        selection_problem = _check_selection(row, row_resolved.menu_item)
        if selection_problem is not None:
            return IntegrityRefusal(selection_problem, str(row.item_id))

        if _allergen_names(row_resolved.allergen_tags) != _allergen_names(
            row.allergen_tags_snapshot
        ):
            return IntegrityRefusal(CLASS_ALLERGENS_CHANGED, str(row.item_id))

    return None


def inspect(order, live_rows, *, now, enforce_publication):
    """The quoted purchase against the catalogue as it is now.

    ``live_rows`` is the undeleted ``OrderItem`` population the caller has already
    fetched — the same list the quote reference and the completeness check use,
    so this adds no row read. Returns ``None`` when the purchase still stands, or
    an ``IntegrityRefusal``.

    ``now`` is the caller's decision-time clock, sampled after its locks.
    """
    parents = [row for row in live_rows if row.parent_item_id is None]
    children_by_parent = {}
    for row in live_rows:
        if row.parent_item_id is not None:
            children_by_parent.setdefault(row.parent_item_id, []).append(row)

    candidate_parents = [row for row in parents if _deliverable(row)]
    if not candidate_parents:
        # Nothing deliverable to examine. The "is there anything to prepare"
        # invariant owns that case and has already answered it.
        return None

    # ONE statement for the whole purchase: parents and their quoted extras, with
    # sections, groups and allergen labels joined in.
    referenced = [
        {
            'item': str(parent.item_id),
            'extras': [
                str(child.item_id)
                for child in children_by_parent.get(parent.pk, [])
                if _deliverable(child)
            ],
        }
        for parent in candidate_parents
    ]
    snapshot = build_snapshot(order.restaurant_id, referenced, now)

    for parent in candidate_parents:
        refusal = _check_parent(
            parent,
            children_by_parent.get(parent.pk, []),
            snapshot,
            restaurant_id=order.restaurant_id,
            now=now,
            enforce_publication=enforce_publication,
        )
        if refusal is not None:
            # Bounded: an order id, a stable classification and one item id. No
            # catalogue JSON, no amounts, no order contents, no personal data.
            logger.info(
                'Order acceptance refused (order_id=%s, reason=%s, class=%s, '
                'item_id=%s)',
                order.pk, REASON_PURCHASE_NEEDS_REVIEW, refusal.classification,
                refusal.item_id,
            )
            return refusal

    return None
