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
