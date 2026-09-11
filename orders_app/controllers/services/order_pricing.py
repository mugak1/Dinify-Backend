"""
D02 — the ONE server-owned order calculation, and D03 — the ONE line identity.

PURE. No query, no HTTP, no clock, no payment. Everything it needs is handed in
already resolved, so the same inputs always produce the same numbers and
recomputing a line twice is identical by construction. It is a calculation for
THIS checkout, not a generic pricing platform: no coupons, no tax, no service
fees, no promotions.

THE CORRECTED CONVENTION (``PRICING_VERSION_CORRECTED``), stated as a change

1. Every monetary UNIT component is canonicalised to 0.01 with ROUND_HALF_EVEN
   EXACTLY ONCE, through the shared money contract. The base-discount formula is
   evaluated at working precision and its result quantized once (see
   ``pricing_policy``), rather than being rounded at each step.
2. Each selected modifier's unit adjustment is canonicalised once, then the
   adjustments are summed EXACTLY. The same canonical components feed the labels,
   the group costs, the reference and effective units and the option totals — there
   is no second arithmetic anywhere.
3. Parent reference unit = reference base + modifier adjustments.
   Parent effective unit = effective base + the SAME adjustments.
   Neither includes separately persisted extras.
4. Each extra carries its own reference/effective unit snapshots, and its quantity
   matches its deliverable parent's quantity (one selected extra per dish).
5. Extended amounts are an immutable canonical unit multiplied by an integer
   quantity — exact, with no second rounding. ``savings`` is comparable reference
   minus comparable effective, NOT a clamp: it is non-negative because both sides
   carry the identical modifier component and the effective base can never exceed
   the reference base.
6. Recalculation and merging read the saved canonical UNIT components. Never a
   previously extended amount (the pre-fix merge did ``cost_of_options * quantity``
   on an already-extended value, so a 2+1 merge charged 9 000 of options where 4 500
   were due), and never a fresh catalogue lookup (that would reprice an order the
   diner has already been quoted).

WHAT THIS CORRECTS, CONCRETELY

* ``total_cost`` excluded modifiers while ``discounted_cost`` included them, so
  ``savings`` went NEGATIVE on any paid modifier and an order's net exceeded its
  gross. Reference and effective now compare the same components.
* An extra was persisted at ``quantity = 1`` regardless of how many dishes it was
  attached to, so three burgers with cheese were charged — and prepared — with one
  cheese.
* ``actual_cost`` was never refreshed by the merge path, so a merged line's stored
  payable stayed at its pre-merge value. That value is what the diner's order-detail
  read renders and what the Popular Items and Menu-performance reports aggregate.

LAST-UNIT DIFFERENCES FROM PRE-FIX BEHAVIOUR ARE REAL AND INTENDED. Canonicalising
each modifier adjustment individually before summing can differ in the last unit
from summing raw values and rounding once; the corrected convention rounds each
component once, so a component's stored value and its contribution to the total
always agree. Accepted historical orders are untouched, and the boundary is
identifiable from ``Order.pricing_version``.
"""
from misc_app.controllers.money import (
    MoneyConfigError, extend_money, parse_money, quantize_money, working_context,
)

#: Named pricing conventions. LEGACY is the pre-D02 calculation; it is the model
#: AND database default so that an existing row, an older application insert that
#: omits the column, and any future writer that forgets to opt in can never be
#: mistaken for a certified corrected order.
PRICING_VERSION_LEGACY = 0
PRICING_VERSION_CORRECTED = 1

_ZERO = parse_money(0)

#: Stable reason codes carried on a refusal. Safe to log; never rendered.
NEGATIVE_PAYABLE = 'negative_payable_unit'
MODIFIER_COST_UNREADABLE = 'modifier_cost_unreadable'


class PricingRefused(Exception):
    """This line cannot be priced from what is stored.

    Callers translate it into their own controlled, diner-safe refusal and fail
    closed. There is deliberately no fallback amount: zero is a real price and
    the undiscounted price is an unearned charge.
    """

    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


class PricedUnit:
    """The immutable per-unit components of one line."""

    __slots__ = ('reference_unit', 'effective_unit', 'modifier_unit',
                 'discount_active')

    def __init__(self, *, reference_unit, effective_unit, modifier_unit,
                 discount_active):
        self.reference_unit = reference_unit
        self.effective_unit = effective_unit
        self.modifier_unit = modifier_unit
        self.discount_active = discount_active


class PricedLine:
    """A priced line: immutable units plus the extended amounts for one quantity."""

    __slots__ = ('quantity', 'reference_unit', 'effective_unit', 'modifier_unit',
                 'discount_active', 'total_cost', 'discounted_cost',
                 'cost_of_options', 'savings', 'actual_cost')

    def __init__(self, *, unit, quantity, total_cost, discounted_cost,
                 cost_of_options, savings, actual_cost):
        self.quantity = quantity
        self.reference_unit = unit.reference_unit
        self.effective_unit = unit.effective_unit
        self.modifier_unit = unit.modifier_unit
        self.discount_active = unit.discount_active
        self.total_cost = total_cost
        self.discounted_cost = discounted_cost
        self.cost_of_options = cost_of_options
        self.savings = savings
        self.actual_cost = actual_cost


def modifier_adjustment(raw_cost, *, field='additionalCost'):
    """Canonicalise ONE stored modifier adjustment.

    Signed by design: a "no cheese, -500" option works end to end today and stays
    legal. The sign is bounded where it matters — at the line, by
    :func:`price_unit`, which refuses a configuration whose payable unit would go
    negative rather than clamping it to zero.
    """
    try:
        return parse_money(raw_cost, field=field, allow_negative=True)
    except MoneyConfigError as exc:
        raise PricingRefused(MODIFIER_COST_UNREADABLE, exc.code) from None


def price_unit(verdict, modifier_adjustments):
    """Build the immutable per-unit components from a price verdict and the
    already-canonical modifier adjustments.

    ``verdict`` is a ``pricing_policy.PriceVerdict`` the caller has already
    confirmed usable.
    """
    # Under the module's own precision, not the process default's 28 digits —
    # addition ROUNDS silently rather than raising, so a sum of schema-valid
    # amounts could quietly come back inexact and be quantized into a number
    # nothing could trace. See money.working_context.
    with working_context():
        modifier_unit = _ZERO
        for adjustment in modifier_adjustments:
            modifier_unit += adjustment
        modifier_unit = quantize_money(modifier_unit, field='cost_of_options')

        reference_unit = quantize_money(
            verdict.reference_base + modifier_unit, field='unit_price',
        )
        effective_unit = quantize_money(
            verdict.effective_base + modifier_unit, field='discounted_price',
        )

    # A genuinely free unit (0.00) is legal and stays orderable. A NEGATIVE
    # payable unit is an invalid configuration and is refused — never rounded or
    # clamped into legitimacy, which is the mistake the pre-fix
    # `price if price > 0 else Decimal('0')` made in the other direction.
    if effective_unit < _ZERO or reference_unit < _ZERO:
        raise PricingRefused(NEGATIVE_PAYABLE)

    return PricedUnit(
        reference_unit=reference_unit,
        effective_unit=effective_unit,
        modifier_unit=modifier_unit,
        discount_active=verdict.discount_active,
    )


def extend(unit, quantity):
    """Extend immutable units over an integer quantity.

    ``quantity`` 0 is the established internal representation of a line that is
    not deliverable: it is neither prepared nor charged, while its unit and name
    snapshots survive for the diner's reconciliation.
    """
    try:
        total_cost = extend_money(unit.reference_unit, quantity,
                                  field='total_cost')
        discounted_cost = extend_money(unit.effective_unit, quantity,
                                       field='discounted_cost')
        cost_of_options = extend_money(unit.modifier_unit, quantity,
                                       field='cost_of_options')
    except MoneyConfigError as exc:
        raise PricingRefused(exc.code, exc.field) from None

    # Comparable reference minus comparable effective. Non-negative by
    # construction — both sides carry the identical modifier component and the
    # effective base can never exceed the reference base — so this is a
    # subtraction, not a clamp.
    with working_context():
        savings = quantize_money(total_cost - discounted_cost, field='savings')

    return PricedLine(
        unit=unit, quantity=quantity,
        total_cost=total_cost, discounted_cost=discounted_cost,
        cost_of_options=cost_of_options, savings=savings,
        actual_cost=discounted_cost,
    )


def unit_from_row(row):
    """Recover the immutable unit components from a PERSISTED line.

    This is what makes a merge or a quantity change a RECALCULATION rather than a
    reinterpretation of previously extended amounts, and it is why recomputing a
    line twice gives the same answer. It reads the row's own stored units — never
    the catalogue, which would reprice an order the diner has already been
    quoted.
    """
    return PricedUnit(
        reference_unit=parse_money(row.unit_price, field='unit_price',
                                   allow_negative=True),
        effective_unit=parse_money(row.discounted_price,
                                   field='discounted_price', allow_negative=True),
        modifier_unit=parse_money(row.unit_cost_of_options or 0,
                                  field='unit_cost_of_options',
                                  allow_negative=True),
        discount_active=bool(row.discounted),
    )


# --- D03: the ONE canonical line identity -----------------------------------

def modifier_identity(selected_modifiers):
    """Order- and duplicate-independent key for a canonical modifier selection.

    Sorted by group and by choice, so two equivalent selections submitted in a
    different order compare equal, and an ABSENT selection is an empty key that
    matches only another empty key. Absence is not a wildcard: the pre-fix
    matcher derived ``has_modifiers`` from the INCOMING side alone, so a plain
    dish merged into a modified one and the diner was served two of the modified
    version.

    CHOICES ARE DE-DUPLICATED, and that is NOT the same decision as
    :func:`extras_identity` makes. D01's canonicalisation collapses a repeated
    choice — it is validated, labelled and charged exactly once — so a stored
    ``['c1','c1']`` and an incoming ``['c1']`` are the SAME selection, and
    keeping them apart would strand the legacy rows that canonicalisation
    deliberately shipped without a data migration. Duplicate EXTRAS, by
    contrast, are REFUSED upstream rather than collapsed, so that key keeps its
    multiset shape.
    """
    return tuple(sorted(
        (str(group_id),
         tuple(sorted({str(choice) for choice in (choices or [])})))
        for group_id, choices in (selected_modifiers or {}).items()
        if choices
    ))


def extras_identity(extra_ids):
    """Order-independent key for a line's selected extras.

    A SORTED TUPLE rather than a set: duplicate extra ids are refused upstream
    today, and keeping the multiset shape means a future change to that policy
    cannot silently make two different lines compare equal.
    """
    return tuple(sorted(str(extra_id) for extra_id in (extra_ids or [])))


def line_identity(*, item_id, selected_modifiers, extra_ids, reference_unit,
                  effective_unit, deliverable, name_snapshot,
                  modifiers_snapshot):
    """THE semantic identity of a parent order line.

    Two lines merge only when they are the same dish, with the same complete
    selections, AND compatible immutable pricing, deliverability and preparation
    snapshots. Within one order, resolved from one catalogue snapshot, the last
    four are constant per item, so this changes nothing about ordinary merging —
    it makes the rule structural rather than incidental, so a caller that prices
    two lines differently can never collapse them onto one stored amount.

    Identity is never derived from labels alone, from JSON insertion order, or
    from anything a client supplied.
    """
    return (
        str(item_id),
        modifier_identity(selected_modifiers),
        extras_identity(extra_ids),
        str(reference_unit),
        str(effective_unit),
        bool(deliverable),
        str(name_snapshot or ''),
        tuple(str(label) for label in (modifiers_snapshot or [])),
    )
