"""
The ONLY supported writers for ``RestaurantSubscriptionTerms`` (Step 3C).

Three named operations, and the set is deliberately closed:

    record_subscription_terms(...)    first terms for a restaurant with none open
    replace_subscription_terms(...)   close the open row + insert its successor, atomically
    end_subscription_terms(...)       close the open row, leaving none

━━ TERMS ROWS ARE NEVER EDITED IN PLACE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``recurring_amount``, ``currency``, ``billing_interval_unit``,
``billing_interval_count``, ``effective_from``, ``recorded_at`` and ``recorded_by``
are IMMUTABLE after creation. The only intended terminal mutation is ``ended_at``.

That is not fastidiousness: a future invoice will be raised UNDER a specific terms
row and a future owner go-live approval will be given FOR a specific terms row, and
both reference it by ``id``. If the numbers could move underneath them, neither
reference would mean anything afterwards — the invoice would claim a price that was
never in force and the approval would claim consent to terms nobody saw.

The DATABASE does not enforce this (a generic immutable-row trigger is not something
this PR adds, and Step 3B deliberately added no signals). THE SERVICE IS THE
DISCIPLINE: these three functions are the whole supported surface, and none of them
writes an existing row's commercial facts.

━━ WHAT THESE ROWS ARE NOT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Not an invoice, not a payment, not paid status, not entitlement, not good standing,
not PSP state, and not owner agreement. Nothing here reads or writes
``DinifyTransaction``, and nothing derives a status word. "Open" means exactly
``ended_at IS NULL`` — a fact, not a state machine.

The actor is recorded as ``recorded_by``: A PLATFORM-SIDE OPERATOR WROTE THIS DOWN.
It is never ``agreed_by`` / ``accepted_by`` / ``signed_by``; an administrator
recording terms does not prove the restaurant's owner agreed to them.

━━ NO SCHEDULING ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A future-dated ``effective_from`` or ``ended_at`` is REFUSED. The open-row invariant
is ``ended_at IS NULL`` — a predicate that consults no clock, because a partial index
cannot — so a row scheduled to take effect later would either be open before it
applied or need a sweeper to promote it, and this repository runs nothing on a
schedule. Backdating is fully supported: recording on Tuesday the terms that took
effect last month is an ordinary, truthful operation.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.utils import timezone

from commercial_app import errors
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    RestaurantSubscriptionTerms,
)
from commercial_app.mutation_context import (
    lock_restaurant,
    parse_uuid,
    resolve_actor,
)
# THE SELECTION RULE IS SHARED, NOT COPIED. `commercial_app.reads.open_terms` is the
# one answer to "which terms row is in force", read by the restaurant's own billing
# surface as well as by the three writers below. Bound by IDENTITY rather than
# re-implemented, so a writer that refuses a second open row and a reader that
# reports the current one cannot come to disagree about which row that is.
from commercial_app.reads import open_terms as _open_terms

# The model stores DecimalField(max_digits=12, decimal_places=2).
_AMOUNT_EXPONENT = Decimal('0.01')
_AMOUNT_CEILING = Decimal(10) ** 10          # 12 digits, two of them decimal
_CURRENCY_PATTERN = re.compile(r'^[A-Z]{3}$')
# PositiveIntegerField is a 32-bit `integer` on PostgreSQL. Without this bound a
# larger count reaches the INSERT and raises `DataError: integer out of range`,
# which would surface through a future adapter as a 500 rather than as a named
# refusal. The same reasoning as `restaurant_reads.MAX_PAGE`, which exists because
# an unbounded page number overflowed a bigint OFFSET.
_MAX_INTERVAL_COUNT = 2 ** 31 - 1


@dataclass(frozen=True)
class SubscriptionTermsResult:
    """
    What happened. ``previous_terms`` is populated only by a replacement.

    One result type for all three operations rather than three near-identical ones:
    a future adapter builds ONE audit event per HTTP call, and this carries exactly
    what such an event needs — which row is current, whether anything actually
    changed, and (for a replacement) which row it superseded.
    """

    terms: Optional[RestaurantSubscriptionTerms]
    changed: bool
    previous_terms: Optional[RestaurantSubscriptionTerms] = None


# --- input normalisation -----------------------------------------------------

def _normalise_amount(raw):
    """
    The recurring fee as a ``Decimal`` at the column's exact scale.

    FLOATS ARE REFUSED OUTRIGHT rather than converted. ``Decimal(0.1)`` is
    0.1000000000000000055511151231257827, and a price that arrives a fraction off
    what the operator typed is the kind of defect nobody notices until an invoice
    disagrees with a contract. Strings, ints and Decimals are accepted.

    A value carrying MORE precision than the column can hold is refused, not rounded:
    silently storing 1000.005 as 1000.01 changes a price the operator stated.
    """
    if isinstance(raw, float):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount must not be a float — pass a Decimal, int or string '
            'so the exact value is preserved.',
            {'field': 'recurring_amount'},
        )
    if isinstance(raw, bool) or raw is None:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount is required.',
            {'field': 'recurring_amount'},
        )
    try:
        amount = Decimal(raw) if not isinstance(raw, Decimal) else raw
    except (InvalidOperation, ValueError, TypeError):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount is not a valid decimal amount.',
            {'field': 'recurring_amount'},
        )

    if not amount.is_finite():
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount must be a finite amount.',
            {'field': 'recurring_amount'},
        )
    if amount < 0:
        # Zero is legitimate (a rehearsing test tenant, a free pilot, a waived
        # period); negative would mean Dinify paying the restaurant, which this row
        # cannot express.
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount must not be negative.',
            {'field': 'recurring_amount'},
        )
    # THE MAGNITUDE CHECK MUST COME FIRST. `Decimal('1e100').quantize(...)` raises
    # `InvalidOperation` — the result would exceed the context precision — so testing
    # the scale before the bound let a syntactically valid but oversized value escape
    # as a decimal exception instead of a domain refusal.
    if amount >= _AMOUNT_CEILING:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount exceeds the stored precision.',
            {'field': 'recurring_amount'},
        )
    try:
        quantized = amount.quantize(_AMOUNT_EXPONENT)
    except InvalidOperation:
        # Defensive: the bound above already excludes every value that can reach
        # this, but a future edit to the ceiling must not be able to reopen the hole.
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount cannot be represented at the stored scale.',
            {'field': 'recurring_amount'},
        )
    if amount != quantized:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'recurring_amount carries more precision than the stored scale of two '
            'decimal places; state the exact amount to be stored.',
            {'field': 'recurring_amount'},
        )
    # Quantized so a stored value and a freshly-normalised one compare identically at
    # the same scale, which is what the no-op and retry proofs below depend on.
    return quantized


def _normalise_currency(raw):
    """
    Canonical form: three uppercase ASCII letters.

    Trimming and upper-casing an otherwise legitimate alphabetic code is ordinary
    canonicalisation, not interpretation. There is deliberately NO hard-coded list of
    world currencies: this repository has no authoritative source for one, and a
    hand-maintained list would start wrong and drift. WHICH currencies Dinify supports
    is a policy question for whoever decides it — the database and this validator
    enforce SHAPE.
    """
    if not isinstance(raw, str):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'currency is required as a three-letter code.',
            {'field': 'currency'},
        )
    cleaned = raw.strip().upper()
    if not _CURRENCY_PATTERN.match(cleaned):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'currency must be a three-letter alphabetic code, e.g. UGX.',
            {'field': 'currency'},
        )
    return cleaned


def _normalise_interval(unit, count):
    """
    Generic recurrence: a unit from the closed vocabulary and a count of at least one.

    Matched EXACTLY — ``'MONTH'`` and ``'monthly'`` are refused rather than coerced.
    A currency code has a canonical case; this vocabulary has exactly four spellings
    and accepting near-misses would make the stored value ambiguous to read back.

    ``per_order`` is refused like any other unknown word: Dinify's revenue model is a
    recurring software subscription, and that unit would restate the
    per-order-commission framing the non-custodial posture exists to keep out.
    """
    if unit not in BILLING_INTERVAL_UNIT_VALUES:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'billing_interval_unit must be one of: '
            f'{", ".join(BILLING_INTERVAL_UNIT_VALUES)}.',
            {'field': 'billing_interval_unit',
             'value': unit if isinstance(unit, str) else None},
        )
    # `bool` is a subclass of `int`; True would otherwise sail through as 1.
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'billing_interval_count must be a whole number of at least 1.',
            {'field': 'billing_interval_count'},
        )
    if count > _MAX_INTERVAL_COUNT:
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            'billing_interval_count exceeds the largest storable value.',
            {'field': 'billing_interval_count'},
        )
    return unit, count


def _normalise_moment(raw, *, field):
    """An aware datetime. Naive input is refused rather than assumed to be UTC."""
    if not isinstance(raw, datetime):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            f'{field} is required as a datetime.',
            {'field': field},
        )
    if timezone.is_naive(raw):
        raise CommercialMutationError(
            errors.INVALID_SUBSCRIPTION_TERMS,
            f'{field} must be timezone-aware.',
            {'field': field},
        )
    return raw


def _normalise_terms_input(*, recurring_amount, currency, billing_interval_unit,
                           billing_interval_count, effective_from):
    """
    Shape validation shared by record and replace. NO CLOCK, no database.

    The clock-dependent rules (nothing in the future; a replacement cannot precede
    the terms it replaces) run later, inside the locked transaction, against a single
    ``now`` captured at the authoritative moment.
    """
    unit, count = _normalise_interval(billing_interval_unit, billing_interval_count)
    return {
        'recurring_amount': _normalise_amount(recurring_amount),
        'currency': _normalise_currency(currency),
        'billing_interval_unit': unit,
        'billing_interval_count': count,
        'effective_from': _normalise_moment(effective_from, field='effective_from'),
    }


# --- comparison helpers ------------------------------------------------------

def _commercial_tuple(terms):
    """
    The four facts that make terms the terms they are — deliberately EXCLUDING
    ``effective_from``.

    Replacing terms means changing what the restaurant pays; re-dating an unchanged
    price is a different (and out-of-scope) correction, so the two questions are asked
    with two different comparisons.
    """
    return (
        terms.recurring_amount,
        terms.currency,
        terms.billing_interval_unit,
        terms.billing_interval_count,
    )


def _requested_tuple(fields):
    return (
        fields['recurring_amount'],
        fields['currency'],
        fields['billing_interval_unit'],
        fields['billing_interval_count'],
    )


def _latest_end(restaurant):
    """
    The most recent ``ended_at`` across this restaurant's history, or ``None``.

    Used to keep the timeline MONOTONIC: terms recorded after an earlier set was
    closed may not begin before that closure. Without it, terms effective 1 July and
    ended 1 August could be followed by new terms effective 15 July, and "which terms
    were in force on 20 July?" would have two answers — the exact ambiguity the
    continuous boundary in ``replace_subscription_terms`` exists to prevent.
    """
    return (
        RestaurantSubscriptionTerms.objects
        .filter(restaurant=restaurant, ended_at__isnull=False)
        .order_by('-ended_at')
        .values_list('ended_at', flat=True)
        .first()
    )


def _refuse_future(moment, now, *, field):
    if moment > now:
        raise CommercialMutationError(
            errors.FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED,
            f'{field} may not be in the future: this domain records terms that '
            'are already in effect and does not schedule future changes.',
            {'field': field},
        )


# --- the writers -------------------------------------------------------------

def record_subscription_terms(*, restaurant_id, recurring_amount, currency,
                              billing_interval_unit, billing_interval_count,
                              effective_from, actor):
    """
    Record a restaurant's FIRST (or current, on an exact retry) subscription terms.

    THIS FUNCTION NEVER REPLACES ANYTHING. If terms are already open and differ in any
    canonical fact, it refuses with ``subscription_terms_already_open`` rather than
    quietly superseding them — an accidental "create" must not be able to rewrite
    commercial history. Replacing is a separate, deliberate operation that names the
    row it expects to supersede.

    An EXACT retry — every canonical fact and ``effective_from`` already equal to the
    open row — is a successful no-op returning that row unchanged, so a lost response
    followed by a resend does not become a conflict. The no-op preserves ``id``,
    ``recorded_at`` and ``recorded_by``: the original recording keeps naming who
    recorded it.
    """
    fields = _normalise_terms_input(
        recurring_amount=recurring_amount,
        currency=currency,
        billing_interval_unit=billing_interval_unit,
        billing_interval_count=billing_interval_count,
        effective_from=effective_from,
    )

    with transaction.atomic():
        restaurant = lock_restaurant(restaurant_id)
        actor_user = resolve_actor(actor)
        now = timezone.now()
        _refuse_future(fields['effective_from'], now, field='effective_from')

        open_terms = _open_terms(restaurant)

        if open_terms is not None:
            same = (
                _commercial_tuple(open_terms) == _requested_tuple(fields)
                and open_terms.effective_from == fields['effective_from']
            )
            if same:
                return SubscriptionTermsResult(terms=open_terms, changed=False)
            raise CommercialMutationError(
                errors.SUBSCRIPTION_TERMS_ALREADY_OPEN,
                'This restaurant already has open subscription terms. Replace them '
                'deliberately rather than recording new ones.',
                {'restaurant_id': str(restaurant.pk),
                 'open_terms_id': str(open_terms.pk)},
            )

        # NOTHING IS OPEN — but history may not be empty (a previous set was ended),
        # and a new set must not begin before that closure or the two windows
        # overlap. Checked here rather than left to the caller: the writer is the
        # only thing that sees the whole timeline.
        latest_end = _latest_end(restaurant)
        if latest_end is not None and fields['effective_from'] < latest_end:
            raise CommercialMutationError(
                errors.INVALID_SUBSCRIPTION_TERMS,
                'These terms would begin before the previous terms ended, leaving '
                'two overlapping sets in force.',
                {'field': 'effective_from'},
            )

        terms = RestaurantSubscriptionTerms.objects.create(
            restaurant=restaurant,
            recorded_by=actor_user,
            recorded_at=now,
            **fields,
        )
        return SubscriptionTermsResult(terms=terms, changed=True)


def replace_subscription_terms(*, restaurant_id, expected_terms_id, recurring_amount,
                               currency, billing_interval_unit,
                               billing_interval_count, effective_from, actor):
    """
    Supersede the open terms with new ones. ONE atomic operation: close, then insert.

    The boundary is CONTINUOUS — the outgoing row's ``ended_at`` is set to exactly the
    replacement's ``effective_from``, so the history has no gap in which the
    restaurant had no terms and no overlap in which it had two. That is only
    expressible because the two writes share a transaction; a caller doing it in two
    calls could be interrupted between them and leave the tenant unconfigured.

    ``expected_terms_id`` names the row the caller believes is current, so a stale
    screen cannot supersede terms somebody else already replaced.

    THE EXACT-RETRY PROOF. A retry after a successful replacement arrives naming the
    OLD row while the open row is now the NEW one. That is treated as a no-op only
    when the previously-completed replacement can be identified precisely: the named
    row belongs to this restaurant, it is ended, an open row exists, its canonical
    facts and ``effective_from`` equal the request, and the old row's ``ended_at``
    equals that same instant. Anything weaker — "some open row happens to have this
    amount" — would let a genuinely stale caller believe their change landed when it
    was somebody else's.
    """
    fields = _normalise_terms_input(
        recurring_amount=recurring_amount,
        currency=currency,
        billing_interval_unit=billing_interval_unit,
        billing_interval_count=billing_interval_count,
        effective_from=effective_from,
    )
    expected_uuid = parse_uuid(
        expected_terms_id,
        code=errors.INVALID_SUBSCRIPTION_TERMS,
        message='expected_terms_id must be a subscription-terms UUID.',
    )

    with transaction.atomic():
        restaurant = lock_restaurant(restaurant_id)
        actor_user = resolve_actor(actor)
        now = timezone.now()
        _refuse_future(fields['effective_from'], now, field='effective_from')

        expected = (
            RestaurantSubscriptionTerms.objects
            .filter(pk=expected_uuid, restaurant=restaurant)
            .first()
        )
        if expected is None:
            # Includes a row belonging to a different restaurant: from this tenant's
            # perspective there is no such terms record, and saying so discloses
            # nothing about the other tenant.
            raise CommercialMutationError(
                errors.SUBSCRIPTION_TERMS_NOT_FOUND,
                'No such subscription terms for this restaurant.',
                {'restaurant_id': str(restaurant.pk),
                 'expected_terms_id': str(expected_uuid)},
            )

        open_terms = _open_terms(restaurant)
        if open_terms is None:
            raise CommercialMutationError(
                errors.NO_OPEN_SUBSCRIPTION_TERMS,
                'This restaurant has no open subscription terms to replace.',
                {'restaurant_id': str(restaurant.pk)},
            )

        if open_terms.pk != expected.pk:
            already_done = (
                expected.ended_at is not None
                and expected.ended_at == fields['effective_from']
                and open_terms.effective_from == fields['effective_from']
                and _commercial_tuple(open_terms) == _requested_tuple(fields)
            )
            if already_done:
                return SubscriptionTermsResult(
                    terms=open_terms, changed=False, previous_terms=expected,
                )
            raise CommercialMutationError(
                errors.STALE_SUBSCRIPTION_TERMS,
                'These are no longer the current subscription terms. Reload and '
                'try again.',
                {'restaurant_id': str(restaurant.pk),
                 'expected_terms_id': str(expected.pk),
                 'open_terms_id': str(open_terms.pk)},
            )

        # The named row IS the open one. If the commercial facts are unchanged there
        # is nothing to supersede: writing a historical row purely to re-date
        # unchanged terms would fabricate a change that never happened. Re-dating an
        # already-current record is a separate correction problem, out of scope.
        if _commercial_tuple(open_terms) == _requested_tuple(fields):
            return SubscriptionTermsResult(terms=open_terms, changed=False)

        if fields['effective_from'] < open_terms.effective_from:
            raise CommercialMutationError(
                errors.INVALID_SUBSCRIPTION_TERMS,
                'Replacement terms cannot take effect before the terms they '
                'replace.',
                {'field': 'effective_from',
                 'open_terms_id': str(open_terms.pk)},
            )

        open_terms.ended_at = fields['effective_from']
        open_terms.save(update_fields=['ended_at'])

        replacement = RestaurantSubscriptionTerms.objects.create(
            restaurant=restaurant,
            recorded_by=actor_user,
            recorded_at=now,
            **fields,
        )
        return SubscriptionTermsResult(
            terms=replacement, changed=True, previous_terms=open_terms,
        )


def end_subscription_terms(*, restaurant_id, expected_terms_id, ended_at):
    """
    Close the open terms, leaving the restaurant with none.

    NO ACTOR PARAMETER, deliberately: Step 3B did not add an ``ended_by`` column, and
    adding schema purely to duplicate what a future audit entry will record would
    create a second place for the same fact to live. The control-plane adapter carries
    the actor, the reason and the audit row.

    Afterwards the restaurant has NO open terms, which a future readiness rule will
    truthfully read as commercially unconfigured. Nothing is auto-created to fill the
    gap — deciding the next terms is a separate decision somebody has to make.
    """
    ended_at = _normalise_moment(ended_at, field='ended_at')
    expected_uuid = parse_uuid(
        expected_terms_id,
        code=errors.INVALID_SUBSCRIPTION_TERMS,
        message='expected_terms_id must be a subscription-terms UUID.',
    )

    with transaction.atomic():
        restaurant = lock_restaurant(restaurant_id)
        now = timezone.now()
        _refuse_future(ended_at, now, field='ended_at')

        expected = (
            RestaurantSubscriptionTerms.objects
            .filter(pk=expected_uuid, restaurant=restaurant)
            .first()
        )
        if expected is None:
            raise CommercialMutationError(
                errors.SUBSCRIPTION_TERMS_NOT_FOUND,
                'No such subscription terms for this restaurant.',
                {'restaurant_id': str(restaurant.pk),
                 'expected_terms_id': str(expected_uuid)},
            )

        open_terms = _open_terms(restaurant)

        # THE RETRY CHECK COMES BEFORE THE OPEN-ROW REQUIREMENT, and must: after a
        # successful end there is no open row at all, so asking "is this the open
        # row?" would refuse a resend of the request that just succeeded.
        #
        # BUT IT IS CONDITIONAL ON NOTHING HAVING OPENED SINCE. If another operator
        # recorded fresh terms after the end, replying "already done" would report
        # success for an operation whose stated postcondition — this restaurant now
        # has no open terms — is no longer true, and would slip past the
        # `expected_terms_id` guard entirely. That is a stale view of the world, so
        # it is reported as one.
        if expected.ended_at is not None and expected.ended_at == ended_at:
            if open_terms is None:
                return SubscriptionTermsResult(terms=expected, changed=False)
            raise CommercialMutationError(
                errors.STALE_SUBSCRIPTION_TERMS,
                'These terms were already ended and new terms have since been '
                'opened. Reload and try again.',
                {'restaurant_id': str(restaurant.pk),
                 'expected_terms_id': str(expected.pk),
                 'open_terms_id': str(open_terms.pk)},
            )

        if open_terms is None:
            raise CommercialMutationError(
                errors.NO_OPEN_SUBSCRIPTION_TERMS,
                'This restaurant has no open subscription terms to end.',
                {'restaurant_id': str(restaurant.pk)},
            )
        if open_terms.pk != expected.pk:
            raise CommercialMutationError(
                errors.STALE_SUBSCRIPTION_TERMS,
                'These are no longer the current subscription terms. Reload and '
                'try again.',
                {'restaurant_id': str(restaurant.pk),
                 'expected_terms_id': str(expected.pk),
                 'open_terms_id': str(open_terms.pk)},
            )
        if ended_at < open_terms.effective_from:
            raise CommercialMutationError(
                errors.INVALID_SUBSCRIPTION_TERMS,
                'Terms cannot end before they took effect.',
                {'field': 'ended_at', 'terms_id': str(open_terms.pk)},
            )

        open_terms.ended_at = ended_at
        # ONLY the terminal stamp. Every commercial fact on the row is immutable.
        open_terms.save(update_fields=['ended_at'])
        return SubscriptionTermsResult(terms=open_terms, changed=True)
