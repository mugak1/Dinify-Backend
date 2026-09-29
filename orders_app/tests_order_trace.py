"""
D15 R2 — the bounded order-command trace, driven through the REAL order endpoints.

Every request here goes through the real customer middleware stack, the real
``OrdersEndpoint`` / ``V2OrdersEndpoint`` and the real controllers, on the repository's
own ``AcceptanceFixture`` (one restaurant, two tables, one dish). A request's trace is
found the way an operator would find it: by the ``X-Request-ID`` its response carried.

What each test pins:

* the outcome word is TRUE for the answer that was given — a refusal names its fixed
  code, an idempotent replay is ``already_accepted`` and not a second order, and an
  ``initiate`` success is ``order_returned`` whether the order it returned is a new
  draft, a replayed draft or an order that was accepted since;
* ``unhandled`` and ``unclassified`` never claim that nothing committed, and a failure
  AFTER a successful answer was built is never reported as that success;
* a foreign identifier the caller attempted is never recorded;
* planted secrets (headers, query string, body, exception messages, a chained cause,
  and the controller that interpolates an exception into its message) reach neither
  the outcome record nor any console line of the traced request;
* the trace adds no database query and changes no response, measured against an
  untraced twin request on an identical fixture;
* the endpoint's reason allowlist is explicit and cannot silently drift from the
  controllers' ``REASON_*`` constants.

The synthetic failures are labelled where they are made: a patched callee raising a
planted exception. Everything else is a real path.
"""
import ast
import contextlib
import inspect
import json
import re
import uuid
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.db import OperationalError, connection
from django.test import Client, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from misc_app.tests_request_correlation import (
    ALIAS, COMMON, HEX32, OutcomeCollector, captured_console, console_records,
    outcome_blob, rid_of,
)
from orders_app.controllers import con_orders, manage_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderAcceptance
from orders_app.tests_order_acceptance import AcceptanceFixture
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

SUBMIT = '/api/v1/orders/submit/'
RETIRE = '/api/v1/orders/retire-quote/'
INITIATE = '/api/v2/orders/initiate/'

SENT = {
    'authorization': 'SENTINEL-AUTH-d41',
    'cookie': 'SENTINEL-COOKIE-d42',
    'query': 'SENTINEL-QS-d43',
    'body': 'SENTINEL-BODY-d44',
    'phone': '256700917773',
    'email': 'sentinel-d46@example.invalid',
    'password': 'SENTINEL-PASS-d47',
    'otp': '573920',
    'exception': 'SENTINEL-EXC-d48',
    'cause': 'SENTINEL-CAUSE-d49',
    'forged_rid': 'SENTINEL-RID-d4a',
    'traceparent': 'SENTINELTRACEd4b',
}
EXC_TEXT = (f"{SENT['exception']} otp={SENT['otp']} password={SENT['password']} "
            f"phone={SENT['phone']} email={SENT['email']}")

_VOLATILE = (
    (re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'), '<uuid>'),
    (re.compile(r'\b[0-9a-f]{64}\b'), '<ref>'),
    (re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?'), '<time>'),
)


def _normalised(content):
    text = content.decode('utf-8')
    for pattern, placeholder in _VOLATILE:
        text = pattern.sub(placeholder, text)
    return text


def _chained_failure():
    """A planted exception whose CAUSE carries text too."""
    try:
        raise KeyError(SENT['cause'])
    except KeyError as cause:
        error = RuntimeError(EXC_TEXT)
        error.__cause__ = cause
        return error


def _untraced_stack():
    return [m for m in settings.MIDDLEWARE if m != COMMON]


class OrderCommandTraceTests(AcceptanceFixture):

    def setUp(self):
        super().setUp()
        # A Client caches its middleware chain on first use, which would silently
        # ignore a per-request MIDDLEWARE override — so each request gets a new one.
        self.c = Client(raise_request_exception=False)
        self.session = issue_table_session(self.table)
        self.session_b = issue_table_session(self.table_b)

    # -- plumbing -------------------------------------------------------------------

    def _secrets(self):
        return [*SENT.values(), self.session, self.session_b]

    def _send(self, method, url, body=None, *, session=None, extra=None, raw=None,
              stack=None, safe=True):
        headers = {
            'HTTP_X_REQUEST_ID': SENT['forged_rid'],
            'HTTP_TRACEPARENT': f"00-{SENT['traceparent']}-b7ad6b7169203331-01",
            'HTTP_COOKIE': f"sessionid={SENT['cookie']}",
        }
        if session is not False:
            headers['HTTP_X_DINER_SESSION'] = session or self.session
        headers.update(extra or {})
        data = raw if raw is not None else json.dumps(body)
        query = f"?otp={SENT['otp']}&token={SENT['query']}"
        with contextlib.ExitStack() as stack_cm:
            if stack is not None:
                stack_cm.enter_context(override_settings(MIDDLEWARE=stack))
            buf = stack_cm.enter_context(captured_console())
            outcomes = stack_cm.enter_context(OutcomeCollector())
            queries = stack_cm.enter_context(CaptureQueriesContext(connection))
            client = Client(raise_request_exception=False)
            r = getattr(client, method)(url + query, data=data,
                                        content_type='application/json', **headers)
        sent = SimpleNamespace(response=r, outcomes=list(outcomes.records),
                               console=console_records(buf), queries=len(queries))
        self.assertNotIn('request_id', r.content.decode('utf-8', 'replace'),
                         'no API body changes')
        if safe:
            self._assert_nothing_leaked(sent)
        return sent

    def _assert_nothing_leaked(self, sent):
        rid = sent.response.headers.get('X-Request-ID')
        for record in sent.outcomes:
            for secret in self._secrets():
                self.assertNotIn(secret, outcome_blob(record))
        mine = [x for x in sent.console if rid_of(x) == rid]
        self.assertTrue(mine, 'the traced request wrote console lines under its own ID')
        for record in mine:
            for secret in self._secrets():
                self.assertNotIn(secret, record, record)

    def _trace(self, sent):
        """The ONE outcome record for this request, found by its response ID."""
        rid = sent.response.headers.get('X-Request-ID')
        self.assertRegex(rid or '', HEX32, 'every answer carries a server request ID')
        self.assertEqual(len(sent.outcomes), 1, 'exactly one outcome record per command')
        line = [x for x in sent.console if 'dinify.outcome' in x.splitlines()[0]]
        self.assertEqual(len(line), 1)
        self.assertEqual(rid_of(line[0]), rid, 'the outcome line carries the response ID')
        return sent.outcomes[0].dinify_outcome

    def _jwt(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(self.owner).access_token}'}

    def _initiate_body(self, key):
        return {'items': [{'item': str(self.item.pk), 'quantity': 1}],
                'client_order_id': key, 'note': SENT['body'], 'email': SENT['email'],
                'password': SENT['password']}

    # -- expected refusals ----------------------------------------------------------------

    def test_a_stale_quote_refusal_is_found_by_its_request_id(self):
        key = str(uuid.uuid4())
        order = self._draft(key=key)
        sent = self._send('put', SUBMIT, {
            'order': str(order.pk), 'quote_ref': 'SENTINEL-QUOTE-d4c', 'note': SENT['body'],
            'phone': SENT['phone'], 'email': SENT['email'], 'password': SENT['password'],
        })
        self.assertEqual(sent.response.status_code, 400)
        self.assertEqual(sent.response.json()['reason'], 'quote_ref_stale')
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'initiated')
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
        fields = self._trace(sent)
        self.assertEqual(fields, {
            'event': 'order.command', 'action': 'submit', 'status': 400,
            'outcome': 'refused', 'reason': 'quote_ref_stale', 'channel': 'diner',
            'order': str(order.pk), 'intent': key, 'error': None, 'where': None,
        })
        self.assertEqual(sent.outcomes[0].levelno, 30)
        self.assertNotIn('SENTINEL-QUOTE-d4c', outcome_blob(sent.outcomes[0]))

    def test_a_paused_restaurant_refusal_on_initiate(self):
        self.restaurant.accepting_orders = False
        self.restaurant.save(update_fields=['accepting_orders'])
        key = str(uuid.uuid4())
        sent = self._send('post', INITIATE, self._initiate_body(key))
        self.assertEqual(sent.response.status_code, 400)
        self.assertFalse(Order.objects.filter(client_order_id=key).exists())
        fields = self._trace(sent)
        self.assertEqual((fields['action'], fields['outcome'], fields['reason'],
                          fields['channel'], fields['intent'], fields['order']),
                         ('initiate', 'refused', 'restaurant_paused', 'diner', key, None))

    def test_a_foreign_scope_refusal_records_no_foreign_identifier(self):
        foreign = self._draft(table=self.table_b, key=str(uuid.uuid4()))
        sent = self._send('put', SUBMIT, {'order': str(foreign.pk),
                                          'quote_ref': quote_ref(foreign)})
        self.assertEqual(sent.response.status_code, 404)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['reason'], fields['order'],
                          fields['intent'], fields['channel']),
                         ('refused', 'not_found', None, None, None))
        self.assertNotIn(str(foreign.pk), outcome_blob(sent.outcomes[0]))
        self.assertNotIn(str(foreign.client_order_id), outcome_blob(sent.outcomes[0]))
        foreign.refresh_from_db()
        self.assertEqual(foreign.order_status, 'initiated')

    def test_a_foreign_staff_caller_is_refused_without_recording_the_order(self):
        """The staff branch FETCHES the order before the module gate refuses it; the
        order it fetched belongs to somebody else and is never recorded."""
        from rest_framework_simplejwt.tokens import RefreshToken
        other_owner = User.objects.create_user(
            first_name='Other', last_name='Owner', email='trace_other_owner@test.com',
            phone_number='256700000973', username='256700000973', country='Uganda',
            password='password', roles=[],
        )
        other = Restaurant.objects.create(name='Other R', location='loc', owner=other_owner,
                                          status=RestaurantStatus_Live)
        RestaurantEmployee.objects.create(user=other_owner, restaurant=other,
                                          roles=[RESTAURANT_OWNER], active=True)
        order = self._draft(key=str(uuid.uuid4()))
        token = RefreshToken.for_user(other_owner).access_token
        sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                          'quote_ref': quote_ref(order)},
                          session=False, extra={'HTTP_AUTHORIZATION': f'Bearer {token}'})
        self.assertEqual(sent.response.status_code, 404)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['reason'], fields['order'],
                          fields['intent'], fields['channel']),
                         ('refused', 'not_found', None, None, None))
        self.assertNotIn(str(order.pk), outcome_blob(sent.outcomes[0]))
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'initiated')

    def test_an_unknown_controller_reason_stays_unknown(self):
        """SYNTHETIC: a controller answer carrying a well-formed code nobody listed."""
        order = self._draft(key=str(uuid.uuid4()))
        answer = {'status': 400, 'message': 'x', 'reason': 'brand_new_reason'}
        with mock.patch('orders_app.endpoints.orders.update_order_status',
                        return_value=answer):
            sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                              'quote_ref': quote_ref(order)})
        self.assertEqual(sent.response.status_code, 400)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['reason']), ('unclassified', None))
        self.assertNotIn('brand_new_reason', outcome_blob(sent.outcomes[0]))

    def test_a_success_without_a_consistent_acceptance_answer_is_not_called_accepted(self):
        """SYNTHETIC: 200 answers whose acceptance fields are missing, malformed or
        disagree. None of them may default to a success word."""
        order = self._draft(key=str(uuid.uuid4()))

        def answer(idempotent, order_id, state, outcome):
            return {'status': 200, 'message': 'ok', 'idempotent': idempotent,
                    'checkout': {'order_id': order_id,
                                 'acceptance': {'state': state, 'outcome': outcome}}}

        mine = str(order.pk)
        cases = {
            'no flag, no answer': {'status': 200, 'message': 'ok'},
            'flag without the answer': {'status': 200, 'idempotent': False},
            'flag that is not a boolean': answer('false', mine, 'accepted', 'newly_accepted'),
            'answer about another order': answer(False, str(uuid.uuid4()), 'accepted',
                                                 'newly_accepted'),
            'not in the accepted state': answer(False, mine, 'not_accepted', 'newly_accepted'),
            'outcome contradicts the flag': answer(True, mine, 'accepted', 'newly_accepted'),
            'answer that is not an object': {'status': 200, 'idempotent': False,
                                             'checkout': ['accepted']},
        }
        for label, body in cases.items():
            with self.subTest(case=label), mock.patch(
                    'orders_app.endpoints.orders.update_order_status', return_value=body):
                sent = self._send('put', SUBMIT, {'order': mine,
                                                  'quote_ref': quote_ref(order)})
                self.assertEqual(sent.response.status_code, 200)
                self.assertEqual(self._trace(sent)['outcome'], 'unclassified')

    def test_a_refusal_drf_makes_before_the_handler_is_still_traced(self):
        order = self._draft(key=str(uuid.uuid4()))
        sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                          'quote_ref': quote_ref(order)},
                          session=False,
                          extra={'HTTP_AUTHORIZATION': f"Bearer {SENT['authorization']}"})
        self.assertEqual(sent.response.status_code, 401)
        fields = self._trace(sent)
        self.assertEqual((fields['action'], fields['outcome'], fields['status']),
                         ('submit', 'unclassified', 401))

    # -- acceptance and replay ------------------------------------------------------------

    def test_a_new_acceptance_then_its_idempotent_replay(self):
        key = str(uuid.uuid4())
        order = self._draft(key=key)
        body = {'order': str(order.pk), 'quote_ref': quote_ref(order)}
        first = self._send('put', SUBMIT, body)
        again = self._send('put', SUBMIT, body)
        self.assertEqual((first.response.status_code, again.response.status_code), (200, 200))
        self.assertIs(first.response.json()['idempotent'], False)
        self.assertIs(again.response.json()['idempotent'], True)
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)
        a, b = self._trace(first), self._trace(again)
        self.assertEqual((a['outcome'], b['outcome']), ('accepted', 'already_accepted'))
        self.assertEqual((a['order'], a['intent'], a['channel']), (str(order.pk), key, 'diner'))
        self.assertEqual((b['order'], b['intent']), (a['order'], a['intent']))
        self.assertNotEqual(first.response.headers['X-Request-ID'],
                            again.response.headers['X-Request-ID'],
                            'one ID per HTTP request; the intent relates them')
        recovered = self.c.get(f'/api/v1/orders/journey/order-details/?intent={key}',
                               HTTP_X_DINER_SESSION=self.session)
        self.assertEqual(recovered.status_code, 200)
        self.assertIn('"state":"accepted"', recovered.content.decode().replace(' ', ''))

    def test_a_staff_acceptance_is_traced_on_the_staff_channel(self):
        order = self._draft(key=str(uuid.uuid4()))
        sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                          'quote_ref': quote_ref(order)},
                          session=False, extra=self._jwt())
        self.assertEqual(sent.response.status_code, 200, sent.response.content)
        fields = self._trace(sent)
        self.assertEqual((fields['channel'], fields['outcome'], fields['order']),
                         ('staff', 'accepted', str(order.pk)))

    def test_initiate_replays_before_and_after_acceptance_are_order_returned(self):
        """``initiate`` drops the controller's internal replay flag, so its success says
        only that an authorized order came back — never that it is new, a draft or
        accepted. A replay after acceptance returns the ACCEPTED order."""
        key = str(uuid.uuid4())
        body = self._initiate_body(key)
        new = self._send('post', INITIATE, body)
        replay = self._send('post', INITIATE, body)
        order = Order.objects.get(client_order_id=key)
        accept = self._send('put', SUBMIT, {'order': str(order.pk),
                                            'quote_ref': quote_ref(order)})
        after = self._send('post', INITIATE, body)
        self.assertEqual([s.response.status_code for s in (new, replay, accept, after)],
                         [200, 200, 200, 200])
        self.assertEqual(Order.objects.filter(client_order_id=key).count(), 1)
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, 'initiated', 'the order was accepted')
        self.assertEqual(after.response.json()['data']['order_details']['id'], str(order.pk))
        for sent in (new, replay, after):
            fields = self._trace(sent)
            self.assertEqual((fields['action'], fields['outcome'], fields['order'],
                              fields['intent'], fields['channel']),
                             ('initiate', 'order_returned', str(order.pk), key, 'diner'))
            self.assertNotIn('draft', sent.outcomes[0].getMessage())
        self.assertEqual(self._trace(accept)['outcome'], 'accepted')
        rids = {s.response.headers['X-Request-ID'] for s in (new, replay, accept, after)}
        self.assertEqual(len(rids), 4)

    def test_retire_quote_reports_the_controllers_own_outcome(self):
        order = self._draft(key=str(uuid.uuid4()))
        sent = self._send('put', RETIRE, {'order': str(order.pk),
                                          'quote_ref': quote_ref(order)})
        self.assertEqual(sent.response.status_code, 200)
        self.assertEqual(sent.response.json()['outcome'], 'quote_still_valid')
        fields = self._trace(sent)
        self.assertEqual((fields['action'], fields['outcome'], fields['reason']),
                         ('retire_quote', 'quote_still_valid', None))

    # -- unexpected and unknown answers ----------------------------------------------------

    def test_an_unexpected_500_is_unhandled_located_and_withheld(self):
        """SYNTHETIC: the retire controller's worker raises a planted, chained error."""
        order = self._draft(key=str(uuid.uuid4()))
        with mock.patch.object(manage_order, '_retire_quote_answer',
                               side_effect=_chained_failure()):
            sent = self._send('put', RETIRE, {'order': str(order.pk),
                                              'quote_ref': quote_ref(order)})
        self.assertEqual(sent.response.status_code, 500)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['status'], fields['error'],
                          fields['order']),
                         ('unhandled', 500, 'RuntimeError', str(order.pk)))
        self.assertRegex(fields['where'],
                         r'\Aorders_app/controllers/manage_order\.py:\d+:retire_quote_for_review\Z')
        failure = [x for x in sent.console if 'django.request' in x.splitlines()[0]]
        self.assertEqual(len(failure), 1)
        self.assertEqual(rid_of(failure[0]), sent.response.headers['X-Request-ID'])
        self.assertIn('exception text withheld', failure[0])
        self.assertIn('File "orders_app/controllers/manage_order.py"', failure[0])
        self.assertIn('KeyError', failure[0])
        self.assertTrue(failure[0].rstrip().endswith('RuntimeError'))

    def test_a_real_project_exception_keeps_its_exact_location(self):
        """REAL path, not repaired here: a JSON array body reaches ``data.get``."""
        sent = self._send('put', SUBMIT, raw=json.dumps([SENT['body']]))
        self.assertEqual(sent.response.status_code, 500, 'existing behaviour, unchanged')
        source, first = inspect.getsourcelines(
            __import__('orders_app.endpoints.orders', fromlist=['x']).OrdersEndpoint.put)
        line = first + next(i for i, text in enumerate(source)
                            if "order_id = data.get('order')" in text)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['error'], fields['where']),
                         ('unhandled', 'AttributeError',
                          f'orders_app/endpoints/orders.py:{line}:put'))

    def test_a_caught_generic_400_is_unclassified_not_refused(self):
        """SYNTHETIC: ``update_order_status`` swallows this into its generic 400."""
        order = self._draft(key=str(uuid.uuid4()))
        with mock.patch.object(manage_order, '_submit_order',
                               side_effect=_chained_failure()):
            sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                              'quote_ref': quote_ref(order)})
        self.assertEqual(sent.response.status_code, 400, 'existing behaviour, unchanged')
        self.assertNotIn('reason', sent.response.json())
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['reason'], fields['status']),
                         ('unclassified', None, 400))
        swallowed = [x for x in sent.console if 'ErrorUpdateOrderStatus' in x]
        self.assertEqual(len(swallowed), 1)
        self.assertEqual(rid_of(swallowed[0]), sent.response.headers['X-Request-ID'])
        self.assertIn('exception text withheld', swallowed[0])
        self.assertIn('RuntimeError', swallowed[0])

    def test_the_initiate_argument_sink_renders_only_the_class(self):
        """SYNTHETIC: ``initiate_order`` logs ``"InitiateOrder-Error: %s", error``."""
        fake = mock.MagicMock()
        fake.objects.get.side_effect = OperationalError(EXC_TEXT)
        with mock.patch.object(con_orders, 'Restaurant', fake):
            sent = self._send('post', INITIATE, self._initiate_body(str(uuid.uuid4())))
        self.assertEqual(sent.response.status_code, 400, 'existing behaviour, unchanged')
        line = [x for x in sent.console if 'InitiateOrder-Error' in x]
        self.assertEqual(len(line), 1)
        self.assertTrue(line[0].endswith('InitiateOrder-Error: OperationalError'), line[0])
        self.assertEqual(self._trace(sent)['outcome'], 'unclassified')

    def test_a_database_failure_before_resolution_records_no_order(self):
        """SYNTHETIC: the session resolution itself fails at the database layer."""
        order = self._draft(key=str(uuid.uuid4()))
        with mock.patch('orders_app.endpoints.orders.resolve_table_session',
                        side_effect=OperationalError(EXC_TEXT)):
            sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                              'quote_ref': quote_ref(order)})
        self.assertEqual(sent.response.status_code, 500)
        fields = self._trace(sent)
        self.assertEqual((fields['outcome'], fields['error'], fields['order'],
                          fields['channel']),
                         ('unhandled', 'OperationalError', None, None))

    def test_a_failure_after_the_acceptance_answer_is_never_reported_as_accepted(self):
        """The acceptance COMMITS, then producing the response fails — once inside the
        view (``finalize_response``) and once in a later middleware. Neither outcome may
        say ``accepted``, and neither establishes that nothing committed."""
        from orders_app.endpoints.orders import OrdersEndpoint
        original = OrdersEndpoint.finalize_response

        def exploding(view, request, response, *args, **kwargs):
            original(view, request, response, *args, **kwargs)
            raise RuntimeError(EXC_TEXT)

        explode_mw = 'misc_app.tests_request_correlation.ExplodeOnResponseMiddleware'
        cases = (
            ('in the view', mock.patch.object(OrdersEndpoint, 'finalize_response', exploding),
             None, 'unhandled'),
            ('in a later middleware', contextlib.nullcontext(),
             [COMMON, explode_mw, *settings.MIDDLEWARE[1:]], 'unclassified'),
        )
        for label, patch, stack, expected in cases:
            with self.subTest(case=label):
                order = self._draft(table=self.table if stack is None else self.table_b,
                                    key=str(uuid.uuid4()))
                session = self.session if stack is None else self.session_b
                with patch:
                    sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                                      'quote_ref': quote_ref(order)},
                                      session=session, stack=stack)
                self.assertEqual(sent.response.status_code, 500)
                self.assertTrue(OrderAcceptance.objects.filter(order=order).exists(),
                                'the acceptance really committed')
                fields = self._trace(sent)
                self.assertEqual((fields['status'], fields['outcome']), (500, expected))
                self.assertNotIn('accepted', sent.outcomes[0].getMessage())

    # -- plumbing properties on the real endpoint --------------------------------------------

    def test_a_doubled_stack_emits_one_outcome_for_a_real_submit(self):
        order = self._draft(key=str(uuid.uuid4()))
        sent = self._send('put', SUBMIT, {'order': str(order.pk),
                                          'quote_ref': quote_ref(order)},
                          stack=[ALIAS, *settings.MIDDLEWARE])
        self.assertEqual(sent.response.status_code, 200)
        self.assertEqual(self._trace(sent)['outcome'], 'accepted')

    def test_the_trace_adds_no_query_and_changes_no_response(self):
        """Each command twice on identical fixtures: once traced, once through the same
        stack WITHOUT the request-context middleware (so every annotation is a no-op)."""
        self._draft(key=str(uuid.uuid4()))           # the day's order counter exists
        twin = _untraced_stack()
        stale_a = self._draft(key=str(uuid.uuid4()))
        stale_b = self._draft(key=str(uuid.uuid4()))
        accept_a = self._draft(table=self.table, key=str(uuid.uuid4()))
        accept_b = self._draft(table=self.table_b, key=str(uuid.uuid4()))
        foreign = self._draft(table=self.table_b, key=str(uuid.uuid4()))
        pairs = {
            'stale refusal': (
                lambda: self._send('put', SUBMIT, {'order': str(stale_a.pk), 'quote_ref': 'x'}),
                lambda: self._send('put', SUBMIT, {'order': str(stale_b.pk), 'quote_ref': 'x'},
                                   stack=twin, safe=False)),
            'foreign refusal': (
                lambda: self._send('put', SUBMIT, {'order': str(foreign.pk), 'quote_ref': 'x'}),
                lambda: self._send('put', SUBMIT, {'order': str(foreign.pk), 'quote_ref': 'x'},
                                   stack=twin, safe=False)),
            'acceptance': (
                lambda: self._send('put', SUBMIT, {'order': str(accept_a.pk),
                                                   'quote_ref': quote_ref(accept_a)}),
                lambda: self._send('put', SUBMIT, {'order': str(accept_b.pk),
                                                   'quote_ref': quote_ref(accept_b)},
                                   session=self.session_b, stack=twin, safe=False)),
            'idempotent replay': (
                lambda: self._send('put', SUBMIT, {'order': str(accept_a.pk),
                                                   'quote_ref': quote_ref(accept_a)}),
                lambda: self._send('put', SUBMIT, {'order': str(accept_b.pk),
                                                   'quote_ref': quote_ref(accept_b)},
                                   session=self.session_b, stack=twin, safe=False)),
            'retire enquiry': (
                lambda: self._send('put', RETIRE, {'order': str(stale_a.pk),
                                                   'quote_ref': quote_ref(stale_a)}),
                lambda: self._send('put', RETIRE, {'order': str(stale_b.pk),
                                                   'quote_ref': quote_ref(stale_b)},
                                   stack=twin, safe=False)),
        }
        for label, (traced, untraced) in pairs.items():
            with self.subTest(command=label):
                a, b = traced(), untraced()
                self.assertEqual(len(a.outcomes), 1)
                self.assertEqual(b.outcomes, [])
                self.assertEqual(a.queries, b.queries, 'the trace added a query')
                self.assertEqual(a.response.status_code, b.response.status_code)
                self.assertEqual(_normalised(a.response.content),
                                 _normalised(b.response.content))


class OrderTraceVocabularyTests(SimpleTestCase):
    """The allowlist is explicit in production code; these fail when it drifts."""

    SIX_MODULES = (
        'orders_app.controllers.manage_order',
        'orders_app.controllers.services.order_eligibility',
        'orders_app.controllers.services.order_intent',
        'orders_app.controllers.services.quote_closure',
        'orders_app.controllers.services.quote_policy',
        'orders_app.controllers.services.purchase_integrity',
    )
    ENDPOINT_CODES = {'order_required', 'capability_denied', 'capability_invalid',
                      'not_found', 'session_required', 'login_required',
                      'invalid_request', 'scope_mismatch'}

    def _endpoint(self):
        return __import__('orders_app.endpoints.orders', fromlist=['x'])

    def test_every_controller_reason_constant_is_explicitly_allowlisted(self):
        import importlib
        allow = self._endpoint()._TRACED_REASONS
        found = {}
        for name in self.SIX_MODULES:
            for attr, value in vars(importlib.import_module(name)).items():
                if attr.startswith('REASON_') and isinstance(value, str):
                    found[f'{name}.{attr}'] = value
        self.assertGreaterEqual(len(found), 20)
        missing = {k: v for k, v in found.items() if v not in allow}
        self.assertEqual(missing, {}, 'a new REASON_* constant is not in _TRACED_REASONS')
        self.assertEqual(set(allow), set(found.values()) | self.ENDPOINT_CODES,
                         'the allowlist holds nothing but controller and endpoint codes')
        for code in allow:
            self.assertRegex(code, r'\A[a-z][a-z0-9_]{0,47}\Z')

    def test_the_retire_outcomes_are_the_controllers_own_words(self):
        from dinify_backend.request_context import SUCCESS_OUTCOMES
        from orders_app.controllers.services import quote_closure
        mapping = self._endpoint()._RETIRE_OUTCOMES
        self.assertEqual(mapping, {
            quote_closure.OUTCOME_STILL_VALID: 'quote_still_valid',
            quote_closure.OUTCOME_CLOSED: 'quote_closed',
            quote_closure.OUTCOME_ALREADY_CLOSED: 'quote_already_closed',
        })
        self.assertTrue(set(mapping.values()) <= SUCCESS_OUTCOMES)

    def test_every_annotation_uses_known_fields_and_allowlisted_codes(self):
        from dinify_backend.request_context import ORDER_COMMAND_FIELDS
        module = self._endpoint()
        tree = ast.parse(inspect.getsource(module))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id in ('_trace', '_trace_refused')]
        self.assertGreaterEqual(len(calls), 15)
        for call in calls:
            names = {kw.arg for kw in call.keywords}
            self.assertLessEqual(names, set(ORDER_COMMAND_FIELDS), ast.dump(call))
            if call.func.id == '_trace_refused':
                code = call.args[1]
                branches = [code.body, code.orelse] if isinstance(code, ast.IfExp) else [code]
                for branch in branches:
                    self.assertIsInstance(branch, (ast.Constant, ast.Name), ast.dump(call))
                    value = (branch.value if isinstance(branch, ast.Constant)
                             else getattr(module, branch.id))
                    self.assertIn(value, module._TRACED_REASONS)

    def test_only_the_three_commands_are_marked(self):
        module = self._endpoint()
        self.assertEqual(module.OrdersEndpoint._TRACED_COMMANDS,
                         {('PUT', 'submit'): 'submit', ('PUT', 'retire-quote'): 'retire_quote'})
        self.assertEqual(module.V2OrdersEndpoint._TRACED_COMMANDS,
                         {('POST', 'initiate'): 'initiate'})

    @override_settings(ROOT_URLCONF='dinify_backend.urls')
    def test_an_unmarked_order_route_writes_no_outcome(self):
        with OutcomeCollector() as outcomes:
            for method, url in (('get', '/api/v2/orders/details/'),
                                ('put', '/api/v1/orders/prepare/'),
                                ('delete', '/api/v2/orders/add-items/')):
                r = getattr(Client(), method)(url, content_type='application/json')
                self.assertEqual(r.status_code, 404)
        self.assertEqual(outcomes.records, [])
