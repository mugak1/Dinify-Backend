"""
The admin-plane audit recording service — synchronous, transactional, loud.

NO AUDIT, NO ACTION. ``record()`` runs in-process on the calling thread, inside
whatever transaction the caller has open, and does NOT catch its own exceptions.
If the audit write fails, the exception propagates: the caller's transaction rolls
back and the request fails. That is the intended behaviour — an administrative
action that cannot be attributed must not be allowed to stand.

This is the deliberate inverse of the legacy ``misc_app.controllers.save_action_log``
path, which spawns a daemon thread, wraps the write in ``except Exception`` →
``logger.error``, and therefore (a) escapes every caller's ``transaction.atomic()``
and (b) cannot report failure even in principle. That legacy path is untouched and
keeps its own consumer; the two systems coexist.

TRANSACTION INTEGRATION: ``ATOMIC_REQUESTS`` is not enabled on either settings
module, so the connection runs in autocommit and nothing is implicitly
transactional. Inside a caller's ``with transaction.atomic():`` autocommit is
suspended for that connection, so the plain ``objects.create()`` below joins the
caller's transaction with no plumbing. Callers that need the action and its audit
entry to be atomic wrap BOTH in one ``transaction.atomic()`` block — the repo-wide
idiom. Correspondingly, ``record()`` must never be wrapped in a try/except that
swallows and continues inside an atomic block: that both defeats the contract and
trips ``TransactionManagementError`` on the next query.

REDACTION: ``before_state`` / ``after_state`` accept arbitrary dicts from callers,
so scrubbing happens HERE and is not left to caller discipline — the direct lesson
of the removed ``archive_user`` signal (PR-0B), where a ``fields='__all__'``
serializer shipped password hashes to an external store.
"""
import json

from platform_admin_app.models import AdminAuditLog, AdminSession

# Case-insensitive SUBSTRINGS. A key matches if any entry appears anywhere in it,
# so 'totp' catches 'totp_secret_encrypted' and 'token' catches 'refresh_token'.
REDACTED_KEY_SUBSTRINGS = (
    'password',
    'token',
    'secret',
    'totp',
    'recovery',
    'otp',
    'csrf',
    'cookie',
    'authorization',
    'session_key',
    'api_key',
    'private',
)

REDACTED_MARKER = '[redacted]'

# Serialized cap per state field. Large payloads are replaced by an explicit
# truncation marker rather than silently stored, so the table cannot be bloated by
# one caller passing a huge dict.
MAX_STATE_BYTES = 16 * 1024
_PREVIEW_CHARS = 1024


def _is_sensitive_key(key):
    lowered = str(key).lower()
    return any(term in lowered for term in REDACTED_KEY_SUBSTRINGS)


def redact(value):
    """
    Recursively scrub sensitive values, preserving key names and structure.

    Walks nested dicts and lists. A matched key's VALUE is replaced with
    ``'[redacted]'`` — the key itself survives, so the shape of the change stays
    legible in the log without the secret being present.
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED_MARKER if _is_sensitive_key(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _prepare_state(value):
    """
    Redact, normalise to plain JSON types, and cap the size of a state payload.

    The ``json`` round-trip with ``default=str`` means a UUID / Decimal / datetime
    handed in by a caller can never blow up at write time — an audit entry must not
    fail for a serialisation detail when the whole point is that it gets recorded.
    """
    if value is None:
        return None

    encoded = json.dumps(redact(value), default=str)
    size = len(encoded.encode('utf-8'))
    if size > MAX_STATE_BYTES:
        return {
            '_truncated': True,
            '_original_bytes': size,
            '_preview': encoded[:_PREVIEW_CHARS],
        }
    return json.loads(encoded)


def record(
    *,
    action,
    result,
    actor=None,
    actor_label='',
    session=None,
    resource_type='',
    resource_id='',
    restaurant_id=None,
    delegation_id=None,
    reason='',
    before_state=None,
    after_state=None,
    error_code='',
    request_id='',
    source_ip=None,
    user_agent='',
):
    """
    Write one audit entry and return it. Keyword-only; raises on failure.

    ``action`` comes from ``platform_admin_app.audit_actions``; ``result`` from the
    ``RESULT_*`` constants on the model. Both state dicts are redacted and capped
    here, so a caller cannot bypass scrubbing by pre-building a payload.
    """
    return AdminAuditLog.objects.create(
        action=action,
        result=result,
        actor=actor,
        actor_label=actor_label or '',
        session=session,
        resource_type=resource_type or '',
        resource_id=str(resource_id) if resource_id else '',
        restaurant_id=restaurant_id,
        delegation_id=delegation_id,
        reason=reason or '',
        before_state=_prepare_state(before_state),
        after_state=_prepare_state(after_state),
        error_code=error_code or '',
        request_id=request_id or '',
        source_ip=source_ip,
        user_agent=user_agent or '',
    )


def _actor_from_request(request):
    """The authenticated User, or None — ``AnonymousUser`` is never an actor."""
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return user


def _session_from_request(request):
    """The ``AdminSession`` placed on ``request.auth`` by AdminSessionAuthentication."""
    auth = getattr(request, 'auth', None)
    return auth if isinstance(auth, AdminSession) else None


def _request_context(request):
    """
    Request-derived audit context.

    ``request_id`` is read from the attribute RequestIDMiddleware set — a
    server-generated uuid4 — and NEVER from a client header, so the correlation id
    cannot be forged or pinned by the caller. ``client_ip`` may legitimately be
    None (ClientIPMiddleware passes through a missing REMOTE_ADDR).
    """
    return {
        'request_id': getattr(request, 'request_id', '') or '',
        'source_ip': getattr(request, 'client_ip', None),
        'user_agent': request.META.get('HTTP_USER_AGENT', '') if hasattr(request, 'META') else '',
    }


def record_from_request(request, action, *, result, **kwargs):
    """
    Record an entry, filling actor / session / request context from ``request``.

    The convenience wrapper for authenticated admin endpoints. Safe on an
    unauthenticated request: actor and session simply resolve to None. Explicitly
    passed keyword arguments win over the request-derived values (a delegated write,
    for instance, names its own actor).
    """
    kwargs.setdefault('actor', _actor_from_request(request))
    kwargs.setdefault('session', _session_from_request(request))
    for key, value in _request_context(request).items():
        kwargs.setdefault(key, value)
    return record(action=action, result=result, **kwargs)


def record_auth_event(
    request, action, *, result, actor=None, actor_label='', error_code='',
):
    """
    Record an authentication event, where there may be no user and no session yet.

    For PR-2b's login / logout / failure paths: a failed login has no resolved user,
    so ``actor`` stays None and ``actor_label`` carries the submitted identifier
    verbatim for forensics. The session is never inferred here — at login time it
    does not exist yet, and on the failure path it must not be implied.
    """
    return record(
        action=action,
        result=result,
        actor=actor,
        actor_label=actor_label,
        error_code=error_code,
        **_request_context(request),
    )
