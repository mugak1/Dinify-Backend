"""
D04 — THE CHECKOUT INTENT: what one deliberate attempt to buy actually is, and
the ONE policy that decides whether a request may be answered with an order
that already exists.

THE PROBLEM. The idempotency key alone said nothing about WHAT was being
replayed. ``_create_order`` selected an existing order by ``(restaurant, key)``
and returned it, so a request naming three burgers received the one-burger
order that key had been used for, with no conflict reported; and a request
naming a DIFFERENT TABLE received the first table's order. The key identified
an attempt but was bound to nothing.

WHAT AN INTENT IS. The independent high-entropy key ``client_order_id``
IDENTIFIES the attempt; the binding says what that attempt WAS. Equal
fingerprints under different keys are NOT the same intent — two diners
deliberately ordering the same meal are two purchases, and nothing here
deduplicates them.

The binding has two halves:

  SCOPE       the server-resolved restaurant and table, and the original
              provenance of the command — staff or diner, and the customer it
              was attributed to.
  FINGERPRINT the canonical purchase: what was asked for, independent of what
              the catalogue happened to say at the time.

WHAT THE FINGERPRINT IS COMPUTED FROM, AND WHEN. The D01-VALIDATED REQUEST,
BEFORE ``normalize_order_items`` touches it. That ordering is the whole point:
normalisation reorders choices into menu-definition order and drops choices the
catalogue no longer offers, so a fingerprint taken afterwards would change when
the MENU changed. A diner recovering a lost response must not be told their
purchase is different because the restaurant edited a dish in between.

It is equally never reconstructed from the PRICED ROWS. An unavailable line is
persisted at quantity 0 and identical configurations are merged, so the saved
rows cannot say what was asked for.

EXCLUDED, deliberately: menu labels, any price, publication or stock state, the
clock, and the signed session token. A token is renewed routinely for the same
table and the same diner; if it entered the fingerprint, an ordinary renewal
would turn a retry into a different purchase.

THIS MODULE DECIDES NOTHING ABOUT AUTHORITY. It answers "is this the same
purchase?", never "may this caller see it". The capability and module checks
that resolved the restaurant and table run first and are untouched.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

#: The canonical encoding's version, carried INSIDE the stored value as
#: ``v1:<digest>``. A separate column would be a second thing to keep in step;
#: a bare digest would be silently reinterpreted the day the encoding changes.
#: An unrecognised prefix is never compared — see ``UNSUPPORTED``.
FINGERPRINT_VERSION = 'v1'


# --- outcomes ---------------------------------------------------------------
#: No order holds this key in this restaurant. The caller may create one.
ABSENT = 'absent'
#: An order holds this key and IS this purchase, at this scope. Return it.
MATCH = 'match'
#: An order holds this key at THIS table, but the purchase differs. A conflict
#: the caller is entitled to understand.
MISMATCH = 'mismatch'
#: An order holds this key at ANOTHER table in the same restaurant. The caller
#: is NOT entitled to know that, so this is answered opaquely — see
#: ``order_intent_unusable`` in ``con_orders``.
OUT_OF_SCOPE = 'out_of_scope'
#: An order holds this key but carries no fingerprint, or one written by an
#: encoding this build does not understand. Equivalence CANNOT BE PROVEN, and
#: it is never assumed in either direction.
UNSUPPORTED = 'unsupported'


@dataclass(frozen=True)
class IntentVerdict:
    """The answer, and the order it is about when there is one."""
    outcome: str
    order: object = None

    @property
    def is_match(self):
        return self.outcome == MATCH


def _canonical_line(line):
    """ONE validated request line, reduced to what makes it that purchase.

    ``item`` and each extra id arrive from D01 as canonical lowercase UUID
    STRINGS, so spelling is already settled for them. MODIFIER group and choice
    ids are OPAQUE — the column is unvalidated and this repository's own
    fixtures use ``'g-req'`` / ``'c1'`` — so they are sorted but never
    case-folded, trimmed or parsed as UUIDs. Two ids differing only in case are
    two different ids to the catalogue, and they must stay two here.

    Absent, null and empty selections collapse to the same canonical empty
    form, because they mean the same thing to the order path. Choices within a
    group are de-duplicated, matching the canonicalisation the server itself
    performs — a repeat is not a second selection.

    EXTRAS ARE SORTED BUT NOT DE-DUPLICATED. A duplicate extra is REFUSED
    upstream, and collapsing one here would turn a request the server rejects
    into a valid replay of a request it accepted. The asymmetry with modifier
    choices is deliberate and is the same one ``order_pricing`` documents.
    """
    modifiers = line.get('selected_modifiers') or {}
    canonical_modifiers = sorted(
        (str(group), sorted(set(str(choice) for choice in (choices or []))))
        for group, choices in modifiers.items()
    )
    extras = sorted(str(extra) for extra in (line.get('extras') or []))
    return [str(line.get('item')), canonical_modifiers, extras]


def _configuration_key(canonical_line):
    """A stable text key for one exact configuration."""
    return json.dumps(canonical_line, sort_keys=True, separators=(',', ':'))


def fingerprint(validated_items):
    """The canonical purchase, as ``v1:<sha256>``.

    ``validated_items`` MUST be the output of ``validate_order_items`` — the
    raw request has not yet passed the D01 ceilings, and this function
    COALESCES, which is exactly the operation those ceilings must precede.

    WHY COALESCING IS SAFE HERE AND ONLY HERE. One line of quantity 3 and two
    lines of 1 and 2 in the same configuration are one purchase: the server
    merges identical configurations into a single stored row, so they produce
    the same order. Making them different intents would hand a spurious
    conflict to any client that tidied its basket between attempts.

    AND WHY THE ORDER OF OPERATIONS MATTERS. The D01 rules count RAW entries —
    a raw per-line quantity of 100 is refused whether or not an equivalent
    99 + 1 spelling would have been accepted, and a duplicate extra is refused
    rather than collapsed. Coalescing before those rules ran would let a
    forbidden request in through an equivalent spelling. Validation first,
    then this.

    PARENT LINE ORDER DOES NOT DISTINGUISH A PURCHASE, so the coalesced
    configurations are sorted.
    """
    totals = {}
    lines = {}
    for line in validated_items:
        canonical = _canonical_line(line)
        key = _configuration_key(canonical)
        totals[key] = totals.get(key, 0) + int(line.get('quantity'))
        lines[key] = canonical

    payload = [
        [lines[key], totals[key]] for key in sorted(totals)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    return f'{FINGERPRINT_VERSION}:{digest}'


def is_supported(stored):
    """Can this build compare the stored fingerprint at all?

    A pre-D04 order has ``None`` here, and one written by a future encoding
    carries a prefix this build cannot interpret. Neither is a mismatch and
    neither is a match: equivalence is simply not decidable, which is a
    distinct outcome with its own honest answer.
    """
    return isinstance(stored, str) and stored.startswith(
        f'{FINGERPRINT_VERSION}:')


def resolve_intent(*, restaurant_id, client_order_id, table_id,
                   request_fingerprint, created_by_id, customer_id,
                   for_update=False) -> IntentVerdict:
    """THE binding policy — the single place every return-existing site asks.

    There are four such sites (the controller's preflight, the service's
    lookup, the post-lock recheck and the unique-conflict recovery) and they
    used to interpret the key independently. They call this instead, so a
    verdict cannot depend on which one happened to run.

    ``for_update`` locks the row for the authoritative callers inside the
    transaction; the preflight passes False because it is only fast feedback
    and decides nothing.

    THE UNIQUENESS NAMESPACE IS RESTAURANT-WIDE and stays that way. The lookup
    deliberately does NOT filter on table: ``uniq_order_restaurant_client_order_id``
    means a key already bound to another table is NOT AVAILABLE, and narrowing
    the lookup would make it look free and drive a second INSERT into a
    constraint violation. The table is compared AFTER the row is found.
    """
    from orders_app.models import Order

    if client_order_id is None:
        return IntentVerdict(ABSENT)

    rows = Order.objects.filter(
        restaurant_id=restaurant_id, client_order_id=client_order_id)
    if for_update:
        rows = rows.select_for_update(of=('self',))
    existing = rows.first()
    if existing is None:
        return IntentVerdict(ABSENT)

    # SCOPE FIRST, and the order matters: a caller at another table is not
    # entitled to a diagnostic about a purchase that is not theirs, so the
    # comparison never reaches the fingerprint.
    if str(existing.table_id) != str(table_id):
        return IntentVerdict(OUT_OF_SCOPE, existing)

    # PROVENANCE. Staff-versus-diner and the attributed customer are part of
    # the command, not decoration: the order was written with that attribution
    # and nothing here may rewrite it. A request that changes either is a
    # different command, refused rather than silently adopted.
    if _identity(existing.created_by_id) != _identity(created_by_id):
        return IntentVerdict(MISMATCH, existing)
    if _identity(existing.customer_id) != _identity(customer_id):
        return IntentVerdict(MISMATCH, existing)

    if not is_supported(existing.request_fingerprint):
        return IntentVerdict(UNSUPPORTED, existing)
    if existing.request_fingerprint != request_fingerprint:
        return IntentVerdict(MISMATCH, existing)
    return IntentVerdict(MATCH, existing)


def _identity(value):
    """Compare ids as text, so a UUID and its string spelling agree."""
    return None if value is None else str(value)


# --- the answers ------------------------------------------------------------
#
# Stable codes a client branches on, beside a sentence a diner reads. A refusal
# here mutates NOTHING: no reprice, no replacement order, no rewritten
# attribution, and above all no second order for a key that is already taken.
REASON_INTENT_MISMATCH = 'checkout_intent_mismatch'
REASON_INTENT_UNUSABLE = 'checkout_intent_unusable'
REASON_INTENT_BINDING_UNAVAILABLE = 'checkout_intent_binding_unavailable'

MESSAGE_INTENT_MISMATCH = (
    'This checkout was already started for a different order. Please review '
    'your basket and place it again.'
)
MESSAGE_INTENT_UNUSABLE = (
    'We could not continue this checkout. Please start a new one.'
)
MESSAGE_INTENT_BINDING_UNAVAILABLE = (
    'We cannot confirm this is the same order you started. Please ask a '
    'member of staff to check before ordering again.'
)


def _refusal(reason, message):
    # 409, not 400: the request is well formed — the world already contains
    # something incompatible with it. Deliberately WITHOUT a `data.order_id`,
    # which on this endpoint is the established table-occupied signal and
    # would send a client down an unrelated recovery.
    return {'status': 409, 'message': message, 'reason': reason}


def mismatch_refusal():
    """Same scope, different purchase — a conflict the caller may understand."""
    return _refusal(REASON_INTENT_MISMATCH, MESSAGE_INTENT_MISMATCH)


def unusable_refusal():
    """The key is taken somewhere this caller cannot see.

    OPAQUE ON PURPOSE. The code says the key cannot be used; it does not say
    that another table holds it, which table, or anything about that order.
    The caller gets no order id, no amount and no attribution — and no
    replacement order is created, because the key is NOT available and
    creating one would drive a second INSERT into the restaurant-wide unique
    constraint.
    """
    return _refusal(REASON_INTENT_UNUSABLE, MESSAGE_INTENT_UNUSABLE)


def binding_unavailable_refusal():
    """An order holds this key, but equivalence cannot be proven.

    A pre-D04 row, or one written by an encoding this build cannot read. The
    honest answer is neither "the same order" nor "a different order", and
    neither guess is acceptable: returning it would hand back a purchase
    nobody matched, and creating a replacement would ignore an occupied key.
    """
    return _refusal(REASON_INTENT_BINDING_UNAVAILABLE,
                    MESSAGE_INTENT_BINDING_UNAVAILABLE)


#: Every non-creating outcome, mapped to the answer it produces. Exhaustive by
#: construction so a new outcome cannot be silently treated as "create one".
REFUSALS = {
    MISMATCH: mismatch_refusal,
    OUT_OF_SCOPE: unusable_refusal,
    UNSUPPORTED: binding_unavailable_refusal,
}
