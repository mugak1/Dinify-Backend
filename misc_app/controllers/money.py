"""
D02 — the ONE monetary parsing and rounding contract.

PURE. No Django model, no query, no app import, no clock. It sits BELOW both the
catalogue services and the order services deliberately: ``restaurants_app`` reads
prices on the public menu and ``orders_app`` charges them, and if the two used
different parsers the price a diner is shown and the price they are charged could
disagree on exactly the inputs neither of them validated. Placing it here is also
what keeps ``restaurants_app.models`` free of any import into an order
orchestration module — that direction would be an import cycle.

WHAT IT IS FOR. Operator-supplied monetary configuration reaches the server as
UNVALIDATED JSON: ``MenuItem.options`` is a plain ``JSONField`` and
``discount_details`` is another. D01 validates the STRUCTURE of a stored modifier
definition and says in terms that it "validates NO monetary configuration"; this
module is that missing half. Before it, ``Decimal(str(value))`` was applied
directly to whatever the column held, so ``'abc'``, ``None``, ``True``, ``[]``,
``{}``, ``'NaN'``, ``'Infinity'`` and ``'1e400'`` each raised out of checkout as an
uncaught 500, and ``'NaN'`` additionally reached the serializer and the persisted
``options`` JSON.

THE RULES, AND WHY EACH ONE EXISTS

* **A malformed monetary value is NEVER priced at zero.** Zero is a legitimate,
  supported amount — a free modifier, a free dish, a waived extra — so using it as
  the failure value would make a paid option free and be indistinguishable from an
  operator deliberately configuring one. Malformed input raises
  ``MoneyConfigError`` and the caller fails closed with a controlled refusal.
* **``bool`` is NOT a number.** It is an ``int`` subclass, so ``Decimal(str(True))``
  is a ``ConversionSyntax`` crash today and ``int(True)`` would silently be 1.
  Neither is a price.
* **Both live catalogue forms are accepted**: a JSON number (``1500``, ``1500.5``)
  and a numeric string (``'1500'``). Both occur in the shipped data and in this
  repository's own fixtures, so refusing either would make existing items
  unorderable.
* **Non-finite is refused before arithmetic.** ``Decimal('NaN')`` and
  ``Decimal('Infinity')`` construct successfully and only explode later, at
  ``quantize`` or at the serializer, by which point a row may already have been
  written.
* **Range and precision are bounded BEFORE conversion.** ``'1e400'`` parses into a
  Decimal with a 400-digit exponent, and the cost of quantizing or formatting it is
  paid before anything notices. The textual length gate runs first so an enormous
  literal is refused without being converted at all.

THE BOUNDS ARE DERIVED, NOT INVENTED. ``MAX_MONEY_DIGITS`` / ``MONEY_DECIMAL_PLACES``
mirror ``DecimalField(max_digits=50, decimal_places=2)``, the column every one of
these values is ultimately stored in; ``MAX_MONEY_MAGNITUDE`` is the largest value
that column can hold. They are TECHNICAL capacity limits and deliberately NOT a
commercial price cap — this module has no opinion about what a restaurant may
charge.

ROUNDING. ``ROUND_HALF_EVEN`` at two decimal places, applied to each monetary UNIT
component exactly once. Half-even is the rule the pre-fix code already used via
``Decimal.quantize``'s default context, and it is stated explicitly here rather than
inherited, so it cannot change because a caller altered the decimal context.
"""
from decimal import (
    Context, Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN,
)

#: Money is carried at two decimal places, matching every monetary column
#: (``DecimalField(max_digits=50, decimal_places=2)``).
MONEY_QUANTUM = Decimal('0.01')
MONEY_DECIMAL_PLACES = 2
#: Mirrors the columns' ``max_digits``.
MAX_MONEY_DIGITS = 50
#: The largest magnitude those columns can hold: 48 integer digits and 2 decimals.
MAX_MONEY_MAGNITUDE = Decimal(10) ** (MAX_MONEY_DIGITS - MONEY_DECIMAL_PLACES)
#: Textual gate applied BEFORE conversion, so an enormous literal is refused
#: without being parsed. Generous relative to MAX_MONEY_DIGITS on purpose: it
#: bounds work, it does not define validity.
MAX_MONEY_TEXT_LENGTH = 64
#: Working precision for the discount formula before its single quantization.
#: Comfortably above MAX_MONEY_DIGITS so a percentage of a large price is computed
#: exactly and rounded once, rather than drifting through several roundings.
MONEY_WORKING_PRECISION = 80

#: Stable reason codes. Safe to log; they never contain the offending value.
NOT_A_NUMBER = 'not_a_number'
NOT_FINITE = 'not_finite'
OUT_OF_RANGE = 'out_of_range'
TOO_LONG = 'too_long'
NEGATIVE_NOT_ALLOWED = 'negative_not_allowed'


class MoneyConfigError(ValueError):
    """A stored monetary value that cannot be priced.

    Carries a stable ``code`` and the ``field`` it came from — never the value.
    Callers translate it into their own controlled, diner-safe refusal; it must
    never reach a response body or a log line as-is.
    """

    def __init__(self, code, field=''):
        super().__init__(f'{field or "money"}: {code}')
        self.code = code
        self.field = field


def parse_money(value, *, field='', allow_negative=False):
    """Parse ONE operator-supplied monetary value into a canonical 2dp Decimal.

    Accepts a real ``int``, a finite ``float``, a ``Decimal``, or a numeric
    ``str``. Refuses ``bool``, ``None``, containers, non-finite values and
    anything outside the columns' representable range, ALWAYS by raising —
    never by substituting a default.

    ``allow_negative`` is opt-in per call site rather than global, because the
    roles differ: a modifier ADJUSTMENT may legitimately be signed (a "no
    cheese, -500" option works end to end today and is kept), while a base
    price, an extra's price and a discount magnitude may not.
    """
    if value is None:
        raise MoneyConfigError(NOT_A_NUMBER, field)
    # bool before int: it is an int subclass and must never read as 1/0.
    if isinstance(value, bool):
        raise MoneyConfigError(NOT_A_NUMBER, field)

    if isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        # A finite float is a legitimate stored form (JSON numbers arrive this
        # way). `str()` first, so the value converts through its shortest
        # repr rather than its full binary expansion.
        if value != value or value in (float('inf'), float('-inf')):
            raise MoneyConfigError(NOT_FINITE, field)
        candidate = _from_text(repr(value), field)
    elif isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, str):
        candidate = _from_text(value, field)
    else:
        # lists, dicts, objects — anything that is not a number
        raise MoneyConfigError(NOT_A_NUMBER, field)

    if not candidate.is_finite():
        raise MoneyConfigError(NOT_FINITE, field)
    if candidate.copy_abs() >= MAX_MONEY_MAGNITUDE:
        raise MoneyConfigError(OUT_OF_RANGE, field)
    if candidate < 0 and not allow_negative:
        raise MoneyConfigError(NEGATIVE_NOT_ALLOWED, field)

    return quantize_money(candidate, field=field)


def _from_text(text, field):
    """Length-gate first, then convert. The gate is what stops an enormous
    literal being parsed at all."""
    if len(text) > MAX_MONEY_TEXT_LENGTH:
        raise MoneyConfigError(TOO_LONG, field)
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise MoneyConfigError(NOT_A_NUMBER, field) from None


def quantize_money(value, *, field=''):
    """Canonicalize to 2dp with ROUND_HALF_EVEN.

    Applied to each monetary UNIT component exactly once. Extended amounts are
    then an exact multiplication of an already-canonical unit by an integer
    quantity, so they need no further rounding and recomputing twice is
    identical by construction.
    """
    try:
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ArithmeticError):
        raise MoneyConfigError(OUT_OF_RANGE, field) from None


def extend_money(unit, quantity, *, field=''):
    """Extend a canonical unit amount over an integer quantity.

    EXACT: a 2dp value times a whole number is already 2dp, so there is no
    second rounding and no path by which two recomputations of the same line
    can disagree. The range check is what stops a large quantity multiplying a
    large unit past the column.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise MoneyConfigError(NOT_A_NUMBER, field or 'quantity')
    if quantity < 0:
        raise MoneyConfigError(NEGATIVE_NOT_ALLOWED, field or 'quantity')
    result = unit * Decimal(quantity)
    if result.copy_abs() >= MAX_MONEY_MAGNITUDE:
        raise MoneyConfigError(OUT_OF_RANGE, field)
    return quantize_money(result, field=field)


def working_context():
    """A local decimal context for multi-step arithmetic (the discount formula).

    Used so the intermediate of a percentage calculation carries full precision
    and is quantized ONCE at the end, instead of being rounded at each step.
    """
    return localcontext(Context(
        prec=MONEY_WORKING_PRECISION, rounding=ROUND_HALF_EVEN,
    ))
