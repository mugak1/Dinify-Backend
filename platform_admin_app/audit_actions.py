"""
Action constants for ``AdminAuditLog``.

NAMING RULE: ``admin.<domain>.<verb>`` — a dotted, lowercase, namespaced string.
``<domain>`` is the area of the control plane (``auth``, ``session``, later
``delegation``, ``restaurant``, ``receivable``); ``<verb>`` is what happened, in
the past tense or as an outcome (``login_success``, ``revoked``).

Action strings live HERE and nowhere else — never inline literals at the call
site — so the set of auditable actions stays enumerable and greppable. Later PRs
append their own constants to this module (PR-4 ``admin.delegation.*``, PR-5
``admin.restaurant.lifecycle_transition``, Phase 1 ``admin.receivable.mark_paid``).

Only what exists or is imminent is defined; the outcome axis (success / failure /
denied) is orthogonal and lives on the model as ``RESULT_*``.
"""

# --- admin.auth: authentication events -------------------------------------------
# First factor accepted; a login challenge was issued but NO session exists yet.
ADMIN_AUTH_CHALLENGE_ISSUED = 'admin.auth.challenge_issued'
# Second factor accepted; the session is minted. This is the completed login.
ADMIN_AUTH_LOGIN_SUCCESS = 'admin.auth.login_success'
ADMIN_AUTH_LOGIN_FAILURE = 'admin.auth.login_failure'
ADMIN_AUTH_LOGOUT = 'admin.auth.logout'
ADMIN_AUTH_TOTP_FAILURE = 'admin.auth.totp_failure'
# A break-glass recovery code was spent — deliberately its own action rather than a
# flavour of login_success, because it should stand out when reading the log.
ADMIN_AUTH_RECOVERY_CODE_USED = 'admin.auth.recovery_code_used'
# The failed-attempt threshold was crossed and the account is now locked.
ADMIN_AUTH_LOCKOUT = 'admin.auth.lockout'
# A lockout was cleared by the break-glass path (a correct password plus a one-shot
# recovery code) or by the unlock_platform_admin command. Its own action rather than a
# flavour of login_success: clearing a lock is the interesting event, and it is
# emitted INSTEAD of the ordinary success entry so the one-entry-per-request
# convention holds.
ADMIN_AUTH_LOCKOUT_CLEARED = 'admin.auth.lockout_cleared'
# A live session re-cleared a second factor (step-up for a sensitive action).
ADMIN_AUTH_ELEVATED = 'admin.auth.elevated'

# --- admin.auth: credential provisioning (management commands) --------------------
ADMIN_AUTH_TOTP_ENROLLED = 'admin.auth.totp_enrolled'
ADMIN_AUTH_TOTP_RESET = 'admin.auth.totp_reset'
ADMIN_AUTH_RECOVERY_CODES_GENERATED = 'admin.auth.recovery_codes_generated'

# --- admin.session: session lifecycle beyond login/logout ------------------------
ADMIN_SESSION_REVOKED = 'admin.session.revoked'

# --- admin.delegation: scoped, time-boxed access into one restaurant --------------
# A grant was minted and the one-time exchange code handed to the administrator.
ADMIN_DELEGATION_MINTED = 'admin.delegation.minted'
# A mint was refused — stale elevation, a bad target, or the live-grant cap.
ADMIN_DELEGATION_MINT_DENIED = 'admin.delegation.mint_denied'
ADMIN_DELEGATION_REVOKED = 'admin.delegation.revoked'
# An earlier unredeemed grant for the same admin+restaurant was auto-revoked because
# a fresh one replaced it — its own action so it is never mistaken for a manual revoke.
ADMIN_DELEGATION_SUPERSEDED = 'admin.delegation.superseded'

# --- admin.delegation: the delegated session on the CUSTOMER plane ----------------
# A one-time exchange code was redeemed and a delegated session minted. This is the
# moment an administrator entered someone else's tenant — the row that answers
# "who reached in, when, under what authority", and the reason delegated READS are
# not audited one row per GET.
ADMIN_DELEGATION_SESSION_STARTED = 'admin.delegation.session_started'
# A redemption was refused: unknown, malformed, expired, already-used or revoked code.
ADMIN_DELEGATION_SESSION_START_DENIED = 'admin.delegation.session_start_denied'
# The administrator voluntarily left the tenant (distinct from an admin-plane revoke).
ADMIN_DELEGATION_SESSION_ENDED = 'admin.delegation.session_ended'
# A state-changing request performed under a delegated session, with its outcome.
ADMIN_DELEGATION_ACTION_PERFORMED = 'admin.delegation.action_performed'
# A delegated request refused — dead session, off-allowlist route, or a write the
# scope does not carry.
ADMIN_DELEGATION_ACTION_DENIED = 'admin.delegation.action_denied'

# --- admin.restaurant: the commercial lifecycle -----------------------------
# A restaurant moved between lifecycle states. before_state / after_state carry the
# from- and to-states; the reason is mandatory at the service.
ADMIN_RESTAURANT_LIFECYCLE_TRANSITION = 'admin.restaurant.lifecycle_transition'
# A transition was refused — an unknown target, a missing or too-short reason, a
# pair outside the matrix, or a failed precondition (go-live readiness, outstanding
# receivables). Recorded rather than silently 400'd: an attempt to suspend or
# offboard a tenant is worth knowing about even when it did not take effect.
ADMIN_RESTAURANT_TRANSITION_DENIED = 'admin.restaurant.transition_denied'

# --- admin.restaurant: platform-owned classification ------------------------
# A restaurant's platform-owned TEST CLASSIFICATION (``Restaurant.is_test``) was
# changed. Its own action rather than a flavour of an edit, because the flag decides
# whether that tenant's orders count as commerce at all: flipping it silently moves a
# restaurant into or out of every revenue figure. before_state / after_state carry the
# old and new booleans and nothing else; the reason is mandatory at the writer.
ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED = (
    'admin.restaurant.test_classification_changed'
)

# --- admin.restaurant: commercial / service configuration -------------------
# A restaurant's canonical PAYMENT TIMING (`pay_first` | `pay_after`) was set through
# the Admin control plane, and its PAYMENT COLLECTION MODE (`offline` | `psp_online`).
#
# ONE ACTION PER ENDPOINT, not one per outcome. There is deliberately no
# `*_changed` / `*_no_op` / `*_failed` trio: `AdminAuditLog.result` already carries
# the outcome axis, and splitting it into the action name would make "how often did
# anyone try to change this restaurant's payment timing?" a question you have to
# know all the spellings to ask. The verb is `_set` for the same reason — it names
# the administrative operation, which happened whether or not a row moved.
#
# before_state / after_state carry ONLY that axis's value before and after; on a
# same-state retry the two are equal, which is exactly the record wanted: the
# request was made, and nothing moved. The reason is mandatory at the endpoint.
ADMIN_RESTAURANT_PAYMENT_TIMING_SET = 'admin.restaurant.payment_timing_set'
ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET = (
    'admin.restaurant.payment_collection_mode_set'
)

# The restaurant -> Dinify software-subscription TERMS. Three actions because there
# are three genuinely different administrative decisions, not because there are three
# outcomes: `result` still carries success/failure/denied, so there is no
# `*_success` / `*_failed` / `*_no_op` variant of any of them.
#
# before_state / after_state carry ONE narrow snapshot of the terms under
# `subscription_terms` — id, amount, currency, interval, effective_from — describing
# THE CANONICAL CURRENT CONFIGURATION as the request found it and left it, never a
# historical transition replayed. On a no-op the two are equal (or both null, for an
# end retry): the request happened, nothing moved.
#
# These record PRICING TERMS. None of them charges anything, proves the owner agreed,
# or touches a payment, invoice or PSP.
ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED = (
    'admin.restaurant.subscription_terms_recorded'
)
ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED = (
    'admin.restaurant.subscription_terms_replaced'
)
ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED = (
    'admin.restaurant.subscription_terms_ended'
)

# --- admin.restaurant: the onboarding domain --------------------------------
# A pre-existing canonical Restaurant was brought into the Admin onboarding domain
# as ``legacy_adopted`` provenance. Emitted only for a NEW adoption: a re-run of the
# runbook against an already-adopted restaurant is a no-op and writes nothing here,
# because the log records decisions that changed platform state, not how many times
# a command was pasted. before_state / after_state carry the onboarding source
# before and after, and nothing else — the tenant itself is not modified.
ADMIN_RESTAURANT_ONBOARDING_ADOPTED = 'admin.restaurant.onboarding_adopted'

# A NEW canonical restaurant was created through the Admin control plane, together
# with its owner authority, its ``admin_created`` provenance and the initial
# ``OwnerInvitation`` credential.
#
# ONE ACTION FOR ONE DECISION. The request writes up to six rows — possibly a User,
# a Restaurant, a RestaurantEmployee, a RestaurantOnboarding, an OwnerInvitation and
# this entry — but it is ONE administrative decision: *create this restaurant under
# this owner and issue its initial claim credential*. Emitting a separate
# `restaurant_created` / `employee_added` / `onboarding_recorded` /
# `invitation_issued` row per table would break the plane's one-entry-per-unsafe-
# request convention and make "how many restaurants were created?" a question you
# have to know four spellings to ask.
#
# ONE ACTION PER OUTCOME TOO — `AdminAuditLog.result` carries success / failure /
# denied, so there is no `*_denied` sibling here (unlike the older
# `transition_denied`, which predates that convention being stated).
#
# `before_state` is absent: the resource did not exist. `after_state` carries the
# narrow platform facts the decision established — lifecycle state, test
# classification, provenance, the owner's UUID, whether that account was created,
# and the invitation's id and expiry. NEVER the owner's name, phone or email, and
# NEVER the raw claim token or its hash.
ADMIN_RESTAURANT_CREATED = 'admin.restaurant.created'

# --- admin.restaurant: the owner-invitation credential lifecycle -------------
# The two administrative decisions Step 2E adds over a restaurant's owner claim
# credential. Both are consequential: one MINTS a live credential and kills whatever
# was outstanding, the other terminates one.
#
# `reissued`, never `resent`. Nothing in this system delivers anything — there is no
# email, no SMS, no notification and no delivery column on the schema — so an action
# name promising a delivery event would put a claim in the permanent record that the
# platform cannot back up. What actually happens is ROTATION, and the log should say
# so.
#
# ONE ACTION PER ENDPOINT, not one per outcome: `AdminAuditLog.result` already carries
# success / failure / denied, so there is no `*_failed` / `*_no_op` / `*_denied`
# sibling for either. before_state / after_state carry ONE narrow invitation snapshot
# each — id, status, issued_at, expires_at — and NEVER the raw claim token, its hash,
# or anything about the invited owner beyond what the restaurant id already implies.
#
# On a cancellation EXACT RETRY the two states are equal and both read `cancelled`:
# the request happened, and nothing moved. Recording the original transition a second
# time would say the credential was cancelled twice.
ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED = (
    'admin.restaurant.owner_invitation_reissued'
)
ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED = (
    'admin.restaurant.owner_invitation_cancelled'
)
