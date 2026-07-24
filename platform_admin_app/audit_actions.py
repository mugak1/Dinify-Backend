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
# A live session re-cleared a second factor (step-up for a sensitive action).
ADMIN_AUTH_ELEVATED = 'admin.auth.elevated'

# --- admin.auth: credential provisioning (management commands) --------------------
ADMIN_AUTH_TOTP_ENROLLED = 'admin.auth.totp_enrolled'
ADMIN_AUTH_TOTP_RESET = 'admin.auth.totp_reset'
ADMIN_AUTH_RECOVERY_CODES_GENERATED = 'admin.auth.recovery_codes_generated'

# --- admin.session: session lifecycle beyond login/logout ------------------------
ADMIN_SESSION_REVOKED = 'admin.session.revoked'
