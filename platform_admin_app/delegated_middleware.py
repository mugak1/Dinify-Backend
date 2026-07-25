"""
The delegated-access GATE on the customer plane.

This middleware is the outer bound of the whole feature. It runs before the view,
and everything downstream — the authenticator that binds ``request.user``, the
permission resolver that scopes to one restaurant — only ever sees requests it has
already validated and allowed.

THE LOAD-BEARING PROPERTY: a request without the ``X-Delegation-Session`` header
returns from ``process_view`` immediately, having touched nothing. The customer
plane is byte-identical to what it was for every existing caller — diners, JWT
staff, anonymous readers alike. There is exactly one entry point into the delegated
path, and it is a header this middleware reads and no one else does.

Why a middleware AND an authenticator, rather than one or the other:

* A DRF authentication class alone can be bypassed by any view that sets
  ``authentication_classes = []`` (one already does, and a future one might). A
  middleware cannot be opted out of, so the deny half belongs here.
* A middleware alone cannot bind ``request.user``, which is what all ~40 existing
  authorization gates read — so the bind half belongs in the authenticator.

Transport is header-only, following ``restaurants_app.controllers.diner_capability``:
``request.headers.get(...)``, never a query parameter, request body or cookie. Those
leak into access logs, ``Referer`` headers, caches and browser history, and the
project deliberately removed every one of them during tenant isolation. Header-only
also means there is NO CSRF surface — a cross-site form cannot set a custom header.
"""
import logging
import uuid

from django.http import JsonResponse
from django.utils.cache import patch_vary_headers

from misc_app.controllers.http import no_store
from platform_admin_app import delegated_sessions
from platform_admin_app.audit_actions import ADMIN_DELEGATION_ACTION_DENIED
from platform_admin_app.audit_actions import ADMIN_DELEGATION_ACTION_PERFORMED
from platform_admin_app.configs.delegation_scopes import SAFE_METHODS, route_rule
from platform_admin_app.middleware import client_ip_from_request
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
)

logger = logging.getLogger(__name__)

# The delegated credential. Mirrors the diner tiering: a one-time code is presented
# to the exchange (X-Delegation-Code, read by that endpoint), and the session it
# mints travels here on every subsequent request.
SESSION_HEADER = 'X-Delegation-Session'
CODE_HEADER = 'X-Delegation-Code'

# Stamped on every delegated response so the acting-as state is visible in a network
# trace even if the portal ignores the exchange payload. Not CORS-exposed on
# purpose — the readable copy is the exchange / session response body.
ACTING_AS_HEADER = 'X-Acting-As'
ACTING_AS_VALUE = 'delegation'

# Where the validated context is parked for the authenticator to pick up. Read from
# the underlying HttpRequest, which DRF's Request proxies.
CONTEXT_ATTR = 'delegation_context'


def session_token_from_request(request):
    """The delegated session token — header only, never anywhere else."""
    return request.headers.get(SESSION_HEADER)


def code_from_request(request):
    """The one-time exchange code — header only, never anywhere else."""
    return request.headers.get(CODE_HEADER)


def delegation_context(request):
    """The validated context this middleware attached, or ``None``."""
    return getattr(request, CONTEXT_ATTR, None)


def stamp_audit_context(request):
    """
    Give a delegated request the audit context ``audit.record_from_request`` expects.

    The customer plane does not install the admin ``RequestIDMiddleware`` /
    ``ClientIPMiddleware`` (they belong to the admin plane, and adding them here
    would touch every request on the plane), so without this a delegated audit row
    would silently carry no request id and no source IP. The id is ALWAYS
    server-generated and never read from a client header, so the correlation id
    cannot be forged or pinned by the caller.
    """
    request.request_id = uuid.uuid4().hex
    request.client_ip = client_ip_from_request(request)
    return request


def stamp_delegated(response):
    """
    Mark a response as delegated: never cached, varied on the credential, acting-as.

    Another tenant's data sitting in an administrator's browser must not be stored,
    and must never be keyed by a cache for a request that carried no credential.
    """
    no_store(response)
    patch_vary_headers(response, (SESSION_HEADER,))
    response[ACTING_AS_HEADER] = ACTING_AS_VALUE
    return response


def _error(status, message, code):
    """
    One shape for every refusal.

    401 means "your credential is no longer good — exchange again"; 403 means "the
    credential is fine but this is not yours to do". Split that way so the portal can
    distinguish an expired session from an out-of-scope action.
    """
    return stamp_delegated(JsonResponse(
        {'status': status, 'message': message, 'error_code': code}, status=status,
    ))


class DelegatedAccessMiddleware:
    """Resolve, allow-list and audit delegated requests; no-op for everyone else."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # New-style middleware has NO ``process_response`` hook (that was removed
        # with MIDDLEWARE_CLASSES in Django 2.0) — response work happens here, on
        # the way back out. ``process_view`` and ``process_exception`` below ARE
        # still hooks and are called by the handler.
        return self._finalize(request, self.get_response(request))

    # --- the gate --------------------------------------------------------------
    def process_view(self, request, view_func, view_args, view_kwargs):
        raw_token = session_token_from_request(request)
        if not raw_token:
            # No delegated credential presented. Nothing is read, nothing is set,
            # nothing is audited — this request is exactly as it was before PR-4b.
            return None

        # A delegated credential WAS presented, so every outcome from here on is
        # auditable and needs the request context attached — including the refusals
        # below, which happen before any session is bound.
        stamp_audit_context(request)

        # Two credentials at once is never a legitimate client. Refuse rather than
        # pick one: an invalid delegated session must NEVER fall back to staff JWT
        # (the diner path's rule), and a valid JWT must not be silently ignored.
        if request.headers.get('Authorization'):
            self._audit_denied(
                request, context=None, error_code='ambiguous_credentials', status=403,
            )
            return _error(
                403,
                'Send either a delegated session or an access token, not both.',
                'ambiguous_credentials',
            )

        context = delegated_sessions.resolve_session(raw_token)
        if context is None:
            # Unknown, ended, expired, revoked, or an administrator who is no longer
            # eligible. One generic 401 — the distinction is not the caller's to know.
            self._audit_denied(
                request, context=None, error_code='session_invalid', status=401,
            )
            return _error(
                401, 'This delegated session is no longer valid.', 'session_invalid',
            )

        route = getattr(request.resolver_match, 'route', '') or ''
        rule = route_rule(route, request.method)
        if rule is None:
            return self._deny(
                request, context,
                'not_permitted_for_delegation',
                'This is not available under a delegated session.',
            )
        if context.scope not in rule.scopes:
            return self._deny(
                request, context,
                'scope_insufficient',
                'This delegation does not carry the scope for that action.',
            )
        for kwarg, permitted in rule.kwargs_allow.items():
            if str(view_kwargs.get(kwarg, '')) not in permitted:
                return self._deny(
                    request, context,
                    'not_permitted_for_delegation',
                    'This is not available under a delegated session.',
                )

        # Only now is the request allowed to become a delegated one.
        setattr(request, CONTEXT_ATTR, context)
        return None

    # --- the record ------------------------------------------------------------
    def process_exception(self, request, exception):
        """Audit a delegated write that blew up, so a 500 still leaves a trace."""
        context = delegation_context(request)
        if context is None or request.method in SAFE_METHODS:
            return None
        self._audit_performed(
            request, context, status=500, result=RESULT_FAILURE,
            error_code=type(exception).__name__,
        )
        # Mark it recorded so ``_finalize`` does not write a second row for the same
        # request when the handler turns the exception into a response.
        setattr(request, '_delegation_audited', True)
        return None

    def _finalize(self, request, response):
        context = delegation_context(request)
        if context is None:
            return response

        if (
            request.method not in SAFE_METHODS
            and not getattr(request, '_delegation_audited', False)
        ):
            # Exactly one row per delegated write, whatever the outcome. A write the
            # GATE allowed but the app's own tenant check refused is recorded as a
            # denial, not as something performed — the log should not read as though
            # the change landed.
            if response.status_code < 400:
                self._audit_performed(
                    request, context,
                    status=response.status_code, result=RESULT_SUCCESS,
                )
            else:
                self._audit_denied(
                    request, context,
                    error_code='refused_downstream',
                    status=response.status_code,
                )

        return stamp_delegated(response)

    # --- helpers ---------------------------------------------------------------
    def _deny(self, request, context, error_code, message):
        self._audit_denied(request, context, error_code, status=403)
        return _error(403, message, error_code)

    def _audit_denied(self, request, context, error_code, status):
        try:
            self._record(
                request, context, ADMIN_DELEGATION_ACTION_DENIED,
                result=RESULT_DENIED, status=status, error_code=error_code,
            )
        except Exception:  # pragma: no cover - auditing must never mask the refusal
            logger.exception('delegated access: failed to audit a refusal')

    def _audit_performed(self, request, context, *, status, result, error_code=''):
        """
        Record a delegated write. Where PR-3's no-audit-no-action contract stops.

        The exchange path DOES hold that contract — its ``session_started`` row is
        written inside the same transaction as the grant and the session, so a failed
        audit unwinds the credential it would have described. This cannot: by the time
        the outcome is known the view's own transaction has already committed, so
        there is nothing left to unwind and raising would only turn a change that
        landed into a misleading 500. Logged loudly instead.
        """
        try:
            self._record(
                request, context, ADMIN_DELEGATION_ACTION_PERFORMED,
                result=result, status=status, error_code=error_code,
            )
        except Exception:  # pragma: no cover
            logger.exception('delegated access: failed to audit a delegated write')

    def _record(self, request, context, action, *, result, status, error_code=''):
        """
        One row, attributed to the ADMINISTRATOR — never to the restaurant's owner.

        Imported lazily so this module stays importable during app loading (it is
        referenced from ``MIDDLEWARE``, which Django resolves early).
        """
        from platform_admin_app import audit

        kwargs = dict(
            result=result,
            resource_type='DelegatedSession',
            error_code=error_code or '',
            after_state={
                'method': request.method,
                'route': getattr(request.resolver_match, 'route', '') or '',
                'status': status,
            },
        )
        if context is not None:
            kwargs.update(
                actor=context.administrator,
                resource_id=str(context.session.id),
                restaurant_id=context.grant.restaurant_id,
                delegation_id=context.grant.id,
                reason=context.grant.reason,
            )
        else:
            # No resolvable session: there is no administrator to name, and
            # request.user is anonymous at this point, so the row is honest about
            # having no actor rather than guessing one.
            kwargs['actor'] = None
        audit.record_from_request(request, action, **kwargs)


# Re-exported so tests and the exchange endpoint share one definition of the header
# names rather than repeating string literals.
__all__ = [
    'ACTING_AS_HEADER',
    'ACTING_AS_VALUE',
    'CODE_HEADER',
    'SESSION_HEADER',
    'DelegatedAccessMiddleware',
    'code_from_request',
    'delegation_context',
    'session_token_from_request',
    'stamp_delegated',
]
