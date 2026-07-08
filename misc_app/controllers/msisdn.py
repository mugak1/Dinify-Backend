"""
Canonical MSISDN handling for Dinify (Uganda).

The canonical STORED/COMPARED form of a user's phone number is a 12-digit,
plus-free string: ``256XXXXXXXXX``. Display formatting (e.g. ``+256 …``) is a
frontend concern and is deliberately NOT applied here.

``normalise_msisdn`` is the single source of truth for that canonicalisation and
is applied at every backend write site, at payment intake, and at OTP
create/verify. It NEVER returns ``None`` or a partially transformed string:
either it returns a validated canonical value or it raises.
"""
import re

# Uganda only, for now. There is deliberately no country -> prefix abstraction
# table: adding another country is an explicit, reviewed change, not a config row.
_UG_COUNTRY_TOKENS = {'UG', 'UGANDA'}
_NON_DIGIT = re.compile(r'\D')


class MsisdnError(ValueError):
    """Base class for MSISDN canonicalisation failures."""


class InvalidMsisdn(MsisdnError):
    """The number cannot be canonicalised (empty, garbage, or wrong length)."""


class UnsupportedCountry(MsisdnError):
    """The country is not supported for canonicalisation (only Uganda today)."""


def normalise_msisdn(msisdn, country: str = 'UG') -> str:
    """
    Canonicalise a Ugandan phone number to ``256XXXXXXXXX`` (12 digits, no '+').

    Accepts ``0772123456``, ``772123456``, ``256772123456``, ``+256772123456``
    and the same values with embedded spaces or hyphens.

    Raises:
        UnsupportedCountry: ``country`` is not Uganda.
        InvalidMsisdn: the value cannot be confidently canonicalised to a
            12-digit ``256`` number (empty, non-digit garbage, or wrong length).

    Idempotent: ``normalise_msisdn(normalise_msisdn(x)) == normalise_msisdn(x)``.

    Note: error messages never include the raw number, so it is safe to log or
    surface the exception without leaking the phone value.
    """
    if str(country or '').strip().upper() not in _UG_COUNTRY_TOKENS:
        raise UnsupportedCountry(
            f"Only Ugandan (UG) phone numbers are supported (country={country!r})."
        )

    # Strip '+', spaces, hyphens and any other separators FIRST, then branch on
    # the remaining digits — never on assumed structure of the raw input.
    digits = _NON_DIGIT.sub('', msisdn or '')
    if not digits:
        raise InvalidMsisdn("Empty or non-numeric phone number.")

    if digits.startswith('256'):
        candidate = digits
    elif digits.startswith('0'):
        candidate = '256' + digits[1:]
    elif len(digits) == 9:
        candidate = '256' + digits
    else:
        raise InvalidMsisdn(
            f"Cannot canonicalise phone number ({len(digits)} digits)."
        )

    if len(candidate) != 12 or not candidate.startswith('256'):
        raise InvalidMsisdn(
            f"Result is not a 12-digit 256 number ({len(candidate)} digits)."
        )

    return candidate


def mask_msisdn(value, lead: int = 4, trail: int = 2) -> str:
    """
    Defensively mask a phone-ish value for logging, based on LENGTH ONLY — never
    on assumed canonical structure — so malformed / sentinel / diverged values
    (e.g. ``'1'`` or garbage) mask safely too.

    Exposes at most the first ``lead`` and last ``trail`` characters and masks
    the middle. Short values are masked more aggressively so the whole value is
    never revealed, and it never indexes out of bounds.
    """
    if value is None:
        return '(none)'
    s = str(value)
    n = len(s)
    if n == 0:
        return '(empty)'
    if n <= 2:
        return '*' * n
    if n <= lead + trail:
        # Too short to reveal both ends without exposing (almost) everything.
        return s[0] + '*' * (n - 1)
    return s[:lead] + '*' * (n - lead - trail) + s[-trail:]


class BackfillPlan:
    """Result of :func:`plan_msisdn_backfill` — five mutually-exclusive buckets."""

    __slots__ = ('writes', 'invalid', 'unsupported', 'diverged', 'collision')

    def __init__(self):
        self.writes = []        # {id, canonical, orig_phone, orig_username, changed}
        self.invalid = []       # {id, orig_phone}
        self.unsupported = []   # {id, orig_phone, country}
        self.diverged = []      # {id, orig_phone, orig_username}
        self.collision = []     # {id, orig_phone, canonical}


def plan_msisdn_backfill(rows):
    """
    Pure planner (NO database access) for the user-table MSISDN backfill.

    ``rows`` is an iterable of ``(row_id, phone_number, username, country)``.
    Returns a ``BackfillPlan`` bucketing every row into exactly one outcome and
    detecting collisions BEFORE any write, so the caller never half-applies then
    crashes on the unique constraint.

    Rules:
      - normalisable + username mirrors phone_number + no collision -> convert.
      - ``InvalidMsisdn``                                           -> invalid.
      - ``UnsupportedCountry``                                      -> unsupported.
      - username diverges from phone_number                        -> diverged.
      - two+ rows normalise to the same canonical, OR the canonical is already
        held by a row that will not change                         -> collision.

    Idempotent: an already-canonical row plans a ``changed=False`` write (a
    no-op), so re-running never corrupts canonical rows.
    """
    plan = BackfillPlan()

    # Rows that will NOT change keep — and therefore occupy — their current
    # phone_number value. A convert candidate targeting an occupied value collides.
    fixed_occupied = {}          # current phone_number -> row_id
    candidates = []              # (row_id, canonical, orig_phone, orig_username)

    def _occupy(phone, row_id):
        if phone is not None:
            fixed_occupied.setdefault(phone, row_id)

    for row_id, phone, username, country in rows:
        try:
            canonical = normalise_msisdn(phone, country=country or 'UG')
        except UnsupportedCountry:
            plan.unsupported.append({'id': row_id, 'orig_phone': phone, 'country': country})
            _occupy(phone, row_id)
            continue
        except InvalidMsisdn:
            plan.invalid.append({'id': row_id, 'orig_phone': phone})
            _occupy(phone, row_id)
            continue

        if username != phone:
            # Do not guess how to reconcile a diverged username — leave untouched.
            plan.diverged.append(
                {'id': row_id, 'orig_phone': phone, 'orig_username': username}
            )
            _occupy(phone, row_id)
            continue

        candidates.append((row_id, canonical, phone, username))

    # Group convert candidates by target canonical to detect collisions. A
    # candidate's target is always a 12-digit 256 value, and any row that is
    # moving has a non-canonical current value, so no candidate can target
    # another candidate's (non-canonical) current value — only shared targets
    # and fixed occupants can collide.
    by_canonical = {}
    for row_id, canonical, phone, username in candidates:
        by_canonical.setdefault(canonical, []).append((row_id, phone, username))

    for canonical, members in by_canonical.items():
        collides = len(members) > 1 or (canonical in fixed_occupied)
        if collides:
            for row_id, phone, username in members:
                plan.collision.append(
                    {'id': row_id, 'orig_phone': phone, 'canonical': canonical}
                )
            continue
        row_id, phone, username = members[0]
        plan.writes.append({
            'id': row_id,
            'canonical': canonical,
            'orig_phone': phone,
            'orig_username': username,
            'changed': phone != canonical or username != canonical,
        })

    return plan
