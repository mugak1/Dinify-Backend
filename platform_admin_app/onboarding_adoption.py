"""
LEGACY ADOPTION — the audited writer that brings ONE already-existing canonical
``Restaurant`` into the Admin onboarding domain.

Step 2A gave the domain its schema and its owner-consistency invariant and, on
purpose, nothing that writes: no service, no command, no backfill, so a restaurant
with no ``RestaurantOnboarding`` row means exactly "not yet represented in Admin".
This module is the first thing that changes that, for the one provenance a
pre-existing tenant can honestly carry:

    an existing canonical Restaurant
              |
    an explicit, attributed platform-admin decision
              |
    RestaurantOnboarding(source='legacy_adopted', adopted_at=..., adopted_by=...)

WHAT ADOPTION MEANS, AND THE THREE THINGS IT DOES NOT MEAN. It means, and means
only: *this pre-existing canonical Restaurant is now represented in the Admin
onboarding domain*. It does NOT mean the restaurant was created by Dinify (that is
the other provenance, and this module can never write it). It does NOT mean anyone
has vouched for the owner's control — the attestation triple is left NULL, because
adoption and attestation answer different questions and only one of them is being
answered here. And it does NOT mean the owner was ever invited: a legacy tenant did
not enter Dinify through the invitation system, and manufacturing an
``OwnerInvitation`` to make the record look tidy would fabricate exactly the
evidence ``legacy_adopted`` exists to avoid claiming.

NOTHING OPERATIONAL IS COPIED OR CHANGED. Baba House already owns its menu, tables,
QR state, orders, employees and lifecycle, and Admin manages that canonical
``Restaurant`` regardless of how it entered Dinify. This service inserts ONE row and
ONE audit entry. It never calls ``restaurant.save()``, never touches lifecycle state
or the test classification, and never repairs ownership.

REPAIR IS NOT ADOPTION'S JOB. A NEW adoption requires the owner of record and the
owner authority to already agree (``assert_owner_consistency``); when they do not,
this refuses and says which way. Quietly creating a missing owner membership, or
picking one of two live owners, would be a decision about who runs a business —
made silently, by a provenance writer, with no reasoning attached to it.

THE SERVICE IS THE UNIT, THE COMMAND IS AN ADAPTER. The Admin HTTP endpoint that
eventually offers this will call this same function, so every rule lives here and
is enforced here — including actor eligibility, which the management command also
checks. A service that trusted its adapter to have validated the actor would be
correct exactly until the second adapter arrived.
"""
import uuid
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import audit
from platform_admin_app.audit_actions import ADMIN_RESTAURANT_ONBOARDING_ADOPTED
from platform_admin_app.models import (
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    RESULT_SUCCESS,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import assert_owner_consistency
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import Restaurant
from users_app.models import User

# --- outcomes ----------------------------------------------------------------
#
# The caller must be able to tell "I adopted this" from "somebody already had"
# WITHOUT re-reading the database and inferring it from state — by then the two are
# indistinguishable, which is precisely how a runbook that was pasted twice gets
# reported as two adoptions.
OUTCOME_ADOPTED = 'adopted'
OUTCOME_ALREADY_ADOPTED = 'already_adopted'

# --- error codes -------------------------------------------------------------
#
# Distinct because each needs a different human response: fix the identifier, pick a
# real tenant, fix the invocation, resolve a provenance conflict that a machine must
# not resolve on its own.
INVALID_RESTAURANT_ID = 'invalid_restaurant_id'
RESTAURANT_NOT_FOUND = 'restaurant_not_found'
INVALID_ACTOR = 'invalid_actor'
INVALID_REASON = 'invalid_reason'
ONBOARDING_SOURCE_CONFLICT = 'onboarding_source_conflict'

ADOPTION_ERROR_CODES = frozenset({
    INVALID_RESTAURANT_ID,
    RESTAURANT_NOT_FOUND,
    INVALID_ACTOR,
    INVALID_REASON,
    ONBOARDING_SOURCE_CONFLICT,
})


class AdoptionError(Exception):
    """
    Adoption was refused. ``code`` is one of ``ADOPTION_ERROR_CODES``.

    A NARROW, NAMED failure rather than whatever the ORM would have raised.
    ``IntegrityError``, ``DoesNotExist`` and ``MultipleObjectsReturned`` all mean
    something specific here — a provenance conflict, a mistyped target — and letting
    them escape would hand an operator a stack trace where a sentence belongs, while
    giving a future endpoint nothing to branch on but exception classes that mean
    different things in different places.

    Mirrors ``OwnerConsistencyError``'s shape deliberately: a short machine-readable
    ``code`` plus ``details`` carrying UUIDs ONLY. These messages reach logs and
    operator-facing surfaces, and an owner's email or phone has no business in
    either — an identifier is enough to investigate with.

    Owner-consistency failures are NOT flattened into this type. They keep their own
    ``OwnerConsistencyError`` and their own three codes, because "this restaurant's
    ownership is ambiguous" is a different problem from "this adoption was invalid",
    needs a different person to fix it, and already has a canonical vocabulary.
    """

    def __init__(self, code, message='', details=None):
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class AdoptionResult:
    """
    What happened, stated by the operation rather than inferred afterwards.

    ``outcome`` is ``OUTCOME_ADOPTED`` or ``OUTCOME_ALREADY_ADOPTED``; ``created``
    is the same fact as a bool for callers that only need the branch. ``onboarding``
    is the row — the NEW one, or the pre-existing one left exactly as it was.

    Frozen, and carrying no user object: an adoption result is a statement about
    provenance, not a channel for handing back credentials or an owner's identity.
    """

    outcome: str
    onboarding: RestaurantOnboarding
    restaurant: Restaurant

    @property
    def created(self) -> bool:
        return self.outcome == OUTCOME_ADOPTED


def _validate_restaurant_id(raw) -> uuid.UUID:
    """
    The target's UUID, parsed strictly and BEFORE any database access.

    One invocation adopts ONE immutable primary key. There is no name lookup, no
    fuzzy match and no ``.first()`` over candidates, for the reason UUID-only
    targeting exists everywhere else on this plane: the wrong-tenant failure is
    silent. An adoption recorded against the wrong restaurant does not error — it
    just permanently misstates how that tenant entered Dinify, attributed to a real
    administrator who never decided any such thing.

    A malformed identifier is its own code rather than ``restaurant_not_found``: a
    typo and a tenant that does not exist call for different reactions, and
    collapsing them would tell an operator to go looking for a restaurant when the
    problem is in their clipboard.
    """
    if isinstance(raw, uuid.UUID):
        return raw
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise AdoptionError(
            INVALID_RESTAURANT_ID,
            'A restaurant UUID is required.',
        )
    try:
        return uuid.UUID(cleaned)
    except (ValueError, AttributeError, TypeError):
        raise AdoptionError(
            INVALID_RESTAURANT_ID,
            'The target must be a restaurant UUID; adoption never accepts a name.',
        )


def _validate_reason(raw) -> str:
    """
    The trimmed operator reason, or ``AdoptionError(INVALID_REASON)``.

    The bar is IMPORTED from ``restaurants_app.controllers.lifecycle`` — the other
    audited, reason-required, platform-owned write about a restaurant — which in turn
    mirrors ``platform_admin_app.delegation.MIN_REASON_LENGTH``. Three privileged
    admin actions on one tenant disagreeing about how much of a reason a reason has
    to be would be arbitrary, and a second literal ``10`` here is how they would
    start to drift.

    Only the CONSTANT is reused. ``lifecycle._validate_reason`` raises a
    ``LifecycleTransitionError`` carrying an endpoint-shaped ``errors`` dict, which
    is the wrong failure for this service and the wrong failure for a shell command.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise AdoptionError(INVALID_REASON, 'A reason is required.')
    if len(cleaned) < MIN_REASON_LENGTH:
        raise AdoptionError(
            INVALID_REASON,
            f'A reason of at least {MIN_REASON_LENGTH} characters is required '
            '(the audit row is only as useful as this sentence).',
        )
    return cleaned


def _resolve_actor(actor) -> User:
    """
    The platform-staff ``User`` whose decision this is, re-read from the database.

    ENFORCED HERE EVEN WHEN THE CALLER ALREADY CHECKED. The management command
    validates its ``--actor`` too, and the future Admin endpoint will arrive with an
    authenticated session — but the service is the thing that writes the audit row,
    so the service is where the row's attribution has to be true. A rule enforced
    only in adapters holds until the second adapter.

    Re-read rather than trusted: the caller hands in an instance, and an instance's
    ``is_active`` / ``account_type`` are whatever they were when it was loaded (or
    whatever anything in between assigned to them in memory). The row is the fact.

    Fails closed on all three counts — not a user, unknown, wrong plane, deactivated
    — before anything is written, so the log can never name an account that could not
    have made the decision it records.
    """
    if not isinstance(actor, User) or actor.pk is None:
        raise AdoptionError(
            INVALID_ACTOR,
            'An actor is required: adoption is an attributed platform decision.',
        )

    fresh = User.objects.filter(pk=actor.pk).first()
    if fresh is None:
        raise AdoptionError(INVALID_ACTOR, 'The actor account no longer exists.')
    if fresh.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
        raise AdoptionError(
            INVALID_ACTOR,
            'Adoption is a platform decision; a restaurant user can never be its '
            'actor.',
        )
    if not fresh.is_active:
        raise AdoptionError(
            INVALID_ACTOR,
            'The actor account is deactivated and cannot be recorded as the actor.',
        )
    return fresh


def _existing_onboarding(restaurant) -> Optional[RestaurantOnboarding]:
    """
    The restaurant's onboarding row, or ``None``. Read INSIDE the adoption lock.

    ``.filter().first()`` rather than ``.get()``: the OneToOne makes at most one row
    possible, and catching ``DoesNotExist`` to express "there isn't one" turns an
    ordinary branch into exception flow.
    """
    return RestaurantOnboarding.objects.filter(restaurant=restaurant).first()


def adopt_existing_restaurant(*, restaurant_id, actor, reason) -> AdoptionResult:
    """
    Represent an existing canonical ``Restaurant`` in the Admin onboarding domain.

    Returns an ``AdoptionResult``. Raises ``AdoptionError`` (invalid target, actor
    or reason; a provenance conflict) or ``OwnerConsistencyError`` (a NEW adoption
    whose owner of record and owner authority disagree).

    THE TRANSACTION, AND WHY THE RESTAURANT ROW IS THE SERIALIZATION POINT.
    Everything from the lock to the audit entry happens in ONE
    ``transaction.atomic()``, and the first statement inside it takes
    ``select_for_update()`` on the target restaurant. That row — not the onboarding
    row, which may not exist yet, and not a table-level anything — is what two
    concurrent operators adopting the same tenant have in common, so locking it is
    what makes them queue instead of racing to insert the one-to-one row. The loser
    of that race then reads the winner's committed row and reports an idempotent
    no-op, rather than surfacing an ``IntegrityError`` from a unique violation.

    NO ADVISORY LOCK, DELIBERATELY. ``lock_admission_exclusive`` exists to stop an
    order being ADMITTED against one lifecycle state or test classification and then
    WRITTEN under another; the order path reads neither of the things this service
    writes, because this service writes nothing an order path reads. Table
    allocation and QR locks are equally unrelated. Taking them anyway would enrol
    adoption in three lock-ordering domains it has no business in — and every lock a
    transaction holds is a lock some future transaction can deadlock against. The
    order here is simply ``Restaurant -> RestaurantOnboarding -> AdminAuditLog``,
    which extends the documented global order at its tail with a table nothing else
    locks, and so cannot cycle against it.

    IDEMPOTENCY IS ABOUT HISTORY, NOT CONVENIENCE. A restaurant already adopted as
    ``legacy_adopted`` returns a successful no-op with its ORIGINAL ``adopted_at``
    and ``adopted_by`` intact and NO second audit row. The first adoption is the
    historical event; re-running the runbook — with a different operator, a different
    reason, a year later — is not a second one, and must not be able to overwrite who
    made the call or when.

    AND IT DOES NOT RE-EXAMINE THE PAST. An already-adopted restaurant is NOT
    re-checked for owner consistency. Ownership can drift long after an adoption, and
    when it does, the historical fact that the restaurant was adopted has not become
    untrue. Current owner consistency is a question for current-state and readiness
    evaluation, which Step 2C and Step 3 own; making it a precondition for merely
    REPORTING an existing adoption would let a present-day inconsistency erase a past
    decision from the operator's view.

    A PROVENANCE CONFLICT IS NEVER RESOLVED SILENTLY. If the restaurant already
    carries ``admin_created`` provenance, this refuses with
    ``onboarding_source_conflict`` and changes nothing. ``admin_created`` asserts that
    Dinify created the tenant and names the staff member who did; rewriting it to
    ``legacy_adopted`` would delete that attribution and replace it with a
    contradictory claim about the tenant's origin. Two records disagreeing is a
    situation a human has to look at.
    """
    # Cheap, purely local validation first: nothing here needs the database, and a
    # mistyped identifier should never reach a row lock.
    target_id = _validate_restaurant_id(restaurant_id)
    cleaned_reason = _validate_reason(reason)
    resolved_actor = _resolve_actor(actor)

    with transaction.atomic():
        # THE SERIALIZATION POINT. `.filter(pk=...)` with no `select_related`, so
        # this locks the `restaurants` row and nothing else — an over-broad lock
        # here is how the delegation-redemption ABBA cycle happened (PR-E).
        restaurant = (
            Restaurant.objects
            .select_for_update()
            .filter(pk=target_id)
            .first()
        )
        # Existence and soft-deletion are re-checked UNDER the lock, and answered
        # with ONE message, mirroring the admin plane's `_not_found()`: a
        # soft-deleted tenant is not a valid target, and adoption has no business
        # confirming that it ever existed.
        if restaurant is None or restaurant.deleted:
            raise AdoptionError(
                RESTAURANT_NOT_FOUND,
                'No such restaurant. (A soft-deleted restaurant is not a valid '
                'target and is reported the same way.)',
                {'restaurant_id': str(target_id)},
            )

        existing = _existing_onboarding(restaurant)
        if existing is not None:
            if existing.source == ONBOARDING_SOURCE_LEGACY_ADOPTED:
                # No write, no audit row, and above all no re-stamping of
                # adopted_at / adopted_by. Returning from inside the block is fine:
                # nothing was written, so the transaction simply commits the reads.
                return AdoptionResult(
                    outcome=OUTCOME_ALREADY_ADOPTED,
                    onboarding=existing,
                    restaurant=restaurant,
                )
            raise AdoptionError(
                ONBOARDING_SOURCE_CONFLICT,
                'This restaurant is already represented in the Admin onboarding '
                f'domain with {existing.source!r} provenance, which cannot be '
                'converted to legacy adoption.',
                {
                    'restaurant_id': str(restaurant.pk),
                    'onboarding_id': str(existing.pk),
                    'existing_source': existing.source,
                },
            )

        # THE PRECONDITION for a NEW adoption, checked under the lock so it is
        # authoritative for the insert that follows. It reads and raises; it never
        # repairs, so an ambiguous or drifted ownership refuses the adoption and
        # leaves the tenant exactly as it was for a human to resolve.
        assert_owner_consistency(restaurant)

        onboarding = RestaurantOnboarding.objects.create(
            restaurant=restaurant,
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            # Dinify did not create this tenant. The legacy_adopted shape constraint
            # enforces this too; stating it here is what makes the intent readable.
            created_by=None,
            # The real moment of a real event: when an administrator reconciled a
            # pre-existing restaurant into this domain. It says nothing about when
            # the restaurant was created or when its owner gained control — neither
            # of which this platform knows for a legacy tenant.
            adopted_at=timezone.now(),
            adopted_by=resolved_actor,
            # LEFT NULL ON PURPOSE, and never inferred. Adoption is not a personal
            # verification that the owner controls the account, so it must not leave
            # behind evidence that says it was. Attestation is a separate, explicit,
            # separately-audited decision — see the module docstring.
            owner_control_attested_at=None,
            owner_control_attested_user=None,
            owner_control_attested_by=None,
        )

        # Inside the transaction, per the no-audit-no-action half of the contract in
        # `platform_admin_app.audit`: a provenance record nobody can be shown to have
        # decided is worse than no provenance record. If this raises, the row above
        # goes with it.
        #
        # The state blobs carry the provenance change and NOTHING else — no owner
        # name, phone or email, no contact details, no menu, table, QR or order data,
        # no token material. The actor, the restaurant UUID and the before/after
        # provenance are the whole decision.
        audit.record(
            action=ADMIN_RESTAURANT_ONBOARDING_ADOPTED,
            result=RESULT_SUCCESS,
            actor=resolved_actor,
            actor_label=resolved_actor.username,
            resource_type='Restaurant',
            resource_id=str(restaurant.pk),
            restaurant_id=restaurant.pk,
            reason=cleaned_reason,
            before_state={'admin_onboarding_source': None},
            after_state={
                'admin_onboarding_source': ONBOARDING_SOURCE_LEGACY_ADOPTED,
            },
        )

    return AdoptionResult(
        outcome=OUTCOME_ADOPTED,
        onboarding=onboarding,
        restaurant=restaurant,
    )
