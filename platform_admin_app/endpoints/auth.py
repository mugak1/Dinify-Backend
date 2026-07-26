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

ATOMICITY. ``verify/`` and ``elevate/`` each do ALL of their work in ONE
``transaction.atomic()``, under row locks, and attach the raw session token to the
response only after it commits. They used to consume the second factor OUTSIDE the
transaction that minted the session and audit AFTER it committed, which meant a
recovery code could be burned or a TOTP counter advanced with no session to show for
it, a live session could exist with no successful-login audit row, and two concurrent
requests could resolve the same challenge and both mint.

LOCK ORDER. ``AdminLoginChallenge`` first, ``PlatformStaffAuth`` second. ``elevate/``
and ``lockout.register_failure`` take only the second. Nothing takes them the other
way round, so there is no deadlock cycle. Keep it that way.

DISCLOSURE. Every failure at ``login/`` returns ONE byte-identical body:
unknown user, wrong password, not platform staff, inactive, unenrolled, holding a
restaurant membership, and locked-out are indistinguishable to the caller. The
password check also runs against a dummy hash for unknown users, so response time
does not leak account existence either. The audit log carries the real reason.

LOCKOUT / BREAK-GLASS. The durable per-account counter is the real guarantee (the
throttles are per-process). With ONE administrator and a discoverable username, a low
threshold and a flat window were a denial-of-service, so ``lockout`` now escalates
progressively — and a locked account can still be recovered by password + a one-shot
RECOVERY code. ``login/`` therefore no longer refuses a locked account before the
password check: with a correct password it issues a ``recovery_only`` challenge, and
``verify/`` accepts nothing but a recovery code against it, clearing the lock on
success. An attacker who has locked the account cannot ride that path — it needs a
secret they do not hold. ``manage.py unlock_platform_admin`` is the shell equivalent.

CSRF. ``logout/`` aside, the authenticated endpoints route through
``AdminSessionAuthentication``, whose ``enforce_csrf`` already covers unsafe methods.
``login/`` and ``verify/`` are necessarily unauthenticated, so they rely on
``SameSite=Strict`` + the ``__Host-`` prefix: a cross-site POST carries neither the
challenge nor the session cookie, so it cannot drive either step.

AUDIT. Exactly one entry per request, on every path including denial — the
convention ``AdminAPIView`` documents. A failure that crosses the lockout threshold
emits the distinct ``lockout`` action INSTEAD of the ordinary failure entry, and a
break-glass unlock emits ``lockout_cleared`` INSTEAD of the ordinary success entry, so
the count stays one and neither is missable when reading the log. Ordinary denials
audit INSIDE the transaction and commit with their own failure accounting — either a
failure is both counted and recorded, or neither. The one path that must ROLL BACK
(losing a race for the challenge, having already consumed the factor) audits after the
block instead, the house pattern from ``restaurants_app.controllers.lifecycle`` and
``platform_admin_app.delegated_sessions``.
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
    ADMIN_AUTH_LOCKOUT_CLEARED,
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


class _LostChallengeRace(Exception):
    """
    Internal signal: another request spent this challenge first.

    Raised from inside the verification transaction so it UNWINDS — the second factor
    was already consumed in that transaction and must be given back. The only
    condition here that needs a rollback; everything else denies in place.
    """


def _deny(message, status=401):
    return Response({'status': status, 'message': message}, status=status)


def _request_ip(request):
    return getattr(request, 'client_ip', None)


def _request_ua(request):
    return request.META.get('HTTP_USER_AGENT', '')


def _ineligible_reason(user, auth_row):
    """
    The audit-only reason this account may not authenticate, or ''.

    Module-level because all three of login, verify and elevate re-check it — the
    account can be demoted, deactivated or given a restaurant membership between any
    two steps, or after a session was already minted.
    """
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

        # A lockout no longer short-circuits ahead of the password check. It used to,
        # which made the lock absolute — and with one administrator, anyone who
        # learned the username could hold the platform shut. A locked account with the
        # CORRECT password now gets a recovery-only challenge instead: the way out
        # requires a one-shot recovery code, which a lockout attacker does not have.
        locked = lockout.is_locked(auth_row)

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
        denial = _ineligible_reason(user, auth_row)
        if denial:
            audit.record_auth_event(
                request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
                actor=user, actor_label=username, error_code=denial,
            )
            return _deny(GENERIC_AUTH_ERROR)

        raw_token, _challenge = challenges.create_challenge(
            user, recovery_only=locked,
        )
        audit.record_auth_event(
            request, ADMIN_AUTH_CHALLENGE_ISSUED, result=RESULT_SUCCESS,
            actor=user, actor_label=username,
            reason='recovery_only (locked out)' if locked else '',
        )
        response = Response(
            {
                'status': 200,
                'message': 'Second factor required.',
                'data': {
                    'second_factor_required': True,
                    # The client needs this to prompt for the right thing; it tells a
                    # caller who already proved the password nothing it did not know.
                    'recovery_code_required': locked,
                },
            },
            status=200,
        )
        return set_challenge_cookie(response, raw_token)


class AdminVerifyView(APIView):
    """POST ``{method, code}`` against the challenge → a session."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AdminLoginThrottle]

    def post(self, request):
        method = second_factor.normalise_method(request.data.get('method'))
        code = str(request.data.get('code') or '').strip()
        raw_challenge = request.COOKIES.get(challenge_cookie_name())

        # Carried OUT of the transaction rather than raised inside it, so a denial's
        # accounting and its audit row commit together. See the module docstring.
        denial = None          # (action, result, error_code, clear_challenge_cookie)
        minted = None          # (raw_session, session, used_recovery, cleared_lock)
        actor = None
        auth_row = None

        try:
            with transaction.atomic():
                # Lock order: challenge first, then PlatformStaffAuth.
                challenge = challenges.resolve_challenge(
                    raw_challenge, for_update=True,
                )

                if challenge is None:
                    denial = (
                        ADMIN_AUTH_TOTP_FAILURE, RESULT_DENIED, 'no_challenge', True,
                    )
                else:
                    actor = challenge.user
                    auth_row = (
                        PlatformStaffAuth.objects
                        .select_for_update()
                        .filter(user=actor)
                        .first()
                    )
                    denial = self._blocked_reason(actor, auth_row, challenge)

                if denial is None:
                    # A recovery-only challenge refuses TOTP as an ordinary bad code —
                    # same body, same accounting. Built directly rather than run
                    # through second_factor so nothing decrypts on this branch.
                    if (
                        challenge.recovery_only
                        and method != second_factor.METHOD_RECOVERY
                    ):
                        verdict = second_factor.FactorVerdict(
                            False, False, 'recovery_required',
                        )
                    else:
                        verdict = second_factor.check(auth_row, method, code)

                    if not verdict.ok:
                        challenges.record_attempt(challenge)
                        triggered = lockout.register_failure(auth_row)
                        denial = (
                            ADMIN_AUTH_LOCKOUT if triggered
                            else ADMIN_AUTH_TOTP_FAILURE,
                            RESULT_DENIED if triggered else RESULT_FAILURE,
                            'locked_out' if triggered else verdict.error_code,
                            False,
                        )
                    elif not challenges.consume(challenge):
                        # Should be unreachable — the challenge is locked and was
                        # re-checked above. Kept as defence in depth: if it ever
                        # fires, someone else spent it, and the factor consumed a few
                        # lines up has to be given back.
                        raise _LostChallengeRace()
                    else:
                        cleared_lock = challenge.recovery_only
                        raw_session, session = sessions.create_session(
                            actor,
                            ip=_request_ip(request),
                            user_agent=_request_ua(request),
                        )
                        # Second factor just cleared — the session starts elevated.
                        sessions.elevate(session)
                        lockout.reset(auth_row)
                        audit.record_auth_event(
                            request,
                            self._success_action(cleared_lock, verdict.used_recovery),
                            result=RESULT_SUCCESS,
                            actor=actor, actor_label=actor.username, session=session,
                            reason=f'method={method}',
                        )
                        minted = (
                            raw_session, session, verdict.used_recovery, cleared_lock,
                        )

                if denial is not None:
                    audit.record_auth_event(
                        request, denial[0], result=denial[1],
                        actor=actor,
                        actor_label=actor.username if actor else '',
                        error_code=denial[2],
                        reason=f'method={method}',
                    )
        except _LostChallengeRace:
            # The block rolled back, so nothing was consumed. The audit is written
            # HERE, outside the aborted transaction, or it would unwind with it.
            audit.record_auth_event(
                request, ADMIN_AUTH_TOTP_FAILURE, result=RESULT_DENIED,
                actor=actor, actor_label=actor.username if actor else '',
                error_code='challenge_race', reason=f'method={method}',
            )
            return _deny(GENERIC_VERIFY_ERROR)

        if denial is not None:
            response = _deny(GENERIC_VERIFY_ERROR)
            return clear_challenge_cookie(response) if denial[3] else response

        raw_session, session, used_recovery, cleared_lock = minted
        response = Response(
            {
                'status': 200,
                'message': 'Signed in.',
                'data': {
                    'username': actor.username,
                    'expires_at': session.absolute_expiry.isoformat(),
                    'used_recovery_code': used_recovery,
                    'lockout_cleared': cleared_lock,
                    'recovery_codes_remaining': recovery.remaining(auth_row),
                },
            },
            status=200,
        )
        set_session_cookie(response, raw_session)
        return clear_challenge_cookie(response)

    @staticmethod
    def _success_action(cleared_lock, used_recovery):
        """One action per request: the most notable fact wins."""
        if cleared_lock:
            return ADMIN_AUTH_LOCKOUT_CLEARED
        if used_recovery:
            return ADMIN_AUTH_RECOVERY_CODE_USED
        return ADMIN_AUTH_LOGIN_SUCCESS

    @staticmethod
    def _blocked_reason(user, auth_row, challenge):
        """Re-checks made UNDER the locks: an audit tuple, or None to proceed."""
        ineligible = _ineligible_reason(user, auth_row)
        if ineligible:
            return (ADMIN_AUTH_LOGIN_FAILURE, RESULT_DENIED, ineligible, True)

        # A recovery-only challenge exists BECAUSE the account is locked, so a live
        # lock must not block it — that is the whole point of the path.
        if not challenge.recovery_only and lockout.is_locked(auth_row):
            return (ADMIN_AUTH_LOGIN_FAILURE, RESULT_DENIED, 'locked_out', True)
        return None


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
    throttle_classes = [AdminLoginThrottle]

    def post(self, request):
        method = second_factor.normalise_method(request.data.get('method'))
        code = str(request.data.get('code') or '').strip()
        user = request.user
        session = request.auth

        denial = None          # (action, result, error_code)
        used_recovery = False
        auth_row = None

        # No challenge on this path, so nothing here can lose a race and nothing needs
        # to roll back: every outcome commits its own accounting and audit row.
        with transaction.atomic():
            auth_row = (
                PlatformStaffAuth.objects
                .select_for_update()
                .filter(user=user)
                .first()
            )

            # Eligibility again, now UNDER the lock. ``AdminSessionAuthentication``
            # already checks account_type / is_active / active-membership before
            # dispatch, so this is not a hole being closed — it removes the window
            # between that read and the factor consumption below, and it is the only
            # place enrolment (a missing or emptied auth row) is checked on this path.
            ineligible = _ineligible_reason(user, auth_row)
            if ineligible:
                denial = (ADMIN_AUTH_LOGIN_FAILURE, RESULT_DENIED, ineligible)
            elif lockout.is_locked(auth_row):
                denial = (ADMIN_AUTH_TOTP_FAILURE, RESULT_DENIED, 'locked_out')
            else:
                verdict = second_factor.check(auth_row, method, code)
                if not verdict.ok:
                    triggered = lockout.register_failure(auth_row)
                    denial = (
                        ADMIN_AUTH_LOCKOUT if triggered else ADMIN_AUTH_TOTP_FAILURE,
                        RESULT_DENIED if triggered else RESULT_FAILURE,
                        'locked_out' if triggered else verdict.error_code,
                    )
                else:
                    used_recovery = verdict.used_recovery
                    sessions.elevate(session)
                    lockout.reset(auth_row)
                    audit.record_auth_event(
                        request,
                        ADMIN_AUTH_RECOVERY_CODE_USED if used_recovery
                        else ADMIN_AUTH_ELEVATED,
                        result=RESULT_SUCCESS,
                        actor=user, actor_label=user.username, session=session,
                        reason=f'method={method}',
                    )

            if denial is not None:
                audit.record_auth_event(
                    request, denial[0], result=denial[1],
                    actor=user, actor_label=user.username, session=session,
                    error_code=denial[2], reason=f'method={method}',
                )

        if denial is not None:
            return _deny(GENERIC_VERIFY_ERROR, status=403)

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
