"""
May this caller place or submit an order against this restaurant RIGHT NOW?

ONE primitive, consulted by BOTH stages of order creation. Order creation is
two-stage — ``initiate`` writes an ``initiated`` draft and ``submit`` moves it to
``pending`` — and before this module only the first stage consulted the
restaurant's lifecycle state at all. A diner could initiate while the restaurant
was ``live``, an administrator could suspend it, and the diner could then submit:
the order reached the kitchen after trading was supposed to have stopped. Whether
the data model calls that "creation" is beside the point; commercially the
restaurant accepted an order after suspension.

Two things make an admission decision trustworthy, and this module owns both:

1. ONE SET OF RULES. The decision is derived from the lifecycle policy
   predicates, never restated. An anonymous diner needs ``allows_diner_ordering``
   (the launch boundary); a staff caller needs ``allows_order_creation``, which
   stays True at ``onboarding`` so the owner can place the end-to-end rehearsal
   order the Phase-1 checklist requires. ``suspended`` and ``offboarded`` are
   False in BOTH predicates, so "no create, no submit, by any caller" falls out
   of the matrix rather than being a third rule someone has to remember.

2. ONE MOMENT. The status is re-read HERE, under an advisory lock, inside the
   caller's transaction — not taken from an instance loaded earlier in
   autocommit. ``initiate_order`` used to load the restaurant with a plain
   ``.get()`` and gate on it, while the transaction that actually writes the
   order opened later and locked only the ``Table``. The two shared no lock, so
   a transition committing in between was invisible: the order was admitted, and
   ``is_test`` derived, against a state that no longer existed. The window spanned
   the BLOCKING table-lock acquisition — widest exactly when contention is highest.

THE LOCK ITSELF LIVES ELSEWHERE. ``restaurants_app.controllers.admission_lock``
owns the advisory-lock primitive, because the thing being locked is a RESTAURANT
and the lifecycle transition on the other side of it is a ``restaurants_app``
concern. That module carries the rationale for choosing an advisory lock over a
``Restaurant`` row lock, the ``_xact_`` requirement, and the full lock-ordering
table. This module owns only the RULE and the moment it is applied.
"""
import logging
from dataclasses import dataclass, replace
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction

from dinify_backend.configss.messages import MESSAGES
from restaurants_app.controllers.admission_lock import lock_admission_shared
from restaurants_app.controllers.lifecycle_policy import (
    allows_diner_ordering,
    allows_order_creation,
)

logger = logging.getLogger(__name__)

# Which stage is asking. This does NOT change the decision — both stages ask the
# same question of the same predicates — it only distinguishes the two in the
# refusal code, so a log line says which half of the funnel turned an order away.
STAGE_CREATE = 'create'
STAGE_SUBMIT = 'submit'


@dataclass(frozen=True)
class AdmissionVerdict:
    """
    The answer, plus the state it was decided from.

    ``status`` is the value read under the lock. Callers that need to derive
    anything else from lifecycle state — ``Order.is_test`` above all — must use
    THIS value rather than re-reading or reusing an earlier instance, or they
    reintroduce the drift the lock was taken to prevent.

    ``restaurant_is_test`` is the tenant-level test flag, read in the SAME locked
    query as ``status`` and carried here for exactly the same reason. It is not an
    admission input — a test restaurant admits orders on precisely the rules a real
    one does — but it IS an input to the classification that follows, and taking it
    from ``restaurant.is_test`` on an instance loaded before the transaction would
    reintroduce the stale-read bug this module exists to close, on a different
    field. One protected moment, both values.

    It defaults to ``False`` so a verdict built by ``evaluate`` alone — the pure
    preflight, which has no database and therefore no authority over this — cannot
    be mistaken for a statement about the tenant. Only ``admit`` sets it.

    ``restaurant_accepting_orders`` and ``restaurant_deleted`` (D06) ride the
    SAME protected read, for the same reason and to answer a different
    question. They are OPERATIONAL facts an order path acts on, an operator can
    change either at any moment, and reading them off an instance loaded before
    the transaction is exactly the stale read this module exists to close — on
    two more fields. ONE query, ONE protected instant, four values: splitting
    them would put a window between them that the lock does not cover.

    They are NOT admission inputs. Whether a restaurant is paused says nothing
    about its lifecycle, and ``evaluate`` neither sees nor uses them — they are
    carried so that ``order_eligibility``, which owns that rule, can be asked
    about the same instant the lifecycle was decided at.

    **THEIR DEFAULTS FAIL CLOSED, in the opposite direction from
    ``restaurant_is_test``.** A verdict that never touched the database must not
    be able to claim a restaurant is open and present, so an accidental
    preflight verdict refuses every order loudly rather than admitting one
    quietly.
    """
    allowed: bool
    status: Optional[str]
    message: str = ''
    code: str = ''
    restaurant_is_test: bool = False
    restaurant_accepting_orders: bool = False
    restaurant_deleted: bool = True


def evaluate(status, created_by, stage: str) -> AdmissionVerdict:
    """
    THE RULE. Pure: a lifecycle state and a caller in, a verdict out.

    No database, no lock, no clock. Both the preflight and the authoritative check
    call this, which is what stops them from ever disagreeing — they differ only in
    WHICH status they hand it (a possibly-stale instance vs one re-read under the
    lock), never in what the rule says.

    ``created_by`` is tested for PRESENCE, never identity: the question is "was
    this order placed by staff, or by the anonymous public?", and the answer is
    the same whether the caller passes a ``User``, a user id or ``None``. That is
    what lets the submit path pass ``order.created_by_id`` — the ORDER's creator,
    which is the identity that decides the rule — without fetching the row.
    """
    if created_by is None:
        permitted = allows_diner_ordering(status)
        # Two refusals, and the difference is meaningful to the person reading it.
        # A restaurant that can take orders but is not trading yet is not open
        # YET — the owner is mid-onboarding and the QR is already on the table. A
        # restaurant that cannot take orders at all is suspended or gone, and
        # saying "check back soon" there would be a guess we are in no position
        # to make. This reproduces exactly what the two gates it replaces said,
        # including which one answered first for a suspended restaurant.
        message = (
            MESSAGES.get('NOT_OPEN_YET') if allows_order_creation(status)
            else MESSAGES.get('BLOCKED_RESTAURANT')
        )
        code = f'{stage}_diner_not_permitted'
    else:
        permitted = allows_order_creation(status)
        message = MESSAGES.get('BLOCKED_RESTAURANT')
        code = f'{stage}_orders_not_permitted'

    if permitted:
        return AdmissionVerdict(allowed=True, status=status)

    return AdmissionVerdict(
        allowed=False, status=status, message=message, code=code,
    )


def admit(*, restaurant_id, created_by, stage: str) -> AdmissionVerdict:
    """
    THE AUTHORITATIVE CHECK: evaluate the rule against state re-read under the lock.

    MUST be called inside a transaction — it takes the shared advisory lock and
    re-reads ``Restaurant.status`` itself, and a transaction-scoped lock taken in
    autocommit is released by the very statement that took it, leaving the re-read
    unprotected and the whole exercise pointless. Asserted rather than assumed:
    that failure is silent, and a silently ineffective lock is worse than none,
    because the tests would still pass.

    This is the check that decides. ``evaluate`` may also be called directly,
    without a lock, for fast feedback BEFORE the transaction opens — the same
    preflight/authoritative split ``validate_order_selections`` already uses on
    this path — but a preflight verdict must never be the last word: the state it
    read can change while the request waits for a lock.

    ``created_by`` is the caller identity the order paths already carry: ``None``
    for an anonymous diner, a ``User`` for a staff/admin order.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'order_admission.admit() must run inside a transaction — a '
            'transaction-scoped advisory lock taken in autocommit is released '
            'immediately and protects nothing.'
        )

    # Imported lazily, matching the other cross-app references on this path
    # (_create_order does the same for Table and ConOrder), so the module stays
    # off the import graph of anything that only wants the lock helpers.
    from restaurants_app.models import Restaurant

    lock_admission_shared(restaurant_id)

    try:
        # ONE query, so both values describe the same protected instant. Splitting
        # them into two reads would put a window between them that the lock does not
        # close, which is the whole failure this function exists to prevent.
        (
            status,
            restaurant_is_test,
            restaurant_accepting_orders,
            restaurant_deleted,
        ) = (
            Restaurant.objects
            .values_list(
                'status', 'is_test', 'accepting_orders', 'deleted')
            .get(pk=restaurant_id)
        )
    except (Restaurant.DoesNotExist, ValidationError, ValueError, TypeError):
        # The row was resolved by the caller moments ago, so this is close to
        # unreachable — but an admission that cannot read the state it depends on
        # denies, like every other unknown in the lifecycle policy.
        return AdmissionVerdict(
            allowed=False,
            status=None,
            message=MESSAGES.get('RESTAURANT_NOT_FOUND'),
            code=f'{stage}_restaurant_not_found',
        )

    verdict = evaluate(status, created_by, stage)
    # `evaluate` is pure and knows nothing about the tenant flag or the
    # operational facts; the authoritative read attaches them. Rebuilt rather than mutated — the dataclass is frozen, which
    # is what stops a later caller from "correcting" a verdict after the fact.
    return replace(
        verdict,
        restaurant_is_test=bool(restaurant_is_test),
        restaurant_accepting_orders=bool(restaurant_accepting_orders),
        restaurant_deleted=bool(restaurant_deleted),
    )
