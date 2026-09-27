"""
Admin session authentication — the ONLY authenticator on the admin control plane.

Scoped to the admin base view / admin settings module; it is NEVER appended to the
main ``settings.py`` ``DEFAULT_AUTHENTICATION_CLASSES``. It reads the opaque session
token from the ``__Host-`` cookie (cookie-only — never a query string or request
body), resolves it against ``AdminSession``, and returns ``(user, session)`` so the
session is available downstream as ``request.auth``.

Fail closed: any problem rejects, and — enforcing the PR-1 invariant at the door —
the account must be active platform staff with ZERO active restaurant memberships.
Because cookie auth is CSRF-susceptible and DRF marks every ``APIView`` ``csrf_exempt``
(so Django's ``CsrfViewMiddleware`` never enforces for it), this class enforces CSRF
itself for unsafe methods, mirroring DRF's own ``SessionAuthentication``.
"""
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, CSRFCheck

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import command_owner, sessions
from platform_admin_app.cookies import cookie_name
from platform_admin_app.services import has_active_membership

_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')


class AdminSessionAuthentication(BaseAuthentication):
    """Opaque-cookie authenticator for the admin control plane."""

    def authenticate(self, request):
        raw_token = request.COOKIES.get(cookie_name())
        if not raw_token:
            # No credential presented — let deny-by-default reject (401 via header).
            return None

        session = sessions.resolve_session(raw_token)
        if session is None:
            raise exceptions.AuthenticationFailed('Invalid or expired admin session.')

        user = session.user
        if user.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
            raise exceptions.AuthenticationFailed('Not a platform-staff account.')
        if not user.is_active:
            raise exceptions.AuthenticationFailed('Account is inactive.')
        if has_active_membership(user):
            # The PR-1 invariant, enforced fail-closed at authentication: a
            # platform-staff account holding any active membership cannot log in
            # anywhere until the dual role is resolved.
            raise exceptions.AuthenticationFailed(
                'Account holds an active restaurant membership.'
            )

        # D10: an unsafe request that names the session it was issued under is
        # refused when this is not that session. After eligibility, BEFORE CSRF, so a
        # stale tab is never sent into a CSRF retry that the new session's token would
        # pass. It only refuses; an absent header proceeds. See command_owner.
        if request.method not in _SAFE_METHODS:
            command_owner.enforce(request, session)

        self.enforce_csrf(request)
        sessions.touch(session)
        return (user, session)

    def enforce_csrf(self, request):
        """
        Enforce CSRF for unsafe methods, mirroring DRF ``SessionAuthentication``.

        DRF wraps every ``APIView`` with ``csrf_exempt`` so Django's middleware skips
        the check; we run it here via DRF's ``CSRFCheck`` so cookie-authenticated
        state-changing admin requests are still protected. Safe methods are exempt.
        """
        if request.method in _SAFE_METHODS:
            return

        def dummy_get_response(request):
            return None

        check = CSRFCheck(dummy_get_response)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise exceptions.PermissionDenied('CSRF Failed: %s' % reason)

    def authenticate_header(self, request):
        # Non-None ⇒ DRF returns 401 (not 403) for an unauthenticated request, giving
        # the SPA a clean "session expired, re-login" signal. The value is only a
        # scheme label (not a recognised browser scheme, so no native auth dialog);
        # the real credential is the cookie.
        return 'Cookie'
