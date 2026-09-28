"""D15 R1 — ``GET /api/v1/health/ready/``: a readiness answer with a real elapsed-time bound.

What is proved here, against the REAL test database, a real helper process and (for the
HTTP class) a real threaded HTTP server:

* the contract — exact 200/503 bodies, ``no-store, private``, GET/HEAD only, request data
  and credentials ignored, no in-process query, audit, Mongo or provider side effect;
* the bound — refused, never-handshaking, stalled-after-handshake (query and result read),
  a helper blocked in a resolver-style read that ignores every catchable signal: each
  answers within ``BOUND_S`` (the 2.0 s budget + 0.5 s STATED scheduling/test tolerance)
  with the helper reaped and no server backend left behind;
* recovery is a FRESH observation, and a ready result cannot outlive two seconds;
* one probe per process under a burst, prompt non-owners, no accumulation across rounds,
  and a late result never overwrites a newer one;
* the consumers this change must NOT move — the old health route, admin liveness, the
  legacy deploy gate and the staged release checks — are characterised unchanged.

FIXTURES, labelled. ``_SilentListener`` completes TCP in the kernel backlog and never
speaks. ``_StallProxy`` is a task-owned TCP proxy in front of the real test database that
becomes a stall once PostgreSQL's first ReadyForQuery has passed; it needs a NON-TLS test
database (CI's PostgreSQL service is one) — through TLS the boundary is invisible, the
stall never happens and those tests FAIL rather than pass vacuously. The resolver stall is
a STAND-IN: a helper blocked in ``recvfrom`` on a task-owned silent UDP socket (the syscall
a stalled DNS lookup blocks in); no real resolver or network is used.

WATCHDOG. Every probing call runs under ``_bounded``: if a broken deadline let it hang, the
watchdog kills only the helper processes THIS test started (identified by command line)
and fails the test, so the runner itself cannot hang.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock, skipUnless
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.db import connection, connections
from django.db.utils import OperationalError
from django.test import Client, LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve

from misc_app import readiness_probe
from misc_app.endpoints import readiness

URL = '/api/v1/health/ready/'
TOLERANCE_S = 0.5                     # scheduling/test tolerance, stated — not part of the bound
BOUND_S = readiness_probe.BUDGET_S + TOLERANCE_S
NON_OWNER_BOUND_S = readiness.NON_OWNER_WAIT_S + TOLERANCE_S
# The CONTRACT's freshness window, written as a literal rather than read from the module:
# every sleep that waits for a cached answer to go stale waits THIS long, so an
# implementation that served a result for longer fails here instead of making the test
# wait as long as it does.
CONTRACT_FRESH_S = 2.0
WATCHDOG_S = 15
READY = {'status': 'ready', 'database': 'connected'}
NOT_READY = {'status': 'not_ready', 'database': 'unreachable'}
STALL_MARKER = 'd15-readiness-test-stall'
HAS_PROC = os.path.isdir('/proc/self/task')
ON_POSTGRES = connection.vendor == 'postgresql'
REPO = Path(__file__).resolve().parent.parent

needs_postgres = skipUnless(ON_POSTGRES, 'the readiness probe answers for PostgreSQL; CI runs it there')
needs_proc = skipUnless(HAS_PROC, 'process accounting reads /proc (Linux, as in CI)')


# --- process and resource accounting -----------------------------------------------------

def _children():
    pids = set()
    for task in os.listdir('/proc/self/task'):
        try:
            with open('/proc/self/task/%s/children' % task) as fh:
                pids.update(int(p) for p in fh.read().split())
        except OSError:
            pass
    return pids


def _cmdline(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as fh:
            return fh.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def _task_owned_helpers():
    """Children of this test process that are readiness helpers or this file's stall stand-ins."""
    return {pid for pid in _children()
            if 'readiness_probe.py' in _cmdline(pid) or STALL_MARKER in _cmdline(pid)}


def _open_fds():
    return len(os.listdir('/proc/self/fd'))


def _kill_task_owned():
    for pid in _task_owned_helpers():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _bounded(testcase, fn):
    """Run ``fn`` under an external watchdog. The call thread is a daemon ONLY so that a
    broken deadline cannot hang the runner after the test has failed loudly."""
    box = {}

    def call():
        try:
            box['value'] = fn()
        except BaseException as exc:  # re-raised on the test thread
            box['error'] = exc

    worker = threading.Thread(target=call, name='readiness-test-call', daemon=True)
    worker.start()
    worker.join(WATCHDOG_S)
    if worker.is_alive():
        _kill_task_owned()
        worker.join(5)
        testcase.fail('a readiness call outlived the %ss watchdog: its deadline is broken' % WATCHDOG_S)
    if 'error' in box:
        raise box['error']
    return box['value']


def _timed_get(testcase, client=None, path=URL, **extra):
    client = client or Client()
    started = time.monotonic()
    response = _bounded(testcase, lambda: client.get(path, **extra))
    return response, time.monotonic() - started


def _get_counting_queries(testcase, **extra):
    """GET the route under the watchdog, counting the queries made on the WORKER thread's
    own connection (connections are thread-local; counting on the test thread would see
    nothing and prove nothing), then close that connection."""
    def call():
        try:
            with CaptureQueriesContext(connection) as queries:
                response = Client().get(URL, **extra)
            return response, len(queries)
        finally:
            connection.close()
    return _bounded(testcase, call)


def _no_growth(testcase, baseline, within=5.0):
    threads, fds = baseline
    ok = _await(lambda: threading.active_count() <= threads and _open_fds() <= fds, within=within)
    testcase.assertTrue(ok, 'threads %d>%d or descriptors %d>%d accumulated'
                        % (threading.active_count(), threads, _open_fds(), fds))


def _run_all(workers):
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _server_backends():
    """Server-side connections the probe left behind (it labels them ``dinify-readiness``
    when no application_name is configured). The snapshot is cleared first: inside a
    transaction PostgreSQL otherwise answers from the first read's snapshot."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_stat_clear_snapshot()')
        cursor.execute(
            'SELECT count(*) FROM pg_stat_activity WHERE application_name = %s AND datname = %s',
            [readiness.APPLICATION_NAME, connection.settings_dict['NAME']],
        )
        return cursor.fetchone()[0]


def _await(predicate, within=5.0):
    stop = time.monotonic() + within
    while time.monotonic() < stop:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _redirect(port):
    """Point ONLY the probe's host/port at a fixture; user, password, database and every
    other option still come from the real settings through the real ``_payload``."""
    real = readiness._payload

    def redirected():
        payload, category = real()
        if payload is not None:
            payload['params'].update(host='127.0.0.1', port=port)
        return payload, category

    return mock.patch.object(readiness, '_payload', redirected)


def _closed_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _SilentListener:
    """Completes TCP in the kernel backlog and never says a word."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.sock.close()


class _StallProxy:
    """A task-owned TCP proxy in front of the real test database (see the module note).

    ``stall=None`` is transparent. ``'query'`` withholds the client's bytes once the
    server's first ReadyForQuery has passed (the query never arrives; the client blocks
    reading its result). ``'result'`` forwards the query and withholds the answer.
    ``resume()`` makes later connections transparent again."""

    READY_FOR_QUERY = b'Z\x00\x00\x00\x05I'

    def __init__(self, stall):
        settings_dict = connection.settings_dict
        self.upstream = (settings_dict['HOST'] or '127.0.0.1', int(settings_dict['PORT'] or 5432))
        self.stall = stall
        self.lock = threading.Lock()
        self.threads = []
        self.sockets = []
        self.closed = False

    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self._spawn(self._accept)
        return self

    def __exit__(self, *exc):
        self.closed = True
        self.listener.close()
        with self.lock:
            for sock in self.sockets:
                self._shut(sock)
        for thread in self.threads:
            thread.join(5)
        assert not any(t.is_alive() for t in self.threads), 'a proxy thread outlived the fixture'

    def resume(self):
        with self.lock:
            self.stall = None

    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    @staticmethod
    def _shut(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _accept(self):
        self.listener.settimeout(0.1)   # closing a listener does not wake a blocked accept()
        while not self.closed:
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.settimeout(None)
            server = socket.create_connection(self.upstream, timeout=5)
            server.settimeout(None)
            with self.lock:
                self.sockets += [client, server]
            state = {'ready': False}
            self._spawn(self._pump, client, server, 'up', state)
            self._spawn(self._pump, server, client, 'down', state)

    def _pump(self, src, dst, direction, state):
        tail = b''
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    return
                if direction == 'down' and not state['ready']:
                    window = tail + data
                    tail = window[-5:]
                    if self.READY_FOR_QUERY in window:
                        state['ready'] = True
                    dst.sendall(data)
                    continue
                with self.lock:
                    stall = self.stall
                if state['ready'] and ((stall == 'query' and direction == 'up')
                                       or (stall == 'result' and direction == 'down')):
                    continue  # withheld
                dst.sendall(data)
        except OSError:
            return
        finally:
            self._shut(src)
            self._shut(dst)


def _stall_argv(body):
    return [sys.executable, '-I', '-S', '-c', '# %s\n%s' % (STALL_MARKER, body)]


class _ReadinessCase:
    """Isolation shared by the probing classes: fresh module state, and no helper, slot or
    task-owned child may survive a test."""

    def setUp(self):
        super().setUp()
        readiness._reset_for_tests()
        self.assertIsNone(readiness_probe._slot)
        if HAS_PROC:
            self.assertEqual(_task_owned_helpers(), set())

    def tearDown(self):
        try:
            if HAS_PROC:
                self.assertTrue(_await(lambda: not _task_owned_helpers(), within=3),
                                'a readiness helper outlived its test')
            self.assertTrue(_await(lambda: not readiness_probe.helper_busy(), within=3))
            self.assertIsNone(readiness_probe._slot)
        finally:
            _kill_task_owned()
            readiness._reset_for_tests()
            super().tearDown()


@needs_postgres
class ReadinessContractTests(_ReadinessCase, TestCase):

    def test_ready_is_200_with_exactly_two_keys_and_no_store(self):
        started = time.monotonic()
        response, queries = _get_counting_queries(self)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), READY)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertLess(elapsed, BOUND_S)
        self.assertEqual(queries, 0, 'the probe must never use the request\'s own connection')
        self.assertEqual(readiness._latest.category, 'ready')

    def test_head_is_served_and_every_other_method_is_refused_without_a_probe(self):
        client = Client(enforce_csrf_checks=True)
        for method in ('post', 'put', 'patch', 'delete', 'options'):
            response = getattr(client, method)(URL)
            self.assertEqual(response.status_code, 405, method)
            self.assertEqual(response['Allow'], 'GET, HEAD', method)
        self.assertEqual(readiness._admitted, 0, 'a refused method must not admit a probe')
        response = _bounded(self, lambda: client.head(URL))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store, private')

    def test_request_data_and_credentials_change_nothing(self):
        seen = []
        real_run = readiness_probe.run

        def spy(payload, admitted_at, argv=None):
            seen.append(dict(payload['params']))
            return real_run(payload, admitted_at, argv)

        expected, _ = readiness._payload()
        with mock.patch.object(readiness_probe, 'run', spy):
            response, _ = _timed_get(
                self, path=URL + '?host=10.255.255.1&port=1&dbname=other&user=x&format=api',
                HTTP_ACCEPT='text/html', HTTP_AUTHORIZATION='Bearer not-a-token',
                HTTP_COOKIE='sessionid=not-a-session; csrftoken=x',
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), READY)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(seen, [expected['params']], 'the target is the configured database, always')

    def test_no_audit_session_mongo_mail_or_provider_side_effect(self):
        import dinify_backend.mongo_db as mongo
        from django.core import mail
        mongo.MONGO_DB.reset_mock()
        with mock.patch('notifications_app.controllers.sms.requests.get') as provider:
            response, queries = _get_counting_queries(
                self, HTTP_AUTHORIZATION='Bearer not-a-token', HTTP_COOKIE='sessionid=not-a-session')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(queries, 0)             # no user, session, audit or action-log row
        self.assertEqual(mongo.MONGO_DB.mock_calls, [])
        self.assertEqual(mail.outbox, [])
        provider.assert_not_called()

    def test_configured_options_reach_the_helper_unchanged_and_settings_are_not_mutated(self):
        options = connections['default'].settings_dict['OPTIONS']
        before = dict(options)
        configured = {'sslmode': 'prefer', 'options': '-c search_path=public', 'application_name': 'configured-name'}
        with mock.patch.dict(options, configured):
            payload, category = readiness._payload()
            response, _ = _timed_get(self)
        self.assertIsNone(category)
        for key, value in configured.items():
            self.assertEqual(payload['params'][key], value, key)
        self.assertFalse({'context', 'cursor_factory', 'prepare_threshold'} & set(payload['params']))
        self.assertEqual(response.status_code, 200, 'the configured options connect for real')
        self.assertEqual(options, before)
        payload, _ = readiness._payload()
        self.assertEqual(payload['params']['application_name'], readiness.APPLICATION_NAME)

    def test_an_engine_that_is_not_postgresql_is_not_ready_and_spawns_nothing(self):
        with mock.patch.object(readiness, 'connections', {'default': SimpleNamespace(vendor='sqlite')}), \
                mock.patch.object(readiness_probe, 'run') as run:
            response, elapsed = _timed_get(self)
        run.assert_not_called()
        self.assertEqual((response.status_code, json.loads(response.content)), (503, NOT_READY))
        self.assertEqual(readiness._latest.category, 'unsupported_engine')
        self.assertLess(elapsed, NON_OWNER_BOUND_S)

    def test_an_option_that_cannot_cross_to_the_helper_is_refused_not_approximated(self):
        wrapper = connections['default']
        params = {**wrapper.get_connection_params(), 'sslcontext': object()}
        with mock.patch.object(type(wrapper), 'get_connection_params', return_value=params), \
                mock.patch.object(readiness_probe, 'run') as run:
            response, _ = _timed_get(self)
        run.assert_not_called()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(readiness._latest.category, 'unsupported_configuration')

    def test_one_fixed_log_line_per_change_of_state_and_nothing_sensitive(self):
        def expire():   # skip the two-second wait; freshness has its own tests
            if readiness._latest is not None:
                readiness._latest = readiness._latest._replace(
                    completed_at=time.monotonic() - CONTRACT_FRESH_S - 1)

        port = _closed_port()
        with self.assertLogs('misc_app.endpoints.readiness', level='INFO') as logs:
            with _redirect(port):
                for _ in range(3):
                    expire()
                    self.assertEqual(_timed_get(self)[0].status_code, 503)
            expire()
            self.assertEqual(_timed_get(self)[0].status_code, 200)
        self.assertEqual(readiness._admitted, 4)
        self.assertEqual(logs.output, [
            'WARNING:misc_app.endpoints.readiness:readiness: not ready (connect_failed)',
            'INFO:misc_app.endpoints.readiness:readiness: database connected',
        ])
        self.assertNotIn(str(port), ' '.join(logs.output))


    def test_a_not_ready_answer_writes_no_log_line_per_caller(self):
        with _redirect(_closed_port()):
            self.assertEqual(_timed_get(self)[0].status_code, 503)     # the one state-change line
            with self.assertNoLogs('django.request', level='WARNING'), \
                    self.assertNoLogs('misc_app.endpoints.readiness', level='INFO'):
                for _ in range(5):
                    self.assertEqual(_timed_get(self)[0].status_code, 503)


@needs_postgres
@needs_proc
class ReadinessBoundTests(_ReadinessCase, TestCase):

    def assertCutAtTheBudget(self, response, elapsed, category):
        self.assertEqual((response.status_code, json.loads(response.content)), (503, NOT_READY))
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertLess(elapsed, BOUND_S)
        self.assertEqual(readiness._latest.category, category)
        self.assertEqual(_task_owned_helpers(), set(), 'the helper must be reaped before the answer')
        self.assertIsNone(readiness_probe._slot)

    def test_a_refused_database_is_not_ready_promptly(self):
        fds = _open_fds()
        with _redirect(_closed_port()):
            response, elapsed = _timed_get(self)
        self.assertCutAtTheBudget(response, elapsed, 'connect_failed')
        self.assertLessEqual(_open_fds(), fds)

    def test_a_listener_that_never_handshakes_is_cut_at_the_budget(self):
        fds = _open_fds()
        with _SilentListener() as silent, _redirect(silent.port):
            response, elapsed = _timed_get(self)
        self.assertCutAtTheBudget(response, elapsed, 'timeout')
        self.assertGreater(elapsed, readiness_probe.BUDGET_S - readiness_probe.CLEANUP_RESERVE_S - TOLERANCE_S)
        self.assertLessEqual(_open_fds(), fds)

    def test_a_stall_after_the_handshake_while_sending_the_query(self):
        with _StallProxy('query') as proxy, _redirect(proxy.port):
            response, elapsed = _timed_get(self)
            self.assertCutAtTheBudget(response, elapsed, 'timeout')
        self.assertTrue(_await(lambda: _server_backends() == 0), 'a probe backend was left on the server')

    def test_a_stall_after_the_handshake_while_reading_the_result(self):
        # Also the discriminator for "an actual SELECT 1": the connection succeeds here, so
        # a probe that answered ready after connecting would pass everything else.
        with _StallProxy('result') as proxy, _redirect(proxy.port):
            response, elapsed = _timed_get(self)
            self.assertCutAtTheBudget(response, elapsed, 'timeout')
        self.assertTrue(_await(lambda: _server_backends() == 0), 'a probe backend was left on the server')

    def test_recovery_is_a_fresh_observation_not_a_cached_one(self):
        with _StallProxy('result') as proxy, _redirect(proxy.port):
            stalled, _ = _timed_get(self)
            self.assertEqual(stalled.status_code, 503)
            first = readiness._latest
            proxy.resume()
            cached, _ = _timed_get(self)          # inside the window: the stall is still the answer
            self.assertEqual(cached.status_code, 503)
            self.assertIs(readiness._latest, first)
            time.sleep(max(0.0, first.completed_at + CONTRACT_FRESH_S - time.monotonic()) + 0.05)
            recovered, elapsed = _timed_get(self)
        self.assertEqual((recovered.status_code, json.loads(recovered.content)), (200, READY))
        self.assertGreater(readiness._latest.seq, first.seq)
        self.assertEqual(readiness._latest.category, 'ready')
        self.assertLess(elapsed, BOUND_S)

    def test_a_resolver_style_stall_that_ignores_signals_is_killed_not_cancelled(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(('127.0.0.1', 0))
        body = (
            'import signal, socket, sys\n'
            'for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM, signal.SIGHUP):\n'
            '    signal.signal(s, signal.SIG_IGN)\n'
            'sys.stdin.buffer.read()\n'
            'q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n'
            'q.sendto(b"probe", ("127.0.0.1", %d))\n'
            'q.recvfrom(512)\n' % udp.getsockname()[1]
        )
        try:
            started = time.monotonic()
            outcome = _bounded(self, lambda: readiness_probe.run({'params': {}, 'paths': []}, started,
                                                                  argv=_stall_argv(body)))
            elapsed = time.monotonic() - started
            udp.settimeout(1)
            self.assertEqual(udp.recvfrom(512)[0], b'probe', 'the stand-in really reached its blocking read')
        finally:
            udp.close()
        self.assertEqual(outcome, readiness_probe.Outcome(False, 'timeout'))
        self.assertLess(elapsed, BOUND_S)
        self.assertEqual(_task_owned_helpers(), set())

    def test_only_the_exact_token_and_a_clean_exit_mean_ready(self):
        not_ready = lambda category: readiness_probe.Outcome(False, category)  # noqa: E731
        cases = [
            ('import sys; sys.stdin.buffer.read(); print("ready")', readiness_probe.Outcome(True, 'ready')),
            ('import sys; sys.stdin.buffer.read(); print("ready"); sys.exit(3)', not_ready('helper_failed')),
            ('import sys; sys.stdin.buffer.read()', not_ready('helper_failed')),
            ('import sys; sys.stdin.buffer.read(); print("readyish")', not_ready('helper_failed')),
            ('import sys; sys.stdin.buffer.read(); print("connect_failed"); sys.exit(1)',
             not_ready('connect_failed')),
            ('import sys, time; sys.stdin.buffer.read(); sys.stdout.write("re"); sys.stdout.flush(); '
             'time.sleep(60)', not_ready('timeout')),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                started = time.monotonic()
                outcome = _bounded(self, lambda b=body: readiness_probe.run({'params': {}, 'paths': []}, started,
                                                                             argv=_stall_argv(b)))
                self.assertLess(time.monotonic() - started, BOUND_S)
                self.assertEqual(outcome, expected)
                self.assertEqual(_task_owned_helpers(), set())

    def test_no_interpreter_is_not_ready_without_spawning(self):
        with mock.patch.object(readiness_probe, 'interpreter', return_value=None):
            response, elapsed = _timed_get(self)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(readiness._latest.category, 'helper_unavailable')
        self.assertLess(elapsed, NON_OWNER_BOUND_S)

    def test_only_a_python_binary_is_ever_executed(self):
        with mock.patch.object(sys, 'executable', '/usr/sbin/apache2'):
            chosen = readiness_probe.interpreter()
        self.assertIsNotNone(chosen)
        self.assertTrue(os.path.basename(chosen).startswith('python'), chosen)
        with mock.patch.object(sys, 'executable', ''), mock.patch.object(sys, '_base_executable', '/bin/sh'), \
                mock.patch.object(sys, 'base_exec_prefix', '/nonexistent'):
            self.assertIsNone(readiness_probe.interpreter())

    def test_an_unreaped_helper_blocks_every_new_probe(self):
        blocker = subprocess.Popen(_stall_argv('import time; time.sleep(60)'), stdin=subprocess.DEVNULL)
        try:
            readiness_probe._slot = blocker
            response, elapsed = _timed_get(self)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(readiness._admitted, 0, 'no probe may start beside an unreaped helper')
            self.assertLess(elapsed, NON_OWNER_BOUND_S)
        finally:
            blocker.kill()
            blocker.wait(5)
        self.assertFalse(readiness_probe.helper_busy())
        self.assertIsNone(readiness_probe._slot)
        response, _ = _timed_get(self)
        self.assertEqual(response.status_code, 200)


@needs_postgres
@needs_proc
class ReadinessCoalescingTests(_ReadinessCase, TestCase):

    def _expire(self):
        latest = readiness._latest
        if latest is not None:
            time.sleep(max(0.0, latest.completed_at + CONTRACT_FRESH_S - time.monotonic()) + 0.05)

    def test_repeated_stalled_rounds_do_not_accumulate_anything(self):
        with _SilentListener() as silent, _redirect(silent.port):
            _timed_get(self)                               # warm the process-level objects
            self._expire()
            baseline = (threading.active_count(), _open_fds())
            for round_ in range(4):
                response, elapsed = _timed_get(self)
                self.assertEqual(response.status_code, 503, round_)
                self.assertLess(elapsed, BOUND_S, round_)
                self.assertEqual(_task_owned_helpers(), set(), round_)
                self.assertIsNone(readiness_probe._slot, round_)
                _no_growth(self, baseline, within=2)
                self._expire()
        self.assertEqual(readiness._admitted, 5)

    def test_a_ready_answer_cannot_outlive_two_seconds(self):
        response, _ = _timed_get(self)
        self.assertEqual(response.status_code, 200)
        observed = readiness._latest
        with _SilentListener() as silent, _redirect(silent.port):
            cached, elapsed = _timed_get(self)
            if time.monotonic() - observed.completed_at <= CONTRACT_FRESH_S:
                self.assertEqual(cached.status_code, 200)    # served inside the window
                self.assertLess(elapsed, NON_OWNER_BOUND_S)
            self._expire()
            expired, _ = _timed_get(self)
        self.assertEqual(expired.status_code, 503, 'a ready answer was served past its two seconds')
        self.assertGreater(readiness._latest.seq, observed.seq)

    def test_a_burst_shares_one_probe_and_its_answer(self):
        """Coalescing on its own, with the helper slot taken out of the picture.

        Through the real helper, a caller that fails to coalesce still meets the slot and
        answers 503 promptly, so a burst admits one probe either way and only the ANSWER
        the other callers get differs. Here the probe is an in-process stand-in that spawns
        nothing, so the slot is always free: only the in-flight bookkeeping can make the
        other callers wait for the owner's result rather than each admitting a probe of its
        own. The stand-in holds its answer until every other caller is waiting on it, so
        the interleaving is decided by the callers' own arrival, not by a sleep.
        """
        callers = 8
        arrived = threading.Condition()
        waiting, calls, statuses = [], [], []

        class CountingEvent(threading.Event):
            def wait(self, timeout=None):
                with arrived:
                    waiting.append(timeout)
                    arrived.notify_all()
                return super().wait(timeout)

        class CountingProbe(readiness._Probe):
            def __init__(self, seq, admitted_at):
                super().__init__(seq, admitted_at)
                self.done = CountingEvent()

        def stand_in(admitted_at):
            calls.append(admitted_at)
            with arrived:
                arrived.wait_for(lambda: len(waiting) >= callers - 1, timeout=1.0)
            return readiness_probe.Outcome(True, 'ready')

        barrier = threading.Barrier(callers)

        def call():
            barrier.wait()
            statuses.append(Client().get(URL).status_code)

        with mock.patch.object(readiness, '_Probe', CountingProbe), \
                mock.patch.object(readiness, '_probe', stand_in):
            _bounded(self, lambda: _run_all([threading.Thread(target=call) for _ in range(callers)]))
        self.assertEqual(len(calls), 1, 'every caller in the burst admitted a probe of its own')
        self.assertEqual(readiness._admitted, 1)
        self.assertEqual(len(waiting), callers - 1)
        self.assertTrue(all(0 < timeout <= readiness.NON_OWNER_WAIT_S for timeout in waiting))
        self.assertEqual(statuses, [200] * callers, 'a waiting caller did not get the shared answer')

    def test_a_late_result_never_replaces_a_newer_observation(self):
        now = time.monotonic()
        older, newer = readiness._Probe(1, now), readiness._Probe(2, now)
        readiness._record(newer, readiness_probe.Outcome(False, 'timeout'))
        readiness._record(older, readiness_probe.Outcome(True, 'ready'))
        self.assertEqual((readiness._latest.seq, readiness._latest.ready), (2, False))
        self.assertTrue(older.done.is_set())
        self.assertEqual(Client().get(URL).status_code, 503)

    def test_an_abandoned_owner_neither_blocks_forever_nor_overwrites(self):
        stuck = readiness._Probe(1, time.monotonic() - readiness.ABANDON_AFTER_S - 0.1)
        readiness._inflight, readiness._admitted = stuck, 1
        response, _ = _timed_get(self)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(readiness._latest.seq, 2)
        readiness._record(stuck, readiness_probe.Outcome(False, 'timeout'))
        self.assertEqual((readiness._latest.seq, readiness._latest.ready), (2, True))


def _http_get(url, timeout=WATCHDOG_S):
    started = time.monotonic()
    try:
        with urlopen(Request(url, headers={'Accept': '*/*'}), timeout=timeout) as reply:
            status, body, cache = reply.status, reply.read(), reply.headers.get('Cache-Control')
    except HTTPError as reply:
        status, body, cache = reply.code, reply.read(), reply.headers.get('Cache-Control')
    return status, json.loads(body), cache, time.monotonic() - started


@needs_postgres
@needs_proc
class ReadinessOverHttpTests(_ReadinessCase, LiveServerTestCase):
    """The same guarantees through a real threaded HTTP server and real sockets."""

    def test_ready_over_http(self):
        status, body, cache, elapsed = _bounded(self, lambda: _http_get(self.live_server_url + URL))
        self.assertEqual((status, body, cache), (200, READY, 'no-store, private'))
        self.assertLess(elapsed, BOUND_S)

    def test_a_stalled_burst_admits_one_probe_and_answers_every_caller_promptly(self):
        callers, results, peak = 12, [], [0]
        sampling = threading.Event()

        def sample():
            while not sampling.is_set():
                peak[0] = max(peak[0], len(_task_owned_helpers()))
                time.sleep(0.01)

        baseline = (threading.active_count(), _open_fds())
        with _SilentListener() as silent, _redirect(silent.port):
            barrier = threading.Barrier(callers)

            def call():
                barrier.wait()
                results.append(_http_get(self.live_server_url + URL))

            sampler = threading.Thread(target=sample)
            sampler.start()
            workers = [threading.Thread(target=call) for _ in range(callers)]
            _bounded(self, lambda: _run_all(workers))
            sampling.set()
            sampler.join()
        self.assertEqual(len(results), callers)
        self.assertEqual(readiness._admitted, 1, 'exactly one probe for the whole burst')
        self.assertEqual(peak[0], 1, 'never more than one helper alive')
        self.assertTrue(all((status, body) == (503, NOT_READY) for status, body, _, _ in results))
        times = sorted(elapsed for _, _, _, elapsed in results)
        self.assertLess(times[-1], BOUND_S, 'the owner')
        self.assertLess(times[-2], NON_OWNER_BOUND_S, 'every other caller answers without waiting on the stall')
        self.assertEqual(_task_owned_helpers(), set())
        _no_growth(self, baseline)

    def test_a_healthy_burst_coalesces_onto_one_probe(self):
        callers, results = 8, []
        barrier = threading.Barrier(callers)

        def call():
            barrier.wait()
            results.append(_http_get(self.live_server_url + URL))

        workers = [threading.Thread(target=call) for _ in range(callers)]
        _bounded(self, lambda: _run_all(workers))
        self.assertEqual(readiness._admitted, 1)
        self.assertIn((200, READY), [(status, body) for status, body, _, _ in results])
        for status, body, _, elapsed in results:
            self.assertIn((status, body), ((200, READY), (503, NOT_READY)))
            self.assertLess(elapsed, BOUND_S)


class UnchangedConsumersTests(SimpleTestCase):
    """What this change must NOT move."""

    def test_the_old_health_route_still_answers_200_degraded_on_a_database_failure(self):
        failing = mock.MagicMock()
        failing.cursor.side_effect = OperationalError('down')
        with mock.patch('misc_app.endpoints.health.connection', failing):
            response = Client().get('/api/v1/health/')
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(set(body), {'status', 'database', 'timestamp'})
        self.assertEqual((body['status'], body['database']), ('degraded', 'unreachable'))

    def test_the_old_health_route_still_answers_ok_connected(self):
        with mock.patch('misc_app.endpoints.health.connection', mock.MagicMock()):
            body = json.loads(Client().get('/api/v1/health/').content)
        self.assertEqual((body['status'], body['database']), ('ok', 'connected'))

    @override_settings(ROOT_URLCONF='dinify_backend.urls_admin')
    def test_admin_health_is_still_pure_liveness_and_the_admin_plane_has_no_readiness_route(self):
        with mock.patch.object(readiness_probe, 'run') as run:
            response = Client().get('/admin/v1/health/')
        run.assert_not_called()
        self.assertEqual((response.status_code, json.loads(response.content)), (200, {'status': 'ok'}))
        for path in ('/admin/v1/health/ready/', '/admin/v1/ready/'):
            with self.assertRaises(Resolver404, msg=path):
                resolve(path, urlconf='dinify_backend.urls_admin')

    def test_the_route_resolves_only_on_the_customer_plane(self):
        self.assertEqual(resolve(URL, urlconf='dinify_backend.urls').func, readiness.readiness)

    def test_no_deploy_or_staged_release_consumer_is_switched_to_it(self):
        deploy = (REPO / '.github' / 'workflows' / 'deploy-uat.yml').read_text()
        self.assertIn('https://api-test.dinifyapp.com/uat/api/v1/health/', deploy)
        self.assertIn('if [ "$db_status" -eq 200 ] && [ "$db_state" = "connected" ]', deploy)
        self.assertNotIn('health/ready', deploy)
        from release import hostprofile
        self.assertEqual(hostprofile.PLANE_PATHS, {
            'customer': {'identity': '/api/v1/release/', 'health': '/api/v1/health/'},
            'admin': {'identity': '/admin/v1/release/', 'health': '/admin/v1/health/'},
        })
        for path in list((REPO / 'release').rglob('*.py')) + list((REPO / 'release' / 'staged').glob('*')):
            if path.is_file():
                self.assertNotIn('health/ready', path.read_text(errors='replace'), str(path))
