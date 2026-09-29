"""
One server request identity per HTTP request, and the bounded order-command trace.

D15 R2. This module answers two narrow questions and no others:

1. WHICH REQUEST WROTE THIS LINE? ``RequestContextMiddleware`` gives every HTTP request
   on both planes one server-generated ID (``uuid4().hex``), echoes it as
   ``X-Request-ID`` and makes it available to logging. It is NEVER read from a client:
   an incoming ``X-Request-ID``, ``traceparent``, body field, token or intent key has no
   influence on it, so a caller cannot pin or forge the ID an operator searches for.
   The admin audit row already reads ``request.request_id``, so the audit row, the
   response header and every console line of one request carry the same value.

2. WHAT DID AN ORDER COMMAND ANSWER? The order endpoint marks three commands
   (``initiate``, ``submit``, ``retire_quote``) and attaches facts it has ALREADY
   established — a fixed outcome word, a fixed reason code, the channel, and the
   authorized order and validated intent as canonical UUIDs. The middleware writes ONE
   ``dinify.outcome`` line per marked request, AFTER the response exists, so the status
   it records is the one actually sent.

ONE ID PER HTTP REQUEST, NOT PER CHECKOUT. A checkout is several requests (initiate,
perhaps a replay of it, submit, perhaps a replay of that); each gets its own ID. What
relates them is the validated intent key and the authorized order ID on each line.

A LOG LINE IS DIAGNOSTIC EVIDENCE, NEVER A LEDGER. It is written outside any
transaction, so a rollback cannot erase it and a commit does not guarantee it; it can
be lost to buffering, a killed process or host rotation. An absent line proves nothing,
and a present one says only what the server answered. ``OrderAcceptance`` and the quote
closure row remain the only authority on what committed.

THE LATE LOG LINE. Django 5.2 logs a handled 4xx/5xx response in
``BaseHandler.get_response`` AFTER every middleware has returned — by which time the
ContextVar below has been reset. That record carries ``record.request``, so the filter
reads the ID from the request FIRST and falls back to the ContextVar only when the
record names no request. A record naming a request this module did not stamp gets
``-``: it is about that request, not the current one.

NESTING. A stack may list this class twice (the historical admin name
``RequestIDMiddleware`` is an alias, and existing admin tests prepend it to a base stack
that already starts with this class). The OUTERMOST instance owns the request: it
creates the context, sets the header and emits the one outcome record. An inner
instance for the SAME request passes straight through. A DIFFERENT request driven
through the middleware while one is live gets its own context, and the outer one is
restored when it finishes.

EXCEPTION TEXT IS WITHHELD FOR TRACED REQUESTS ONLY. An exception message can carry
anything its raiser put there, and three existing sinks on the order path print one: a
``django.request`` traceback, ``manage_order``'s swallowed-exception traceback, and
``con_orders``' ``"InitiateOrder-Error: %s", error``. For a record that belongs to a
traced request, ``RequestContextFormatter`` formats a COPY of the record with the
exception class and project-relative frames kept and every message removed — the
exception chain, a cached ``exc_text``, an exception passed as an argument or as the
message itself, and ``stack_info`` included. The shared record is never mutated, so
another handler sees exactly what it saw before. Nothing here calls ``str()`` on an
exception. Every other route keeps today's full tracebacks.

DIAGNOSTICS NEVER CHANGE AN ANSWER. Annotation, emission, the filter and the formatter
all fail closed: a bad value is omitted, an unknown classification becomes
``unclassified``, a broken emission leaves one fixed fallback line, and nothing here
catches or alters a business exception.

IMPORT-LIGHT ON PURPOSE. Django's ``LOGGING`` configuration imports the filter and the
formatter before any app is ready, so this module imports nothing from Django or from
the project. The order-specific reason allowlist lives with the order endpoint.

Synchronous only, like every other middleware here; mod_wsgi is what serves.
"""
import contextvars
import copy
import logging
import os
import re
import uuid

REQUEST_ID_HEADER = 'X-Request-ID'
NO_VALUE = '-'
NO_REQUEST_ID = NO_VALUE

#: The attribute on the UNDERLYING HttpRequest that holds its context. DRF's Request
#: proxies attribute reads to the HttpRequest but not writes, so everything here
#: resolves ``request._request`` before reading or writing it.
_CONTEXT_ATTR = '_dinify_request_context'

_REQUEST_ID = re.compile(r'[0-9a-f]{32}\Z')
_REASON = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
_CLASS_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
_FUNCTION = re.compile(r'[A-Za-z0-9_<>]{1,64}\Z')
_UNSAFE_PATH = re.compile(r'[^A-Za-z0-9_./-]')

#: Bounds. ``where`` is one project-relative ``path:line:function``; a withheld
#: traceback shows at most ``MAX_FRAMES`` innermost project frames per exception and at
#: most ``MAX_CHAINED`` exceptions of a chain, and never source text or locals.
MAX_WHERE = 160
MAX_FRAMES = 8
MAX_CHAINED = 3
_MAX_TRAVERSAL = 1000

WITHHELD_HEADER = 'Traceback (exception text withheld; project frames only):'
CHAINED_SEPARATOR = 'The exception above led to the one below (text withheld):'
STACK_WITHHELD = 'Stack (withheld)'
FORMAT_FAILED = 'log record withheld: it could not be formatted safely'

#: The project root is the directory holding ``dinify_backend``. A frame counts as a
#: project frame only when its file is under it AND not inside an installed package
#: (a virtualenv may live inside the checkout).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THIRD_PARTY_DIRS = frozenset({'site-packages', 'dist-packages'})

# --- the order-command vocabulary ---------------------------------------------------------

ORDER_COMMAND = 'order.command'
ACTIONS = frozenset({'initiate', 'submit', 'retire_quote'})
CHANNELS = frozenset({'diner', 'staff'})

OUTCOME_REFUSED = 'refused'
OUTCOME_UNHANDLED = 'unhandled'
OUTCOME_UNCLASSIFIED = 'unclassified'

#: A success word is kept only if the FINAL status is 2xx: a response that failed after
#: the answer was built is not that answer.
#:
#: ``order_returned`` is all an ``initiate`` success can say. The controller drops its
#: internal replay flag, and a replay may hand back an order that has been accepted or
#: progressed since, so it does NOT say the order is new, a draft or accepted.
SUCCESS_OUTCOMES = frozenset({
    'order_returned', 'accepted', 'already_accepted',
    'quote_still_valid', 'quote_closed', 'quote_already_closed',
})
_NOTED_OUTCOMES = SUCCESS_OUTCOMES | {OUTCOME_REFUSED}

#: The fields an annotation may carry. Anything else is dropped at runtime; a test on
#: the endpoint fails when a call names one.
ORDER_COMMAND_FIELDS = frozenset({'action', 'outcome', 'reason', 'channel', 'order', 'intent'})

OUTCOME_LOGGER = logging.getLogger('dinify.outcome')
_OUTCOME_KEYS = ('action', 'status', 'outcome', 'reason', 'channel', 'order', 'intent',
                 'error', 'where')
_OUTCOME_MESSAGE = ORDER_COMMAND + ' ' + ' '.join(f'{key}=%s' for key in _OUTCOME_KEYS)
_OUTCOME_FALLBACK = ORDER_COMMAND + ' outcome unavailable: the diagnostics failed'


class _RequestContext:
    """What one request carries for its whole life. Mutable, and shared by the
    ContextVar and the request attribute, so a late log line and an in-stack one agree."""

    __slots__ = ('request_id', 'traced', 'emitted', 'action', 'outcome', 'reason',
                 'channel', 'order', 'intent', 'error', 'where')

    def __init__(self, request_id):
        self.request_id = request_id
        self.traced = False
        self.emitted = False
        self.action = None
        self.outcome = None
        self.reason = None
        self.channel = None
        self.order = None
        self.intent = None
        self.error = None
        self.where = None


_current = contextvars.ContextVar('dinify_request_context', default=None)


# --- identity helpers ------------------------------------------------------------------------

def is_request_id(value):
    """Exactly 32 lowercase hex characters."""
    return isinstance(value, str) and _REQUEST_ID.match(value) is not None


def request_id_for(request):
    """The trusted server ID already on ``request`` (HttpRequest or DRF Request), or
    ``None``. Only server code sets this attribute; a client header never reaches it."""
    try:
        value = getattr(request, 'request_id', None)
    except Exception:
        return None
    return value if is_request_id(value) else None


def current_request_id():
    """The ID of the request whose middleware is live on this thread, or ``None``."""
    context = _current.get()
    return context.request_id if context is not None else None


def _http(request):
    try:
        return getattr(request, '_request', request)
    except Exception:
        return None


def _context_of(request):
    try:
        context = getattr(_http(request), _CONTEXT_ATTR, None)
    except Exception:
        return None
    return context if isinstance(context, _RequestContext) else None


def _identity(record):
    """``(request_id, context)`` for a log record. Never raises."""
    try:
        request = getattr(record, 'request', None)
        if request is not None:
            context = _context_of(request)
            if context is not None:
                return context.request_id, context
            return request_id_for(_http(request)) or NO_REQUEST_ID, None
        context = _current.get()
        if context is not None:
            return context.request_id, context
    except Exception:
        pass
    return NO_REQUEST_ID, None


# --- the middleware ----------------------------------------------------------------------------

class RequestContextMiddleware:
    """Installed exactly once and OUTERMOST on both planes (see the settings modules)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _context_of(request) is not None:
            # NESTED: an outer instance owns this request. It re-mints nothing, sets
            # the header and emits the outcome once, with the final status.
            return self.get_response(request)
        # Reuse an ID only when trusted server code already set one on this request
        # object (a nested or standalone caller); otherwise mint. Never a client value.
        request_id = request_id_for(request) or uuid.uuid4().hex
        context = _RequestContext(request_id)
        request.request_id = request_id
        setattr(request, _CONTEXT_ATTR, context)
        token = _current.set(context)
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            _emit_outcome(context, response)
            return response
        finally:
            # Never left alive for whatever this thread does next. The late
            # ``log_response`` line recovers the ID from ``record.request`` instead.
            _current.reset(token)

    def process_exception(self, request, exception):
        """Record the class and project location of an exception a TRACED view raised.
        Returns ``None``: Django's handling of the exception is unchanged."""
        context = _context_of(request)
        if context is not None and context.traced and context.error is None:
            try:
                context.error = _class_name(exception)
                context.where = _where(exception)
            except Exception:
                pass
        return None


# --- annotation and emission ----------------------------------------------------------------------

def note_outcome(request, event, **fields):
    """Attach allowlisted facts about an order command to its request.

    The first note must name a known ``action``; that is what marks the request as
    traced. Unknown field names and ill-shaped values are dropped, and nothing here can
    raise into the caller: a diagnostic must never change a business answer."""
    try:
        _note(request, event, fields)
    except Exception:
        pass


def _note(request, event, fields):
    if event != ORDER_COMMAND:
        return
    context = _context_of(request)
    if context is None:
        return
    action = fields.get('action')
    if context.action is None and isinstance(action, str) and action in ACTIONS:
        context.action = action
        context.traced = True
    if not context.traced:
        return
    for name, value in fields.items():
        if name == 'action':
            continue
        cleaner = _CLEANERS.get(name)
        if cleaner is None:
            continue
        clean = cleaner(value)
        if clean is not None:
            setattr(context, name, clean)


def _word(allowed):
    return lambda value: value if isinstance(value, str) and value in allowed else None


def _reason(value):
    return value if isinstance(value, str) and _REASON.match(value) else None


def _canonical_uuid(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and 32 <= len(value) <= 45:
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None
    return None


_CLEANERS = {
    'outcome': _word(_NOTED_OUTCOMES),
    'reason': _reason,
    'channel': _word(CHANNELS),
    'order': lambda value: _canonical_uuid(value),
    'intent': lambda value: _canonical_uuid(value),
}


def _outcome_fields(context, status):
    """The record for one traced request, classified against its FINAL status."""
    outcome = context.outcome
    if outcome in SUCCESS_OUTCOMES and not 200 <= status < 300:
        outcome = None
    elif outcome == OUTCOME_REFUSED and not 400 <= status < 500:
        outcome = None
    reason = context.reason if outcome is not None else None
    if outcome is None:
        # Neither word establishes that nothing committed.
        outcome = (OUTCOME_UNHANDLED if context.error is not None and status >= 500
                   else OUTCOME_UNCLASSIFIED)
    return {
        'event': ORDER_COMMAND, 'action': context.action, 'status': status,
        'outcome': outcome, 'reason': reason, 'channel': context.channel,
        'order': context.order, 'intent': context.intent,
        'error': context.error, 'where': context.where,
    }


def _emit_outcome(context, response):
    if not context.traced or context.emitted:
        return
    context.emitted = True
    try:
        status = response.status_code
        fields = _outcome_fields(context, status)
        level = (logging.ERROR if status >= 500
                 else logging.WARNING if status >= 400 else logging.INFO)
        OUTCOME_LOGGER.log(
            level, _OUTCOME_MESSAGE,
            *(NO_VALUE if fields[key] is None else fields[key] for key in _OUTCOME_KEYS),
            extra={'dinify_outcome': fields},
        )
    except Exception:
        try:
            OUTCOME_LOGGER.warning(_OUTCOME_FALLBACK)
        except Exception:
            pass


# --- exception locations ----------------------------------------------------------------------------

def _class_name(value):
    cls = value if isinstance(value, type) else type(value)
    name = getattr(cls, '__name__', None)
    return name if isinstance(name, str) and _CLASS_NAME.match(name) else NO_VALUE


def _project_path(filename):
    if not isinstance(filename, str) or not filename.endswith('.py'):
        return None
    absolute = os.path.abspath(filename)
    if not absolute.startswith(_PROJECT_ROOT + os.sep):
        return None
    relative = absolute[len(_PROJECT_ROOT) + 1:].replace(os.sep, '/')
    if _THIRD_PARTY_DIRS.intersection(relative.split('/')):
        return None
    return _UNSAFE_PATH.sub('_', relative)


def _project_frames(tb):
    frames = []
    steps = 0
    while tb is not None and steps < _MAX_TRAVERSAL:
        steps += 1
        code = tb.tb_frame.f_code
        path = _project_path(code.co_filename)
        if path is not None:
            line = tb.tb_lineno if isinstance(tb.tb_lineno, int) else 0
            name = code.co_name if _FUNCTION.match(code.co_name or '') else '?'
            frames.append((path, line, name))
        tb = tb.tb_next
    return frames


def _frame_text(path, line, name):
    suffix = f':{line}:{name}'
    budget = MAX_WHERE - len(suffix)
    if len(path) > budget:
        path = path[-budget:] if budget > 0 else ''
    return f'{path}{suffix}'[-MAX_WHERE:]


def _where(exception):
    frames = _project_frames(getattr(exception, '__traceback__', None))
    return _frame_text(*frames[-1]) if frames else None


def _chain(exception):
    """The exception and what it was raised from, earliest first, at most MAX_CHAINED."""
    chain, seen, current = [], set(), exception
    while isinstance(current, BaseException) and id(current) not in seen:
        if len(chain) == MAX_CHAINED:
            return list(reversed(chain)), True
        seen.add(id(current))
        chain.append(current)
        following = current.__cause__
        if following is None and not current.__suppress_context__:
            following = current.__context__
        current = following
    return list(reversed(chain)), False


def _withheld_traceback(exception):
    lines = [WITHHELD_HEADER]
    chain, truncated = _chain(exception)
    if truncated:
        lines.append('  (earlier chained exceptions omitted)')
    for index, error in enumerate(chain):
        if index:
            lines.append(CHAINED_SEPARATOR)
        frames = _project_frames(error.__traceback__)
        if len(frames) > MAX_FRAMES:
            lines.append(f'  ({len(frames) - MAX_FRAMES} earlier project frames omitted)')
            frames = frames[-MAX_FRAMES:]
        for path, line, name in frames:
            text = _frame_text(path, line, name)
            shown_path = text[:len(text) - len(f':{line}:{name}')]
            lines.append(f'  File "{shown_path}", line {line}, in {name}')
        lines.append(_class_name(error))
    return '\n'.join(lines)


def _safe_argument(value, depth=0):
    if isinstance(value, BaseException):
        return _class_name(value)
    if depth >= 2:
        return value
    if isinstance(value, tuple):
        return tuple(_safe_argument(item, depth + 1) for item in value)
    if isinstance(value, list):
        return [_safe_argument(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: _safe_argument(item, depth + 1) for key, item in value.items()}
    return value


def _withhold(safe):
    """Remove every exception MESSAGE from a copy of a traced record."""
    exc_info = safe.exc_info
    safe.exc_info = None
    safe.exc_text = None                   # never reuse text another handler cached
    if (isinstance(exc_info, tuple) and len(exc_info) == 3
            and isinstance(exc_info[1], BaseException)):
        safe.exc_text = _withheld_traceback(exc_info[1])
    if safe.stack_info:
        safe.stack_info = STACK_WITHHELD
    if isinstance(safe.msg, BaseException):
        safe.msg = _class_name(safe.msg)
    safe.args = _safe_argument(safe.args)


# --- logging ---------------------------------------------------------------------------------------

class RequestContextFilter(logging.Filter):
    """Attach ``record.request_id`` — always overwritten, so a caller's ``extra`` cannot
    supply one — and never raise: an exception from a filter escapes into the caller."""

    def filter(self, record):
        try:
            record.request_id = _identity(record)[0]
        except Exception:
            pass
        return True


class RequestContextFormatter(logging.Formatter):
    """The console formatter. Unchanged output for untraced records; for a record of a
    traced request, a copy with every exception message withheld."""

    def format(self, record):
        request_id, context = _identity(record)
        if context is None or not context.traced:
            stamped = getattr(record, 'request_id', None)
            if isinstance(stamped, str) and stamped == request_id:
                return super().format(record)      # exactly today's output, plus the ID
            unstamped = copy.copy(record)
            unstamped.request_id = request_id
            return super().format(unstamped)
        try:
            safe = copy.copy(record)
            safe.request_id = request_id
            _withhold(safe)
            return super().format(safe)
        except Exception:
            # A failure here must not fall through to logging's error handler, which
            # prints the ORIGINAL message and arguments.
            return self._fallback(record, request_id)

    def _fallback(self, record, request_id):
        try:
            name = record.name if isinstance(record.name, str) else NO_VALUE
            minimal = logging.makeLogRecord({
                'name': name[:120], 'levelno': record.levelno,
                'levelname': str(record.levelname)[:20], 'created': record.created,
                'msecs': record.msecs, 'msg': FORMAT_FAILED, 'args': (),
                'request_id': request_id,
            })
            return super().format(minimal)
        except Exception:
            return FORMAT_FAILED
