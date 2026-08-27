"""
Owner-claim token resolution — Step 2F.1, the first half of redemption.

WHAT THIS SLICE DOES, AND THE LINE IT DOES NOT CROSS
───────────────────────────────────────────────────────────────────────────────
It answers ONE question: *does this raw claim token currently name a real,
outstanding owner-claim, and who is the identity it belongs to?* On yes, the caller
sends that identity a purpose-specific OTP.

It changes NOTHING durable. No invitation is consumed, no ``customer_access_state``
moves, no password is set, no customer token is minted and no owner control is
established. All of that happens ATOMICALLY in Step 2F.2, which is not built.

WHY REDEMPTION IS SPLIT AT ALL
───────────────────────────────────────────────────────────────────────────────
Owner claim is TWO-FACTOR:

  * the high-entropy ``OwnerInvitation`` token proves POSSESSION of the credential
    Dinify issued;
  * an OTP to the invited identity's canonical phone proves CURRENT CONTROL of that
    identity.

The token alone must never establish customer access — it is a bearer credential that
was handed over out of band (operator-mediated today), and anyone who intercepted it
would otherwise become the owner. Splitting the flow means the second factor can be
delivered before anything irreversible happens.

THIS RESOLUTION IS A PREFLIGHT, NOT THE CLAIM BOUNDARY
───────────────────────────────────────────────────────────────────────────────
**It takes no lock and opens no transaction, deliberately.**

The challenge grants no durable authority, so a snapshot is sufficient for it. If the
invitation is reissued, cancelled, expires, or ownership drifts a millisecond after
this read, the only consequence is that an OTP was sent that Step 2F.2 will refuse to
honour. That is harmless.

The alternative — holding the ``Restaurant`` row across OTP delivery — is not. PR #306
established the cost precisely: ``Notification.create_notification`` writes to MongoDB
synchronously with a 2s server-selection timeout, and a lifecycle transition waiting on
that row holds the EXCLUSIVE admission advisory lock while it waits, so every diner
order at the restaurant queues behind it. **A harmless stale OTP is always preferable
to external I/O under the ownership serialization lock.**

Step 2F.2 is where the authoritative check lives: it must take the ``Restaurant`` row
(the same barrier ``restaurants_app.controllers.employee_membership_lock`` gives the
customer plane), re-read every fact below under it, and only then consume anything.
Nothing here may be trusted by it.

NO ORACLE
───────────────────────────────────────────────────────────────────────────────
The endpoint above this module is ``AllowAny``. Every refusal — unknown token,
expired, cancelled, superseded, consumed, legacy tenant, deleted restaurant, owner
moved, ownership drifted, inactive account, wrong account type, no phone — raises the
SAME exception type and the endpoint renders ONE message. The ``code`` exists for the
server log and must never reach a response body.

**NO CREDENTIAL MATERIAL LEAVES THIS MODULE.** ``ClaimRefused.details`` is empty by
construction, the result object carries no token and no hash, and neither the raw
token nor its hash is ever logged, echoed, or put into an exception.
"""
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
)
from platform_admin_app import sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED, OwnerInvitation,
)
from platform_admin_app.onboarding import (
    OwnerConsistencyError, assert_owner_consistency,
)
from platform_admin_app.onboarding_reads import select_head_invitation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from platform_admin_app.models import RestaurantOnboarding
    from restaurants_app.models import Restaurant
    from users_app.models import User

logger = logging.getLogger(__name__)

# Log-only reason codes. NONE of these reaches a response body — see the module
# docstring. They exist so an operator reading the server log can tell a stale
# credential from a drifted tenant without the endpoint becoming an oracle.
NO_TOKEN = 'no_token'
UNKNOWN_TOKEN = 'unknown_token'
ONBOARDING_NOT_TRACKED = 'onboarding_not_tracked'
NOT_ADMIN_CREATED = 'not_admin_created'
RESTAURANT_DELETED = 'restaurant_deleted'
INVITATION_RESOLVED = 'invitation_resolved'
INVITATION_EXPIRED = 'invitation_expired'
NOT_THE_HEAD_INVITATION = 'not_the_head_invitation'
INVITED_USER_MISSING = 'invited_user_missing'
INVITED_USER_NOT_OWNER = 'invited_user_not_owner'
OWNER_RELATIONSHIP_INCONSISTENT = 'owner_relationship_inconsistent'
INVITED_USER_INACTIVE = 'invited_user_inactive'
INVITED_USER_NOT_RESTAURANT_USER = 'invited_user_not_restaurant_user'
INVITED_USER_HAS_NO_PHONE = 'invited_user_has_no_phone'


class ClaimRefused(Exception):
    """
    This token does not currently name a claimable owner invitation.

    ONE exception type for every reason, because the caller is unauthenticated and a
    per-reason type invites a per-reason response. ``code`` is for the log only.

    It carries NO ``details`` mapping at all — not an empty one that a later change
    could quietly start filling. The other domains in this app pass UUIDs in
    ``details`` because their callers are authenticated administrators; this caller is
    anonymous and holds nothing but a guess.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClaimPreflight:
    """
    A resolved, currently-eligible owner claim.

    Model INSTANCES only, so a caller can act without re-querying — and deliberately
    NO ``token`` and NO ``token_hash`` field. This object is passed to a view and is
    freely repr-able in a traceback; a credential has no business being reachable that
    way, and the surest guarantee is that it was never put here.

    ``credential_setup_required`` is read from the explicit customer-access axis and
    from nothing else — never from password usability, ``prompt_password_change``,
    ``last_login`` or the invitation's age. Those are the inferences Step 2D.1 exists
    to replace.
    """

    invitation: 'OwnerInvitation'
    onboarding: 'RestaurantOnboarding'
    restaurant: 'Restaurant'
    invited_user: 'User'
    credential_setup_required: bool


def resolve_claim_token(raw_token: Optional[str]) -> ClaimPreflight:
    """
    Resolve a raw owner-claim token to its currently-eligible claim, or refuse.

    Raises ``ClaimRefused`` for every unclaimable state. Returns a ``ClaimPreflight``.

    **Pure read.** No transaction, no ``select_for_update``, no advisory lock, no
    write of any kind — see the module docstring for why that is the correct shape for
    a preflight and the wrong shape for Step 2F.2.
    """
    if not raw_token or not isinstance(raw_token, str) or not raw_token.strip():
        raise ClaimRefused(NO_TOKEN)

    # The SAME primitive the invitation was minted with. A second hash implementation
    # would silently stop matching the day either one changed; there is exactly one,
    # in `platform_admin_app.sessions`, and both creation and reissue mint through it.
    token_hash = sessions.hash_token(raw_token.strip())

    invitation = (
        OwnerInvitation.objects
        .select_related('onboarding', 'onboarding__restaurant', 'invited_user')
        .filter(token_hash=token_hash)
        .first()
    )
    if invitation is None:
        # Indistinguishable from every other refusal. The database stores only
        # hashes, so "no such token" is genuinely all the server knows.
        raise ClaimRefused(UNKNOWN_TOKEN)

    onboarding = invitation.onboarding
    if onboarding is None:
        raise ClaimRefused(ONBOARDING_NOT_TRACKED)

    # A legacy-adopted tenant never entered Dinify through a claim flow and has no
    # claim credential to redeem. Converting provenance to make a redemption possible
    # would replace a true statement about the tenant's origin with a false one.
    if onboarding.source != ONBOARDING_SOURCE_ADMIN_CREATED:
        raise ClaimRefused(NOT_ADMIN_CREATED)

    restaurant = onboarding.restaurant
    if restaurant is None or restaurant.deleted:
        raise ClaimRefused(RESTAURANT_DELETED)

    # `is_claimable` is the model's own definition and is used rather than restated:
    # unresolved AND unexpired. Split into two codes for the log only — the caller
    # cannot tell them apart.
    if invitation.is_resolved:
        raise ClaimRefused(INVITATION_RESOLVED)
    if invitation.is_expired:
        raise ClaimRefused(INVITATION_EXPIRED)

    # THE ONE DEFINITION OF "THE CURRENT INVITATION". `select_head_invitation` is what
    # the Admin read publishes and what Step 2E's reissue and cancel act on; deciding
    # here instead would let this endpoint accept a credential the rest of the system
    # considers superseded. A token that is unresolved but not the head cannot occur
    # while the one-unresolved partial index holds — it is refused anyway, because an
    # invariant enforced elsewhere is not a reason to skip the check.
    head = select_head_invitation(onboarding, restaurant)
    if head.invitation is None or head.invitation.pk != invitation.pk:
        raise ClaimRefused(NOT_THE_HEAD_INVITATION)

    invited_user = invitation.invited_user
    if invited_user is None:
        raise ClaimRefused(INVITED_USER_MISSING)

    # THE OWNER OF RECORD, re-read now rather than trusted from issuance time. An
    # invitation instructs one specific person to take control; if ownership has moved
    # since it was minted, that person is no longer the one this restaurant belongs to
    # and the credential must not be honoured.
    if restaurant.owner_id is None or invited_user.pk != restaurant.owner_id:
        raise ClaimRefused(INVITED_USER_NOT_OWNER)

    # ...and the owner of record must agree with the owner AUTHORITY. Same reasoning
    # as reissue: handing someone a claim while the two answers to "who owns this?"
    # disagree would establish authority on whichever answer happened to be read.
    # Validation only — a drifted tenant is refused and left exactly as it was.
    try:
        assert_owner_consistency(restaurant)
    except OwnerConsistencyError:
        raise ClaimRefused(OWNER_RELATIONSHIP_INCONSISTENT)

    # Account eligibility, which `assert_owner_consistency` deliberately does not ask.
    # None of these is repaired here: reactivating an account or changing its type is
    # a separate decision with its own actor and reason.
    if not invited_user.is_active:
        raise ClaimRefused(INVITED_USER_INACTIVE)
    if invited_user.account_type != ACCOUNT_TYPE_RESTAURANT_USER:
        raise ClaimRefused(INVITED_USER_NOT_RESTAURANT_USER)
    if not (invited_user.phone_number or '').strip():
        # The second factor is delivered by SMS to the canonical MSISDN. Without one
        # there is no factor to deliver, so this is a refusal rather than a silent
        # single-factor claim.
        raise ClaimRefused(INVITED_USER_HAS_NO_PHONE)

    return ClaimPreflight(
        invitation=invitation,
        onboarding=onboarding,
        restaurant=restaurant,
        invited_user=invited_user,
        credential_setup_required=(
            invited_user.customer_access_state
            == CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM
        ),
    )
