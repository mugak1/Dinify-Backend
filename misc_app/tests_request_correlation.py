"""
D15 R2 — one server request identity through the real Django handler, on both planes.

What is under test is the CONFIGURED behaviour: the settings modules' own middleware
stacks and ``LOGGING``, and the text the real configured ``console`` handler writes.
Output is captured by swapping that handler's stream, so what a test reads is exactly
what its filter and formatter would put on stderr in production. Nothing here reads a
log file, a host or a collector.

The views below are local test views on a local urlconf; the order commands are
covered through the real endpoints in ``orders_app.tests_order_trace``, which imports
the capture helpers from this module.

The properties, in the order the classes below prove them:

* the common middleware is installed exactly once, outermost, on both planes;
* every answer (success, expected 4xx, routing 404, handled 500, uncaught 500) carries
  a fresh 32-hex server ID, a forged ``X-Request-ID``/``traceparent`` is never adopted,
  and the header, the log line and (on the admin plane) the audit row agree;
* Django's LATE ``log_response`` — written after every middleware has returned and the
  request context has been reset — still carries the ID;
* concurrent requests, reused worker threads and background logs never share an ID or
  a withholding decision;
* a traced command gets exactly one outcome line carrying its final status, even on a
  stack that wraps the common middleware twice, and a diagnostic failure changes no
  answer;
* exception text is withheld only for traced requests, including a chain, a cached
  ``exc_text``, an exception passed as an argument and ``stack_info`` — and untraced
  routes keep today's full text;
* browser exposure is additive for allowed origins only, and the delegated audit row
  and the readiness exemption behave as before.
"""
import contextlib
import importlib
import io
import json
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.conf import settings
from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from django.utils.module_loading import import_string

from platform_admin_app import audit
from platform_admin_app.models import (
    RESULT_FAILURE, RESULT_SUCCESS, SCOPE_VIEW, AdminAuditLog,
)
from platform_admin_app.tests_delegated_session import (
    EXCHANGE_URL, PROFILE_URL, SETUP_URL, _CODE_META, _SESSION_META,
    _ThrottleIsolation, _make_admin, _mint, _session_for,
)

HEX32 = re.compile(r'[0-9a-f]{32}\Z')
COMMON = 'dinify_backend.request_context.RequestContextMiddleware'
ALIAS = 'platform_admin_app.middleware.RequestIDMiddleware'
CLIENT_IP = 'platform_admin_app.middleware.ClientIPMiddleware'
DELEGATED = 'platform_admin_app.delegated_middleware.DelegatedAccessMiddleware'

#: The customer stack as it stood before D15 R2. The common middleware is added in
#: FRONT of it and nothing else moves.
PRE_EXISTING_CUSTOMER_STACK = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    DELEGATED,
]

FORGED = {
    'HTTP_X_REQUEST_ID': 'ffffffffffffffffffffffffffffffff',
    'HTTP_TRACEPARENT': '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
}

#: Planted text. None of it may appear in a TRACED request's console output or in an
#: outcome record; the untraced control must still show its own.
SECRET_MESSAGE = 'SENTINEL-EXC-c41 otp=482917 password=SENTINEL-PASS-c42'
SECRET_CAUSE = 'SENTINEL-CAUSE-c43 phone=256700917771'
SECRET_ARGUMENT = 'SENTINEL-ARG-c44 email=sentinel-c44@example.invalid'
UNTRACED_TEXT = 'SENTINEL-UNTRACED-c45'
SECRETS = ('SENTINEL-EXC-c41', '482917', 'SENTINEL-PASS-c42', 'SENTINEL-CAUSE-c43',
           '256700917771', 'SENTINEL-ARG-c44', 'sentinel-c44@example.invalid')

THIS_FILE = 'misc_app/tests_request_correlation.py'


# --- capture helpers (also used by orders_app.tests_order_trace) --------------------

def console_handler():
    """The configured ``console`` handler — the one ``django.request`` writes through."""
    for handler in logging.getLogger('django.request').handlers:
        if isinstance(handler, logging.StreamHandler):
            return handler
    raise AssertionError('no configured console handler on django.request')


@contextlib.contextmanager
def captured_console():
    """Swap the REAL configured console handler's stream for the duration."""
    handler = console_handler()
    buf = io.StringIO()
    old = handler.setStream(buf)
    try:
        yield buf
    finally:
        handler.setStream(old)


_RECORD_START = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[')
_RID = re.compile(r' rid=([0-9a-f]{32}|-): ')


def console_records(buf):
    """Group captured console text into records (a traceback continues its record)."""
    out, current = [], None
    for line in buf.getvalue().splitlines():
        if _RECORD_START.match(line):
            current = [line]
            out.append(current)
        elif current is not None:
            current.append(line)
    return ['\n'.join(record) for record in out]


def rid_of(record_text):
    match = _RID.search(record_text.splitlines()[0])
    return match.group(1) if match else None


class OutcomeCollector(logging.Handler):
    """Collects the raw ``dinify.outcome`` records, so a test can inspect the exact
    arguments and ``dinify_outcome`` extra — not only the formatted text."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        logging.getLogger('dinify.outcome').addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger('dinify.outcome').removeHandler(self)
        return False


def outcome_blob(record):
    """Everything an outcome record exposes to a handler, as text to scan."""
    return ' '.join([
        record.getMessage(), repr(record.args),
        json.dumps(getattr(record, 'dinify_outcome', {}), sort_keys=True, default=str),
    ])


def _client():
    return Client(raise_request_exception=False)


# --- local test views ----------------------------------------------------------------

view_log = logging.getLogger('misc_app.tests_request_correlation')
_barrier = {'value': None}


def ok_view(request, n='0'):
    view_log.info('inner n=%s', n)
    return HttpResponse('ok')


def bad_view(request):
    return JsonResponse({'status': 400, 'message': 'bad'}, status=400)


def handled_500_view(request, n='0'):
    view_log.info('inner n=%s', n)
    return JsonResponse({'status': 500}, status=500)        # logged AFTER middleware


def untraced_boom_view(request, n='0'):
    view_log.info('inner n=%s', n)
    raise RuntimeError(f'untraced failure {UNTRACED_TEXT} n={n}')


def concurrent_view(request, n):
    _barrier['value'].wait(timeout=5)                       # all three are in-stack
    view_log.info('concurrent n=%s', n)
    _barrier['value'].wait(timeout=5)
    return HttpResponse(n)


def _note(request, **fields):
    from dinify_backend.request_context import ORDER_COMMAND, note_outcome
    note_outcome(request, ORDER_COMMAND, **fields)


def traced_ok_view(request):
    _note(request, action='submit')
    _note(request, outcome='accepted', channel='diner',
          order='6f0e6a4c-1a70-4b0e-9d7e-0c1c7a51e0a1')
    view_log.info('traced inner')
    return JsonResponse({'status': 200, 'message': 'traced'})


def traced_boom_view(request):
    _note(request, action='submit')
    try:
        raise KeyError(SECRET_CAUSE)
    except KeyError as cause:
        raise RuntimeError(SECRET_MESSAGE) from cause


def traced_logging_view(request):
    """Every in-stack exception sink a traced request could hit."""
    _note(request, action='initiate')
    logger = logging.getLogger('misc_app.tests_request_correlation.sinks')
    try:
        try:
            raise KeyError(SECRET_CAUSE)
        except KeyError:
            raise ValueError(SECRET_MESSAGE)                # implicit __context__
    except ValueError as error:
        logger.exception('traced sink one')                 # a traceback sink
        logger.error('traced sink two: %s', error)          # the argument sink
        logger.error('traced sink three: %(e)s', {'e': error})
        logger.error(error)                                 # the exception AS the message
        nested = LookupError(SECRET_ARGUMENT)                # one level down in an argument
        logger.warning('traced sink five: %s', ('context', nested), stack_info=True)
        # Deeper in an argument, and past the depth bound: an exception is still its
        # class, and a container nested beyond the bound is withheld whole.
        logger.warning('traced sink six: %s',
                       [[LookupError(SECRET_ARGUMENT)], [['deeper', nested]]])
        # A set member and a mapping KEY are arguments too.
        logger.warning('traced sink seven: %s %s', {nested}, {nested: 'key'})
        logger.warning(['traced sink eight', nested])       # a container AS the message
        # CONTROL: a container holding no exception is formatted exactly as before,
        # its own type and all.
        logger.warning('traced sink nine: %s', OrderedDict([('kept', ['as', 'is'])]))
    return JsonResponse({'status': 400, 'message': 'handled'}, status=400)


def audited_view(request):
    audit.record_from_request(request, 'admin.probe.d15r2', result=RESULT_SUCCESS,
                              resource_type='Probe')
    view_log.info('audited inner')
    return JsonResponse({'ok': True})


def audited_traced_view(request):
    _note(request, action='submit')
    audit.record_from_request(request, 'admin.probe.d15r2', result=RESULT_SUCCESS,
                              resource_type='Probe')
    view_log.info('audited inner')
    _note(request, outcome='accepted')
    return JsonResponse({'ok': True})


def audited_then_fails_view(request):
    with transaction.atomic():
        audit.record_from_request(request, 'admin.probe.d15r2', result=RESULT_FAILURE,
                                  resource_type='Probe')
        raise RuntimeError('the mutation failed after its audit row was written')


def nested_view(request):
    """A DIFFERENT request driven through the middleware while this one is live."""
    from dinify_backend.request_context import RequestContextMiddleware
    view_log.info('outer before')
    seen = {}

    def inner(inner_request):
        view_log.info('inner request')
        seen['inner'] = inner_request.request_id
        return HttpResponse('inner')

    RequestContextMiddleware(inner)(RequestFactory().get('/inner/'))
    view_log.info('outer after')
    return JsonResponse({'inner': seen['inner']})


urlpatterns = [
    path('probe/ok/', ok_view),
    path('probe/ok/<str:n>/', ok_view),
    path('probe/bad/', bad_view),
    path('probe/h500/<str:n>/', handled_500_view),
    path('probe/boom/<str:n>/', untraced_boom_view),
    path('probe/concurrent/<str:n>/', concurrent_view),
    path('probe/traced/ok/', traced_ok_view),
    path('probe/traced/boom/', traced_boom_view),
    path('probe/traced/sinks/', traced_logging_view),
    path('probe/nested/', nested_view),
    path('admin/v1/probe/health/', ok_view),
    path('admin/v1/probe/h500/<str:n>/', handled_500_view),
    path('admin/v1/probe/audited/', audited_view),
    path('admin/v1/probe/audited-traced/', audited_traced_view),
    path('admin/v1/probe/rollback/', audited_then_fails_view),
]


def admin_stack():
    """The admin plane's REAL production stack."""
    return list(importlib.import_module('dinify_backend.settings_admin').MIDDLEWARE)


class ExplodeOnResponseMiddleware:
    """Test-only: fails while PROCESSING the response, after the view has answered."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        raise RuntimeError(f'response processing failed {SECRET_MESSAGE}')


# --- 1. configuration ------------------------------------------------------------------

class ConfiguredSettingsTests(SimpleTestCase):

    def _resolved(self, stack):
        from dinify_backend.request_context import RequestContextMiddleware
        return [import_string(m) is RequestContextMiddleware for m in stack]

    def test_the_customer_plane_installs_it_once_outermost_and_moves_nothing_else(self):
        stack = list(settings.MIDDLEWARE)
        self.assertEqual(self._resolved(stack).count(True), 1, stack)
        self.assertEqual(stack[0], COMMON)
        self.assertEqual(stack[1:], PRE_EXISTING_CUSTOMER_STACK)

    def test_the_admin_plane_installs_it_once_then_client_ip_then_the_base_stack(self):
        stack = admin_stack()
        self.assertEqual(self._resolved(stack).count(True), 1, stack)
        self.assertEqual(stack[:2], [COMMON, CLIENT_IP])
        self.assertNotIn(DELEGATED, stack, 'delegation stays off the control plane')
        self.assertEqual(stack[2:], [m for m in PRE_EXISTING_CUSTOMER_STACK if m != DELEGATED])

    def test_the_historical_admin_name_is_the_common_class(self):
        from dinify_backend.request_context import RequestContextMiddleware
        from platform_admin_app.middleware import RequestIDMiddleware
        self.assertIs(RequestIDMiddleware, RequestContextMiddleware)

    def test_the_live_console_handler_carries_the_filter_and_the_formatter(self):
        from dinify_backend.request_context import (
            RequestContextFilter, RequestContextFormatter,
        )
        handler = console_handler()
        self.assertTrue(any(isinstance(f, RequestContextFilter) for f in handler.filters))
        self.assertIsInstance(handler.formatter, RequestContextFormatter)
        self.assertIn('rid={request_id}', handler.formatter._fmt)
        self.assertEqual(settings.LOGGING['handlers']['console']['filters'],
                         ['request_context'])
        # the SAME handler object serves root, so every console line is covered
        self.assertIn(handler, logging.getLogger().handlers)

    def test_the_request_id_is_exposed_but_never_invited(self):
        self.assertIn('x-request-id', [h.lower() for h in settings.CORS_EXPOSE_HEADERS])
        self.assertNotIn('x-request-id', [h.lower() for h in settings.CORS_ALLOW_HEADERS])


# --- 2. one identity per request, customer plane --------------------------------------

@override_settings(ROOT_URLCONF=__name__)
class CustomerLifecycleTests(SimpleTestCase):

    def test_every_answer_gets_one_fresh_server_id_and_forged_ids_are_ignored(self):
        seen = []
        for url, status in (('/probe/ok/', 200), ('/probe/bad/', 400),
                            ('/probe/nowhere/', 404), ('/probe/h500/1/', 500),
                            ('/probe/boom/1/', 500)):
            r = _client().get(url, **FORGED)
            self.assertEqual(r.status_code, status, url)
            rid = r.headers.get('X-Request-ID')
            self.assertRegex(rid or '', HEX32, f'{url}: no server X-Request-ID')
            self.assertNotEqual(rid, FORGED['HTTP_X_REQUEST_ID'])
            self.assertNotIn('0af7651916cd43dd8448eb211c80319c', rid)
            seen.append(rid)
        self.assertEqual(len(set(seen)), len(seen), 'an ID is fresh per request')

    def test_a_handled_500_logged_after_the_middleware_unwound_keeps_its_id(self):
        """Django writes this line in ``BaseHandler.get_response`` AFTER every
        middleware has returned — the request context has already been reset."""
        from dinify_backend.request_context import current_request_id
        with captured_console() as buf:
            r = _client().get('/probe/h500/7/')
        rid = r.headers.get('X-Request-ID')
        self.assertRegex(rid or '', HEX32)
        late = [x for x in console_records(buf) if 'django.request' in x.splitlines()[0]]
        self.assertEqual(len(late), 1, console_records(buf))
        self.assertIn('/probe/h500/7/', late[0])
        self.assertEqual(rid_of(late[0]), rid, late[0])
        inner = [x for x in console_records(buf) if 'inner n=7' in x]
        self.assertEqual(rid_of(inner[0]), rid)
        self.assertIsNone(current_request_id(), 'no context outlives its request')

    def test_an_uncaught_exception_is_logged_in_stack_with_the_same_id(self):
        with captured_console() as buf:
            r = _client().get('/probe/boom/8/')
        self.assertEqual(r.status_code, 500)
        rid = r.headers.get('X-Request-ID')
        self.assertRegex(rid or '', HEX32)
        failure = [x for x in console_records(buf) if '/probe/boom/8/' in x.splitlines()[0]]
        self.assertEqual(len(failure), 1, console_records(buf))
        self.assertEqual(rid_of(failure[0]), rid)
        self.assertEqual(rid_of([x for x in console_records(buf) if 'inner n=8' in x][0]), rid)

    def test_no_response_body_changes(self):
        r = _client().get('/probe/bad/')
        self.assertEqual(r.json(), {'status': 400, 'message': 'bad'})
        self.assertNotIn('request_id', r.content.decode())


# --- 3. concurrency, worker reuse, background logs --------------------------------------

@override_settings(ROOT_URLCONF=__name__)
class ConcurrencyAndIsolationTests(SimpleTestCase):

    def test_truly_concurrent_requests_never_share_an_id(self):
        _barrier['value'] = threading.Barrier(3)

        def run(n):
            return n, _client().get(f'/probe/concurrent/{n}/').headers.get('X-Request-ID')

        try:
            with captured_console() as buf, ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(run, ['a', 'b', 'c']))
        finally:
            _barrier['value'] = None
        ids = [rid for _n, rid in results]
        self.assertEqual(len(set(ids)), 3, ids)
        for n, rid in results:
            self.assertRegex(rid or '', HEX32)
            line = [x for x in console_records(buf) if x.endswith(f'concurrent n={n}')]
            self.assertEqual(len(line), 1)
            self.assertEqual(rid_of(line[0]), rid, line[0])

    def test_sixty_mixed_requests_on_three_reused_workers_never_bleed(self):
        paths = [(i, f"/probe/{('ok', 'h500', 'boom')[i % 3]}/{i}/") for i in range(60)]

        def run(item):
            i, url = item
            return i, url, _client().get(url).headers.get('X-Request-ID'), threading.get_ident()

        with captured_console() as buf:
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(run, paths))
                list(pool.map(lambda _: view_log.info('background after requests'), range(6)))
            view_log.info('main thread, no request')
        self.assertLessEqual(len({r[3] for r in results}), 3)
        records = console_records(buf)
        for i, url, rid, _thread in results:
            mine = [x for x in records
                    if x.splitlines()[0].endswith(f'inner n={i}') or url in x.splitlines()[0]]
            self.assertTrue(mine, url)
            for record in mine:
                self.assertEqual(rid_of(record), rid, record.splitlines()[0])
        background = [x for x in records if 'background after requests' in x
                      or 'main thread, no request' in x]
        self.assertEqual(len(background), 7)
        for record in background:
            self.assertEqual(rid_of(record), '-', record)

    def test_withholding_never_bleeds_between_requests_on_one_worker(self):
        """A traced failure then an untraced one on the SAME thread, and the reverse:
        the untraced request keeps today's full text, the traced one never shows its."""
        from dinify_backend.request_context import current_request_id

        def sequence(first, second):
            out = []
            for url in (first, second):
                out.append(_client().get(url).headers.get('X-Request-ID'))
            out.append(current_request_id())
            view_log.info('background on the worker')
            return out

        for order in (('/probe/traced/boom/', '/probe/boom/5/'),
                      ('/probe/boom/5/', '/probe/traced/boom/')):
            with self.subTest(order=order):
                with captured_console() as buf, ThreadPoolExecutor(max_workers=1) as pool:
                    rids = pool.submit(sequence, *order).result()
                ids = dict(zip(order, rids[:2]))
                self.assertIsNone(rids[2], 'the worker keeps no request context')
                records = console_records(buf)
                untraced = [x for x in records if rid_of(x) == ids['/probe/boom/5/']]
                traced = [x for x in records if rid_of(x) == ids['/probe/traced/boom/']]
                self.assertTrue(untraced and traced)
                self.assertTrue(any(UNTRACED_TEXT in x for x in untraced),
                                'an untraced route keeps its existing exception text')
                for record in traced:
                    for secret in SECRETS:
                        self.assertNotIn(secret, record)
                background = [x for x in records if 'background on the worker' in x]
                self.assertEqual(rid_of(background[0]), '-')

    def test_logs_with_no_request_or_a_foreign_request_format_cleanly(self):
        class ForeignRequest:
            request_id = 'not-a-server-id'

        class HostileRequest:
            def __getattr__(self, name):
                raise RuntimeError('hostile attribute access')

        with captured_console() as buf:
            for foreign in (ForeignRequest(), HostileRequest(), 'a string', 42):
                logging.getLogger('django.request').error(
                    'foreign %s', 'x', extra={'request': foreign, 'status_code': 500})
            view_log.warning('no request at all')
            view_log.warning('caller supplied', extra={'request_id': 'f' * 32})
        text = buf.getvalue()
        self.assertNotIn('--- Logging error ---', text)
        self.assertNotIn('not-a-server-id', text)
        records = console_records(buf)
        self.assertEqual(len(records), 6)
        for record in records:
            self.assertEqual(rid_of(record), '-', record)

    def test_a_nested_request_gets_its_own_id_and_restores_the_outer_one(self):
        with captured_console() as buf:
            r = _client().get('/probe/nested/')
        outer, inner = r.headers.get('X-Request-ID'), r.json()['inner']
        self.assertRegex(inner, HEX32)
        self.assertNotEqual(outer, inner)
        by_text = {x.split(': ', 1)[-1]: rid_of(x) for x in console_records(buf)}
        self.assertEqual(by_text['outer before'], outer)
        self.assertEqual(by_text['inner request'], inner)
        self.assertEqual(by_text['outer after'], outer, 'the outer context is restored')


# --- 4. the admin plane: retained behaviour, audit agreement -------------------------------

class AdminPlaneTests(TestCase):

    def run(self, result=None):
        with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=admin_stack()):
            return super().run(result)

    def test_admin_keeps_one_fresh_server_id_and_ignores_a_forged_one(self):
        seen = []
        for url, status in (('/admin/v1/probe/health/', 200),
                            ('/admin/v1/probe/nowhere/', 404),
                            ('/admin/v1/probe/h500/2/', 500)):
            r = _client().get(url, **FORGED)
            self.assertEqual(r.status_code, status)
            self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)
            self.assertNotEqual(r.headers['X-Request-ID'], FORGED['HTTP_X_REQUEST_ID'])
            seen.append(r.headers['X-Request-ID'])
        self.assertEqual(len(set(seen)), 3)

    def test_the_committed_audit_row_the_header_and_the_log_agree(self):
        with captured_console() as buf:
            r = _client().get('/admin/v1/probe/audited/', **FORGED)
        rid = r.headers.get('X-Request-ID')
        self.assertEqual(AdminAuditLog.objects.get(action='admin.probe.d15r2').request_id, rid)
        line = [x for x in console_records(buf) if 'audited inner' in x][0]
        self.assertEqual(rid_of(line), rid, line)

    def test_a_late_admin_500_line_keeps_its_id(self):
        with captured_console() as buf:
            r = _client().get('/admin/v1/probe/h500/3/')
        late = [x for x in console_records(buf) if '/admin/v1/probe/h500/3/' in x]
        self.assertEqual(rid_of(late[0]), r.headers.get('X-Request-ID'))

    def test_a_rolled_back_audit_still_leaves_a_correlated_failure_line(self):
        with captured_console() as buf:
            r = _client().get('/admin/v1/probe/rollback/')
        self.assertEqual(r.status_code, 500)
        self.assertFalse(AdminAuditLog.objects.filter(action='admin.probe.d15r2').exists(),
                         'the audit row rolled back with the mutation')
        line = [x for x in console_records(buf) if '/admin/v1/probe/rollback/' in x][0]
        self.assertEqual(rid_of(line), r.headers.get('X-Request-ID'), line)


class DoubledStackTests(TestCase):
    """The shape existing admin tests build: the historical alias prepended to a base
    stack that already starts with the common class. ONE ID and ONE outcome."""

    def _stacks(self):
        return {
            'customer': [ALIAS, *settings.MIDDLEWARE],
            'admin-tests': [ALIAS, CLIENT_IP, *settings.MIDDLEWARE],
        }

    def test_a_doubled_stack_yields_one_id_and_one_outcome_attempt(self):
        for label, stack in self._stacks().items():
            with self.subTest(stack=label):
                before = list(AdminAuditLog.objects.values_list('pk', flat=True))
                with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=stack), \
                        captured_console() as buf, OutcomeCollector() as outcomes:
                    r = _client().get('/admin/v1/probe/audited-traced/')
                rid = r.headers.get('X-Request-ID')
                self.assertRegex(rid or '', HEX32)
                rows = AdminAuditLog.objects.exclude(pk__in=before).filter(
                    action='admin.probe.d15r2')
                self.assertEqual([row.request_id for row in rows], [rid])
                self.assertEqual(rid_of([x for x in console_records(buf)
                                         if 'audited inner' in x][0]), rid)
                self.assertEqual(len(outcomes.records), 1, 'exactly one outcome attempt')
                self.assertEqual(outcomes.records[0].dinify_outcome['status'], 200)
                self.assertEqual(rid_of([x for x in console_records(buf)
                                         if 'dinify.outcome' in x][0]), rid)


    def test_the_outer_owner_reports_the_final_status_through_a_doubled_stack(self):
        """Something BETWEEN the two wrappers turns a success into a 500. The one
        outcome record must carry that final 500 — not the inner wrapper's 200."""
        stack = [ALIAS, f'{__name__}.ExplodeOnResponseMiddleware', *settings.MIDDLEWARE]
        with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=stack), \
                captured_console() as buf, OutcomeCollector() as outcomes:
            r = _client().get('/probe/traced/ok/')
        self.assertEqual(r.status_code, 500)
        rid = r.headers.get('X-Request-ID')
        self.assertRegex(rid or '', HEX32)
        self.assertEqual(len(outcomes.records), 1, 'exactly one outcome attempt')
        fields = outcomes.records[0].dinify_outcome
        self.assertEqual((fields['status'], fields['outcome']), (500, 'unclassified'))
        for record in console_records(buf):
            self.assertEqual(rid_of(record), rid)
            for secret in SECRETS:
                self.assertNotIn(secret, record)


# --- 5. the traced-command outcome, and failures of the diagnostics themselves ------------

@override_settings(ROOT_URLCONF=__name__)
class TraceEmissionTests(SimpleTestCase):

    def test_one_outcome_line_with_the_final_status_and_bounded_fields(self):
        with captured_console() as buf, OutcomeCollector() as outcomes:
            r = _client().get('/probe/traced/ok/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(outcomes.records), 1)
        record = outcomes.records[0]
        self.assertEqual(record.dinify_outcome, {
            'event': 'order.command', 'action': 'submit', 'status': 200,
            'outcome': 'accepted', 'reason': None, 'channel': 'diner',
            'order': '6f0e6a4c-1a70-4b0e-9d7e-0c1c7a51e0a1', 'intent': None,
            'error': None, 'where': None,
        })
        self.assertEqual(record.levelno, logging.INFO)
        line = [x for x in console_records(buf) if 'dinify.outcome' in x]
        self.assertEqual(len(line), 1)
        self.assertEqual(rid_of(line[0]), r.headers['X-Request-ID'])
        self.assertIn('order.command action=submit status=200 outcome=accepted', line[0])

    def test_an_untraced_request_writes_no_outcome(self):
        with OutcomeCollector() as outcomes:
            _client().get('/probe/ok/')
            _client().get('/probe/boom/1/')
        self.assertEqual(outcomes.records, [])

    def test_an_unexpected_failure_is_unhandled_with_a_project_location(self):
        with OutcomeCollector() as outcomes:
            r = _client().get('/probe/traced/boom/')
        self.assertEqual(r.status_code, 500)
        fields = outcomes.records[0].dinify_outcome
        self.assertEqual((fields['outcome'], fields['status'], fields['error']),
                         ('unhandled', 500, 'RuntimeError'))
        self.assertRegex(fields['where'], rf'\A{re.escape(THIS_FILE)}:\d+:traced_boom_view\Z')
        self.assertLessEqual(len(fields['where']), 160)
        self.assertEqual(outcomes.records[0].levelno, logging.ERROR)

    def test_a_response_phase_failure_after_a_success_note_is_not_reported_as_success(self):
        stack = [COMMON, f'{__name__}.ExplodeOnResponseMiddleware', *settings.MIDDLEWARE[1:]]
        with override_settings(MIDDLEWARE=stack), captured_console() as buf, \
                OutcomeCollector() as outcomes:
            r = _client().get('/probe/traced/ok/')
        self.assertEqual(r.status_code, 500)
        fields = outcomes.records[0].dinify_outcome
        self.assertEqual(fields['status'], 500)
        self.assertEqual(fields['outcome'], 'unclassified',
                         'the final status overrides the earlier success annotation')
        self.assertNotIn('accepted', outcomes.records[0].getMessage())
        for record in console_records(buf):
            for secret in SECRETS:
                self.assertNotIn(secret, record)

    def test_a_failing_outcome_emission_changes_no_answer_and_still_cleans_up(self):
        from dinify_backend import request_context
        control = _client().get('/probe/traced/ok/')
        with mock.patch.object(request_context, '_outcome_fields',
                               side_effect=RuntimeError('diagnostics broke')), \
                captured_console() as buf:
            r = _client().get('/probe/traced/ok/')
        self.assertEqual((r.status_code, r.content), (control.status_code, control.content))
        self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)
        self.assertIsNone(request_context.current_request_id())
        fallback = [x for x in console_records(buf) if 'dinify.outcome' in x]
        self.assertEqual(len(fallback), 1)
        self.assertIn('outcome unavailable', fallback[0])
        self.assertEqual(rid_of(fallback[0]), r.headers['X-Request-ID'])
        self.assertNotIn('diagnostics broke', buf.getvalue())

    def test_a_failing_annotation_changes_no_answer(self):
        from dinify_backend import request_context
        control = _client().get('/probe/traced/ok/')
        with mock.patch.object(request_context, '_canonical_uuid',
                               side_effect=RuntimeError('annotation broke')):
            r = _client().get('/probe/traced/ok/')
        self.assertEqual((r.status_code, r.content), (control.status_code, control.content))
        self.assertIsNone(request_context.current_request_id())

    def test_unknown_fields_and_values_are_dropped_not_echoed(self):
        from dinify_backend import request_context
        request = RequestFactory().get('/x')
        captured = {}

        def view(req):
            request_context.note_outcome(req, request_context.ORDER_COMMAND, action='submit')
            request_context.note_outcome(
                req, request_context.ORDER_COMMAND, outcome='totally_fine',
                reason='Free text with SENTINEL-ARG-c44', channel='kitchen',
                order='not-a-uuid', intent=12345, body='SENTINEL-EXC-c41')
            request_context.note_outcome(req, 'some.other.event', action='initiate')
            return HttpResponse('x')

        with OutcomeCollector() as outcomes:
            request_context.RequestContextMiddleware(view)(request)
        captured = outcomes.records[0].dinify_outcome
        self.assertEqual((captured['action'], captured['outcome'], captured['reason'],
                          captured['channel'], captured['order'], captured['intent']),
                         ('submit', 'unclassified', None, None, None, None))
        for secret in SECRETS:
            self.assertNotIn(secret, outcome_blob(outcomes.records[0]))


# --- 6. exception text is withheld for traced requests only ------------------------------

@override_settings(ROOT_URLCONF=__name__)
class ExceptionWithholdingTests(SimpleTestCase):

    def test_every_in_stack_sink_is_withheld_but_keeps_class_frames_and_id(self):
        sink_logger = logging.getLogger('misc_app.tests_request_correlation.sinks')
        other = OutcomeCollector()           # a SECOND handler that formats first
        other.setFormatter(logging.Formatter('%(message)s'))
        cached = []

        def pre_format(record):
            cached.append(other.format(record))     # caches record.exc_text in full
            other.records.append(record)

        other.emit = pre_format
        sink_logger.addHandler(other)
        try:
            with captured_console() as buf:
                r = _client().get('/probe/traced/sinks/')
        finally:
            sink_logger.removeHandler(other)
        self.assertEqual(r.status_code, 400)
        rid = r.headers.get('X-Request-ID')
        records = console_records(buf)
        sinks = [x for x in records if '.sinks rid=' in x.splitlines()[0]]
        self.assertEqual(len(sinks), 9, records)
        text = '\n'.join(records)
        for record in records:
            self.assertEqual(rid_of(record), rid)
            for secret in SECRETS:
                self.assertNotIn(secret, record)
        self.assertIn('exception text withheld', text)
        self.assertIn(f'File "{THIS_FILE}"', text)
        self.assertIn('in traced_logging_view', text)
        self.assertIn('KeyError', text)       # the chain keeps its classes
        self.assertIn('traced sink two: ValueError', text)
        self.assertIn('traced sink three: ValueError', text)
        self.assertIn("traced sink five: ('context', 'LookupError')", text)
        self.assertIn("traced sink six: [['LookupError'], ['<withheld>']]", text)
        self.assertIn("traced sink seven: {'LookupError'} {'LookupError': 'key'}", text)
        self.assertIn("['traced sink eight', 'LookupError']", text)
        self.assertIn("traced sink nine: OrderedDict({'kept': ['as', 'is']})", text)
        self.assertTrue(any(x.splitlines()[0].endswith(': ValueError') for x in sinks),
                        'an exception passed AS the message renders as its class')
        self.assertIn('Stack (withheld)', text)
        self.assertNotIn('File "/', text, 'no absolute or site-packages path')
        # The other handler's view of the SAME records is untouched: the console
        # formatter worked on a copy, so nothing was mutated underneath it.
        self.assertTrue(any('SENTINEL-EXC-c41' in c for c in cached))
        first = other.records[0]
        self.assertIsNotNone(first.exc_info)
        self.assertIn('SENTINEL-EXC-c41', first.exc_text)

    def test_a_formatting_failure_on_a_traced_record_reveals_nothing(self):
        from dinify_backend import request_context
        with mock.patch.object(request_context, '_withheld_traceback',
                               side_effect=RuntimeError('format broke')), \
                captured_console() as buf:
            r = _client().get('/probe/traced/sinks/')
        self.assertEqual(r.status_code, 400)
        text = buf.getvalue()
        self.assertNotIn('--- Logging error ---', text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn('withheld', text)

    def test_untraced_routes_keep_their_existing_exception_text(self):
        with captured_console() as buf:
            _client().get('/probe/boom/6/')
        failure = [x for x in console_records(buf) if '/probe/boom/6/' in x.splitlines()[0]][0]
        self.assertIn(UNTRACED_TEXT, failure)
        self.assertIn('Traceback (most recent call last)', failure)


# --- 7. browser visibility ------------------------------------------------------------------

ALLOWED = 'https://app.allowed.example'
OTHER = 'https://elsewhere.example'


def _exposed(r):
    return [h.strip().lower() for h in
            r.headers.get('Access-Control-Expose-Headers', '').split(',') if h.strip()]


@override_settings(ROOT_URLCONF=__name__, CORS_ORIGIN_ALLOW_ALL=False,
                   CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=[ALLOWED])
class CorsTests(SimpleTestCase):

    def test_an_allowed_origin_may_read_the_request_id(self):
        r = _client().get('/probe/ok/', HTTP_ORIGIN=ALLOWED)
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), ALLOWED)
        self.assertIn('x-request-id', _exposed(r))
        self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)

    def test_a_disallowed_origin_gains_nothing(self):
        r = _client().get('/probe/ok/', HTTP_ORIGIN=OTHER)
        self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))
        self.assertEqual(_exposed(r), [])
        self.assertIsNone(r.headers.get('Access-Control-Allow-Credentials'))

    def test_no_client_is_invited_to_send_an_id(self):
        pre = _client().options('/probe/ok/', HTTP_ORIGIN=ALLOWED,
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='x-request-id')
        allowed = [h.strip().lower() for h in
                   pre.headers.get('Access-Control-Allow-Headers', '').split(',')]
        self.assertNotIn('x-request-id', allowed)
        self.assertRegex(pre.headers.get('X-Request-ID') or '', HEX32)


class AdminCorsTests(SimpleTestCase):

    def test_the_admin_plane_stays_same_origin(self):
        admin = importlib.import_module('dinify_backend.settings_admin')
        with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=admin_stack(),
                               CORS_ORIGIN_ALLOW_ALL=admin.CORS_ORIGIN_ALLOW_ALL,
                               CORS_ALLOW_ALL_ORIGINS=False,
                               CORS_ALLOWED_ORIGINS=admin.CORS_ALLOWED_ORIGINS,
                               CORS_ALLOW_CREDENTIALS=admin.CORS_ALLOW_CREDENTIALS):
            r = _client().get('/admin/v1/probe/health/', HTTP_ORIGIN=ALLOWED)
        self.assertEqual(admin.CORS_ALLOWED_ORIGINS, [])
        self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))
        self.assertEqual(_exposed(r), [])
        self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)


# --- 8. delegated audit correlation, and the readiness exemption ----------------------------

class DelegatedCorrelationTests(_ThrottleIsolation, TestCase):
    """The customer urlconf and stack, with a real delegated session."""

    def setUp(self):
        super().setUp()
        from dinify_backend.configss.string_definitions import RestaurantStatus_Live
        from restaurants_app.models import Restaurant
        from users_app.models import User
        self.admin = _make_admin()
        owner = User.objects.create_user(
            first_name='R2', last_name='Owner', email='r2-delegated-owner@t.com',
            phone_number='256709931001', username='256709931001', country='Uganda',
            password='correct-horse-battery', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='R2 Delegated', location='loc', status=RestaurantStatus_Live, owner=owner)

    def _new_rows(self, before):
        return list(AdminAuditLog.objects.exclude(pk__in=before))

    def test_a_refused_delegated_request_carries_one_id_on_audit_header_and_log(self):
        before = list(AdminAuditLog.objects.values_list('pk', flat=True))
        with captured_console() as buf:
            r = _client().get(SETUP_URL, **{_SESSION_META: 'junk-session'})
        self.assertEqual(r.status_code, 401)
        rid = r.headers.get('X-Request-ID')
        self.assertRegex(rid or '', HEX32)
        rows = self._new_rows(before)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].request_id, rid)
        for record in console_records(buf):
            self.assertIn(rid_of(record), (rid, '-'))

    def test_a_valid_session_off_the_allowlist_is_refused_with_one_id(self):
        token, _ctx = _session_for(self.admin, self.restaurant, SCOPE_VIEW)
        before = list(AdminAuditLog.objects.values_list('pk', flat=True))
        r = _client().get(PROFILE_URL, **{_SESSION_META: token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._new_rows(before)[0].request_id, r.headers.get('X-Request-ID'))

    def test_an_allowed_delegated_read_keeps_its_decision_and_carries_the_id(self):
        from platform_admin_app.delegated_middleware import ACTING_AS_HEADER
        token, _ctx = _session_for(self.admin, self.restaurant, SCOPE_VIEW)
        before = list(AdminAuditLog.objects.values_list('pk', flat=True))
        r = _client().get(SETUP_URL, **{_SESSION_META: token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get(ACTING_AS_HEADER), 'delegation')
        self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)
        self.assertEqual(self._new_rows(before), [], 'a delegated safe read is not audited')

    def test_the_code_exchange_audit_reuses_the_request_id(self):
        raw_code, _grant = _mint(self.admin, self.restaurant, SCOPE_VIEW)
        before = list(AdminAuditLog.objects.values_list('pk', flat=True))
        r = _client().post(EXCHANGE_URL, **{_CODE_META: raw_code})
        self.assertEqual(r.status_code, 201)      # the body holds a token: never printed
        rows = self._new_rows(before)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.request_id, r.headers.get('X-Request-ID'))

    def test_standalone_stamping_still_mints_a_server_id(self):
        from platform_admin_app.delegated_middleware import stamp_audit_context
        request = RequestFactory().get('/x', HTTP_X_REQUEST_ID='f' * 32)
        stamp_audit_context(request)
        self.assertRegex(request.request_id, HEX32)
        self.assertNotEqual(request.request_id, 'f' * 32)
        request.request_id = 'not-trusted'
        stamp_audit_context(request)
        self.assertRegex(request.request_id, HEX32)


class ReadinessThroughTheCommonStackTests(TestCase):
    READY_URL = '/api/v1/health/ready/'

    def test_junk_delegation_and_bearer_input_still_bypass_the_gate(self):
        from misc_app import readiness_probe
        from misc_app.endpoints import readiness
        from platform_admin_app import delegated_middleware, delegated_sessions
        from platform_admin_app.delegated_middleware import ACTING_AS_HEADER

        readiness._reset_for_tests()
        before = AdminAuditLog.objects.count()
        with mock.patch.object(readiness_probe, 'run',
                               return_value=readiness_probe.Outcome(True, 'ready')) as run, \
                mock.patch.object(delegated_middleware, 'session_token_from_request',
                                  side_effect=AssertionError('delegation header read')), \
                mock.patch.object(delegated_sessions, 'resolve_session',
                                  side_effect=AssertionError('session resolved')), \
                CaptureQueriesContext(connection) as queries, \
                OutcomeCollector() as outcomes:
            r = _client().get(self.READY_URL, **{_SESSION_META: 'junk',
                                                 'HTTP_AUTHORIZATION': 'Bearer junk'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(queries), 0, [q['sql'] for q in queries])
        self.assertEqual(AdminAuditLog.objects.count(), before)
        self.assertIsNone(r.headers.get(ACTING_AS_HEADER))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(outcomes.records, [])
        self.assertRegex(r.headers.get('X-Request-ID') or '', HEX32)

    def test_not_ready_callers_add_no_per_caller_line(self):
        from misc_app import readiness_probe
        from misc_app.endpoints import readiness

        readiness._reset_for_tests()
        with mock.patch.object(readiness_probe, 'run',
                               return_value=readiness_probe.Outcome(False, 'timeout')), \
                captured_console() as buf, OutcomeCollector() as outcomes:
            codes = [_client().get(self.READY_URL).status_code for _ in range(3)]
        self.assertEqual(codes, [503, 503, 503])
        records = console_records(buf)
        self.assertEqual(len([x for x in records if 'not ready' in x]), 1, records)
        self.assertFalse([x for x in records if 'django.request' in x.splitlines()[0]])
        self.assertEqual(outcomes.records, [])
