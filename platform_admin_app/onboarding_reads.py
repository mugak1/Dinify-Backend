"""
The READ projection over the onboarding domain (Phase 1, Step 2C).

The third and last face of the domain, beside its invariant (``onboarding.py``) and
its writer (``onboarding_adoption.py``). Steps 2A and 2B built a truthful record of
how a restaurant entered Admin; until now nothing showed it to anybody, so the detail
endpoint still told every operator ``claim_tracked: false`` — accurate before Step 2
existed, and a lie the moment a restaurant is adopted.

It lives here rather than in ``restaurant_reads`` because what it computes is
ONBOARDING semantics, not directory presentation: what counts as evidence that an
owner controls their restaurant is a domain rule, and it belongs with the two modules
that already state the domain's other rules. ``restaurant_reads.serialize_detail``
calls it the same way it calls ``lifecycle.check_go_live_readiness`` — a thin
delegation to whoever owns the question.

━━ EVIDENCE, NOT INFERENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The whole point of this projection is what it REFUSES to conclude. Owner control is
derived from exactly two kinds of evidence — a legacy attestation naming its subject,
or a consumed ``OwnerInvitation`` — and from nothing else. It is never inferred from
``User.last_login``, ``prompt_password_change``, ``User.is_active``, an OTP row, the
owner FK existing, an owner membership existing, prior orders or portal activity.
Every one of those is easy to reach for and every one answers a different question:
they say an account exists and has been used, not that the right human is behind it.

A projection that guessed would be indistinguishable from one that knew, which is the
exact failure ``legacy_adopted`` provenance and the stored attestation SUBJECT were
designed to prevent. So the honest answers include ``not_established``, and
``not_established`` is what Baba House reads today.

━━ STALE EVIDENCE STOPS COUNTING BY ITSELF ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 2A stored the attestation's SUBJECT (``owner_control_attested_user``) instead of
leaving it implicit, precisely so that reassigning ``Restaurant.owner`` cannot silently
re-point the evidence at somebody nobody vouched for. This module is where that
decision pays off: it compares the attested subject against the CURRENT owner and
reports ``stale_attestation`` when they differ. The same rule governs invitations — a
consumed invitation proves control for the user who consumed it, and for nobody else.

━━ READ-ONLY, AND MEANT LITERALLY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing here writes, locks, opens a transaction, or records an audit entry. It never
repairs an inconsistency it finds, never stamps an expired invitation, never creates
an onboarding row for a restaurant that has none. A GET that mutated to tidy up what
it saw would make the drift undetectable next time — and would do it with no actor,
no reason and nothing recording that it happened.

CREDENTIALS ARE NEVER PROJECTED. No ``token_hash``, no raw token, no claim URL. An
invitation is reported as a STATE WORD plus the safe metadata Step 2E needs to make
it actionable — its id and its issue and expiry instants. The id is an opaque handle
an operator names when they reissue or cancel; the TOKEN is the credential, and the
two must never become interchangeable because they sit in the same object.

━━ ONE DEFINITION OF THE CURRENT INVITATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``select_head_invitation`` is that definition, and Step 2E's writers
(``onboarding_invitations``) call it rather than deciding for themselves. A writer
with its own opinion would disagree with this projection in exactly the case that
matters — an operator reads a screen showing invitation A, clicks Cancel, and the
server cancels something else.
"""
from dataclasses import dataclass
from typing import Optional

from django.utils import timezone

from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    OwnerConsistencyError,
    assert_owner_consistency,
)

# --- vocabulary --------------------------------------------------------------
#
# THREE AXES, THREE VOCABULARIES, deliberately not merged. "Is this restaurant in the
# domain at all", "do our two answers about who owns it agree" and "has anyone
# established that the owner controls it" are independent questions, and a single
# flattened status word would force a reader to guess which one it was answering.

# The relationship axis. `consistent` plus the three canonical
# `OwnerConsistencyError` codes, which are re-used rather than re-spelled so the read
# and the writer name the same drift the same way.
RELATIONSHIP_CONSISTENT = 'consistent'

# The owner-control axis.
CONTROL_NOT_ESTABLISHED = 'not_established'
CONTROL_ATTESTED = 'attested'
CONTROL_INVITATION_REDEEMED = 'invitation_redeemed'
# Evidence exists but certifies somebody who is no longer the owner. Its own state,
# never folded into `not_established`: the operator needs to see that a previous
# owner WAS vouched for, or the history looks like it never happened.
CONTROL_STALE_ATTESTATION = 'stale_attestation'

# The invitation axis (admin-created provenance only).
INVITATION_NOT_ISSUED = 'not_issued'
INVITATION_PENDING = 'pending'
INVITATION_EXPIRED = 'expired'
# The Step-2F.2 invitation-level guess budget is spent. UNRESOLVED, like `expired`: the
# credential still holds the per-onboarding slot, is still reissuable and still
# cancellable — it simply cannot be challenged or redeemed any more. Its own word rather
# than a flavour of `expired`, because the operator's remedy is the same (reissue) but
# the cause is not, and "expired" on a credential issued this morning reads as a clock
# problem when it is a security event.
INVITATION_VERIFICATION_LOCKED = 'verification_locked'
INVITATION_CONSUMED = 'consumed'
INVITATION_CANCELLED = 'cancelled'
INVITATION_SUPERSEDED = 'superseded'
# Legacy adoption has no invitation requirement AT ALL — a pre-existing restaurant did
# not enter Dinify through a claim flow. Distinct from `not_issued`, which would imply
# one is owed.
INVITATION_NOT_APPLICABLE = 'not_applicable'

# Shared by all three axes: the question does not apply because the restaurant is not
# in the onboarding domain. NOT the same as a negative answer — an untracked
# restaurant is not "inconsistent" and its owner control is not "not established";
# both are simply unasked, and saying otherwise would manufacture a problem out of
# Step 2 not having reached this tenant yet.
STATUS_UNAVAILABLE = 'unavailable'

# What kind of evidence backs an owner-control verdict. Named separately from the
# status so the portal can say *why* rather than just *whether*.
EVIDENCE_LEGACY_ATTESTATION = 'legacy_attestation'
EVIDENCE_INVITATION_REDEEMED = 'invitation_redeemed'


def _iso(value):
    return value.isoformat() if value else None


def _control(status, evidence=None, evidence_at=None):
    """One owner-control verdict. Evidence and its timestamp move together."""
    return {
        'status': status,
        'evidence': evidence,
        'evidence_at': _iso(evidence_at),
    }


def _untracked():
    """
    The projection for a restaurant with no ``RestaurantOnboarding`` row.

    Every axis is ``unavailable`` and nothing is invented. This is still the truthful
    answer for most restaurants: Step 2B ships the adoption mechanism, and adopting
    any given tenant is a separate explicit decision that may never have been made.
    """
    return {
        'tracked': False,
        'source': None,
        'recorded_at': None,
        'owner_relationship': {'status': STATUS_UNAVAILABLE},
        'owner_control': _control(STATUS_UNAVAILABLE),
        'invitation': _no_invitation(STATUS_UNAVAILABLE),
    }


def owner_relationship_status(restaurant) -> str:
    """
    Whether the owner of record and the owner authority currently agree.

    Delegates to the canonical ``assert_owner_consistency`` — the same check the
    adoption writer runs as a precondition — so the read and the write can never
    disagree about what consistent means. Its three error codes ARE the vocabulary
    here; translating them into friendlier prose would give the operator a word they
    could not search the audit log or the codebase for.

    THE EXCEPTION IS CAUGHT AND RENDERED, not propagated. A drifted tenant is exactly
    the tenant an operator most needs to open, and a 500 on its detail page would hide
    the problem behind the symptom. Inconsistency is DATA on a read path.

    Returns a status string only. The exception's ``details`` are deliberately not
    projected: they carry membership user ids that add nothing an operator can act on
    from this screen, and the narrower the payload the fewer ways it can leak.
    """
    try:
        assert_owner_consistency(restaurant)
    except OwnerConsistencyError as error:
        return error.code
    return RELATIONSHIP_CONSISTENT


def _legacy_owner_control(onboarding, restaurant):
    """
    Owner control for ``legacy_adopted`` provenance: the attestation, or nothing.

    The triple is all-or-nothing at the database level
    (``restaurant_onboarding_attestation_triple``), so a partial triple cannot exist
    to be misread here; all three are checked anyway because the cost is an ``and``
    and the alternative is a projection that trusts a constraint it never names.

    THE SUBJECT COMPARISON IS THE WHOLE POINT. An attestation certifies ONE person's
    control at ONE moment. If it named nobody, reassigning the owner would hand the
    replacement evidence nobody ever gave them — so the subject is stored, and here it
    is checked against the CURRENT owner. When they differ the evidence is still
    surfaced (with its real timestamp, because it really happened) under a status that
    refuses to count it.
    """
    attested_at = onboarding.owner_control_attested_at
    complete = (
        attested_at is not None
        and onboarding.owner_control_attested_user_id is not None
        and onboarding.owner_control_attested_by_id is not None
    )
    if not complete:
        # The expected state for a legacy adoption: the tenant is represented in
        # Admin, and nobody has vouched for its owner. Adoption deliberately does not
        # attest — see `onboarding_adoption`.
        return _control(CONTROL_NOT_ESTABLISHED)

    if onboarding.owner_control_attested_user_id == restaurant.owner_id:
        return _control(
            CONTROL_ATTESTED, EVIDENCE_LEGACY_ATTESTATION, attested_at,
        )
    return _control(
        CONTROL_STALE_ATTESTATION, EVIDENCE_LEGACY_ATTESTATION, attested_at,
    )


# --- the head invitation -----------------------------------------------------
#
# ONE DEFINITION OF "THE CURRENT INVITATION", used by the projection below AND by
# every writer in ``platform_admin_app.onboarding_invitations``.
#
# Before Step 2E only this module needed the answer, so the rules lived inline in
# ``_admin_created_evidence``. A writer that decided for itself which invitation was
# current would be a SECOND opinion, and the two would disagree in exactly the case
# that matters: an operator reads a screen showing invitation A, clicks Cancel, and
# the server cancels something else. Sharing the selector makes that disagreement
# structurally impossible rather than merely tested for.


@dataclass(frozen=True)
class HeadInvitation:
    """
    The invitation this onboarding currently presents, and how it reads.

    ``invitation`` is the ACTIONABLE credential — ``None`` only for ``not_issued`` —
    and ``status`` is the word the projection publishes for it.

    ``control_evidence`` is a SEPARATE row: the invitation consumed by the CURRENT
    owner, if one exists. **The two are computed independently and are frequently not
    the same row**, which is the whole reason the axes are not flattened. An
    invitation consumed by a PREVIOUS owner is consumed and establishes nothing; an
    invitation consumed by the current owner establishes control while a LIVE
    unresolved credential may still be outstanding beside it.

    Frozen, and carrying no token material: this object is passed to a projection and
    to an audit-state builder, and neither has any business near a credential.
    """

    invitation: Optional['OwnerInvitation']
    status: str
    control_evidence: Optional['OwnerInvitation']

    @property
    def establishes_current_owner_control(self) -> bool:
        return self.control_evidence is not None


def _redeemed_by_current_owner(onboarding, restaurant, *, for_update=False):
    """
    The most recent invitation CONSUMED BY THE CURRENT OWNER, or ``None``.

    One indexed ``LIMIT 1`` query. Ordered by ``-consumed_at`` because the question is
    "when was control last established", and a reissue-then-consume sequence can
    legitimately leave more than one consumed row for the same user; the database does
    not forbid it, so the projection picks the latest rather than pretending the
    situation cannot arise.

    A restaurant with no owner short-circuits: ``invited_user`` is a non-null FK, so
    no row could match, and asking would be a query spent proving it.
    """
    if restaurant.owner_id is None:
        return None
    queryset = OwnerInvitation.objects.filter(
        onboarding=onboarding,
        invited_user_id=restaurant.owner_id,
        consumed_at__isnull=False,
    )
    return _locked(queryset, for_update).order_by('-consumed_at').first()


def _unresolved_invitation(onboarding, *, for_update=False):
    """
    The onboarding's live invitation, or ``None``. At most one can exist.

    ``one_unresolved_owner_invitation_per_onboarding`` guarantees the uniqueness, and
    note what it does NOT include: expiry. A partial-index predicate must be immutable,
    so an EXPIRED invitation is still unresolved and still holds the slot. That is why
    expiry is decided here, against the clock, and never stored.
    """
    queryset = OwnerInvitation.objects.filter(
        onboarding=onboarding,
        consumed_at__isnull=True,
        cancelled_at__isnull=True,
        superseded_at__isnull=True,
    )
    return _locked(queryset, for_update).first()


def _latest_resolved_invitation(onboarding, *, for_update=False):
    """
    The most recent invitation that reached a terminal stamp, or ``None``.

    The fallback for "nothing is live and the current owner has not consumed one":
    something was tried and ended. Ordered by ``-issued_at``, the model's own default
    ordering, so "most recent" means the same thing here as everywhere else.
    """
    queryset = (
        OwnerInvitation.objects
        .filter(onboarding=onboarding)
        .exclude(
            consumed_at__isnull=True,
            cancelled_at__isnull=True,
            superseded_at__isnull=True,
        )
    )
    return _locked(queryset, for_update).order_by('-issued_at').first()


def _locked(queryset, for_update):
    """
    ``select_for_update(of=('self',))`` when a WRITER is asking; the plain queryset
    otherwise.

    ``of=('self',)`` is not decoration. On PostgreSQL a bare ``select_for_update()``
    locks every row the statement joins, which is how the delegation-redemption ABBA
    cycle happened (PR-E): a query that only meant to lock a grant also held a
    ``User`` row and a ``Restaurant`` row. None of the three queries above uses
    ``select_related``, so there is no join to widen today — and stating the narrow
    scope explicitly is what stops a future ``select_related`` from silently widening
    it.

    READS NEVER LOCK. ``for_update`` defaults to False so the projection's behaviour
    is unchanged and a GET still opens no transaction; Django would raise on a
    ``select_for_update`` outside one anyway, which is the failure mode this default
    exists to keep impossible.
    """
    return queryset.select_for_update(of=('self',)) if for_update else queryset


def _resolved_state(invitation) -> str:
    """The terminal stamp on an already-resolved invitation, as one word."""
    if invitation.consumed_at is not None:
        return INVITATION_CONSUMED
    if invitation.cancelled_at is not None:
        return INVITATION_CANCELLED
    return INVITATION_SUPERSEDED


def _unresolved_status(invitation, moment) -> str:
    """
    How an UNRESOLVED invitation reads. Three words, and the precedence is explicit.

        verification_locked  the guess budget is spent (Step 2F.2)
        expired              the issue window has passed
        pending              usable

    VERIFICATION LOCK COMES FIRST, and the ordering is a decision rather than an
    accident. Both states are unclaimable and both are remedied by a reissue, so an
    invitation that is simultaneously locked and expired could honestly be reported
    either way — but only one of them tells the operator that somebody sat there
    guessing. Reporting `expired` would file a security event as a clock problem.

    Neither is stored. `expires_at` is compared against ONE captured `moment` so a
    decision and the record of it cannot straddle the boundary; the lock is derived from
    the counter by the model's own `is_verification_locked`, used rather than restated so
    the read and the redemption service cannot disagree about what "locked" means.
    """
    if invitation.is_verification_locked:
        return INVITATION_VERIFICATION_LOCKED
    if invitation.expires_at <= moment:
        return INVITATION_EXPIRED
    return INVITATION_PENDING


def select_head_invitation(
    onboarding, restaurant, *, now=None, for_update=False,
) -> HeadInvitation:
    """
    THE canonical current invitation for an ``admin_created`` onboarding, plus the
    separate question of whether the CURRENT owner's control has been established.

    ━━ THE HEAD IS THE ACTIONABLE CREDENTIAL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

      1. the single UNRESOLVED row, read as ``expired`` or ``pending`` against the
         clock. A live credential is ALWAYS what an operator needs to see and act on;
      2. otherwise an invitation CONSUMED BY THE CURRENT OWNER — nothing is
         outstanding, and what happened to the credential is that it was claimed;
      3. otherwise the latest RESOLVED row — something was tried and ended;
      4. otherwise nothing has ever been issued.

    **UNRESOLVED COMES FIRST, AND THAT ORDERING IS A FIX RATHER THAN A PREFERENCE.**
    It used to come second, behind the current owner's consumed row, and that hid a
    live credential in a state Step 2E made reachable: owner A consumes an invitation,
    ownership moves to B, a credential is issued to B, ownership moves back to A. A's
    consumed row then answered "what is this onboarding's invitation?", so the read
    published a resolved id, cancellation refused the id it had published (already
    resolved) and refused B's id (stale), and reissue refused outright because A's
    control was established — leaving B's live claim credential **impossible to revoke
    through the API at all**. A credential that cannot be killed is the one outcome a
    credential-lifecycle surface must not produce.

    ━━ OWNER CONTROL IS COMPUTED INDEPENDENTLY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ``control_evidence`` is its own lookup and its own row. The two questions —
    *what is outstanding?* and *has the current owner claimed?* — are genuinely
    independent, and the bug above is exactly what answering both from one
    short-circuiting chain produced. They CAN be the same row (a settled restaurant
    whose only invitation was claimed by its owner) and they frequently are not.

    ━━ COST ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    TWO OR THREE ``LIMIT 1`` QUERIES, each answering one named question, constant in
    the size of the tenant's history. Two when anything is outstanding or the current
    owner has claimed; three only when neither is true. That is one more than the old
    chain spent on a settled claimed restaurant — and it IS the fix: the only way to
    know nothing is outstanding is to ask.

    ``now`` lets a caller pass ONE captured instant so a decision and the record of it
    cannot straddle the expiry boundary. ``for_update`` is for writers only and
    requires an open transaction — see ``_locked``.
    """
    moment = now or timezone.now()

    unresolved = _unresolved_invitation(onboarding, for_update=for_update)
    redeemed = _redeemed_by_current_owner(
        onboarding, restaurant, for_update=for_update,
    )

    if unresolved is not None:
        return HeadInvitation(unresolved, _unresolved_status(unresolved, moment), redeemed)

    if redeemed is not None:
        return HeadInvitation(redeemed, INVITATION_CONSUMED, redeemed)

    historical = _latest_resolved_invitation(onboarding, for_update=for_update)
    if historical is not None:
        return HeadInvitation(historical, _resolved_state(historical), None)

    return HeadInvitation(None, INVITATION_NOT_ISSUED, None)


def invitation_projection(head: HeadInvitation) -> dict:
    """
    One head invitation as the API publishes it: a state word plus SAFE metadata.

    THE METADATA IS THE CONCURRENCY TOKEN. ``id`` is exactly what the reissue and
    cancel endpoints require as ``expected_invitation_id``, which is the point of
    exposing it: an operator can only act on the invitation state they actually
    reviewed if the read hands them a way to name it. ``issued_at`` and ``expires_at``
    are what make ``pending`` and ``expired`` legible — "expires in two days" and
    "expired last month" are different operational situations and the status word
    alone cannot tell them apart.

    NOTHING ELSE. No ``token_hash``, no raw token, no claim URL, no delivery state,
    no password or OTP state, no invited-user identity. An invitation id is an opaque
    handle; a token is a credential; the two must not become interchangeable because
    they happen to sit in the same object.

    ``not_issued`` carries null metadata, which is honest rather than a placeholder:
    there is no row, so there is nothing to name.
    """
    invitation = head.invitation
    return {
        'status': head.status,
        'id': str(invitation.id) if invitation is not None else None,
        'issued_at': _iso(invitation.issued_at) if invitation is not None else None,
        'expires_at': _iso(invitation.expires_at) if invitation is not None else None,
    }


def _no_invitation(status: str) -> dict:
    """
    The invitation projection for a restaurant that has no ``OwnerInvitation`` axis at
    all — ``not_applicable`` (legacy adoption) or ``unavailable`` (untracked, or an
    unrecognised provenance).

    Same KEYS as a represented invitation, all null. A client should not have to
    branch on the status word to know which keys exist; a missing key and a null one
    read very differently to a portal that forgot to check.
    """
    return {'status': status, 'id': None, 'issued_at': None, 'expires_at': None}


def _admin_created_evidence(onboarding, restaurant):
    """
    Owner control and invitation state for ``admin_created`` provenance.

    Both come from ``select_head_invitation`` — the one selector the Step-2E writers
    also use — but from its two INDEPENDENT answers rather than from one chain. For a
    restaurant Dinify created, control is established by exactly one thing: an
    invitation consumed by the person who is the owner NOW.

    TWO AXES, AND THE PAIRS THAT LOOK LIKE CONTRADICTIONS ARE NOT:

      * ``invitation: consumed`` with ``owner_control: not_established`` means
        somebody consumed an invitation and is no longer the owner;
      * ``invitation: pending`` with ``owner_control: invitation_redeemed`` means the
        current owner has claimed AND a live credential is outstanding beside that —
        the state whose live credential the old single-chain ordering hid.

    The invitation axis reports what is OUTSTANDING; the control axis reports whether
    the CURRENT owner's control was established. Conflating them is how a replacement
    owner inherits evidence nobody gave them, and how a live credential becomes
    invisible.
    """
    head = select_head_invitation(onboarding, restaurant)

    if head.control_evidence is not None:
        control = _control(
            CONTROL_INVITATION_REDEEMED,
            EVIDENCE_INVITATION_REDEEMED,
            head.control_evidence.consumed_at,
        )
    else:
        # No current-owner evidence. Control is not established whatever the
        # invitation history says — a pending, expired, cancelled or superseded
        # invitation is an attempt, not a confirmation.
        control = _control(CONTROL_NOT_ESTABLISHED)

    return control, invitation_projection(head)


def _recorded_at(onboarding):
    """
    WHEN THIS RESTAURANT ENTERED THE ADMIN ONBOARDING DOMAIN. Nothing else.

    It is NOT when the business was founded, NOT when the ``Restaurant`` row was
    created (that is ``created_at`` on the tenant, already exposed as the detail's own
    ``created_at``), and NOT when the owner claimed the account — which for a legacy
    tenant is a date this platform does not know and must never appear to.

    For ``legacy_adopted`` that moment is ``adopted_at``: the real timestamp of the
    real adoption decision. For ``admin_created`` it is the onboarding row's own
    ``created_at``, since creating the record IS the entry.
    """
    if onboarding.source == ONBOARDING_SOURCE_LEGACY_ADOPTED:
        return onboarding.adopted_at
    return onboarding.created_at


def onboarding_summary(restaurant):
    """
    The onboarding projection for one restaurant. READ ONLY — see the module docstring.

    Costs ONE query for an untracked restaurant (the onboarding lookup that decides
    it). A tracked one adds the owner-membership read behind
    ``assert_owner_consistency``, plus at most three bounded invitation lookups for
    ``admin_created`` provenance. All are constant in the size of the tenant.

    ``source`` is passed through as the persisted canonical value — never translated
    into display prose here. ``legacy_adopted`` renders as "Pre-existing restaurant"
    in a portal; the API carries the machine vocabulary that the writer, the audit log
    and the database constraint all already use.
    """
    onboarding = (
        RestaurantOnboarding.objects.filter(restaurant=restaurant).first()
    )
    if onboarding is None:
        return _untracked()

    if onboarding.source == ONBOARDING_SOURCE_LEGACY_ADOPTED:
        owner_control = _legacy_owner_control(onboarding, restaurant)
        invitation = _no_invitation(INVITATION_NOT_APPLICABLE)
    elif onboarding.source == ONBOARDING_SOURCE_ADMIN_CREATED:
        owner_control, invitation = _admin_created_evidence(onboarding, restaurant)
    else:
        # UNREACHABLE behind `restaurant_onboarding_source_vocabulary`, which is a
        # database CheckConstraint and not merely `choices=`. Handled explicitly all
        # the same, and handled by REFUSING to guess: a source this module does not
        # understand gets no provenance-specific evidence rule applied to it, so it
        # can never be silently treated as legacy. The raw value is still passed
        # through in `source`, so the anomaly is visible rather than smoothed away,
        # and both derived axes fail closed.
        owner_control = _control(CONTROL_NOT_ESTABLISHED)
        invitation = _no_invitation(STATUS_UNAVAILABLE)

    return {
        'tracked': True,
        'source': onboarding.source,
        'recorded_at': _iso(_recorded_at(onboarding)),
        'owner_relationship': {'status': owner_relationship_status(restaurant)},
        'owner_control': owner_control,
        'invitation': invitation,
    }
