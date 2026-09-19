"""
D06 — RETIRING A QUOTE THAT MAY NEVER BE ACCEPTED, EXACTLY ONCE.

THE PROBLEM IT SOLVES. When a saved quote can no longer be honoured, the diner
needs a fresh one. Minting a second attempt for the same basket is only safe if
the FIRST one can never later execute — otherwise a queued or retried acceptance
lands after the replacement and the diner has bought the same meal twice. A
refusal message is not that guarantee: it is one process's opinion at one moment,
it is not durable, and another worker holding a stale request knows nothing about
it. Expiry alone is not that guarantee either for the availability case, because
stock can come back and make an "invalidated" purchase valid again.

So the guarantee is a committed row. ``OrderQuoteClosure`` says: this draft is
permanently barred from first acceptance. Every acceptance path reads it under the
same locks before it can accept anything, so once it commits, every request still
in flight for that draft refuses — whatever it believed when it started.

TWO ENTRY PATHS, ONE SERVICE, ONE SET OF RULES.

  1. An actual first-acceptance attempt that finds the quote definitively
     unacceptable closes it as part of deciding, inside the locks it already
     holds.
  2. ``retire_for_review`` — the diner asking for a new quote without submitting
     one. It is deliberately a separate operation rather than a flag on the
     acceptance route, so "review" can never be a misspelling of "accept": this
     function contains no path that accepts an order, and the acceptance route
     contains no path that skips its invariants.

Both call ``close``. Neither creates the replacement — a new quote is a new,
explicitly requested purchase, and this service's only job is to make that safe.

ONLY TWO REASONS CLOSE A QUOTE, AND A TRANSIENT REFUSAL IS NEVER ONE OF THEM. A
restaurant that paused, a table switched to menu-only or taken out of service, a
lost network response, a permission failure, an unreadable body, a database error
— none of these says the purchase is finished. They say "not now". Closing on any
of them would destroy a perfectly good quote and force a reprice the diner never
asked for. The vocabulary is frozen on the model precisely so a future caller
cannot quietly widen it.

WHAT CANNOT BE CLOSED.

  * An ACCEPTED order. The acceptance is the outcome; there is nothing to retire,
    and recording a closure beside it would create a row that reads "this was both
    accepted and permanently unacceptable". Acceptance is resolved FIRST, always.
  * An order that is no longer a draft but carries no acceptance evidence. That is
    D04's ``evidence_unavailable`` — a statement of ignorance — and guessing here
    would convert ignorance into a terminal fact. It is refused for manual review
    and nothing is written.
  * A quote whose reference the caller cannot name correctly. Closure retires ONE
    reference; a caller naming a different one is not repeating this decision.

IDEMPOTENCE PRESERVES HISTORY. Re-closing returns the ORIGINAL row — its reason,
its moment, its reference — and writes nothing. A caller arriving with a different
reference gets a controlled conflict rather than a rewritten record.

THE ROW COMMITS WITH THE DECISION OR NOT AT ALL. It is written inside the caller's
transaction, under the same admission → Table → Order synchronisation an acceptance
takes, so an acceptance and a closure racing for the same draft are ordered by the
database rather than by luck: whichever takes the ``Order`` row first wins, and the
loser reads what the winner committed and reports it instead of acting. This is the
one deliberate exception to "a refusal writes nothing" — the write IS the refusal's
durability — and it is why the response must be returned only after that
transaction commits.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from django.db import transaction

from orders_app.models import (
    OrderAcceptance,
    OrderQuoteClosure,
    QUOTE_CLOSURE_REASON_EXPIRED,
    QUOTE_CLOSURE_REASON_PURCHASE_CHANGED,
    QUOTE_CLOSURE_REASONS,
)

#: Re-exported so a caller names a closure reason through the service that
#: enforces the vocabulary, rather than reaching into the model for a string.
#: The vocabulary is FROZEN: only these two facts are terminal (see the module
#: docstring), and a transient refusal must never be spelled as one of them.
REASON_EXPIRED = QUOTE_CLOSURE_REASON_EXPIRED
REASON_PURCHASE_CHANGED = QUOTE_CLOSURE_REASON_PURCHASE_CHANGED
from orders_app.controllers.services.quote_policy import QUOTE_POLICY_VERSION

logger = logging.getLogger(__name__)

#: The caller asked to retire a quote that is still perfectly good. Not an error
#: and not a closure: the answer is "your quote still stands".
OUTCOME_STILL_VALID = 'quote_still_valid'
#: Closed by this call.
OUTCOME_CLOSED = 'quote_closed'
#: Already closed; the original row is returned unchanged.
OUTCOME_ALREADY_CLOSED = 'quote_already_closed'

#: The order was accepted. Closure is impossible and the caller is told the
#: truth rather than given a terminal fact about an order that succeeded.
REASON_ALREADY_ACCEPTED = 'order_already_accepted'
#: The order is no longer a draft and carries no acceptance evidence — D04's
#: `evidence_unavailable`. Refused for review; nothing is written.
REASON_EVIDENCE_UNAVAILABLE = 'acceptance_evidence_unavailable'
#: The caller named a reference that is not this draft's saved quote.
REASON_QUOTE_REF_MISMATCH = 'quote_ref_stale'

MESSAGE_ALREADY_ACCEPTED = (
    'This order has already been placed. Please check its status rather than '
    'reviewing it again.'
)
MESSAGE_EVIDENCE_UNAVAILABLE = (
    'We could not confirm the status of this order. Please ask a member of '
    'staff for help.'
)
#: This draft's quote has already been retired — by an earlier acceptance
#: attempt that found it unacceptable, or by an explicit retire-for-review. It
#: is a DEFINITIVE refusal: the answer will never change, whatever happens to
#: the restaurant, the table or the menu afterwards.
REASON_QUOTE_CLOSED = 'quote_closed'

MESSAGE_QUOTE_CLOSED = (
    'This order needs to be reviewed again before it can be placed. Please '
    'review it and place it again.'
)

MESSAGE_QUOTE_REF_MISMATCH = (
    'Your order changed since you reviewed it. Please review the updated order '
    'and place it again.'
)


@dataclass(frozen=True)
class ClosureResult:
    outcome: str
    closure: Optional[OrderQuoteClosure] = None


class ClosureRefused(Exception):
    """Closure is not available for this order, and no row was written."""

    def __init__(self, reason, message):
        super().__init__(reason)
        self.reason = reason
        self.message = message


def read_closure(order):
    """The committed closure for ``order``, or ``None``.

    Prefers a relation the caller already joined — ``select_related`` on the read
    path — so the preference is automatic rather than something every caller has
    to remember, exactly as ``acceptance_result.read_evidence`` does for
    acceptance evidence. A plain attribute access on a joined relation issues no
    query; the fallback lookup is right for an ad-hoc caller and is what the
    pinned query counts catch if a hot path ever starts doing it.
    """
    if order is None:
        return None
    try:
        return order.quote_closure
    except OrderQuoteClosure.DoesNotExist:
        return None
    except AttributeError:                       # pragma: no cover - defensive
        return OrderQuoteClosure.objects.filter(order=order).first()


def close(order, *, quote_ref, reason, now, evidence=None):
    """Retire ``order``'s saved quote, or report why that is impossible.

    MUST run inside the caller's transaction, holding the locks an acceptance
    takes, so that this decision and a competing acceptance are ordered by the
    database rather than by arrival. A closure written in autocommit beside an
    acceptance still deciding would be exactly the double-purchase this service
    exists to prevent, and that failure is silent.

    A1(C) — EXACTLY HOW MUCH OF THAT IS ASSERTED HERE, stated precisely because
    this docstring used to say "asserted rather than assumed" about the whole
    sentence and only half of it was:

      * INSIDE A TRANSACTION — genuinely asserted below, and it raises.
      * HOLDING THE ORDER ROW — NOT asserted, and not assertable. PostgreSQL
        records a row lock on the tuple itself rather than in `pg_locks`, so
        there is no cheap query that answers "do I hold FOR UPDATE on this
        row"; a `FOR UPDATE NOWAIT` probe would succeed just as readily when
        NOBODY holds it, which is the case that matters. Claiming an assertion
        that cannot exist is worse than naming the guarantee that does.

    WHAT ACTUALLY GUARANTEES IT: every production path that reaches here takes
    `Order.objects.select_for_update()` on the target first, and
    `tests_closure_preconditions.py` scans the source to keep that true rather
    than leaving it a convention nobody checks. That is this repository's
    established answer to a cross-function discipline — the same shape as the
    membership-serialization and ambient-authority ratchets.

    There is deliberately NO caller-supplied "already locked" flag. A trusted
    switch would let the one caller that gets it wrong assert its way past the
    only thing standing between a closure and an acceptance.

    ``evidence`` is the caller's already-loaded ``OrderAcceptance`` (or None) when
    it has one, so the ordinary acceptance path spends no extra query proving the
    order is unaccepted — it has just asked that question itself.
    """
    if reason not in QUOTE_CLOSURE_REASONS:
        # Programmer error, not a caller outcome. A transient refusal reaching
        # here would permanently destroy a good quote, so it raises loudly
        # rather than being coerced into the nearest valid reason.
        raise ValueError(f'{reason!r} is not a quote-closure reason')

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'quote_closure.close() must run inside the caller\'s transaction, '
            'holding the locks an acceptance takes — otherwise a closure and an '
            'acceptance can both decide they won.'
        )

    if not isinstance(quote_ref, str) or not quote_ref:
        raise ClosureRefused(REASON_QUOTE_REF_MISMATCH, MESSAGE_QUOTE_REF_MISMATCH)

    existing = read_closure(order)
    if existing is not None:
        if existing.quote_ref != quote_ref:
            # A different reference is a different decision. The committed one
            # stands; nothing is rewritten.
            raise ClosureRefused(
                REASON_QUOTE_REF_MISMATCH, MESSAGE_QUOTE_REF_MISMATCH)
        return ClosureResult(outcome=OUTCOME_ALREADY_CLOSED, closure=existing)

    # ACCEPTANCE IS RESOLVED BEFORE ANY CLOSURE IS CONSIDERED. An accepted order
    # has an outcome already; a non-draft with no evidence is D04's statement of
    # ignorance and must not be converted into a terminal fact here.
    if evidence is None:
        evidence = OrderAcceptance.objects.filter(order=order).first()
    if evidence is not None:
        raise ClosureRefused(REASON_ALREADY_ACCEPTED, MESSAGE_ALREADY_ACCEPTED)

    from dinify_backend.configss.string_definitions import OrderItemStatus_Initiated
    if order.order_status != OrderItemStatus_Initiated:
        raise ClosureRefused(
            REASON_EVIDENCE_UNAVAILABLE, MESSAGE_EVIDENCE_UNAVAILABLE)

    closure = OrderQuoteClosure.objects.create(
        order=order,
        closed_at=now,
        quote_ref=quote_ref,
        reason=reason,
        policy_version=QUOTE_POLICY_VERSION,
    )
    # Bounded: an order id, the reason, the policy version. No amounts, no order
    # contents, no personal data, no reference material beyond what the diner
    # already holds.
    logger.info(
        'Quote closed (order_id=%s, reason=%s, policy_version=%s)',
        order.pk, reason, QUOTE_POLICY_VERSION,
    )
    return ClosureResult(outcome=OUTCOME_CLOSED, closure=closure)


def closure_projection(closure):
    """The closure as a client reads it, or ``None``.

    Deliberately narrow: what happened, when, to which reference, under which
    policy. No actor, no catalogue detail, no credential, no order contents — a
    client needs to know the old attempt is finished, not why in forensic terms.
    """
    if closure is None:
        return None
    return {
        'closed_at': closure.closed_at.isoformat() if closure.closed_at else None,
        'reason': closure.reason,
        'quote_ref': closure.quote_ref,
        'policy_version': closure.policy_version,
    }
