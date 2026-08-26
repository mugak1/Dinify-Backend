"""
THE OWNER-INVITATION CREDENTIAL LIFECYCLE (Phase 1, Step 2E).

Step 2D mints a restaurant's FIRST claim credential and Step 2F will redeem one.
Between those two moments an invitation is a live credential that a platform
administrator has to be able to manage, and until now they could not: a lost 201, a
misdelivered link, an expired window or an onboarding that should simply stop had no
answer at all. This module is that answer, and it is deliberately only two operations:

    REISSUE   invalidate whatever unresolved credential this onboarding currently
              represents, and mint a fresh one for the CURRENT canonical owner,
              returning one raw token exactly once.

    CANCEL    terminate this exact unresolved credential and replace it with nothing.

━━ REISSUE, NOT RESEND ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The name is load-bearing. "Resend" would claim a delivery event exists, and this
system has never delivered anything: there is no email, no SMS, no notification, no
delivery column on the schema and no provider. What actually happens is ROTATION —
the old credential dies, a new one is born, and the raw token is handed to the
authenticated, elevated operator who asked for it. Calling that "resend" would be a
promise the platform cannot keep, made in the one place an operator most needs to
know exactly what happened.

Rotation is also what makes a LOST RESPONSE recoverable. If a reissue commits and its
response is lost, the platform holds a credential nobody knows — so the fix is to
reissue AGAIN, which supersedes that unknown credential and mints a known one. There
is deliberately no way to recover the plaintext, and no pretence that an identical
retry returns the same token.

━━ THE ASYMMETRY THAT MATTERS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REISSUE MINTS AUTHORITY, so it insists the owner relationship is sound first: an
invitation is an instruction to one specific person to take control of a restaurant,
and issuing one while the two answers to "who owns this?" disagree would hand
authority to whichever answer happened to win. It requires ``assert_owner_consistency``
and binds to ``Restaurant.owner`` as read under the lock.

CANCELLATION REMOVES AUTHORITY, so it insists on none of that. If a tenant's ownership
has drifted AND a live claim credential is outstanding, that is precisely when an
administrator most needs to be able to kill the credential — and making revocation
depend on the drift being fixed first would leave it live for as long as the mess
took to resolve. Cancellation still infers nothing and repairs nothing; it just does
not require the world to be tidy.

━━ WHAT NEITHER OPERATION TOUCHES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``User.customer_access_state`` above all. A restaurant-scoped credential is not an
identity claim: issuing another one does not establish customer access, and
cancelling one does not revoke it. A ``pending_initial_claim`` owner stays pending
(they simply have no claimable credential until Admin reissues), and an
``established`` owner stays established (their access to their OTHER restaurants has
nothing to do with this one). Redemption remains the only supported writer of that
transition, and Step 2F still has to build it.

Also untouched: the owner's password, ``is_active``, ``prompt_password_change`` and
``last_login``; ``Restaurant.owner`` and the owner membership; the onboarding row's
provenance and attestation triple; and every historical invitation's stamps, expiry
and token hash. History is evidence, and evidence is not edited.

━━ NO AUTHORIZATION HERE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

These services never inspect an ``AdminSession``, elevation, CSRF or a request, and
they write no ``AdminAuditLog`` row. ``_resolve_actor`` answers *whose decision this
was*, never *were they allowed to make it*. The adapter in
``endpoints/owner_invitation`` owns authority and the audit entry, and wraps the
mutation and that entry in ONE transaction so a failed audit rolls the credential
change back — which only works because the audit write does not live down here.
"""
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
)
from platform_admin_app import sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import assert_owner_consistency
from platform_admin_app.onboarding_reads import (
    HeadInvitation,
    select_head_invitation,
)
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import Restaurant
from users_app.models import User

# --- the credential policy, in ONE place -------------------------------------
#
# MOVED HERE FROM ``onboarding_creation``, not copied. Initial issuance and reissue
# must produce indistinguishable credentials — same entropy, same hash function, same
# window — because they are the same kind of thing, and "the reissued one is slightly
# weaker" is the sort of divergence nobody notices until it matters. Creation imports
# these back, so there is exactly one definition of what an owner invitation IS.

# The default claim window when the deployment does not override it. Overridable via
# ``ADMIN_OWNER_INVITATION_TTL`` — read through ``getattr`` with this same default, as
# every other admin constant is, so a settings module that predates the setting still
# works.
OWNER_INVITATION_TTL_DEFAULT = timedelta(days=7)

# Raw claim-token entropy, matching ``sessions._TOKEN_BYTES``: 48 bytes -> a 64-char
# url-safe string, ~288 bits. The same standard as an admin session token, a login
# challenge and a delegation exchange code.
_TOKEN_BYTES = 48


def owner_invitation_ttl():
    """The configured owner claim window."""
    return getattr(
        settings, 'ADMIN_OWNER_INVITATION_TTL', OWNER_INVITATION_TTL_DEFAULT,
    )


def mint_owner_invitation(*, onboarding, invited_user, issued_by, now):
    """
    Create one unresolved ``OwnerInvitation`` and return ``(invitation, raw_token)``.

    THE ONLY PLACE AN OWNER CLAIM CREDENTIAL IS GENERATED. Both callers — the Step-2D
    creation service and the Step-2E reissue service — go through here, so there is
    one token length, one hash function, one TTL and one definition of "issued".

    ``now`` is passed IN rather than read here, and that is the point: the caller has
    already captured one instant for its whole decision, and calling ``timezone.now()``
    again would put a meaningless sub-millisecond drift between issue and expiry and
    make the window something other than exactly the configured TTL.

    ONLY THE HASH IS PERSISTED, via the same ``sessions.hash_token`` used for admin
    sessions and delegation codes rather than a second hashing function. The raw token
    is returned to the caller and exists nowhere else, ever: not in the row, not in a
    log, not in an audit entry, not in an exception. Its whole life is one HTTP
    response.

    IT DOES NOT SUPERSEDE ANYTHING. Freeing the per-onboarding unresolved slot is the
    CALLER'S job, done under the caller's lock — see ``reissue_owner_invitation``.
    Hiding a supersede inside a function called "mint" would make the destructive half
    of a rotation invisible at the call site.

    NOTHING IS DELIVERED. No email, SMS, notification or action log; the schema has no
    delivery columns for exactly that reason.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    invitation = OwnerInvitation.objects.create(
        onboarding=onboarding,
        invited_user=invited_user,
        issued_by=issued_by,
        token_hash=sessions.hash_token(raw_token),
        issued_at=now,
        expires_at=now + owner_invitation_ttl(),
        # Left unresolved. Marking it consumed here would assert that the owner
        # confirmed control at the moment the credential was created, which is the one
        # thing this credential exists to find out.
        consumed_at=None,
        cancelled_at=None,
        cancelled_by=None,
        superseded_at=None,
    )
    return invitation, raw_token


# --- error codes -------------------------------------------------------------
#
# Distinct because each needs a different reaction: fix the identifier, pick a real
# tenant, reload the screen, use a different operation, or fix the tenant's ownership
# before minting authority for it.

# Targeting and invocation.
INVALID_RESTAURANT_ID = 'invalid_restaurant_id'
RESTAURANT_NOT_FOUND = 'restaurant_not_found'
INVALID_ACTOR = 'invalid_actor'
INVALID_REASON = 'invalid_reason'
INVALID_EXPECTED_INVITATION_ID = 'invalid_expected_invitation_id'

# The onboarding domain does not offer this operation for this tenant.
ONBOARDING_NOT_TRACKED = 'onboarding_not_tracked'
OWNER_INVITATION_NOT_APPLICABLE = 'owner_invitation_not_applicable'

# Optimistic concurrency: the caller's belief about WHICH invitation is current no
# longer matches what the locked onboarding presents. Its own code because the correct
# reaction is "reload and look again", not "fix your input".
STALE_OWNER_INVITATION = 'stale_owner_invitation'
# There is no invitation at all to name — nothing has ever been issued.
OWNER_INVITATION_NOT_ISSUED = 'owner_invitation_not_issued'

# Reissue-only refusals.
OWNER_CONTROL_ALREADY_ESTABLISHED = 'owner_control_already_established'
OWNER_ACCOUNT_NOT_FOUND = 'owner_account_not_found'
OWNER_ACCOUNT_INACTIVE = 'owner_account_inactive'
OWNER_ACCOUNT_NOT_RESTAURANT_USER = 'owner_account_not_restaurant_user'

# Cancel-only refusal: the named invitation already resolved in a way cancellation
# cannot and must not overwrite.
OWNER_INVITATION_ALREADY_RESOLVED = 'owner_invitation_already_resolved'

INVALID_REQUEST_CODES = frozenset({
    INVALID_RESTAURANT_ID,
    INVALID_ACTOR,
    INVALID_REASON,
    INVALID_EXPECTED_INVITATION_ID,
})

CONFLICT_CODES = frozenset({
    ONBOARDING_NOT_TRACKED,
    OWNER_INVITATION_NOT_APPLICABLE,
    STALE_OWNER_INVITATION,
    OWNER_INVITATION_NOT_ISSUED,
    OWNER_CONTROL_ALREADY_ESTABLISHED,
    OWNER_ACCOUNT_NOT_FOUND,
    OWNER_ACCOUNT_INACTIVE,
    OWNER_ACCOUNT_NOT_RESTAURANT_USER,
    OWNER_INVITATION_ALREADY_RESOLVED,
})

# Answered with the plane's silent 404 rather than a conflict: a soft-deleted or
# absent restaurant is not a target, and the admin plane never distinguishes "gone"
# from "never existed".
NOT_FOUND_CODES = frozenset({INVALID_RESTAURANT_ID, RESTAURANT_NOT_FOUND})

INVITATION_ERROR_CODES = (
    INVALID_REQUEST_CODES | CONFLICT_CODES | {RESTAURANT_NOT_FOUND}
)


class OwnerInvitationError(Exception):
    """
    A credential-lifecycle operation was refused. ``code`` is one of
    ``INVITATION_ERROR_CODES``.

    Mirrors ``AdoptionError`` / ``RestaurantCreationError`` deliberately: a short
    machine-readable ``code``, a sentence a human can act on, and ``details`` carrying
    UUIDs ONLY.

    NO PII AND NO CREDENTIAL, EVER. These messages reach logs, operator screens and
    the audit log's ``error_code``. An owner's phone or email has no business in any
    of them, and neither has a raw token or a ``token_hash`` — an identifier is enough
    to investigate with, and a hash in a log is a standing invitation to treat it as
    a credential.
    """

    def __init__(self, code, message='', details=None):
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)

    def __str__(self):
        return self.message


# --- results -----------------------------------------------------------------

@dataclass(frozen=True)
class ReissueResult:
    """
    What a reissue did, stated by the operation rather than inferred afterwards.

    ``head`` is the invitation the request FOUND — the row the operator named — and
    ``head_status`` is how it read under the lock, BEFORE any stamp this call wrote.
    Both are captured rather than left to be re-read, because a re-read afterwards
    would describe the outcome: a superseded row can no longer tell anybody whether it
    was a live link or an expired one a moment ago, and "I killed a live credential"
    and "I replaced a dead one" are different operational events.

    ``superseded`` says whether that head was actually stamped. It is False when the
    head was already resolved — a cancelled credential being reopened, or a historical
    one belonging to a previous owner — because a resolved row occupies no slot and
    must not be re-stamped. ``invitation`` is the new unresolved row.

    ``claim_token`` IS THE RAW CREDENTIAL and the only copy that will ever exist: only
    its SHA-256 hash is persisted. It lives on this object exactly long enough to be
    written into one HTTP response, and must never be logged, audited, stored, put in
    a URL or a cookie, or included in an exception.

    ``changed`` is always True. A reissue that changed nothing is not a thing: either
    a new credential was minted or the operation was refused.
    """

    restaurant: Restaurant
    onboarding: RestaurantOnboarding
    head: OwnerInvitation
    head_status: str
    superseded: bool
    invitation: OwnerInvitation
    claim_token: str

    @property
    def changed(self) -> bool:
        return True


@dataclass(frozen=True)
class CancellationResult:
    """
    What a cancellation did.

    ``changed`` distinguishes a real cancellation from an EXACT RETRY of one that
    already committed — the same request arriving twice because its response was lost.
    The retry is a success with ``changed=False`` and moves neither ``cancelled_at``
    nor ``cancelled_by``, because the terminal event happened once and the log must
    not be able to say it happened twice.

    ``invitation`` is the row as it stands after the call: freshly cancelled, or the
    already-cancelled row untouched. There is no token here and never will be —
    cancellation produces no credential.
    """

    restaurant: Restaurant
    onboarding: RestaurantOnboarding
    invitation: OwnerInvitation
    changed: bool


# --- validation --------------------------------------------------------------

def _validate_restaurant_id(raw) -> uuid.UUID:
    """
    The target's UUID, parsed strictly and BEFORE any database access.

    One invocation targets ONE immutable primary key: no name lookup, no fuzzy match,
    no ``.first()`` over candidates. The wrong-tenant failure is silent — rotating the
    wrong restaurant's credential does not error, it just invalidates a link somebody
    else is waiting on.
    """
    if isinstance(raw, uuid.UUID):
        return raw
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise OwnerInvitationError(
            INVALID_RESTAURANT_ID, 'A restaurant UUID is required.',
        )
    try:
        return uuid.UUID(cleaned)
    except (ValueError, AttributeError, TypeError):
        raise OwnerInvitationError(
            INVALID_RESTAURANT_ID, 'The target must be a restaurant UUID.',
        )


def _validate_expected_invitation_id(raw) -> uuid.UUID:
    """
    The concurrency token, parsed strictly and BEFORE any database access.

    REQUIRED, AND NEVER INFERRED. There is no "whatever is current when the POST
    arrives" fallback, because that is exactly the failure this token exists to
    prevent: an operator reviews invitation A, somebody else reissues A into B, and
    the first operator's Cancel click lands on B. With the token that becomes a
    conflict; without it, it silently cancels the wrong credential.

    A malformed token is a 400 about the caller's own input, deliberately NOT a 409:
    a conflict means the world moved, and manufacturing one out of a typo would send
    an operator to reload a screen that was never stale.
    """
    if isinstance(raw, uuid.UUID):
        return raw
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise OwnerInvitationError(
            INVALID_EXPECTED_INVITATION_ID,
            'The id of the invitation you reviewed is required.',
        )
    try:
        return uuid.UUID(cleaned)
    except (ValueError, AttributeError, TypeError):
        raise OwnerInvitationError(
            INVALID_EXPECTED_INVITATION_ID,
            'The expected invitation id must be a UUID.',
        )


def _validate_reason(raw) -> str:
    """
    The trimmed operator reason, or ``OwnerInvitationError(INVALID_REASON)``.

    The bar is IMPORTED from ``restaurants_app.controllers.lifecycle`` — which mirrors
    ``platform_admin_app.delegation.MIN_REASON_LENGTH`` — exactly as the adoption and
    creation services do. A second literal ``10`` on a fourth privileged surface is
    how four surfaces start disagreeing about how much of a reason a reason has to be.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise OwnerInvitationError(INVALID_REASON, 'A reason is required.')
    if len(cleaned) < MIN_REASON_LENGTH:
        raise OwnerInvitationError(
            INVALID_REASON,
            f'A reason of at least {MIN_REASON_LENGTH} characters is required '
            '(the audit row is only as useful as this sentence).',
        )
    return cleaned


def _resolve_actor(actor) -> User:
    """
    The platform-staff ``User`` whose decision this is, re-read from the database.

    ENFORCED HERE EVEN THOUGH THE ADAPTER ALREADY AUTHENTICATED. The service is what
    stamps ``issued_by`` and ``cancelled_by``, so the service is where that
    attribution has to be true — a rule enforced only in adapters holds until the
    second adapter, and this domain is written expecting a management command to
    arrive eventually.

    Re-read rather than trusted: the caller hands in an instance whose ``is_active``
    and ``account_type`` are whatever they were when it was loaded. The row is the
    fact.
    """
    if not isinstance(actor, User) or actor.pk is None:
        raise OwnerInvitationError(
            INVALID_ACTOR,
            'An actor is required: a credential operation is an attributed platform '
            'decision.',
        )

    fresh = User.objects.filter(pk=actor.pk).first()
    if fresh is None:
        raise OwnerInvitationError(INVALID_ACTOR, 'The actor account no longer exists.')
    if fresh.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
        raise OwnerInvitationError(
            INVALID_ACTOR,
            'Owner-invitation lifecycle is a platform decision; a restaurant user can '
            'never be its actor.',
        )
    if not fresh.is_active:
        raise OwnerInvitationError(
            INVALID_ACTOR,
            'The actor account is deactivated and cannot be recorded as the actor.',
        )
    return fresh


# --- the shared preamble -----------------------------------------------------

def _lock_target(target_id):
    """
    The live ``Restaurant`` row, locked. THE SERIALIZATION POINT for this domain.

    Every invitation operation for a tenant takes this row first, so reissue-vs-
    reissue, reissue-vs-cancel and cancel-vs-cancel all become a queue with a
    well-defined winner and a clean domain refusal for the loser — rather than a race
    resolved by ``one_unresolved_owner_invitation_per_onboarding`` raising an
    ``IntegrityError`` at somebody. That constraint is the final database backstop, not
    the concurrency user experience.

    ``.filter(pk=...)`` with no ``select_related``, so this locks the ``restaurants``
    row and nothing else — an over-broad lock here is how the delegation-redemption
    ABBA cycle happened (PR-E).

    Existence and soft-deletion are re-checked UNDER the lock and answered with ONE
    code, mirroring the admin plane's ``_not_found()``.
    """
    restaurant = (
        Restaurant.objects.select_for_update().filter(pk=target_id).first()
    )
    if restaurant is None or restaurant.deleted:
        raise OwnerInvitationError(
            RESTAURANT_NOT_FOUND,
            'No such restaurant. (A soft-deleted restaurant is not a valid target '
            'and is reported the same way.)',
            {'restaurant_id': str(target_id)},
        )
    return restaurant


def _lock_onboarding(restaurant) -> RestaurantOnboarding:
    """
    The restaurant's ``admin_created`` onboarding row, locked. Or a refusal.

    TWO DISTINCT REFUSALS, because they call for different reactions:

      * ``onboarding_not_tracked`` — the restaurant is not represented in the Admin
        onboarding domain at all, so it has no credential lifecycle to manage. Nothing
        here manufactures the missing row: creating provenance as a side effect of
        somebody clicking Reissue would invent a fact about how the tenant entered
        Dinify.

      * ``owner_invitation_not_applicable`` — the restaurant is ``legacy_adopted``. A
        pre-existing tenant did not enter Dinify through a claim flow and does not use
        ``OwnerInvitation`` as its claim mechanism at all, which is precisely what the
        read projection already says with ``not_applicable``. Converting the
        provenance to make the operation possible would delete a true statement about
        the tenant's origin and replace it with a false one.
    """
    onboarding = (
        RestaurantOnboarding.objects
        .select_for_update(of=('self',))
        .filter(restaurant=restaurant)
        .first()
    )
    if onboarding is None:
        raise OwnerInvitationError(
            ONBOARDING_NOT_TRACKED,
            'This restaurant is not represented in the Admin onboarding domain, so '
            'it has no owner-invitation lifecycle.',
            {'restaurant_id': str(restaurant.pk)},
        )
    if onboarding.source != ONBOARDING_SOURCE_ADMIN_CREATED:
        raise OwnerInvitationError(
            OWNER_INVITATION_NOT_APPLICABLE,
            'This restaurant did not enter Dinify through a claim flow, so it has no '
            'owner invitation to reissue or cancel.',
            {
                'restaurant_id': str(restaurant.pk),
                'onboarding_id': str(onboarding.pk),
                'onboarding_source': onboarding.source,
            },
        )
    return onboarding


def _head_under_lock(onboarding, restaurant, expected_id, now) -> HeadInvitation:
    """
    The head invitation, re-read and LOCKED, with the caller's token checked against it.

    ━━ WHAT ``expected_invitation_id`` ASSERTS — STATED EXACTLY ━━━━━━━━━━━━━━━━━━━

    IDENTITY, NOT STATUS. It asserts *the invitation I reviewed is still the one this
    onboarding presents as its head*. It does NOT assert that the invitation is still
    in the state the operator saw it in.

    That distinction is deliberate and has a concrete consequence: if another operator
    CANCELS invitation A while this request is in flight, A remains the head (nothing
    unresolved exists, and A is the latest resolved row), so a reissue naming A still
    proceeds and mints B. That is the same outcome the operator would have reached by
    reloading the screen — which would show ``cancelled``, id ``A`` — and deliberately
    clicking Reissue, and reopening a cancelled onboarding is a REQUIRED workflow, not
    an accident (see ``reissue_owner_invitation``).

    What the token does prevent is the failure it was introduced for: once a reissue
    has moved the head from A to B, a request naming A is stale and refused, so an old
    Cancel click can never terminate a credential the operator has never seen.

    ━━ IDENTITY IS NOT ENOUGH ON ITS OWN ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Matching the id does NOT mean "any transition is now fine". Each operation applies
    its OWN preconditions to the state found HERE, under the lock — never to the state
    a pre-lock read happened to see. Cancellation refuses a consumed or superseded
    head; reissue refuses one that already establishes the current owner's control.

    A head of ``not_issued`` gets its own code rather than ``stale``: there is no
    invitation to be stale about, and telling an operator to reload when the honest
    answer is "this restaurant has never had one" sends them looking for a change that
    never happened.
    """
    head = select_head_invitation(
        onboarding, restaurant, now=now, for_update=True,
    )
    if head.invitation is None:
        raise OwnerInvitationError(
            OWNER_INVITATION_NOT_ISSUED,
            'This restaurant has no owner invitation.',
            {'restaurant_id': str(restaurant.pk)},
        )
    if head.invitation.pk != expected_id:
        raise OwnerInvitationError(
            STALE_OWNER_INVITATION,
            'The owner invitation changed since it was loaded.',
            {
                'restaurant_id': str(restaurant.pk),
                'expected_invitation_id': str(expected_id),
                'current_invitation_id': str(head.invitation.pk),
            },
        )
    return head


# --- reissue -----------------------------------------------------------------

def _resolve_current_owner(restaurant) -> User:
    """
    The ``User`` a new credential will be issued to: ``Restaurant.owner``, re-read.

    THE CANONICAL RESTAURANT OWNS THIS FACT. Never the previous invitation's
    ``invited_user`` (which may name a former owner), never a request field (the
    request carries no owner identity at all, and must not), never
    ``onboarding.created_by`` (that is the platform staff member who created the
    tenant), and never an email or phone lookup.

    Re-read under the lock rather than trusted from ``restaurant.owner``, for the same
    reason ``_resolve_actor`` re-reads: eligibility is a property of the ROW, and the
    instance's ``is_active`` and ``account_type`` are whatever they were when it was
    loaded.

    ELIGIBILITY IS CHECKED, NOT REPAIRED. An inactive owner or one that has become
    platform staff is refused; nothing here reactivates an account or changes its
    plane, both of which are separate decisions with their own actor and reason.
    """
    if restaurant.owner_id is None:
        raise OwnerInvitationError(
            OWNER_ACCOUNT_NOT_FOUND,
            'This restaurant has no owner of record, so there is nobody to invite.',
            {'restaurant_id': str(restaurant.pk)},
        )

    owner = User.objects.filter(pk=restaurant.owner_id).first()
    if owner is None:
        raise OwnerInvitationError(
            OWNER_ACCOUNT_NOT_FOUND,
            'The owner account no longer exists.',
            {'restaurant_id': str(restaurant.pk)},
        )
    if not owner.is_active:
        raise OwnerInvitationError(
            OWNER_ACCOUNT_INACTIVE,
            'The owner account is deactivated. Reactivating it is a separate '
            'decision, and this operation was not asked to make it.',
            {'owner_user_id': str(owner.pk)},
        )
    if owner.account_type != ACCOUNT_TYPE_RESTAURANT_USER:
        raise OwnerInvitationError(
            OWNER_ACCOUNT_NOT_RESTAURANT_USER,
            'The owner account is not a restaurant user and cannot be invited to '
            'claim a restaurant.',
            {'owner_user_id': str(owner.pk)},
        )
    return owner


def reissue_owner_invitation(
    *, restaurant_id, expected_invitation_id, actor, reason,
) -> ReissueResult:
    """
    Rotate this onboarding's owner claim credential. Returns a ``ReissueResult``.

    Invalidates whatever unresolved invitation the onboarding currently represents and
    mints a fresh one for the CURRENT canonical owner, returning one raw token exactly
    once. Raises ``OwnerInvitationError`` or ``OwnerConsistencyError``.

    ━━ LOCK ORDER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        Restaurant -> RestaurantOnboarding -> head OwnerInvitation

    all in ONE ``transaction.atomic()``, with the adapter's ``AdminAuditLog`` write
    nesting outside it. That extends the documented global order at its tail:
    ``onboarding_adoption`` already establishes ``Restaurant -> RestaurantOnboarding``,
    and ``OwnerInvitation`` is a table nothing else locks, so this cannot cycle against
    anything that exists.

    NO ADMISSION ADVISORY LOCK, deliberately. ``lock_admission_exclusive`` exists to
    stop an order being admitted against one lifecycle state or test classification and
    written under another; the order path reads no invitation fact, because there is no
    invitation fact it could read. Taking that lock AFTER the ``Restaurant`` row would
    also invert the lifecycle transition's ``advisory -> Restaurant`` order and
    reintroduce exactly the cycle ``restaurants_app.controllers.admission_lock``
    documents. Take it first or not at all — and this domain does not need it at all.

    ━━ THE FOUR STATES A REISSUE IS LEGITIMATE FROM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    PENDING — the ordinary case. The old credential is superseded and dies; whoever
    held its link can no longer use it.

    EXPIRED — the old row is STILL UNRESOLVED (expiry is derived, and a partial-index
    predicate cannot consult a clock), so it still occupies the per-onboarding slot and
    must be superseded before the replacement can be inserted. It is stamped
    ``superseded_at`` and NOT some persisted "expired" status: this repository runs no
    scheduler, and a status column nothing maintains is a lie with a timestamp on it.

    CANCELLED — there is no unresolved row and the head is the cancelled one. Reopening
    a deliberately-closed onboarding is a REQUIRED workflow: an administrator may kill
    a credential today and decide next week that this restaurant should be onboarded
    after all. There is no "uncancel" — A stays cancelled forever as historical
    evidence, and B is a new credential.

    CONSUMED BY A PREVIOUS OWNER — the restaurant has since been re-owned, the current
    owner has no control evidence, and a fresh credential is exactly what is needed.
    The historical row is NOT mutated and its evidence is NOT transferred.

    ━━ THE ONE STATE IT REFUSES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    The CURRENT owner has already redeemed an invitation, so
    ``owner_control: invitation_redeemed``. Control is established; this is no longer
    an invitation problem, and minting a second claim credential for somebody who has
    already claimed would create a live credential with nothing left to do but be
    stolen.

    NOTE THE QUESTION. It is *has the CURRENT owner's control been established?*, not
    *has any invitation in history ever been consumed?* — read from the head
    selector's ``establishes_current_owner_control``, which is the same evidence rule
    the read projection publishes, and never from a raw ``consumed_at``.

    ━━ AND THE PRECONDITION THAT IS NOT ABOUT INVITATIONS ━━━━━━━━━━━━━━━━━━━━━━━━

    ``assert_owner_consistency`` must pass before anything is minted. An invitation
    instructs one specific person to take control of a restaurant, so issuing one while
    the owner of record and the owner authority disagree would hand authority to
    whichever of two answers happened to be read. It validates and NEVER repairs: a
    drifted tenant is refused and left exactly as it was for a human to resolve.
    """
    # Cheap, purely local validation first: nothing here needs the database, and a
    # mistyped identifier should never reach a row lock.
    target_id = _validate_restaurant_id(restaurant_id)
    expected_id = _validate_expected_invitation_id(expected_invitation_id)
    _validate_reason(reason)
    resolved_actor = _resolve_actor(actor)

    # ONE captured instant for the whole credential, and for the expiry comparison
    # that decides whether the head reads pending or expired. Two `timezone.now()`
    # calls would let a decision and the record of it straddle that boundary.
    now = timezone.now()

    with transaction.atomic():
        restaurant = _lock_target(target_id)
        onboarding = _lock_onboarding(restaurant)
        head = _head_under_lock(onboarding, restaurant, expected_id, now)

        if head.establishes_current_owner_control:
            raise OwnerInvitationError(
                OWNER_CONTROL_ALREADY_ESTABLISHED,
                'This restaurant\'s current owner has already claimed it, so no '
                'further claim credential is needed.',
                {
                    'restaurant_id': str(restaurant.pk),
                    'invitation_id': str(head.invitation.pk),
                },
            )

        owner = _resolve_current_owner(restaurant)

        # Checked under the lock, so it is authoritative for the insert that follows.
        # Raises `OwnerConsistencyError`, which keeps its own type and its own three
        # codes rather than being flattened into this module's vocabulary: "this
        # restaurant's ownership is ambiguous" is a different problem from "this
        # reissue was invalid" and needs a different person to fix it.
        assert_owner_consistency(restaurant)

        # FREE THE SLOT BEFORE INSERTING, exactly as `challenges.create_challenge`
        # consumes before it inserts under `one_live_admin_challenge_per_user`. Only
        # an UNRESOLVED head occupies it; a cancelled or consumed head already left
        # the slot empty and must not be re-stamped — a superseded_at on a cancelled
        # row would violate the terminal-exclusivity constraint and, worse, would
        # rewrite what actually happened to that credential.
        superseded = not head.invitation.is_resolved
        if superseded:
            head.invitation.superseded_at = now
            # ONE COLUMN. `save()` with no `update_fields` would write back every
            # field on an instance read before the lock decisions were made, which is
            # how a stale in-memory `expires_at` or `token_hash` gets resurrected.
            head.invitation.save(update_fields=['superseded_at'])

        invitation, claim_token = mint_owner_invitation(
            onboarding=onboarding,
            invited_user=owner,
            issued_by=resolved_actor,
            now=now,
        )

    return ReissueResult(
        restaurant=restaurant,
        onboarding=onboarding,
        head=head.invitation,
        head_status=head.status,
        superseded=superseded,
        invitation=invitation,
        claim_token=claim_token,
    )


# --- cancel ------------------------------------------------------------------

def cancel_owner_invitation(
    *, restaurant_id, expected_invitation_id, actor, reason,
) -> CancellationResult:
    """
    Terminate this exact unresolved owner claim credential, replacing it with nothing.

    Returns a ``CancellationResult``. Raises ``OwnerInvitationError``.

    ━━ IT DELIBERATELY DOES NOT REQUIRE OWNER CONSISTENCY ━━━━━━━━━━━━━━━━━━━━━━━━

    Reissue does, because minting authority needs a sound owner target. Cancellation
    REMOVES authority, and a drifted tenant with a live claim credential outstanding is
    precisely when an administrator most needs to be able to kill it. Making revocation
    wait for the ownership mess to be resolved would leave the credential live for
    exactly as long as the mess took to fix.

    It still infers nothing and repairs nothing: it does not read who the owner is,
    does not correct a membership, and does not touch ``Restaurant.owner``. It targets
    the exact unresolved invitation under this ``admin_created`` onboarding and stamps
    two columns.

    ━━ WHAT IT WRITES, AND THE FOUR THINGS IT MUST NOT ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ``cancelled_at`` and ``cancelled_by``, and nothing else. It does NOT delete the row
    (history is evidence), does NOT set ``superseded_at`` or ``consumed_at`` (each of
    those says something different and untrue about how the credential ended), does NOT
    touch ``expires_at`` or ``token_hash``, and does NOT create a replacement — that is
    reissue's job, and conflating the two would make "cancel" mean "rotate".

    ━━ THE EXACT RETRY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Cancellation returns no credential, so unlike reissue it CAN safely be repeated: a
    lost response followed by an identical retry is answered ``changed=False`` with the
    original ``cancelled_at`` and ``cancelled_by`` intact. The terminal event happened
    once and the record must not be able to say it happened twice.

    That retry is only available while nothing has moved: if a reissue has since made a
    NEW invitation the head, the request names a stale id and is refused, so an old
    cancellation token can never terminate a credential the operator never saw. And a
    head that is CONSUMED or SUPERSEDED is not a retry at all — it resolved some other
    way, and answering "already done" would report success for something that did not
    happen.
    """
    target_id = _validate_restaurant_id(restaurant_id)
    expected_id = _validate_expected_invitation_id(expected_invitation_id)
    _validate_reason(reason)
    resolved_actor = _resolve_actor(actor)

    now = timezone.now()

    with transaction.atomic():
        restaurant = _lock_target(target_id)
        onboarding = _lock_onboarding(restaurant)
        head = _head_under_lock(onboarding, restaurant, expected_id, now)
        invitation = head.invitation

        if invitation.cancelled_at is not None:
            # THE EXACT RETRY. Nothing is written — not the timestamp, not the actor,
            # not a second anything. Returning from inside the block is fine: no write
            # happened, so the transaction commits the reads it did.
            return CancellationResult(
                restaurant=restaurant,
                onboarding=onboarding,
                invitation=invitation,
                changed=False,
            )

        if invitation.is_resolved:
            # Consumed or superseded. Both are terminal and neither is a cancellation,
            # so there is nothing here to cancel and nothing to report as already done.
            raise OwnerInvitationError(
                OWNER_INVITATION_ALREADY_RESOLVED,
                'This owner invitation has already resolved and cannot be cancelled.',
                {
                    'restaurant_id': str(restaurant.pk),
                    'invitation_id': str(invitation.pk),
                    'invitation_status': head.status,
                },
            )

        # PENDING or EXPIRED — both are unresolved, both hold the slot, and both are
        # legitimately cancellable. An expired credential is not self-cleaning: it
        # keeps occupying the per-onboarding slot until something resolves it.
        invitation.cancelled_at = now
        invitation.cancelled_by = resolved_actor
        invitation.save(update_fields=['cancelled_at', 'cancelled_by'])

    return CancellationResult(
        restaurant=restaurant,
        onboarding=onboarding,
        invitation=invitation,
        changed=True,
    )
