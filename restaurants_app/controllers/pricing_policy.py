"""
D02 — the ONE reading of an item's stored PRICE configuration.

The shared half of pricing: given a stored ``primary_price`` and a stored
``discount_details`` blob, is this item priceable at all, is its discount live at
a given instant, and what is its effective per-unit BASE price? Both planes that
care about a price consume this — the public menu READ (``restaurants_app``) and
the order path (``orders_app``) — so the price a diner is shown and the price they
are charged cannot be produced by two different readings of the same row.

IMPORT DIRECTION IS LOAD-BEARING. This module imports ``misc_app.controllers.money``
and ``django.utils.timezone`` and NOTHING ELSE — no model, no queryset, no order
service. That is what lets ``restaurants_app.models`` depend on it without the
catalogue coming to depend on order orchestration, which would be an import cycle.

WHAT CHANGED, STATED AS A CHANGE (D02)

* ``discount_percentage`` / ``discount_amount`` / ``primary_price`` were read with a
  bare ``Decimal(str(...))``. A malformed value (``'abc'``, ``True``) raised
  ``InvalidOperation`` out of ``is_discount_active`` — which the PUBLIC MENU
  serializer calls — so one bad catalogue row returned HTTP 500 for the whole
  restaurant's menu, not merely for its own item. Parsing now goes through the
  shared money contract and an unreadable configuration becomes a VERDICT the
  caller can act on.
* ``effective_base_price`` ended in ``return price if price > 0 else Decimal('0')``.
  A ``discount_amount`` larger than the price, or a percentage above 100, was
  therefore CLAMPED TO ZERO and the dish silently became free. An incoherent
  discount is now UNUSABLE, not free. A genuinely free item — ``primary_price`` 0,
  or a 100% discount — is untouched and stays orderable.
* The clock was read per call (a four-line order made 9 ``localdate`` and 19
  ``localtime`` calls). ``now`` is now an explicit argument so one captured instant
  can price a whole order; it still defaults to reading the clock for the
  single-item callers that have no snapshot.

WHAT DELIBERATELY DID NOT CHANGE

* An ABSENT, empty or zero-magnitude discount is INACTIVE, not an error. ``{}``,
  ``[]``, ``None``, ``0`` and a missing key all mean "no discount", exactly as
  before — that is supported configuration, and treating it as malformed would make
  most of the catalogue unorderable.
* A malformed DATE or TIME bound is still treated as UNBOUNDED on that side. That
  is pre-existing, deliberate behaviour ("malformed = treat as no lower bound
  (never suppress)") and changing it would silently move live prices. Only the
  MONETARY reading is tightened here.
* Empty ``recurring_days`` still means "every day".
* ``end_date`` remains INCLUSIVE.
"""
from datetime import datetime

from django.utils import timezone

from misc_app.controllers.money import (
    MoneyConfigError, parse_money, quantize_money, working_context,
)

#: Stable, diner-safe reason codes for an unusable configuration. They are logged
#: and carried on the verdict; they are never rendered to a diner.
PRICE_UNREADABLE = 'price_unreadable'
DISCOUNT_UNREADABLE = 'discount_unreadable'
DISCOUNT_EXCEEDS_PRICE = 'discount_exceeds_price'

_ZERO = quantize_money(parse_money(0))
_HUNDRED = parse_money(100)


class PriceVerdict:
    """The complete answer about ONE item's price at ONE instant.

    ``usable`` False means this item cannot be priced from what is stored. The
    caller decides what to do about it — the menu read omits the item, checkout
    refuses the line — but NO caller may substitute a fallback price, and in
    particular none may fall back to zero (free) or to the undiscounted price
    (an unearned full charge presented as valid).
    """

    __slots__ = ('usable', 'reason', 'reference_base', 'effective_base',
                 'discount_active')

    def __init__(self, *, usable, reason=None, reference_base=None,
                 effective_base=None, discount_active=False):
        self.usable = usable
        self.reason = reason
        self.reference_base = reference_base
        self.effective_base = effective_base
        self.discount_active = discount_active

    def __repr__(self):  # pragma: no cover - diagnostic only
        if not self.usable:
            return f'<PriceVerdict unusable {self.reason}>'
        return (f'<PriceVerdict ref={self.reference_base} '
                f'eff={self.effective_base} active={self.discount_active}>')


def _unusable(reason):
    return PriceVerdict(usable=False, reason=reason)


def resolve_price(primary_price, discount_details, now=None):
    """Resolve an item's reference and effective per-unit BASE price.

    ``now`` is the instant the discount window is evaluated against; pass the
    order's single captured time so every line of one order is priced at one
    moment. Never raises — an unreadable configuration comes back as an
    unusable verdict.
    """
    now = now or timezone.localtime()

    try:
        reference = parse_money(primary_price or 0, field='primary_price')
    except MoneyConfigError:
        return _unusable(PRICE_UNREADABLE)

    details = discount_details if isinstance(discount_details, dict) else {}

    # An absent / empty / zero-magnitude discount is SUPPORTED CONFIGURATION,
    # not an error: resolve it before any money parsing can object. `or 0`
    # preserves the established treatment of None and '' as "unset".
    raw_pct = details.get('discount_percentage', 0) or 0
    raw_amt = details.get('discount_amount', 0) or 0
    if _is_unset(raw_pct) and _is_unset(raw_amt):
        return PriceVerdict(
            usable=True, reference_base=reference, effective_base=reference,
            discount_active=False,
        )

    # THE WINDOW IS CHECKED BEFORE THE MAGNITUDE IS PARSED, and the order is
    # deliberate: a closed window means no discount can apply at all, so an
    # unreadable magnitude behind one is harmless and the item prices from
    # `primary_price` as it always did. Only a discount that is CURRENTLY
    # SCHEDULED can make an item unpriceable, which is the tightest containment
    # available — an operator's broken future promotion does not take today's
    # menu down. The window gates read no money.
    if not _window_is_open(details, now):
        return PriceVerdict(
            usable=True, reference_base=reference, effective_base=reference,
            discount_active=False,
        )

    try:
        # `allow_negative` so a negative magnitude is READ rather than refused.
        # A non-positive magnitude has always meant "no discount" here, and
        # keeping that reading is what stops a harmless data error (a stored
        # -5%) turning an item that sells perfectly well today into an
        # unorderable one. It can never produce a discount and can never raise
        # the price: the gate immediately below sends it to the inactive path.
        pct = parse_money(raw_pct, field='discount_percentage',
                          allow_negative=True)
        amt = parse_money(raw_amt, field='discount_amount',
                          allow_negative=True)
    except MoneyConfigError:
        # An ACTIVE but unreadable discount must NOT become "no discount" and
        # must NOT become zero. It is an unusable item.
        return _unusable(DISCOUNT_UNREADABLE)

    if pct <= 0 and amt <= 0:
        return PriceVerdict(
            usable=True, reference_base=reference, effective_base=reference,
            discount_active=False,
        )

    # ONE evaluation at working precision, quantized ONCE at the end, so a
    # percentage of a large price does not drift through intermediate rounding.
    with working_context():
        if pct > 0:
            raw_effective = reference - (reference * pct / _HUNDRED)
        else:
            raw_effective = reference - amt
    try:
        effective = quantize_money(raw_effective, field='effective_base')
    except MoneyConfigError:
        return _unusable(DISCOUNT_UNREADABLE)

    # A discount may take the price to zero (100% off, a waived dish) but never
    # below it, and never ABOVE the reference. Either direction is an incoherent
    # configuration, and the pre-fix zero clamp turned the first one into a free
    # dish. Fail closed instead.
    if effective < _ZERO or effective > reference:
        return _unusable(DISCOUNT_EXCEEDS_PRICE)

    return PriceVerdict(
        usable=True, reference_base=reference, effective_base=effective,
        discount_active=True,
    )


def _is_unset(value):
    """True for the supported 'no discount configured' forms.

    Deliberately narrow: only ``0`` (any numeric spelling of it) counts, so a
    malformed value still reaches the money parser and is reported rather than
    quietly read as "unset". ``None`` and ``''`` were already collapsed to 0 by
    the caller's ``or 0``.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        try:
            return parse_money(stripped) == _ZERO
        except MoneyConfigError:
            return False
    return False


def _window_is_open(details, now):
    """Date / weekday / time-of-day gates, byte-for-byte the pre-fix rules.

    A malformed bound is treated as UNBOUNDED on that side — pre-existing,
    deliberate, and left alone so no live price moves.
    """
    today = timezone.localdate(now) if timezone.is_aware(now) else now.date()

    start_date = details.get('start_date') or ''
    end_date = details.get('end_date') or ''
    if start_date:
        parsed = _date(start_date)
        if parsed is not None and today < parsed:
            return False
    if end_date:
        # INCLUSIVE: the last day the discount is valid.
        parsed = _date(end_date)
        if parsed is not None and today > parsed:
            return False

    recurring_days = details.get('recurring_days') or []
    # Empty means EVERY day (the established decision lever).
    if isinstance(recurring_days, (list, tuple)) and len(recurring_days) > 0:
        if today.isoweekday() not in recurring_days:
            return False

    now_time = (timezone.localtime(now) if timezone.is_aware(now) else now).time()
    start_time = details.get('start_time') or ''
    end_time = details.get('end_time') or ''
    if start_time:
        parsed = _time(start_time)
        if parsed is not None and now_time < parsed:
            return False
    if end_time:
        parsed = _time(end_time)
        if parsed is not None and now_time > parsed:
            return False
    return True


def _date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _time(value):
    try:
        return datetime.strptime(f'{value}:00', '%H:%M:%S').time()
    except (ValueError, TypeError):
        return None
