"""
THE OWNER-CLAIM AUTHORITY TRANSACTION — Step 2F.2, the second half of redemption.

Step 2F.1 turns a raw claim token into a delivered ``owner-claim`` OTP and writes
nothing. This module turns those TWO credentials into durable owner-control evidence,
in ONE database transaction:

    raw OwnerInvitation token   (POSSESSION of what Dinify issued)
              +
    owner-claim OTP             (CURRENT CONTROL of the invited identity's phone)
              ↓
    invitation consumed  ·  customer session minted
    ...and for a brand-new owner, additionally:
    password established  ·  pending_initial_claim -> established
    prompt_password_change -> False

━━ TWO TRANSITIONS, AND THE SECOND ONE IS THE FALSE POSITIVE TO AVOID ━━━━━━━━━━━━━

A BRAND-NEW OWNER (``customer_access_state == pending_initial_claim``) is completing
their FIRST claim. All four facts above move together or none of them do. There must
never be a committed state that says *invitation consumed but access still pending*,
*access established but invitation not consumed*, *access established but the password
is still unusable*, or *a new password persisted while the invitation stays pending*.

AN ESTABLISHED OWNER claiming an ADDITIONAL restaurant is a completely different
operation that happens to share a route. ``OwnerInvitation`` and
``RestaurantOnboarding`` are RESTAURANT-scoped, and one ``User`` may own several
restaurants — so this transaction consumes THIS restaurant's credential and mints a
session, and touches the identity's password, ``prompt_password_change``,
``customer_access_state``, email, phone and ``account_type`` NOT AT ALL. An established
account that happens to have an unusable password stays an established account with an
unusable password; repairing that during a restaurant claim would be reinterpreting a
restaurant-scoped fact as global account onboarding, which is exactly the mistake
``users_app.customer_access`` exists to prevent.

━━ WHAT AUTHORITY LOOKS LIKE AFTERWARDS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

THE CONSUMED INVITATION *IS* THE EVIDENCE. Nothing writes a ``claimed`` boolean, an
``owner_control`` column or an approval row — ``onboarding_reads`` already derives
``owner_control: invitation_redeemed`` from an invitation consumed by the CURRENT
owner, and a second representation of that fact would be a second thing able to drift.

━━ NOT AN ADMIN ACTION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No ``AdminAuditLog`` row is written and none should be. That log is an append-only
record of PLATFORM-STAFF decisions, and its ``actor`` is a platform staff member; a row
naming a restaurant owner as the actor of an admin action would corrupt what the log
means and would put a tenant-initiated event into the operator's activity strip. The
consumed invitation, with its timestamp, is the durable record of what happened here.
A future unified Activity surface may project that evidence — separately.

━━ AND NO EXTERNAL I/O, ANYWHERE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No SMS, no email, no ``Notification`` (MongoDB), no legacy ``save_action``, no network
call of any kind — which is why this module does NOT reuse ``change_password`` or
``reset_password``, both of which perform synchronous MongoDB I/O. The transaction
below holds the ``Restaurant`` row, and PR #306 measured what holding that row across
unreachable-MongoDB I/O costs: a lifecycle transition waiting on it holds the EXCLUSIVE
order-admission advisory lock while it waits, so every diner order at the restaurant
queues behind it.

━━ LOCK ORDER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Restaurant -> RestaurantOnboarding -> head OwnerInvitation -> owner User -> UserOtp

A tail extension of the documented global order. ``Restaurant -> RestaurantOnboarding
-> OwnerInvitation`` is exactly what ``onboarding_invitations`` already takes;
``UserOtp`` is locked by nothing except ``OtpManager.verify_otp``; and ``User`` is
taken after ``Restaurant``, the same direction as the lifecycle transition (which holds
``Restaurant`` and then takes ``FOR KEY SHARE`` on ``User`` for its audit FK insert).
The one service that locks ``User`` FIRST — ``onboarding_creation`` — goes on to INSERT
a ``Restaurant`` and never waits on an existing one, so it cannot close a cycle.

**NO ADMISSION ADVISORY LOCK.** The order path reads no invitation, OTP or
customer-access fact, and taking that lock AFTER the ``Restaurant`` row would invert the
lifecycle transition's ``advisory -> Restaurant`` order — the exact cycle
``restaurants_app.controllers.admission_lock`` documents. Take it first or not at all,
and this transaction does not need it at all.

**AND THE MEMBERSHIP BARRIER IS CONSUMED, NOT RE-BUILT.** Taking the ``Restaurant`` row
is what PR #306 gave the customer plane: every production ``RestaurantEmployee`` writer
now takes the same row, so a role removal, deactivation, soft-delete, insert or
REACTIVATION cannot interleave between ``assert_owner_consistency`` below and the
invitation being consumed.

ONE MEASURED CONSEQUENCE OF THE ``User`` LOCK, stated rather than discovered later: while
this transaction runs, any INSERT carrying a foreign key to the owner's ``users`` row
waits, because PostgreSQL's referential integrity takes ``FOR KEY SHARE`` on the parent.
The realistic case is that owner logging in concurrently (``RefreshToken.for_user``
INSERTs an ``OutstandingToken``). The wait is bounded by this transaction, which performs
no I/O at all — which is precisely why the password hash is computed before it starts.

━━ TWO DIFFERENT TRANSACTION SEMANTICS, ON PURPOSE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A WRONG OTP is a failed security attempt, and its evidence must SURVIVE. So the
failed-verification path does not raise: it increments the counters and returns a
result, and the transaction COMMITS. Raising out of ``transaction.atomic()`` would let
an attacker erase their own attempt count by definition.

A FAILURE AFTER A CORRECT OTP is the opposite. If the invitation save, the identity
save or the token mint fails, EVERYTHING rolls back, including the OTP's consumption —
so a legitimate claimant does not lose a valid second factor to a server-side database
error, and can simply retry with the same code.
"""
import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from django.db import transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
)
from misc_app.controllers.msisdn import MsisdnError, normalise_msisdn
from platform_admin_app import sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    OwnerConsistencyError, assert_owner_consistency,
)
from platform_admin_app.onboarding_reads import select_head_invitation
from restaurants_app.models import Restaurant
from users_app import customer_access
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from platform_admin_app.models import OwnerInvitation

logger = logging.getLogger(__name__)

# The OTP purpose this transaction will accept, and the ONLY one. Not in
# `CUSTOMER_AUTH_OTP_PURPOSES` — those are the two flows that end in a customer session
# or password and are refused to a pending identity, which is the one identity this
# flow exists to reach.
OWNER_CLAIM_OTP_PURPOSE = 'owner-claim'

# Log-only reason codes, exactly as in `owner_claim`. NONE of these reaches a response
# body: the endpoint renders ONE sentence for every one of them, because a caller who
# could tell "unknown token" from "right token, wrong tenant state" would learn which
# guess was closest and would learn facts about a restaurant they have no relationship
# with.
NO_TOKEN = 'no_token'
UNKNOWN_TOKEN = 'unknown_token'
RESTAURANT_GONE = 'restaurant_gone'
ONBOARDING_NOT_TRACKED = 'onboarding_not_tracked'
NOT_ADMIN_CREATED = 'not_admin_created'
NOT_THE_HEAD_INVITATION = 'not_the_head_invitation'
TOKEN_DOES_NOT_MATCH_HEAD = 'token_does_not_match_head'
INVITATION_RESOLVED = 'invitation_resolved'
INVITATION_EXPIRED = 'invitation_expired'
INVITATION_VERIFICATION_LOCKED = 'invitation_verification_locked'
OWNER_MISSING = 'owner_missing'
INVITED_USER_NOT_OWNER = 'invited_user_not_owner'
OWNER_RELATIONSHIP_INCONSISTENT = 'owner_relationship_inconsistent'
OWNER_INACTIVE = 'owner_inactive'
OWNER_NOT_RESTAURANT_USER = 'owner_not_restaurant_user'
OWNER_PHONE_NOT_CANONICAL = 'owner_phone_not_canonical'
# The only outstanding owner-claim factor for this identity was delivered to a
# destination that is no longer theirs. Its OWN code, and refused BEFORE verification,
# so it costs no attempt budget — see `_refuse_stale_destination`.
OTP_DESTINATION_STALE = 'otp_destination_stale'
# The identity's access state under the lock disagrees with the request that was built
# against the pre-lock snapshot: a password was supplied for an established owner, or
# none was supplied for a pending one. Generic refusal — the world moved, and the
# caller's remedy is to start again.
CREDENTIAL_REQUIREMENT_CHANGED = 'credential_requirement_changed'
INVALID_OTP = 'invalid_otp'


class RedemptionRefused(Exception):
    """
    This redemption cannot proceed.

    ONE exception type for every reason, mirroring ``owner_claim.ClaimRefused`` and for
    the same reason: the caller is anonymous, and a per-reason type invites a per-reason
    response. ``code`` is for the server log only.

    NO ``details`` MAPPING AT ALL — not an empty one a later change could start filling.
    The other domains in this app pass UUIDs in ``details`` because their callers are
    authenticated administrators; this caller holds a guess.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RedemptionResult:
    """
    A COMPLETED owner claim.

    ``credential_established`` records which of the two transitions ran: True for a
    brand-new owner whose password and access state were established, False for an
    established owner who claimed an additional restaurant and whose identity was not
    modified in any way. It is STATED by the operation rather than re-derived
    afterwards — a moment later the two are indistinguishable.

    ``tokens`` is the ``RefreshToken`` from the one sanctioned customer mint. It is
    rendered once into the HTTP response and stored nowhere: JWT plaintext is not
    recoverable state and must not become so.

    Carries no claim token and no token hash, by construction — this object is passed to
    a view and is freely repr-able in a traceback.
    """

    restaurant: 'Restaurant'
    invitation: 'OwnerInvitation'
    owner: 'User'
    credential_established: bool
    tokens: object


@dataclass(frozen=True)
class RedemptionAttemptFailed:
    """
    A wrong owner-claim OTP, with its attempt evidence already committed.

    RETURNED RATHER THAN RAISED, and that is the whole design of the failure path: an
    exception escaping ``transaction.atomic()`` rolls the block back, which would erase
    the very counters the attempt was supposed to increment. So the service returns this,
    the transaction commits, and the ENDPOINT renders the generic refusal afterwards.

    ``remaining`` is for the server log — never a response. Telling an anonymous caller
    "two guesses left" reports progress on their own attack.
    """

    remaining: int


def _hash(raw_token: str) -> str:
    """
    The claim token's stored form.

    ``platform_admin_app.sessions.hash_token`` — the SAME primitive the invitation was
    minted with by ``mint_owner_invitation`` and the same one Step 2F.1 resolves with.
    There is exactly one hashing function for this credential; a second implementation
    would silently stop matching the day either one changed.
    """
    return sessions.hash_token(raw_token)


def _lock_owner(restaurant) -> 'User':
    """
    The restaurant's CURRENT owner of record, re-read and row-locked.

    ``of=('self',)`` with no ``select_related``, so this locks the ``users`` row and
    nothing else. An over-broad ``select_for_update`` over a join locks every row in the
    join on PostgreSQL — that is how the delegation-redemption ABBA cycle happened
    (PR-E) — and the explicit ``of=`` also stops a future ``select_related`` from
    silently widening it.

    THE LOCK IS WHAT MAKES THE FACTOR CHECK AUTHORITATIVE. The phone this row carries is
    compared against the destination the OTP was actually delivered to, and holding the
    row means that comparison cannot be invalidated by a concurrent write between the
    check and the commit.

    ELIGIBILITY IS CHECKED, NEVER REPAIRED. An inactive account or one that has become
    platform staff is refused; reactivating an account or changing its plane are separate
    decisions with their own actor and reason, and a claim flow is not the place to make
    either.
    """
    if restaurant.owner_id is None:
        raise RedemptionRefused(OWNER_MISSING)

    owner = (
        User.objects.select_for_update(of=('self',))
        .filter(pk=restaurant.owner_id)
        .first()
    )
    if owner is None:
        raise RedemptionRefused(OWNER_MISSING)
    if not owner.is_active:
        raise RedemptionRefused(OWNER_INACTIVE)
    if owner.account_type != ACCOUNT_TYPE_RESTAURANT_USER:
        raise RedemptionRefused(OWNER_NOT_RESTAURANT_USER)
    return owner


def _canonical_phone(owner) -> str:
    """
    The owner's stored MSISDN, proved already canonical.

    Compared against the STORED value rather than merely parsed: ``+256772000000``
    parses to ``256772000000`` but is not what the row holds, and the OTP destination
    binding is an exact match against what was actually stored on the ``UserOtp`` row.
    A non-canonical stored value is reachable — the ``users_app/0008`` backfill
    deliberately skips four buckets, and ``mode=existing`` attaches such an account
    without modifying it.
    """
    stored = (owner.phone_number or '').strip()
    if not stored:
        raise RedemptionRefused(OWNER_PHONE_NOT_CANONICAL)
    try:
        canonical = normalise_msisdn(stored)
    except MsisdnError:
        raise RedemptionRefused(OWNER_PHONE_NOT_CANONICAL)
    if canonical != owner.phone_number:
        raise RedemptionRefused(OWNER_PHONE_NOT_CANONICAL)
    return canonical


def _authoritative_head(onboarding, restaurant, token_hash, now):
    """
    The head invitation, LOCKED, proved to be the one this raw token names.

    TWO CHECKS, AND BOTH ARE NEEDED.

    ``select_head_invitation`` is THE canonical definition of "the current invitation",
    shared with the Admin read and with Step 2E's reissue and cancel. Redemption asks it
    rather than doing ``OwnerInvitation.objects.get(token_hash=…)`` precisely because
    Step 2E exists to invalidate a credential between the challenge and the redemption:
    a superseded or cancelled token would still be found by a bare hash lookup, and
    consuming whatever came back would make reissue and cancel decorative.

    The hash comparison then proves the caller holds THIS head's token rather than some
    other invitation's. ``hmac.compare_digest`` because it is a credential-derived
    comparison and constant time costs nothing here.

    ``now`` is the caller's ONE captured instant, so the expiry decision and the record
    of it cannot straddle the boundary.
    """
    head = select_head_invitation(
        onboarding, restaurant, now=now, for_update=True,
    )
    invitation = head.invitation
    if invitation is None:
        raise RedemptionRefused(NOT_THE_HEAD_INVITATION)
    if not hmac.compare_digest(invitation.token_hash, token_hash):
        # The token names an invitation that is NOT what this onboarding currently
        # presents — superseded, cancelled behind a newer head, or from another tenant
        # entirely. All one refusal.
        raise RedemptionRefused(TOKEN_DOES_NOT_MATCH_HEAD)

    # State re-checked on the LOCKED row rather than trusted from any earlier read.
    if invitation.is_resolved:
        raise RedemptionRefused(INVITATION_RESOLVED)
    if invitation.expires_at <= now:
        raise RedemptionRefused(INVITATION_EXPIRED)
    if invitation.is_verification_locked:
        # The guess budget is spent. Refused BEFORE verification, so a locked credential
        # cannot even be used to probe whether a code is right.
        raise RedemptionRefused(INVITATION_VERIFICATION_LOCKED)
    return invitation


def _refuse_stale_destination(owner, canonical_phone, now):
    """
    Refuse — WITHOUT spending attempt budget — when the identity's only outstanding
    owner-claim factor went somewhere that is no longer their number.

    ━━ WHY THIS IS NOT JUST "A WRONG CODE" ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    The destination binding already makes such a code unusable: it narrows the locked
    query, so the stale row is never selected and verification returns invalid. But
    routing that through the wrong-code path would spend one of five guesses on a
    situation the claimant did not cause and cannot see — and five of them would lock a
    perfectly good credential because somebody else changed a phone number.

    ━━ IT IS NOT AN ORACLE, AND IT IS NOT PROBEABLE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    The caller's response is byte-identical either way; only the counter differs. And
    the condition is decided ENTIRELY from server state — an attacker cannot create a
    live owner-claim row bound to a number the owner no longer has, because issuing one
    requires the challenge endpoint, which always binds to the CURRENT canonical phone.

    ━━ THE "AND NO CURRENT ONE" CLAUSE IS LOAD-BEARING ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ``make_otp`` deletes prior rows by ``(user, msisdn)``, so a fresh challenge to a NEW
    number does not remove the row bound to the old one — both are live. Refusing merely
    because a stale row EXISTS would then block an owner who had already done the right
    thing and re-challenged. The refusal is therefore narrow: something is outstanding,
    and none of it went to where it should go now.
    """
    live = UserOtp.objects.filter(
        user=owner,
        purpose=OWNER_CLAIM_OTP_PURPOSE,
        consumed_at__isnull=True,
        expiry_time__gte=now,
    )
    if live.filter(msisdn=canonical_phone).exists():
        return
    if live.exists():
        raise RedemptionRefused(OTP_DESTINATION_STALE)


def _record_failed_attempt(invitation) -> RedemptionAttemptFailed:
    """
    Count one failed owner-claim verification against the CREDENTIAL.

    Capped at ``OWNER_CLAIM_MAX_FAILED_ATTEMPTS`` so a sixth attempt cannot push the
    counter past the policy — and so the value can never violate
    ``owner_invitation_claim_attempts_bounded``, which is the database backstop for
    exactly this.

    The invitation row is already locked by ``_authoritative_head``, so two concurrent
    wrong attempts serialize and neither increment can be lost. ONE COLUMN is written:
    ``save()`` without ``update_fields`` would write back every field of an instance read
    before the lock decisions were made.
    """
    invitation.claim_failed_attempts = min(
        invitation.claim_failed_attempts + 1, OWNER_CLAIM_MAX_FAILED_ATTEMPTS,
    )
    invitation.save(update_fields=['claim_failed_attempts'])
    return RedemptionAttemptFailed(
        remaining=max(
            0, OWNER_CLAIM_MAX_FAILED_ATTEMPTS - invitation.claim_failed_attempts,
        ),
    )


def redeem_owner_claim(
    *, raw_token: Optional[str], otp: Optional[str],
    encoded_password: Optional[str] = None,
):
    """
    Redeem an owner claim. Returns ``RedemptionResult`` or ``RedemptionAttemptFailed``.

    Raises ``RedemptionRefused`` for every unclaimable state; a WRONG OTP is returned
    rather than raised, so its attempt evidence commits (see the module docstring).

    ``encoded_password`` is an ALREADY-HASHED Django password string, or ``None``. It is
    hashed by the caller OUTSIDE this transaction on purpose: PBKDF2 at Django 5.2's
    1,000,000 iterations is ~258ms of CPU, and this transaction holds the ``Restaurant``
    row, which is simultaneously the ownership/membership barrier and a row the lifecycle
    transition waits on while holding the exclusive order-admission advisory lock.
    Password hashing needs no tenant serialization; parking that lock for a quarter of a
    second per claim would stall every diner order at the restaurant for no benefit.

    It must be supplied for an identity that needs credential setup and must be absent
    otherwise — the caller establishes that against the pre-lock snapshot, and this
    function re-checks it against the LOCKED identity and refuses a disagreement rather
    than guessing.
    """
    if not raw_token or not isinstance(raw_token, str) or not raw_token.strip():
        raise RedemptionRefused(NO_TOKEN)
    token_hash = _hash(raw_token.strip())

    # PRE-LOCK DISCOVERY ONLY. A read-only hash lookup to learn WHICH restaurant to
    # lock — it grants no authority and nothing it returns is trusted. Every fact is
    # re-queried under the lock below, including this token's identity.
    #
    # `.values_list()` rather than fetching instances, so there is no pre-lock model
    # object around to be accidentally acted on later.
    target = (
        RestaurantOnboarding.objects
        .filter(owner_invitations__token_hash=token_hash)
        .values_list('restaurant_id', flat=True)
        .first()
    )
    if target is None:
        # Indistinguishable from every other refusal. The database stores only hashes,
        # so "no such token" is genuinely all the server knows — and no lock is taken,
        # so an unknown token cannot be used to probe for lock contention either.
        raise RedemptionRefused(UNKNOWN_TOKEN)

    # ONE captured instant for the whole decision: the expiry comparison, the
    # invitation's `consumed_at`, and therefore `owner_control.evidence_at`.
    now = timezone.now()

    with transaction.atomic():
        # 1. THE SERIALIZATION POINT. `.filter(pk=…)` with no `select_related`, so this
        #    locks the `restaurants` row and nothing else.
        restaurant = (
            Restaurant.objects.select_for_update().filter(pk=target).first()
        )
        if restaurant is None or restaurant.deleted:
            raise RedemptionRefused(RESTAURANT_GONE)

        # 2. The onboarding row, locked. Provenance re-checked here: a legacy-adopted
        #    tenant never entered Dinify through a claim flow and has no credential to
        #    redeem, and converting provenance to make one possible would replace a true
        #    statement about the tenant's origin with a false one.
        onboarding = (
            RestaurantOnboarding.objects
            .select_for_update(of=('self',))
            .filter(restaurant=restaurant)
            .first()
        )
        if onboarding is None:
            raise RedemptionRefused(ONBOARDING_NOT_TRACKED)
        if onboarding.source != ONBOARDING_SOURCE_ADMIN_CREATED:
            raise RedemptionRefused(NOT_ADMIN_CREATED)

        # 3. The head invitation, locked, and proved to be this token's.
        invitation = _authoritative_head(onboarding, restaurant, token_hash, now)

        # 4. The owner, locked. Taken BEFORE the consistency assertion so the row the
        #    assertion agrees with cannot move underneath the rest of the transaction.
        owner = _lock_owner(restaurant)

        # 5. The invitation must name the person who owns this restaurant NOW. An
        #    invitation instructs one specific person to take control; if ownership has
        #    moved since it was minted, that person is no longer the one it belongs to.
        if invitation.invited_user_id != owner.pk:
            raise RedemptionRefused(INVITED_USER_NOT_OWNER)

        # 6. ...and the owner of record must agree with the owner AUTHORITY, which is
        #    what the customer plane actually resolves permissions from. Establishing
        #    control while the two answers disagree would grant authority on whichever
        #    answer happened to be read. Validation only — a drifted tenant is refused
        #    and left exactly as it was for a human to resolve.
        #
        #    THIS IS THE ASSERTION PR #306 EXISTS TO PROTECT: it reads a snapshot, and
        #    the `Restaurant` row taken in step 1 is what stops a customer-plane
        #    membership write from committing between here and the consume below.
        try:
            assert_owner_consistency(restaurant)
        except OwnerConsistencyError:
            raise RedemptionRefused(OWNER_RELATIONSHIP_INCONSISTENT)

        # 7. The destination the second factor must have been delivered to.
        canonical_phone = _canonical_phone(owner)

        # 8. Which transition this is, decided from the LOCKED identity — never from the
        #    pre-lock snapshot the request was shaped against. A disagreement is a
        #    generic refusal rather than a guess: supplying a password for an established
        #    owner would rewrite a credential this operation has no business touching,
        #    and having none for a pending owner would leave them established with an
        #    unusable password.
        needs_credential = (
            owner.customer_access_state == CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM
        )
        if needs_credential != (encoded_password is not None):
            raise RedemptionRefused(CREDENTIAL_REQUIREMENT_CHANGED)

        # 9. A FACTOR DELIVERED SOMEWHERE ELSE is refused before it can cost anything.
        #    The binding below would reject it anyway — this is about not charging the
        #    claimant an attempt for a phone change they did not make. See the helper.
        _refuse_stale_destination(owner, canonical_phone, now)

        # 10. THE SECOND FACTOR, bound three ways: to this identity, to the `owner-claim`
        #    purpose, and to the destination the code was actually delivered to.
        #
        #    The bindings NARROW the locked query, so a login or reset code for the same
        #    identity is not merely rejected — it is never selected, and its own attempt
        #    counter is untouched. Under `ENV=dev` every code is `1234`, so without the
        #    purpose binding a login code would satisfy this by construction.
        #
        #    `verify_otp` opens its own `transaction.atomic()`, which nests here as a
        #    savepoint: its consumption of a correct code therefore rolls back with
        #    everything else if a later stage fails, and its wrong-guess increment
        #    commits with the invitation counter below.
        verification = OtpManager().verify_otp(
            user_id=str(owner.pk),
            otp=otp,
            expected_purpose=OWNER_CLAIM_OTP_PURPOSE,
            expected_msisdn=canonical_phone,
        )
        if not verification['data']['valid']:
            # NOT RAISED. An exception here would roll back the very increments this
            # branch exists to record, letting an attacker erase their own attempt count
            # by definition. The transaction commits and the caller renders the refusal.
            return _record_failed_attempt(invitation)

        # 11. CONSUME THE CREDENTIAL. Single-use, stamped with the shared `now`, so
        #     `owner_control.evidence_at` is exactly this instant. ONE COLUMN.
        invitation.consumed_at = now
        invitation.save(update_fields=['consumed_at'])

        # 12. THE IDENTITY TRANSITION — for a brand-new owner only.
        if needs_credential:
            # The pre-computed hash is ASSIGNED rather than re-derived: calling
            # `set_password` here would redo the ~258ms hash under the `Restaurant` lock,
            # which is the one thing step 6 of the plan exists to avoid. The result is
            # byte-equivalent — `set_password` is exactly `self.password =
            # make_password(raw)` plus stashing the raw value for the no-op
            # `password_changed` hook (no configured validator implements it; a test
            # asserts that, so adding one that does fails the build).
            owner.password = encoded_password
            owner.customer_access_state = CUSTOMER_ACCESS_ESTABLISHED
            # The account no longer needs to be told to choose a password: it has just
            # chosen one. Never the proof of claim, and never read as such — but leaving
            # it True would send a freshly-claimed owner straight into a change-password
            # prompt for a credential they set thirty seconds ago.
            owner.prompt_password_change = False
            # THREE COLUMNS, NAMED. A bare `save()` would write back every field of an
            # instance loaded before the lock decisions were made.
            owner.save(update_fields=[
                'password', 'customer_access_state', 'prompt_password_change',
            ])
        # An ESTABLISHED owner is not modified at all — see the module docstring. There
        # is deliberately no `else` branch to read.

        # 13. THE SESSION, through the one sanctioned mint. For a pending owner this is
        #     reached only after the transition above has been both applied in memory and
        #     persisted, so the chokepoint's own refusal cannot fire — and if a future
        #     change reordered these, it would fire loudly rather than mint quietly.
        #
        #     `RefreshToken.for_user` INSERTs an `OutstandingToken`, so token minting is
        #     a real database write inside this transaction: a persistence failure here
        #     rolls the whole redemption back rather than leaving a consumed invitation
        #     with no session.
        tokens = customer_access.issue_customer_tokens(owner)

    logger.info(
        'owner claim redeemed (credential_established=%s)', needs_credential,
    )
    return RedemptionResult(
        restaurant=restaurant,
        invitation=invitation,
        owner=owner,
        credential_established=needs_credential,
        tokens=tokens,
    )
