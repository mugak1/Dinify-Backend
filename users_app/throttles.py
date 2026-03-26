"""
Rate limiting for auth-sensitive endpoints.

Uses DRF's built-in AnonRateThrottle keyed by client IP.
Rates are configured in settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'].
"""
from rest_framework.throttling import AnonRateThrottle


class LoginThrottle(AnonRateThrottle):
    scope = 'auth_login'


class OtpThrottle(AnonRateThrottle):
    scope = 'auth_otp'


class PasswordResetThrottle(AnonRateThrottle):
    scope = 'auth_password_reset'
