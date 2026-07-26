"""
Transactional audit for delegated TENANT writes — bringing them under the
audit-atomic half of the contract in ``platform_admin_app.audit``.

THE GAP THIS CLOSES. ``DelegatedAccessMiddleware`` audits a delegated write from
``_finalize``, i.e. after the view has returned. By then the view's own transaction has
committed, so a failed audit write has nothing left to unwind — the middleware could
only swallow the failure and log it. A delegated change could therefore land with no
record of it, which is exactly the thing PR-3's contract exists to make impossible.

THE FIX. The two customer-plane writes a delegation can reach call
``audit_delegated_write`` from INSIDE their own transaction, alongside the write.
``platform_admin_app.audit.record`` raises rather than swallowing, so a failed audit
now rolls the write back with it. Each call marks the request so the middleware does
not write a second row.

This module is imported LAZILY, from inside the function that needs it — the same
discipline ``restaurants_app.controllers.lifecycle._audit`` follows, so the customer
plane and the admin app are not bound together at module scope.

The two covered writes are ``POST api/v1/support/issues/`` and
``PUT api/v1/kitchen/menu-items/<pk>/stock/``. ``POST api/v1/delegation/end/`` is the
third non-safe route on the allowlist but is deliberately NOT covered here: it writes
admin-plane rows only, and ``delegated_sessions.end_session`` already audits its own
``session_ended`` inside its transaction.
"""
from platform_admin_app.audit_actions import ADMIN_DELEGATION_ACTION_PERFORMED
from platform_admin_app.delegated_middleware import delegation_context
from platform_admin_app.models import RESULT_SUCCESS

# The in-memory marker DelegatedSessionAuthentication puts on the principal. Spelled
# as a string for the same reason permissions_check does: no import of the admin app
# is needed to ask the question.
_DELEGATION_ATTR = 'active_delegation'

# The flag DelegatedAccessMiddleware._finalize honours to avoid double-writing.
_AUDITED_FLAG = '_delegation_audited'


def _context(request):
    """
    The delegation this request is acting under, or ``None`` for every other principal.

    Two seams, checked in order: the context the middleware parked on the request, and
    the marker the authenticator put on the principal. They agree in production; both
    are consulted so a caller holding only one of them still works.
    """
    context = delegation_context(request)
    if context is not None:
        return context
    return getattr(getattr(request, 'user', None), _DELEGATION_ATTR, None)


def _mark_audited(request):
    """
    Tell the middleware this write has already been recorded.

    Set on the UNDERLYING ``HttpRequest``: the middleware reads the Django request,
    while a DRF view holds a ``rest_framework.request.Request`` wrapper whose
    ``__setattr__`` does NOT proxy through. Setting it on the wrapper would leave the
    middleware unaware and produce two rows for one write.
    """
    setattr(getattr(request, '_request', request), _AUDITED_FLAG, True)


def audit_delegated_write(request, *, status=200, after_state=None):
    """
    Record a delegated write, in the CALLER'S transaction. Returns True if it wrote.

    A no-op — returning False — for every non-delegated principal, so ordinary staff
    writes are completely unchanged and no admin row is produced for them.

    MUST be called inside the same ``transaction.atomic()`` as the write it describes,
    and its exception MUST NOT be caught: that propagation is the whole mechanism.
    ``audit.record`` raises on failure, unwinding the write it would have described.

    The row is shaped exactly like the middleware's so the two are indistinguishable
    to a reader of the log — same action, same ``DelegatedSession`` resource, same
    attribution to the administrator rather than the restaurant's owner, and the same
    ``method`` / ``route`` / ``status`` in ``after_state``. ``status`` is the one the
    caller is about to return: the write has already succeeded by the time this runs,
    so the outcome is known even though the response object does not exist yet.
    """
    from platform_admin_app import audit

    context = _context(request)
    if context is None:
        return False

    payload = {
        'method': request.method,
        'route': getattr(getattr(request, 'resolver_match', None), 'route', '') or '',
        'status': status,
        # Distinguishes a row written here from one the middleware wrote after the
        # fact, so the log shows which delegated writes are under the contract.
        'transactional': True,
    }
    if after_state:
        payload.update(after_state)

    audit.record_from_request(
        request,
        ADMIN_DELEGATION_ACTION_PERFORMED,
        result=RESULT_SUCCESS,
        actor=context.administrator,
        resource_type='DelegatedSession',
        resource_id=str(context.session.id),
        restaurant_id=context.grant.restaurant_id,
        delegation_id=context.grant.id,
        reason=context.grant.reason,
        after_state=payload,
    )
    _mark_audited(request)
    return True
