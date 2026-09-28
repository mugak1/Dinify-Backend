"""THE BOUNDED READINESS PROBE (D15 R1) — one ``SELECT 1`` in a helper process that can be killed.

``misc_app/endpoints/readiness.py`` asks one question: can a NEW connection to the configured
database run ``SELECT 1`` right now? This module answers it inside ONE monotonic budget,
from admission through disposal, whatever the database, the network or the resolver does.

WHY A PROCESS AND NOT A TIMEOUT ARGUMENT. Every in-process bound has a hole. libpq resolves
the host name synchronously (``connect_timeout`` does not cover it); a server that freezes
after the handshake stalls the query or the result read, where neither ``connect_timeout``
nor a server-side ``statement_timeout`` reaches; psycopg's ``wait(timeout)`` is a polling
interval, and cancelling a stuck async operation can itself wait; a thread or future that
"times out" leaves the blocking call running. A helper process is the one boundary the
caller can end unconditionally: ``SIGKILL`` to its process group, then reap it. Nothing the
helper is blocked in — DNS, TLS, a socket read — can outlive that.

THE BUDGET. ``BUDGET_S`` runs from the caller's admission of the probe to the moment the
helper has been reaped. ``CLEANUP_RESERVE_S`` of it is kept back from the query phase so
that kill + reap fit inside the same budget, never after it.

AT MOST ONE HELPER PER APPLICATION PROCESS. ``_slot`` holds the one helper this process may
have alive. It is cleared only once the helper has been REAPED, so a helper that somehow
outlives its budget blocks every later probe (the caller answers not-ready) instead of
accumulating beside it. The coalescing that keeps request threads off a stalled probe
lives in the endpoint; this module only guarantees there is never a second child.

WHAT CROSSES THE BOUNDARY. The parent sends one JSON document on the helper's STDIN: the
libpq keywords Django itself would connect with, and the directories the parent loaded the
driver from. Nothing secret is ever on the command line (``ps`` shows only the interpreter,
two flags and this file's path). The helper answers one fixed token on stdout; its stderr
is discarded, so no driver message, host name or traceback can reach a log from here. The
parent maps the token to a fixed category and ignores anything else it sees.

THE HELPER imports only the standard library and psycopg. It never loads Django, settings or
an environment file, and it runs isolated (``-I -S``) so neither ``PYTHON*`` variables nor a
site ``.pth`` file can change what it executes. It arms its own ``SIGALRM`` so that a
helper whose parent vanished (a daemon restart mid-probe) still exits on its own.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import NamedTuple

BUDGET_S = 2.0
CLEANUP_RESERVE_S = 0.25
ORPHAN_LIMIT_S = 3
MAX_INPUT_BYTES = 65536

READY = 'ready'
# Fixed diagnostic categories. Nothing else is ever reported, returned or logged.
HELPER_TOKENS = (
    'bad_input', 'driver_unavailable', 'configuration_rejected', 'connect_failed',
    'query_failed', 'unexpected_result',
)
PARENT_CATEGORIES = (
    'timeout', 'helper_unavailable', 'helper_failed', 'helper_unreaped',
)
_ANSWERS = {(token + '\n').encode(): token for token in (READY,) + HELPER_TOKENS}


class Outcome(NamedTuple):
    ready: bool
    category: str


_slot_lock = threading.Lock()
_slot = None


def helper_busy():
    """True while an earlier helper of this process has not been reaped."""
    global _slot
    with _slot_lock:
        proc = _slot
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        _close_pipes(proc)
        _slot = None
        return False


def interpreter():
    """A Python interpreter binary to run the helper with, or None.

    ``sys.executable`` is the obvious answer, but an embedding host such as mod_wsgi can
    report an empty value or its own binary; only a file whose name starts with
    ``python`` is ever executed. The base interpreter is the fallback: the helper receives
    the driver's directories explicitly, so it does not depend on which one it is.
    """
    version = 'python%d.%d' % sys.version_info[:2]
    candidates = (
        sys.executable,
        getattr(sys, '_base_executable', ''),
        os.path.join(sys.base_exec_prefix, 'bin', version),
    )
    for candidate in candidates:
        if (candidate and os.path.basename(candidate).startswith('python')
                and os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            return candidate
    return None


def helper_argv():
    """The command that runs the helper, or None when no interpreter can be found."""
    python = interpreter()
    if python is None:
        return None
    return [python, '-I', '-S', os.path.abspath(__file__)]


def driver_paths():
    """The directories the PARENT loaded the database driver from, for the helper's
    ``sys.path``. Read from the modules already imported, so the helper finds the same
    driver however the parent's own path was assembled (a venv, or python-path)."""
    paths = []
    for name in ('psycopg', 'psycopg_binary', 'psycopg_c', 'typing_extensions'):
        location = getattr(sys.modules.get(name), '__file__', None)
        if not location:
            continue
        root = os.path.dirname(os.path.abspath(location))
        if os.path.basename(location).startswith('__init__.'):
            root = os.path.dirname(root)
        if root not in paths:
            paths.append(root)
    return paths


def run(payload, admitted_at, argv=None):
    """Probe once. Returns an ``Outcome`` no later than ``admitted_at + BUDGET_S`` (plus
    scheduling), with the helper killed and reaped — or, if the kernel has not released it
    by then, parked in ``_slot`` so that no second helper can start beside it."""
    global _slot
    deadline = admitted_at + BUDGET_S
    query_deadline = deadline - CLEANUP_RESERVE_S
    argv = argv or helper_argv()
    if argv is None:
        return Outcome(False, 'helper_unavailable')
    data = json.dumps(payload).encode('utf-8')
    with _slot_lock:
        if _slot is not None:
            return Outcome(False, 'helper_unreaped')
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            )
        except OSError:
            return Outcome(False, 'helper_unavailable')
        _slot = proc
    out, timed_out = b'', False
    try:
        out, _ = proc.communicate(data, timeout=max(0.0, query_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _dispose(proc, deadline)
    if timed_out:
        return Outcome(False, 'timeout')
    answer = _ANSWERS.get(out)
    if answer == READY and proc.returncode == 0:
        return Outcome(True, READY)
    if answer in HELPER_TOKENS:
        return Outcome(False, answer)
    return Outcome(False, 'helper_failed')


def _dispose(proc, deadline):
    """Kill the helper's whole process group if it is still running, then reap it within
    what is left of the budget. The slot is released only once the helper is reaped."""
    global _slot
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    _close_pipes(proc)
    if proc.returncode is not None:
        with _slot_lock:
            if _slot is proc:
                _slot = None


def _close_pipes(proc):
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


# --- the helper ---------------------------------------------------------------------------

def _answer(token, code=1):
    sys.stdout.buffer.write((token + '\n').encode())
    sys.stdout.buffer.flush()
    os._exit(code)  # no atexit, no finalizer: nothing may block on the way out


def _helper():
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(ORPHAN_LIMIT_S)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        payload = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
        params, paths = payload['params'], payload['paths']
        if not isinstance(params, dict) or not all(isinstance(p, str) for p in paths):
            raise ValueError
    except Exception:
        _answer('bad_input')
    sys.path[:0] = paths
    try:
        import psycopg
    except Exception:
        _answer('driver_unavailable')
    try:
        conn = psycopg.connect(**{**params, 'autocommit': True})
    except psycopg.ProgrammingError:
        _answer('configuration_rejected')
    except Exception:
        _answer('connect_failed')
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1')
            row = cursor.fetchone()
    except Exception:
        _answer('query_failed')
    try:
        conn.close()
    except Exception:
        pass
    if row != (1,):
        _answer('unexpected_result')
    _answer(READY, code=0)


if __name__ == '__main__':
    _helper()
