"""
Admin authentication endpoints — the two-step login and its session operations.

FLOW. ``login/`` checks username + password and, on success, mints a short-lived
``AdminLoginChallenge`` delivered in its own ``__Host-`` cookie. It does NOT create a
session and reveals nothing beyond "a second factor is required". ``verify/`` takes a
TOTP or recovery code, validates it against the challenge, and only then mints the
``AdminSession``. So a session never exists in a half-authenticated state.

SECOND FACTOR. ``verify/`` and ``elevate/`` both require an explicit
``{"method": "totp"|"recovery", "code": "..."}``. The method is NOT inferred and has
no default: these endpoints used to try TOTP first and fall through to recovery, which
chained both factors through ``ADMIN_SECRET_ENCRYPTION_KEY`` and meant a lost key took
the recovery codes down with it. ``platform_admin_app.second_factor`` owns the
dispatch and the reasoning; note that this module no longer imports ``totp`` at all.

DISCLOSURE. Every failure at ``login/`` returns ONE byte-identical body:
unknown user, wrong password, not platform staff, inactive, unenrolled, holding a
restaurant membership, and locked-out are indistinguishable to the caller. The
password check also runs against a dummy hash for unknown users, so response time
does not leak account existence either. The audit log carries the real reason.

CSRF. ``logout/`` aside, the authenticated endpoints route through
``AdminSessionAuthentication``, whose ``enforce_csrf`` already covers unsafe methods.
``login/`` and ``verify/`` are necessarily unauthenticated, so they rely on
``SameSite=Strict`` + the ``__Host-`` prefix: a cross-site POST carries neither the
challenge nor the session cookie, so it cannot drive either step.

AUDIT. Exactly one entry per request, on every path including denial — the
convention ``AdminAPIView`` documents. A failure that crosses the lockout threshold
emits the distinct ``lockout`` action INSTEAD of the ordinary failure entry, so the
count stays one and the lockout is impossible to miss when reading the log.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import (
    audit,
    challenges,
    lockout,
    recovery,
    second_factor,
    sessions,
)
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_CHALLENGE_ISSUED,
    ADMIN_AUTH_ELEVATED,
    ADMIN_AUTH_LOCKOUT,
    ADMIN_AUTH_LOGIN_FAILURE,
    ADMIN_AUTH_LOGIN_SUCCESS,
    ADMIN_AUTH_LOGOUT,
    ADMIN_AUTH_RECOVERY_CODE_USED,
    ADMIN_AUTH_TOTP_FAILURE,
)
from platform_admin_app.authentication import AdminSessionAuthentication
from platform_admin_app.cookies import (
    challenge_cookie_name,
    clear_challenge_cookie,
    clear_session_cookie,
    cookie_name,
    set_challenge_cookie,
    set_session_cookie,
)
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    PlatformStaffAuth,
)
from platform_admin_app.services import has_active_membership
from platform_admin_app.throttles import (
    AdminLoginIdentifierThrottle,
    AdminLoginThrottle,
)

User = get_user_model()

# The ONLY message any authentication failure returns. Never vary it by cause.
GENERIC_AUTH_ERROR = 'Invalid credentials.'
GENERIC_VERIFY_ERROR = 'Invalid or expired verification.'


def _deny(message, status=401):
    return Response({'status': status, 'message': message}, status=status)


def _request_ip(request):
    return getattr(request, 'client_ip', None)


def _request_ua(request):
    return request.META.get('HTTP_USER_AGENT', '')


class AdminLoginView(APIView):
    """POST username + password → a second-factor challenge. Never a session."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AdminLoginThrottle, AdminLoginIdentifierThrottle]

    def post(self, request):
        username = str(request.data.get('username') or '').strip()
        password = str(request.data.get('password') or '')

        user = User.objects.filter(username=username).first()

        # Equalise timing: hashing a password against a throwaway user costs the
        # same as against a real one, so "no such account" is not measurable.
        # (The idiom Django's own ModelBackend uses.)
        if user is None:
            User().set_password(password)
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_FAILURE,
                actor_label=username, error_code='unknown_user',
            )
            return _deny(GENERIC_AUTH_ERROR)

        auth_row = PlatformStaffAuth.objects.filter(user=user).first()

        # Locked accounts short-circuit BEFORE the password check, so a lockout
        # cannot be probed away and a correct password buys nothing while locked.
        if lockout.is_locked(auth_row):
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=username, error_code='locked_out',
            )
            return _deny(GENERIC_AUTH_ERROR)

        if not user.check_password(password):
            triggered = lockout.register_failure(auth_row)
            audit.record_auth_event(
                request,
                ADMIN_AUTH_LOCKOUT if triggered else ADMIN_AUTH_LOGIN_FAILURE,
                result=RESULT_DENIED if triggered else RESULT_FAILURE,
                actor=user, actor_label=username,
                error_code='locked_out' if triggered else 'bad_password',
            )
            return _deny(GENERIC_AUTH_ERROR)

        # Password is correct. Everything below is an eligibility gate, and each
        # returns the SAME body — a correct password must not confirm that the
        # account is (or is not) an admin.
        denial = self._ineligible_reason(user, auth_row)
        if denial:
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=username, error_code=denial,
            )
            return _deny(GENERIC_AUTH_ERROR)

        raw_token, _challenge = challenges.create_challenge(user)
        audit.record_auth_event(
            request, ADMIN_AUTH_CHALLENGE_ISSUED, result=RESULT_SUCCESS,
            actor=user, actor_label=username,
        )
        response = Response(
            {
                'status': 200,
                'message': 'Second factor required.',
                'data': {'second_factor_required': True},
            },
            status=200,
        )
        return set_challenge_cookie(response, raw_token)

    @staticmethod
    def _ineligible_reason(user, auth_row):
        """The audit-only reason this account may not log in, or ''."""
        if user.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
            return 'not_platform_staff'
        if not user.is_active:
            return 'inactive'
        # The PR-1 invariant, fail-closed at the door: a platform-staff account
        # holding an active membership cannot log in anywhere until it is resolved.
        if has_active_membership(user):
            return 'active_membership'
        if auth_row is None or not auth_row.totp_secret_encrypted:
            return 'totp_not_enrolled'
        return ''


class AdminVerifyView(APIView):
    """POST ``{method, code}`` against the challenge → a session."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AdminLoginThrottle]

    def post(self, request):
        method = second_factor.normalise_method(request.data.get('method'))
        code = str(request.data.get('code') or '').strip()
        challenge = challenges.resolve_challenge(
            request.COOKIES.get(challenge_cookie_name())
        )

        if challenge is None:
            audit.record_auth_event(
                request, ADMIN_AUTH_TOTP_FAILURE, result=RESULT_DENIED,
                error_code='no_challenge',
            )
            response = _deny(GENERIC_VERIFY_ERROR)
            return clear_challenge_cookie(response)

        user = challenge.user
        auth_row = PlatformStaffAuth.objects.filter(user=user).first()

        if lockout.is_locked(auth_row):
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=user.username, error_code='locked_out',
            )
            return clear_challenge_cookie(_deny(GENERIC_VERIFY_ERROR))

        # Re-check eligibility: the account could have been demoted, deactivated or
        # given a membership in the seconds between the two steps.
        denial = AdminLoginView._ineligible_reason(user, auth_row)
        if denial:
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=user.username, error_code=denial,
            )
            return clear_challenge_cookie(_deny(GENERIC_VERIFY_ERROR))

        verdict = second_factor.check(auth_row, method, code)
        if not verdict.ok:
            challenges.record_attempt(challenge)
            triggered = lockout.register_failure(auth_row)
            audit.record_auth_event(
                request,
                ADMIN_AUTH_LOCKOUT if triggered else ADMIN_AUTH_TOTP_FAILURE,
                result=RESULT_DENIED if triggered else RESULT_FAILURE,
                actor=user, actor_label=user.username,
                error_code='locked_out' if triggered else verdict.error_code,
                reason=f'method={method}',
            )
            return _deny(GENERIC_VERIFY_ERROR)

        used_recovery = verdict.used_recovery

        # Consume the challenge and mint the session together: a spent recovery code
        # must never be lost to a half-completed login.
        with transaction.atomic():
            challenges.consume(challenge)
            raw_session, session = sessions.create_session(
                user, ip=_request_ip(request), user_agent=_request_ua(request),
            )
            # Second factor just cleared — the session starts elevated.
            sessions.elevate(session)
            lockout.reset(auth_row)

        audit.record_auth_event(
            request,
            ADMIN_AUTH_RECOVERY_CODE_USED if used_recovery else ADMIN_AUTH_LOGIN_SUCCESS,
            result=RESULT_SUCCESS,
            actor=user, actor_label=user.username, session=session,
            reason=f'method={method}',
        )

        response = Response(
            {
                'status': 200,
                'message': 'Signed in.',
                'data': {
                    'username': user.username,
                    'expires_at': session.absolute_expiry.isoformat(),
                    'used_recovery_code': used_recovery,
                    'recovery_codes_remaining': recovery.remaining(auth_row),
                },
            },
            status=200,
        )
        set_session_cookie(response, raw_session)
        return clear_challenge_cookie(response)


class AdminLogoutView(APIView):
    """
    POST → revoke the current session and clear both cookies. Idempotent.

    Deliberately ``AllowAny`` with a manual cookie read: logging out must succeed
    (200, cookies cleared) even when the session is already gone or invalid, rather
    than 401-ing and leaving a stale cookie in the browser.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        session = sessions.resolve_session(request.COOKIES.get(cookie_name()))
        if session is not None:
            sessions.revoke(session, 'logout')

        audit.record_auth_event(
            request, ADMIN_AUTH_LOGOUT, result=RESULT_SUCCESS,
            actor=session.user if session else None,
            actor_label=session.user.username if session else '',
            session=session,
        )

        response = Response({'status': 200, 'message': 'Signed out.'}, status=200)
        clear_session_cookie(response)
        return clear_challenge_cookie(response)


class AdminSessionView(APIView):
    """GET the current session identity and expiry for the SPA. No secrets."""

    authentication_classes = [AdminSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        session = request.auth
        return Response(
            {
                'status': 200,
                'message': 'ok',
                'data': {
                    'username': request.user.username,
                    'email': request.user.email,
                    'issued_at': session.issued_at.isoformat(),
                    'expires_at': session.absolute_expiry.isoformat(),
                    'elevated_at': (
                        session.elevated_at.isoformat()
                        if session.elevated_at else None
                    ),
                    'server_time': timezone.now().isoformat(),
                },
            },
            status=200,
        )


class AdminElevateView(APIView):
    """POST ``{method, code}`` on a live session → stamp ``elevated_at``."""

    authentication_classes = [AdminSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        method = second_factor.normalise_method(request.data.get('method'))
        code = str(request.data.get('code') or '').strip()
        user = request.user
        session = request.auth
        auth_row = PlatformStaffAuth.objects.filter(user=user).first()

        if lockout.is_locked(auth_row):
            audit.record_auth_event(
                request, ADMIN_AUTH_TOTP_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=user.username, session=session,
                error_code='locked_out',
            )
            return _deny(GENERIC_VERIFY_ERROR, status=403)

        verdict = second_factor.check(auth_row, method, code)
        if not verdict.ok:
            triggered = lockout.register_failure(auth_row)
            audit.record_auth_event(
                request,
                ADMIN_AUTH_LOCKOUT if triggered else ADMIN_AUTH_TOTP_FAILURE,
                result=RESULT_DENIED if triggered else RESULT_FAILURE,
                actor=user, actor_label=user.username, session=session,
                error_code='locked_out' if triggered else verdict.error_code,
                reason=f'method={method}',
            )
            return _deny(GENERIC_VERIFY_ERROR, status=403)

        used_recovery = verdict.used_recovery
        sessions.elevate(session)
        lockout.reset(auth_row)
        audit.record_auth_event(
            request,
            ADMIN_AUTH_RECOVERY_CODE_USED if used_recovery else ADMIN_AUTH_ELEVATED,
            result=RESULT_SUCCESS,
            actor=user, actor_label=user.username, session=session,
            reason=f'method={method}',
        )
        return Response(
            {
                'status': 200,
                'message': 'Elevated.',
                'data': {
                    'elevated_at': session.elevated_at.isoformat(),
                    'used_recovery_code': used_recovery,
                    'recovery_codes_remaining': recovery.remaining(auth_row),
                },
            },
            status=200,
        )
