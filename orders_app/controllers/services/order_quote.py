"""
D02/P9 — the server-priced quote, and the acknowledgement that binds a submission
to it.

THE PROBLEM. The diner used to confirm "are you sure you want to place this
order?" against a price the BROWSER computed, and the client then auto-submitted
whenever nothing happened to be sold out — so the server's amount was never shown
before the order was accepted. Correct calculation is not agreement to an amount.

THE CONTRACT. ``initiate`` saves a server-priced DRAFT and returns its
``quote_ref``. The diner reviews THAT — the server's lines, the server's total —
and ``submit`` must supply the same ``quote_ref``. It is validated under the
existing scoped order lookup and inside the acceptance transaction, against the
order's own stored rows.

WHAT THE REFERENCE IS BOUND TO. The canonical quote CONTENTS, read from persisted
immutable data:

  * the order and its pricing version;
  * every undeleted row, parent and child, in a stable order — its identity, its
    parent relationship, quantity, deliverability and status;
  * the immutable unit components and every extended amount, reference and
    effective alike;
  * the canonical selections and the preparation snapshots (name, modifier labels,
    allergen labels) the kitchen will actually work from;
  * the order's reference, effective, savings and payable totals.

It is NOT bound to current catalogue names or prices, to a fresh clock reading, or
to an unrelated status timestamp — any of those would make the reference unstable
and a diner's honest acknowledgement would expire for no reason.

WHAT IT IS NOT. It is not authorization and it is not proof that a human read a
screen. Every existing diner-capability and staff/module check still applies, and
there is deliberately no internal or staff bypass: a caller being trusted is not a
reason to skip the acceptance invariant. A bare ``confirmed: true`` would assert
nothing about WHICH quote was confirmed, which is the whole point.

WHY NO NEW TABLE OR COLUMN. The reference is DERIVED from the persisted rows, so
it cannot drift from them and there is nothing extra to keep in step. A separate
quote table, service or revision column would be terminology, not a guarantee.
"""
import hashlib
import json
from misc_app.controllers.money import MoneyConfigError, format_money
from orders_app.models import OrderItem


def _money(value):
    """Canonical 2dp string for a monetary field.

    NOT a bare ``str()``. A model instance can hold the raw Python value it was
    constructed with (``0``) while the database column holds ``Decimal('0.00')``,
    so stringifying whichever one happened to be in hand would make the reference
    depend on whether the instance had been refreshed — the digest would differ
    between the response that issues it and the transition that checks it, for an
    order nobody touched. Normalising here makes the reference a property of the
    SAVED VALUES and nothing else.

    THE SHARED FORMATTER IS USED, AND NOT A LOCAL QUANTIZE. Quantizing here
    under the ambient decimal context raised ``InvalidOperation`` for any amount
    needing more than the default 28 significant digits, and the escape hatch
    below then built the key out of a Python ``repr`` — stable, but a repr of the
    value rather than the value, and different in form from every other
    fingerprint. ``format_money`` takes the module's own context, so a
    schema-valid large amount fingerprints as the plain string it is. For every
    normal-value amount the two produce byte-identical output, so existing
    references are unchanged.
    """
    try:
        return format_money(value, field='quote')
    except MoneyConfigError:
        # Unreachable for a DecimalField; a non-numeric value still has to
        # produce a stable, non-matching key rather than raise.
        return f'!{value!r}'


def _row_fingerprint(row):
    """Everything about ONE persisted line that a diner is entitled to have
    acknowledged. Stored values only."""
    return [
        str(row.pk),
        str(row.parent_item_id) if row.parent_item_id else '',
        str(row.item_id),
        int(row.quantity),
        bool(row.available),
        str(row.status or ''),
        _money(row.unit_price),
        _money(row.discounted_price),
        _money(row.unit_cost_of_options),
        _money(row.total_cost),
        _money(row.discounted_cost),
        _money(row.cost_of_options),
        _money(row.savings),
        _money(row.actual_cost),
        str(row.item_name_snapshot or ''),
        json.dumps(row.selected_modifiers or {}, sort_keys=True,
                   separators=(',', ':')),
        json.dumps(row.modifiers_snapshot or [], sort_keys=True,
                   separators=(',', ':')),
        json.dumps(row.allergen_tags_snapshot or [], sort_keys=True,
                   separators=(',', ':')),
    ]


def group_live_children(live_rows):
    """Attach each live child to a live parent, and name the ones with none.

    THE ONE definition of "which rows can a quote represent", shared by the
    serializer that RENDERS the quote and by the acceptance check that decides
    whether one may be honoured. Two copies would be two answers, and the case
    they disagree on is exactly the one that matters: a response saying the
    quote is incomplete while the transition accepts it anyway.

    ``live_rows`` must already BE the live population (``deleted=False``) —
    re-filtering here would be a second definition of "live". Returns
    ``(by_parent, orphaned)``: children keyed by the parent they belong under,
    and the live children whose parent is NOT in the population.

    An orphaned child is not merely unrenderable. Its amount is part of the
    order's saved payable (``update_order_amounts`` reconciles over the live
    rows), so no itemised quote built from the remaining lines can add up to
    what the diner would be charged.
    """
    live_ids = {row.pk for row in live_rows}
    by_parent = {}
    orphaned = []
    for row in live_rows:
        if row.parent_item_id is None:
            continue
        if row.parent_item_id in live_ids:
            by_parent.setdefault(row.parent_item_id, []).append(row)
        else:
            orphaned.append(row)
    return by_parent, orphaned


def quote_ref(order, rows=None):
    """The opaque acknowledgement reference for ``order`` as it is saved now.

    Deterministic: the same saved draft always yields the same reference, and any
    change to a quantity, a selection, a deliverability flag, a preparation
    snapshot or any amount yields a different one. Rows are ordered by primary
    key so the digest does not depend on how they happened to be fetched.
    """
    if rows is None:
        rows = OrderItem.objects.filter(order=order, deleted=False)
    payload = {
        'order': str(order.pk),
        'pricing_version': int(order.pricing_version),
        'totals': [
            _money(order.total_cost), _money(order.discounted_cost),
            _money(order.savings), _money(order.actual_cost),
        ],
        'rows': sorted(
            (_row_fingerprint(row) for row in rows),
            key=lambda fingerprint: fingerprint[0],
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def matches(order, supplied, rows=None):
    """Whether ``supplied`` acknowledges the order exactly as it is saved.

    Compared as plain strings after a type check. A missing or non-string value
    is not a match and is never coerced into one.
    """
    if not isinstance(supplied, str) or not supplied:
        return False
    return supplied == quote_ref(order, rows=rows)
