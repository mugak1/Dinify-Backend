"""
D06 — MAY NEW WORK HAPPEN AT THIS RESTAURANT, AT THIS TABLE, RIGHT NOW?

ONE RULE, ASKED AT THREE MOMENTS. ``evaluate`` is pure: facts in, verdict out. The
controller preflight asks it on instances loaded in autocommit (fast feedback), and
BOTH authoritative boundaries ask it again on rows they hold locks on — the create
transaction after the ``Table`` row lock, and the acceptance transaction after the
``Table`` and ``Order`` locks. The three cannot drift into different readings of the
same state because they call the same function; they differ only in WHICH snapshot
of the facts they hand it, which is the whole point.

WHAT WAS WRONG BEFORE. These three facts were read once, in
``ConOrder.initiate_order``, off instances fetched in autocommit before the
transaction opened, and only for anonymous diners. Nothing re-read them after the
blocking table-lock acquisition, and nothing consulted them at acceptance at all.
So: a restaurant could pause and the draft a diner had already initiated still
reached the kitchen; a table could be switched to menu-only, taken out of service
or soft-deleted and a draft still became an order on it; and a change committing
while a request waited on the table row was invisible to the request that then
wrote. Measured, over the real routes, on unmodified 32fe4f9.

THREE FACTS, THREE DIFFERENT KINDS OF FACT — and the difference decides who they
bind:

``accepting_orders`` is a COMMERCIAL PAUSE on new diner ordering. It stops the QR
public placing and completing orders. It does not cancel accepted orders, does not
stop the kitchen, and does not stop an authorized member of staff taking an order
on someone's behalf — that staff exception already existed and is preserved
deliberately, because "stop the QR codes" is what the control means. A pause is
therefore NOT an emergency stop, and the settings copy says so.

``qr_mode`` is ORDERING POLICY for the QR public: a ``menu_only`` table is a menu
a diner may read and not order from. The same staff exception applies for the same
reason.

Table LIVENESS and restaurant LIVENESS are neither: a soft-deleted, disabled,
inactive or out-of-service table is not a place an order can exist, and a
soft-deleted restaurant is not a tenant that can take one. They bind EVERY
provenance, staff included. A member of staff may order for a diner; nobody may
order onto a table that has been removed.

PROVENANCE IS THE ORDER'S, NEVER THE REQUESTER'S. ``created_by`` is tested for
presence only, exactly as ``order_admission.evaluate`` tests it, and at acceptance
the caller passes the ORDER's ``created_by_id``. A diner's draft stays a diner's
draft however senior the person who taps submit; a staff member submitting one
does not convert it into a management action. The endpoint is what decides
provenance at creation, behind the existing module/tenant authorization — being
authenticated is not the exception, holding the tables module at THAT restaurant
is.

WHAT THIS MODULE IS NOT. It is not authorization: the diner capability channel and
the staff module gate decide WHO may act, answer in their own established shapes
(an opaque 404, a 401), and run before any of this. It is not the lifecycle rule
either — ``order_admission`` owns ``status`` and the launch boundary, and a staff
exception here never overrides a suspension there. And it is not the quote rules:
age is ``quote_policy``'s and purchase meaning is ``purchase_integrity``'s.
"""
from dataclasses import dataclass
from typing import Optional

#: The QR modes a diner may order through. A WHITELIST, so a future mode that
#: nobody thought about here fails closed rather than accidentally selling food.
ORDERING_QR_MODES = ('order_pay', 'order_only')

#: Stable machine codes. A client uses these to decide what to DO; the messages
#: are what a diner reads. Every one of them is TRANSIENT — the restaurant can
#: resume, the table can be brought back — which is why none of them ever closes
#: a quote (see ``quote_closure``).
REASON_RESTAURANT_PAUSED = 'restaurant_paused'
REASON_RESTAURANT_UNAVAILABLE = 'restaurant_unavailable'
REASON_TABLE_ORDERING_UNAVAILABLE = 'table_ordering_unavailable'
REASON_TABLE_UNAVAILABLE = 'table_unavailable'

#: The set a client may treat as "try the same attempt again later". Named here
#: so the frontend contract and the server agree by construction rather than by
#: two lists that drift.
TRANSIENT_REASONS = frozenset({
    REASON_RESTAURANT_PAUSED,
    REASON_RESTAURANT_UNAVAILABLE,
    REASON_TABLE_ORDERING_UNAVAILABLE,
    REASON_TABLE_UNAVAILABLE,
})

#: The first three messages are the EXACT strings the preflight has always
#: returned, kept byte-identical: they are pinned by existing tests and read by a
#: deployed client, and this change adds a machine code beside them rather than
#: rewording a refusal a diner already understands.
MESSAGE_RESTAURANT_PAUSED = 'This restaurant is not currently accepting orders'
MESSAGE_TABLE_ORDERING_UNAVAILABLE = 'Ordering is not available at this table'
MESSAGE_TABLE_UNAVAILABLE = 'This table is not available for ordering'
MESSAGE_RESTAURANT_UNAVAILABLE = 'This restaurant is not available right now'


@dataclass(frozen=True)
class OperationalFacts:
    """The state one eligibility decision is made from.

    Built by the caller from rows it is entitled to trust at that moment — the
    locked ``Restaurant`` read for the authoritative checks, the locked ``Table``
    row, or (preflight only) instances loaded in autocommit. Keeping the facts in
    a value object is what lets the rule be pure and lets a test state a
    situation without a database.

    ``table_present`` is separate from the other table fields so "there is no
    table row" and "the table is unusable" reach the same refusal without the
    rule having to reason about None.
    """

    accepting_orders: bool
    restaurant_deleted: bool = False
    table_present: bool = True
    table_qr_mode: Optional[str] = None
    table_scannable: bool = True


@dataclass(frozen=True)
class EligibilityVerdict:
    allowed: bool
    reason: str = ''
    message: str = ''

    def as_refusal(self, *, extra=None):
        """The established ``{status, message, reason}`` refusal envelope.

        HTTP 400, matching every other business refusal on these two routes, so
        the DEPLOYED client's existing forwarding rule (an ``orders/submit`` 400
        carrying a string ``reason``) already delivers the code to the basket
        instead of flattening it to a toast. ``extra`` carries the D06 quote
        metadata where the caller has it.
        """
        body = {'status': 400, 'message': self.message, 'reason': self.reason}
        if extra:
            body.update(extra)
        return body


_ALLOWED = EligibilityVerdict(allowed=True)


def facts_from_rows(restaurant, table) -> OperationalFacts:
    """Read the facts off a restaurant and a table row.

    ``restaurant`` may be a ``Restaurant`` instance or any object exposing the
    two attributes — the authoritative callers pass a small value object built
    from the locked ``values_list``, precisely so no lazy attribute access can
    reach back to the database for a fact this decision depends on.
    """
    return OperationalFacts(
        accepting_orders=bool(getattr(restaurant, 'accepting_orders', False)),
        restaurant_deleted=bool(getattr(restaurant, 'deleted', False)),
        table_present=table is not None,
        table_qr_mode=getattr(table, 'qr_mode', None),
        # The model predicate, not a re-spelling of it: `is_available_for_scan`
        # already means "not soft-deleted, enabled, active, not out of service",
        # it lives on the model so the rule survives the endpoint substrate, and
        # the diner capability resolver enforces the same one. Two copies would
        # disagree the first time a field is added to either.
        table_scannable=bool(
            table is not None and table.is_available_for_scan()
        ),
    )


def facts_from_verdict(verdict, table) -> OperationalFacts:
    """The facts an AUTHORITATIVE boundary is entitled to decide from.

    The restaurant half comes from the ``AdmissionVerdict``, i.e. from the one
    query ``order_admission.admit`` runs under the shared advisory lock. The table
    half comes from the row the caller is holding a lock on. That pairing is the
    whole point: both halves describe a moment this transaction has protected,
    rather than whatever the request happened to load on the way in.

    It duck-types on the verdict so this module imports nothing — ``evaluate`` is
    pure, and a rule that has to import an admission service to state itself is
    one boundary away from importing a queryset.

    Do NOT pass a ``Restaurant`` instance here to save a lookup: a verdict is a
    protected read and an instance is not, and the two must stay distinguishable
    at the call site. ``facts_from_rows`` is the entry point for the preflight,
    which is allowed to be stale and says so.
    """
    return OperationalFacts(
        accepting_orders=bool(
            getattr(verdict, 'restaurant_accepting_orders', False)),
        restaurant_deleted=bool(getattr(verdict, 'restaurant_deleted', True)),
        table_present=table is not None,
        table_qr_mode=getattr(table, 'qr_mode', None),
        table_scannable=bool(
            table is not None and table.is_available_for_scan()
        ),
    )


def evaluate(facts: OperationalFacts, created_by) -> EligibilityVerdict:
    """THE RULE. Pure: no database, no lock, no clock.

    ``created_by`` is the ORDER's provenance — ``None`` for the anonymous QR
    public, anything else for an authorized staff-origin order. It is tested for
    PRESENCE only, so a ``User``, a primary key or ``None`` all behave the same,
    which is what lets the acceptance path pass ``order.created_by_id`` without
    fetching a row.

    ORDER OF CHECKS. Liveness first and provenance-blind, because "this thing no
    longer exists" is true for everyone and is the more accurate answer; then the
    two policy gates, which a staff-origin order is allowed past.
    """
    if facts.restaurant_deleted:
        return EligibilityVerdict(
            allowed=False,
            reason=REASON_RESTAURANT_UNAVAILABLE,
            message=MESSAGE_RESTAURANT_UNAVAILABLE,
        )

    if not facts.table_present or not facts.table_scannable:
        return EligibilityVerdict(
            allowed=False,
            reason=REASON_TABLE_UNAVAILABLE,
            message=MESSAGE_TABLE_UNAVAILABLE,
        )

    # --- policy gates: the QR public only -----------------------------------
    if created_by is not None:
        return _ALLOWED

    if not facts.accepting_orders:
        return EligibilityVerdict(
            allowed=False,
            reason=REASON_RESTAURANT_PAUSED,
            message=MESSAGE_RESTAURANT_PAUSED,
        )

    if facts.table_qr_mode not in ORDERING_QR_MODES:
        return EligibilityVerdict(
            allowed=False,
            reason=REASON_TABLE_ORDERING_UNAVAILABLE,
            message=MESSAGE_TABLE_ORDERING_UNAVAILABLE,
        )

    return _ALLOWED


def evaluate_rows(restaurant, table, created_by) -> EligibilityVerdict:
    """``evaluate`` over rows the caller already holds. The usual entry point."""
    return evaluate(facts_from_rows(restaurant, table), created_by)
