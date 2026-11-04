"""
The ONLY supported writers for ``RestaurantServiceConfiguration`` (Step 3C).

Two named operations, one per axis:

    set_payment_timing(...)           pay_first | pay_after
    set_payment_collection_mode(...)  offline   | psp_online

THE AXES STAY SEPARATE, and so do their writers. A generic
``update_commercial_profile(**kwargs)`` would hide two decisions with different
meanings, different attribution and — plausibly — different future write authority
behind one call, and would make "what exactly did this operator change?" unanswerable
from the call site. The shared machinery below is private precisely so the public
surface stays two named operations.

━━ OPTIMISTIC CONCURRENCY: ``expected_current`` ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Row locking makes concurrent writes queue; it does not stop the SECOND operator
overwriting the first, because by the time they got the lock their screen was already
stale. ``expected_current`` closes that: the caller states what it believed the
current value to be, and the service compares against the value read UNDER THE LOCK.

It has NO DEFAULT. ``None`` is a meaningful assertion here — "I believe nobody has
configured this yet" — so a default would let a caller who simply forgot the argument
make that claim by accident, which is the one claim that succeeds against a fresh
restaurant.

━━ SAME-STATE RETRY IS A NO-OP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the stored value already equals the requested one, the call succeeds having written
NOTHING — not even ``set_at`` / ``set_by`` — **even when ``expected_current`` is
stale**. That ordering is the whole point: a lost HTTP response followed by an
identical retry must not become a conflict the operator has to reason about, and must
not rewrite the attribution of a decision somebody else already made. The stored
attribution keeps naming who actually decided, and when.

━━ PAYMENT TIMING TAKES THE ADMISSION LOCK FIRST (Step 2, PR BE-B) ━━━━━━━━━━━━━━━━

From Step 2 the timing copied onto a visit at acceptance decides whether a round is
held, so a timing change must not commit in the middle of an acceptance. The order
paths read the timing after ``order_admission.admit`` has taken the SHARED admission
lock (``round_placement._cutoff_and_timing``); the timing writer therefore takes the
EXCLUSIVE side, FIRST, before the ``Restaurant`` row lock:

    payment timing        advisory(restaurant) EXCLUSIVE -> Restaurant
                          -> RestaurantServiceConfiguration

That is the lifecycle transition's own order, so it joins an ordering already proven
acyclic. A row lock alone would not do: the order path reads the timing with a plain
statement, and under MVCC that read does not block on a row held ``FOR UPDATE``.

THE COLLECTION-MODE AXIS TAKES NO ADMISSION LOCK, deliberately. Nothing in the order
path reads it, at admission or anywhere else, so the barrier would only queue diner
orders behind a decision no order depends on. It stays ``Restaurant -> ...``.

━━ ``pay_first`` IS REFUSED UNTIL STEP 4 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Under Step 2's release rule a pay-first round leaves the held state only through a
recorded payment or a manager release, and neither exists before Step 4. So the timing
writer refuses ``pay_first`` with ``PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE``, before any
lock and with no state change. A restaurant whose stored value is ALREADY ``pay_first``
keeps it (its rounds release at once in Step 2) and may move off it to ``pay_after``.
The refusal applies to the requested VALUE only: ``expected_current`` may still name
``pay_first``, which is how that move is made. An exact retry of ``pay_first`` against
a restaurant already on it is refused too, rather than reported as a no-op: nothing
changes either way, and one answer for one value is simpler to reason about.
**Step 4 removes the refusal.**
"""
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from commercial_app import errors
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    PAYMENT_COLLECTION_MODE_VALUES,
    PAYMENT_TIMING_VALUES,
    RestaurantServiceConfiguration,
)
from commercial_app.models import PAYMENT_TIMING_PAY_FIRST
from commercial_app.mutation_context import (
    lock_restaurant, parse_uuid, resolve_actor,
)
from restaurants_app.controllers.admission_lock import lock_admission_exclusive

#: The stable refusal for ``pay_first`` until Step 4 (see the module docstring).
#: Defined here rather than in ``errors`` because it is a TEMPORARY rule of this one
#: writer, and Step 4 deletes it together with the refusal.
PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE = 'payment_timing_pay_first_unavailable'
PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE_MESSAGE = 'Pay first is not available yet.'


@dataclass(frozen=True)
class ServiceConfigurationResult:
    """
    What happened, stated by the operation rather than inferred afterwards.

    ``changed`` distinguishes a real write from a no-op retry, so a future adapter
    can decide whether the operation is worth an audit entry without re-reading the
    database and guessing. ``previous_value`` / ``current_value`` are the machine
    values that entry would carry.

    Frozen, and carrying no HTTP status: a domain result is a statement about the
    domain, and baking a status code in would make it usable by exactly one adapter.
    """

    configuration: RestaurantServiceConfiguration
    changed: bool
    previous_value: Optional[str]
    current_value: Optional[str]


def _validate_axis_value(value, *, allowed, code, label):
    """The requested value must be in the closed vocabulary. Never ``None``."""
    if value not in allowed:
        raise CommercialMutationError(
            code,
            f'{label} must be one of: {", ".join(sorted(allowed))}.',
            {'value': value if isinstance(value, str) else None},
        )
    return value


def _validate_expected(expected, *, allowed, code, label):
    """
    ``expected_current`` may be ``None`` (meaning "not configured") or a real value.

    Validated too, so a caller passing a typo is told their ASSERTION is malformed
    rather than being handed a stale-configuration conflict that can never resolve.
    """
    if expected is None or expected in allowed:
        return expected
    raise CommercialMutationError(
        code,
        f'expected_current for {label} must be null or one of: '
        f'{", ".join(sorted(allowed))}.',
        {'expected_current': expected if isinstance(expected, str) else None},
    )


def _set_axis(*, restaurant_id, value, actor, expected_current, field, allowed,
              code, label, admission_barrier=False):
    """
    The shared body of both public writers. See the module docstring for the rules.

    PRIVATE ON PURPOSE. It takes a field name, which is exactly the shape that must
    not be exposed: a caller able to name the field could write either axis through
    one entry point, and the two named public functions would stop being the record
    of which decision was made.
    """
    # Cheap, request-shaped validation FIRST, against no database state: a bad value
    # or a malformed assertion needs no row lock to refuse. Mirrors how
    # `lifecycle.transition_restaurant` validates its target and reason before
    # reaching for a lock.
    value = _validate_axis_value(value, allowed=allowed, code=code, label=label)
    expected_current = _validate_expected(
        expected_current, allowed=allowed, code=code, label=label,
    )

    set_at_field = f'{field}_set_at'
    set_by_field = f'{field}_set_by'

    if admission_barrier:
        # Parsed BEFORE the lock, because the advisory key is derived from the UUID:
        # a malformed id must be the domain's own refusal, not a ValueError out of
        # `advisory_key`. `lock_restaurant` parses it again; that is the same rule.
        restaurant_id = parse_uuid(
            restaurant_id,
            code=errors.INVALID_RESTAURANT_ID,
            message='The target must be a restaurant UUID.',
        )

    with transaction.atomic():
        if admission_barrier:
            # FIRST, before the Restaurant row lock — the lifecycle transition's
            # order. Taking it after the row would invert that order and close a
            # cycle against the transition. See the module docstring.
            lock_admission_exclusive(restaurant_id)
        restaurant = lock_restaurant(restaurant_id)
        actor_user = resolve_actor(actor)

        configuration = (
            RestaurantServiceConfiguration.objects
            .filter(restaurant=restaurant)
            .first()
        )
        current = getattr(configuration, field) if configuration is not None else None

        # NO-OP FIRST, and deliberately before the staleness comparison — see the
        # module docstring. An exact retry of a request that already succeeded is
        # not a conflict, whatever the caller still believes about the old value.
        if current == value:
            return ServiceConfigurationResult(
                configuration=configuration,
                changed=False,
                previous_value=current,
                current_value=current,
            )

        if current != expected_current:
            raise CommercialMutationError(
                errors.STALE_SERVICE_CONFIGURATION,
                f'The stored {label} is no longer the value you were working from. '
                'Reload and try again.',
                {
                    'restaurant_id': str(restaurant.pk),
                    'field': field,
                    'expected_current': expected_current,
                    'actual_current': current,
                },
            )

        now = timezone.now()
        if configuration is None:
            # Populate ONLY this axis's triple. The other axis stays NULL — absence
            # is the honest "nobody has decided" state, and defaulting it here would
            # manufacture a commercial decision as a side effect of an unrelated one.
            configuration = RestaurantServiceConfiguration(restaurant=restaurant)
            setattr(configuration, field, value)
            setattr(configuration, set_at_field, now)
            setattr(configuration, set_by_field, actor_user)
            configuration.save()
        else:
            setattr(configuration, field, value)
            setattr(configuration, set_at_field, now)
            setattr(configuration, set_by_field, actor_user)
            # `update_fields` is the precise expression of "only this triple moved":
            # a full save would rewrite the other axis's columns with identical
            # values, which is harmless today and misleading to anyone reading the
            # SQL. `updated_at` is `auto_now`, so it must be named to be written.
            configuration.save(
                update_fields=[field, set_at_field, set_by_field, 'updated_at'],
            )

        return ServiceConfigurationResult(
            configuration=configuration,
            changed=True,
            previous_value=current,
            current_value=value,
        )


def set_payment_timing(*, restaurant_id, value, actor, expected_current):
    """
    Record this restaurant's PAYMENT TIMING — a service-model fact.

    ``pay_first``: settlement must be recorded before the kitchen may fire the order
    (counter café, QSR, nightlife). ``pay_after``: the order fires immediately and the
    tab settles at the end (full-service dining).

    It says NOTHING about custody, tender or provider, and this writer touches nothing
    else: not the collection mode, not its attribution, not subscription terms, not
    the ``Restaurant`` row, not lifecycle, and not the legacy
    ``require_order_prepayments`` / ``Table.prepayment_required`` flags — which are not
    canonical payment timing and are never read to choose a default.

    UNTIL STEP 4 ``pay_first`` IS REFUSED (``PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE``),
    before any lock is taken. The write takes the EXCLUSIVE admission lock before the
    ``Restaurant`` row. Both rules are explained in the module docstring.
    """
    if value == PAYMENT_TIMING_PAY_FIRST:
        raise CommercialMutationError(
            PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE,
            PAYMENT_TIMING_PAY_FIRST_UNAVAILABLE_MESSAGE,
            {'value': value},
        )
    return _set_axis(
        restaurant_id=restaurant_id,
        value=value,
        actor=actor,
        expected_current=expected_current,
        field='payment_timing',
        allowed=PAYMENT_TIMING_VALUES,
        code=errors.INVALID_PAYMENT_TIMING,
        label='payment timing',
        admission_barrier=True,
    )


def set_payment_collection_mode(*, restaurant_id, value, actor, expected_current):
    """
    Record this restaurant's PAYMENT COLLECTION MODE — a custody fact.

    ``offline``: Dinify does not initiate the diner payment; the restaurant collects
    it itself. ``psp_online``: Dinify initiates through a licensed provider on the
    restaurant's behalf, with the restaurant as merchant of record.

    ``offline`` IS AN ORDINARY CONFIGURED VALUE — not false, not a fallback, not a
    bypass, and emphatically not "unconfigured", which is ``NULL``.

    ``psp_online`` is recordable TODAY even though no PSP integration exists: the
    value is a commercial decision, and whether the merchant side is ready is a
    separate question for a future readiness rule. This writer therefore infers no
    provider, creates no merchant state and calls no provider API.
    """
    return _set_axis(
        restaurant_id=restaurant_id,
        value=value,
        actor=actor,
        expected_current=expected_current,
        field='payment_collection_mode',
        allowed=PAYMENT_COLLECTION_MODE_VALUES,
        code=errors.INVALID_PAYMENT_COLLECTION_MODE,
        label='payment collection mode',
    )
