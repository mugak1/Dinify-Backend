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

# --- admin.auth: authentication events (written by PR-2b's login/logout paths) ---
ADMIN_AUTH_LOGIN_SUCCESS = 'admin.auth.login_success'
ADMIN_AUTH_LOGIN_FAILURE = 'admin.auth.login_failure'
ADMIN_AUTH_LOGOUT = 'admin.auth.logout'
ADMIN_AUTH_TOTP_FAILURE = 'admin.auth.totp_failure'

# --- admin.session: session lifecycle beyond login/logout ------------------------
ADMIN_SESSION_REVOKED = 'admin.session.revoked'
