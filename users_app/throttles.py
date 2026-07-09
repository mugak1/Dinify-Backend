"""
Rate limiting for auth-sensitive endpoints.

The IP-keyed throttles below use DRF's built-in AnonRateThrottle. The
per-identifier OtpIdentifierThrottle keys on the account/phone the request
targets instead of the client IP, so resend-and-retry brute force can't be
spread across many IPs. Rates are configured in
settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'].
"""
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle

from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError


class LoginThrottle(AnonRateThrottle):
    scope = 'auth_login'


class OtpThrottle(AnonRateThrottle):
    scope = 'auth_otp'


class PasswordResetThrottle(AnonRateThrottle):
    scope = 'auth_password_reset'


class OtpIdentifierThrottle(SimpleRateThrottle):
    """
    Throttle OTP verify/resend per TARGET IDENTITY rather than per client IP.

    The per-IP OtpThrottle is trivially bypassed by rotating source IPs; keying
    on the account (authenticated user) or the phone/identifier in the request
    body caps how fast a single victim's code can be attacked regardless of how
    many IPs the attacker uses. Applied alongside (not instead of) OtpThrottle.

    Returns None (→ not throttled by this class, falls back to the per-IP
    throttle) when no identity can be derived from the request.
    """
    scope = 'auth_otp_identifier'

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = f"user:{request.user.id}"
        else:
            data = getattr(request, 'data', None)
            if not isinstance(data, dict):
                return None
            raw = (
                data.get('user')
                or data.get('identifier')
                or data.get('msisdn')
                or data.get('phone_number')
            )
            if not raw:
                return None
            raw = str(raw).strip()
            if not raw:
                return None
            # Canonicalise phone-shaped identifiers so 0.../+256.../256... share
            # one bucket; non-phone identifiers (user id, email) pass through.
            # Defensive: a malformed number must not turn throttling into a 500.
            try:
                raw = normalise_msisdn(raw)
            except MsisdnError:
                pass
            ident = f"id:{raw}"
        return self.cache_format % {'scope': self.scope, 'ident': ident}
