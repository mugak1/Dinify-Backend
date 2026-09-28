"""``GET /api/v1/health/ready/`` — can this process's configured database run ``SELECT 1``
through a NEW connection, right now? (D15 R1.)

  200 {"status": "ready", "database": "connected"}
  503 {"status": "not_ready", "database": "unreachable"}

Exactly two keys, ``Cache-Control: no-store, private``, and nothing else: no host, version,
driver message, timing or category, whatever went wrong. The fixed category is written to
the log, once per change of state, never per caller.

WHAT IT IS NOT. ``/api/v1/health/`` is unchanged and still answers HTTP 200 with
``status: degraded`` when the database is unreachable (the legacy deploy gate reads its
body), and it is still UNBOUNDED: it runs ``SELECT 1`` on the request's own persistent
connection. The admin plane's health route stays pure liveness. No deploy step, monitor or
staged release check reads THIS route; switching one is a separate decision.

AUTHORITY. A plain Django view, deliberately not a DRF ``APIView``: it never authenticates
(this view reads no header, cookie or token), and there is no content negotiation, so
neither ``Accept`` nor ``?format=`` can change the response. No request data is read at
all: the query string cannot move the probe target. GET and HEAD only; every other method
is 405. It writes nothing, audits nothing and performs no Mongo, provider or in-process
database I/O — the probe runs in a helper process (``misc_app/readiness_probe.py``), never
on the request's persistent connection, which a frozen server would hang indefinitely.

DELEGATION. The customer plane's ``DelegatedAccessMiddleware`` runs before every view and
resolves an ``X-Delegation-Session`` header with a query on the request's own connection.
This route is in its ``EXEMPT_ROUTES``, matched on the URL pattern before the header is
read, so a request to it is never a delegated one: no lookup, no 401/403, no audit row,
and the same answer inside the same budget whatever it carries. Without the exemption such
a request, against a frozen database, got no answer within 8 s where the same request
without the header answered 503 in 1.76 s.

COALESCING, PER APPLICATION PROCESS. At most one probe is in flight in this process. A
completed result is served for at most ``FRESH_FOR_S`` seconds after it completed (a
monotonic clock), so a stale "ready" cannot outlive two seconds. A caller that finds no
fresh result while a probe is in flight waits for it at most ``NON_OWNER_WAIT_S`` and then
answers from whatever is fresh — or not-ready. Nothing queues and nothing runs in the
background: the probe runs on the admitting request's own thread. A result may only
replace an OLDER observation, so work that finishes late can never overwrite a newer one.
Each daemon process has its own state: this bounds probes per process, not across workers.

ENGINES. PostgreSQL is the dependency this answers for. Any other engine (the SQLite used
by some local test runs) answers 503 ``unsupported_engine`` rather than a synthesized
"connected", and a configuration this probe cannot hand to the helper faithfully answers
503 ``unsupported_configuration`` rather than being approximated.
"""

import logging
import threading
import time
from typing import NamedTuple

from django.db import connections
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from misc_app import readiness_probe

logger = logging.getLogger(__name__)

FRESH_FOR_S = 2.0
NON_OWNER_WAIT_S = 0.25
# An owner still unfinished this long after admission has broken its own bound; a new
# probe may be admitted beside the bookkeeping (never beside a live helper — see
# ``readiness_probe.helper_busy``), and the late owner's result will be discarded.
ABANDON_AFTER_S = readiness_probe.BUDGET_S + 1.0

READY_BODY = {'status': 'ready', 'database': 'connected'}
NOT_READY_BODY = {'status': 'not_ready', 'database': 'unreachable'}

# Python-level psycopg objects Django adds; they are not libpq connection keywords and
# change nothing about which server is reached or how.
_NOT_CONNECTION_KEYWORDS = frozenset({'context', 'cursor_factory', 'prepare_threshold'})
APPLICATION_NAME = 'dinify-readiness'


class _Observation(NamedTuple):
    seq: int
    ready: bool
    category: str
    completed_at: float


class _Probe:
    __slots__ = ('seq', 'admitted_at', 'done')

    def __init__(self, seq, admitted_at):
        self.seq = seq
        self.admitted_at = admitted_at
        self.done = threading.Event()


_lock = threading.Lock()
_latest = None
_inflight = None
_admitted = 0


@csrf_exempt
@require_safe
def readiness(request):
    ready = _observe()
    response = JsonResponse(READY_BODY if ready else NOT_READY_BODY, status=200 if ready else 503)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    # Django logs every 5xx response at ERROR through ``django.request`` — one line per
    # CALLER. A 503 here is an answer, not an error, and the state change behind it is
    # already logged once (``_record``); without this a caller could write a log line per
    # request for as long as the database is down. The marker is Django's own guard
    # against logging one response twice (``django.utils.log.log_response``).
    response._has_been_logged = True
    return response


def _fresh(now):
    if _latest is not None and now - _latest.completed_at <= FRESH_FOR_S:
        return _latest
    return None


def _observe():
    global _inflight, _admitted
    now = time.monotonic()
    with _lock:
        fresh = _fresh(now)
        if fresh is not None:
            return fresh.ready
        probe = _inflight
        if probe is not None and now - probe.admitted_at < ABANDON_AFTER_S:
            owner = False
        elif readiness_probe.helper_busy():
            return False
        else:
            _admitted += 1
            probe = _inflight = _Probe(_admitted, now)
            owner = True
    if not owner:
        probe.done.wait(max(0.0, min(NON_OWNER_WAIT_S, probe.admitted_at + readiness_probe.BUDGET_S - now)))
        with _lock:
            fresh = _fresh(time.monotonic())
        return fresh.ready if fresh is not None else False
    outcome = readiness_probe.Outcome(False, 'helper_failed')
    try:
        outcome = _probe(probe.admitted_at)
    finally:
        _record(probe, outcome)
    return outcome.ready


def _probe(admitted_at):
    payload, category = _payload()
    if payload is None:
        return readiness_probe.Outcome(False, category)
    return readiness_probe.run(payload, admitted_at)


def _payload():
    """What the helper connects with: the libpq keywords Django itself connects with, or a
    fixed refusal. Never mutates settings; never approximates an option it cannot pass."""
    try:
        connection = connections['default']
        if connection.vendor != 'postgresql':
            return None, 'unsupported_engine'
        params = connection.get_connection_params()
    except Exception:
        return None, 'unsupported_configuration'
    clean = {}
    for key, value in params.items():
        if key in _NOT_CONNECTION_KEYWORDS or value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            return None, 'unsupported_configuration'
        clean[key] = value
    clean.setdefault('application_name', APPLICATION_NAME)
    return {'params': clean, 'paths': readiness_probe.driver_paths()}, None


def _record(probe, outcome):
    global _latest, _inflight
    completed_at = time.monotonic()
    with _lock:
        previous = _latest
        if previous is None or probe.seq > previous.seq:
            _latest = _Observation(probe.seq, outcome.ready, outcome.category, completed_at)
        changed = _latest.seq == probe.seq and (previous is None or previous.category != outcome.category)
        if _inflight is probe:
            _inflight = None
    probe.done.set()
    if changed:
        if outcome.ready:
            logger.info('readiness: database connected')
        else:
            logger.warning('readiness: not ready (%s)', outcome.category)


def _reset_for_tests():
    global _latest, _inflight, _admitted
    with _lock:
        _latest = None
        _inflight = None
        _admitted = 0
