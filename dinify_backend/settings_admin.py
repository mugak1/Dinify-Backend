"""
Admin control-plane settings.

Inherits everything from the base ``settings`` module, then overrides only what the
admin plane needs: its own root urlconf + WSGI entry point, a locked-down host list,
the two admin middlewares, the session-cookie authenticator as the sole DRF
authenticator (deny-by-default permission inherited), CSRF for cookie auth, a locked
same-origin CORS posture, and the admin session / cookie constants.

Served by ``dinify_backend.wsgi_admin`` on ``admin.dinifyapp.com`` behind a dedicated
Apache ``WSGIDaemonProcess`` (Topology A, same-origin — no CORS needed). Base
``settings.py`` is unchanged.
"""
from datetime import timedelta

from dinify_backend.settings import *  # noqa: F401,F403

# --- Routing / entry point --------------------------------------------------------
ROOT_URLCONF = 'dinify_backend.urls_admin'
WSGI_APPLICATION = 'dinify_backend.wsgi_admin.application'

# The admin plane answers only on its own host (env-overridable for local / dev).
ALLOWED_HOSTS = config(
    'ADMIN_ALLOWED_HOSTS',
    default='admin.dinifyapp.com',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()],
)

# --- Middleware -------------------------------------------------------------------
# Prepend the admin request-id / client-ip middleware to the inherited base stack.
# Prepend-only: keeping Session/Auth/Message middleware satisfies the
# admin.E408/E409/E410 system checks (django.contrib.admin is installed) and lets
# CsrfViewMiddleware manage the CSRF cookie.
#
# The delegated-access gate is REMOVED here rather than inherited. Delegation is a
# customer-plane credential; it has no meaning on the control plane, and no admin
# route is on its allowlist. Dropping it keeps the two planes provably separate
# instead of relying on the allowlist to refuse.
_DELEGATED_ACCESS_MIDDLEWARE = (
    'platform_admin_app.delegated_middleware.DelegatedAccessMiddleware'
)
MIDDLEWARE = [
    'platform_admin_app.middleware.RequestIDMiddleware',
    'platform_admin_app.middleware.ClientIPMiddleware',
    *(m for m in MIDDLEWARE if m != _DELEGATED_ACCESS_MIDDLEWARE),
]

# --- DRF: admin authenticator only, deny-by-default -------------------------------
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'platform_admin_app.authentication.AdminSessionAuthentication',
    ),
    # DEFAULT_PERMISSION_CLASSES=(IsAuthenticated,) is inherited = deny-by-default.
    # Drop the Browsable API renderer — an admin plane serves JSON only.
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
}

# --- Admin session / cookie constants ---------------------------------------------
# Read in code via getattr(settings, NAME, <same default>) so the session / cookie
# helpers also work under the base / test settings where these names are absent.
ADMIN_SESSION_COOKIE_NAME = '__Host-dinify_admin_session'
ADMIN_SESSION_ABSOLUTE_LIFETIME = timedelta(hours=8)
ADMIN_SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
ADMIN_SESSION_TOUCH_THROTTLE = timedelta(minutes=5)
# Apache is the only hop (no proxy in front), so raw X-Forwarded-For is not trusted.
ADMIN_TRUSTED_PROXY_DEPTH = 0

# --- Admin authentication constants ------------------------------------------------
# Same getattr-with-matching-default contract as above.
# The first-factor challenge: minutes, not hours — it only bridges password → TOTP.
ADMIN_CHALLENGE_COOKIE_NAME = '__Host-dinify_admin_challenge'
ADMIN_CHALLENGE_TTL = timedelta(minutes=5)
# Second-factor guesses one challenge tolerates. Its OWN setting — it used to read
# ADMIN_LOCKOUT_THRESHOLD, which coupled the per-challenge budget to the account
# lockout policy and would have widened when that threshold was raised.
ADMIN_CHALLENGE_MAX_ATTEMPTS = 5
# Durable, DB-backed lockout (the throttles are per-process and cannot be relied on).
# Threshold + progressive backoff rather than a flat window: with ONE administrator
# and a discoverable username, 5-strikes-then-15-flat-minutes let anybody who learned
# the username deny the founder access indefinitely. The window now doubles per
# failure past the threshold — 10th → 1 min, 11th → 2, 12th → 4 … 16th+ → 60 (cap) —
# and a correct password plus a one-shot recovery code clears a lock outright
# (endpoints/auth.py), which a lockout attacker cannot trigger.
ADMIN_LOCKOUT_THRESHOLD = 10
ADMIN_LOCKOUT_BACKOFF_BASE = timedelta(minutes=1)
ADMIN_LOCKOUT_BACKOFF_CAP = timedelta(minutes=60)
# How recently a session must have cleared a second factor to perform a sensitive
# action. Consumed by platform_admin_app.permissions (attached to nothing yet).
ADMIN_ELEVATION_MAX_AGE = timedelta(minutes=5)

# --- Admin delegation constants -----------------------------------------------------
# Two independent clocks: the exchange code is a HANDOFF window (the admin has minutes
# to pass it to the restaurant portal), while the delegated session it buys is a WORK
# window. Conflating them would either make the code linger or cut the work short.
ADMIN_DELEGATION_CODE_TTL = timedelta(minutes=3)
ADMIN_DELEGATION_SESSION_TTL_DEFAULT = 900     # 15 minutes
ADMIN_DELEGATION_SESSION_TTL_MAX = 3600        # 1 hour — a hard ceiling, not advice
# How many live grants one administrator may hold at once, across all restaurants.
# A runaway mint loop should hit a wall rather than fill a drawer with live codes.
ADMIN_DELEGATION_MAX_LIVE_GRANTS = 5

# --- CSRF (cookie auth is CSRF-susceptible; the SPA echoes X-CSRFToken) -----------
CSRF_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False  # the SPA must read it to echo the header
CSRF_TRUSTED_ORIGINS = config(
    'ADMIN_CSRF_TRUSTED_ORIGINS',
    default='https://admin.dinifyapp.com',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()],
)

# --- CORS: locked for the same-origin admin plane (Topology A needs none) ---------
# Hardcoded (not env-driven) so a shared dev / prod env cannot loosen the admin plane.
CORS_ORIGIN_ALLOW_ALL = False
CORS_ALLOWED_ORIGINS = []
CORS_ALLOW_CREDENTIALS = False

# NOTE: SECURE_PROXY_SSL_HEADER is deliberately OMITTED. Apache terminates TLS
# directly on this box and mod_wsgi sets wsgi.url_scheme=https, so request.is_secure()
# is already correct — there is no proxy whose X-Forwarded-Proto we could trust.
# SESSION_COOKIE_* settings are irrelevant here: the admin plane does not rely on
# django.contrib.sessions — its session is the AdminSession row, not a Django session.
