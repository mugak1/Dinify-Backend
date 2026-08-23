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
from commercial_app.mutation_context import lock_restaurant, resolve_actor


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
              code, label):
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

    with transaction.atomic():
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
    """
    return _set_axis(
        restaurant_id=restaurant_id,
        value=value,
        actor=actor,
        expected_current=expected_current,
        field='payment_timing',
        allowed=PAYMENT_TIMING_VALUES,
        code=errors.INVALID_PAYMENT_TIMING,
        label='payment timing',
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
