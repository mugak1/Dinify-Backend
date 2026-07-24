"""
Request-level rate limiting for the admin authentication endpoints.

DEFENCE IN DEPTH ONLY — these are NOT the lockout. Django has no ``CACHES``
configured, so DRF throttle counters live in a per-process ``LocMemCache``: under
Apache/mod_wsgi with N daemon workers the effective ceiling is N × the rate, and
every counter resets on restart. Anything that must actually hold is enforced in
the database, on ``PlatformStaffAuth.failed_attempts`` / ``locked_until``.

What these DO buy is cheap: they blunt a flood before it reaches password hashing
or a DB write, and they cap one identity's attempt rate even when the source IP
rotates. Mirrors ``users_app/throttles.py``, which is the house pattern.
"""
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle


class AdminLoginThrottle(AnonRateThrottle):
    """Per-IP cap on admin authentication attempts."""

    scope = 'admin_login'


class AdminLoginIdentifierThrottle(SimpleRateThrottle):
    """
    Throttle admin authentication per TARGET IDENTITY rather than per client IP.

    The per-IP throttle is trivially bypassed by rotating source addresses; keying
    on the submitted username caps how fast a single account can be attacked no
    matter how many IPs are used. Applied alongside (not instead of) the IP throttle.

    Returns None — not throttled by this class, falling back to the per-IP one —
    when no identity can be derived, so a malformed body degrades rather than 500s.
    """

    scope = 'admin_login_identifier'

    def get_cache_key(self, request, view):
        data = getattr(request, 'data', None)
        if not isinstance(data, dict):
            return None
        raw = data.get('username')
        if not raw:
            return None
        raw = str(raw).strip().lower()
        if not raw:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': f'admin:{raw}'}
