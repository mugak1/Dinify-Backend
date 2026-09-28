"""
The consolidated SMS sender's truth contract, the Messenger delegates, and the
send_test_sms command. Ports the retired payment_integrations_app safety suite
(transport-error → False; timeout kwarg present) onto the ONE sender and adds
the gateway-body matrix: HTTP 200 is NOT success — ``ybs_autocreate_status=OK``
is the only success signal (verified live 2026-07-20).
"""
from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from notifications_app.controllers.messenger import Messenger
from notifications_app.controllers.sms import DEFAULT_TIMEOUT, YO_SMS_URL, send_sms

OK_BODY = 'ybs_autocreate_message=256700000000%3ASUBMITTED&ybs_autocreate_status=OK'
FAILED_BODY = 'ybs_autocreate_message=256700000000%3AREJECTED&ybs_autocreate_status=ERROR'


def _response(status_code=200, body=OK_BODY):
    return MagicMock(status_code=status_code, text=body)


def _config(env='prod'):
    values = {'ENV': env, 'YO_SMS_ACCOUNT_NO': 'test-acct', 'YO_SMS_PASSWORD': 'test-pw'}

    def _cfg(key, **kwargs):
        if key in values:
            return values[key]
        return kwargs.get('default')

    return _cfg


@patch('notifications_app.controllers.sms.time.sleep', return_value=None)
@patch('notifications_app.controllers.sms.config')
@patch('notifications_app.controllers.sms.requests.get')
class SmsSenderTruthTests(TestCase):

    def test_2xx_with_ok_returns_true(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        self.assertTrue(send_sms(message='Hi', msisdn='256700000000'))
        self.assertEqual(mock_get.call_count, 1)

    def test_2xx_without_ok_is_failure_with_no_retry(self, mock_get, mock_config, _sleep):
        # A parsed gateway failure is a definitive answer, not a transport blip.
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response(body=FAILED_BODY)
        self.assertFalse(send_sms(message='Hi', msisdn='256700000000'))
        self.assertEqual(mock_get.call_count, 1)

    def test_non_2xx_is_failure_with_no_retry(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response(status_code=500, body=OK_BODY)
        self.assertFalse(send_sms(message='Hi', msisdn='256700000000'))
        self.assertEqual(mock_get.call_count, 1)

    def test_per_destination_states_are_parsed_and_counted_not_logged(self, mock_get, mock_config, _sleep):
        # D11 B1 (intentional change): the success log used to repeat each
        # per-destination <msisdn>:<STATE> pair, i.e. the full destination. It now
        # records how many states the gateway reported, and never the pair itself.
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        with self.assertLogs('notifications_app.controllers.sms', level='INFO') as logs:
            self.assertTrue(send_sms(message='Hi', msisdn='256700000000'))
        self.assertTrue(any('destination states reported=1' in line for line in logs.output))
        self.assertFalse(any('256700000000' in line for line in logs.output))

    def test_ampersand_and_hash_in_message_travel_intact_as_params(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        message = 'Buy 1 & get #2 free'
        self.assertTrue(send_sms(message=message, msisdn='256700000000'))
        # The request must be built from params, never an interpolated URL.
        self.assertEqual(mock_get.call_args.args[0], YO_SMS_URL)
        self.assertEqual(mock_get.call_args.kwargs['params']['sms_content'], message)
        self.assertEqual(mock_get.call_args.kwargs['params']['destinations'], '256700000000')

    def test_default_timeout_reaches_the_transport(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        send_sms(message='Hi', msisdn='256700000000')
        self.assertEqual(mock_get.call_args.kwargs['timeout'], DEFAULT_TIMEOUT)

    def test_exactly_one_retry_on_transport_error_then_success(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.side_effect = [requests.Timeout('timed out'), _response()]
        self.assertTrue(send_sms(message='Hi', msisdn='256700000000'))
        self.assertEqual(mock_get.call_count, 2)

    def test_transport_error_after_the_single_retry_returns_false(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.side_effect = [requests.Timeout('one'), requests.ConnectionError('two')]
        self.assertFalse(send_sms(message='Hi', msisdn='256700000000'))
        self.assertEqual(mock_get.call_count, 2)

    def test_env_skip_returns_true_logs_info_and_makes_no_call(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('dev')
        with self.assertLogs('notifications_app.controllers.sms', level='INFO') as logs:
            self.assertTrue(send_sms(message='Hi', msisdn='256700000000'))
        mock_get.assert_not_called()
        self.assertTrue(any('SMS skipped: ENV=dev' in line for line in logs.output))

    def test_bypass_env_gate_sends_despite_dev(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('dev')
        mock_get.return_value = _response()
        self.assertTrue(send_sms(message='Hi', msisdn='256700000000', bypass_env_gate=True))
        mock_get.assert_called_once()

    def test_capture_receives_status_and_raw_body(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        exchange = {}
        send_sms(message='Hi', msisdn='256700000000', capture=exchange)
        self.assertEqual(exchange['status_code'], 200)
        self.assertEqual(exchange['body'], OK_BODY)


@patch('notifications_app.controllers.sms.time.sleep', return_value=None)
@patch('notifications_app.controllers.sms.config')
@patch('notifications_app.controllers.sms.requests.get')
class MessengerSmsDelegateTests(TestCase):
    """Messenger.send_sms is a thin delegate over the ONE sender (ports the
    retired payment_integrations_app safety assertions)."""

    def test_delegate_passes_timeout_and_returns_the_truth(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        result = Messenger().send_sms(message='Test', msisdn='256700000000')
        self.assertTrue(result)
        self.assertIn('timeout', mock_get.call_args.kwargs)

    def test_delegate_transport_error_returns_false(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.side_effect = requests.Timeout('Timed out')
        result = Messenger().send_sms(message='Test', msisdn='256700000000')
        self.assertFalse(result)


class MessengerEmailTruthTests(TestCase):

    def test_send_email_success_returns_true_via_locmem(self):
        result = Messenger().send_email(
            to=['staff@example.com'], cc=[], subject='Hello', message='<b>hi</b>'
        )
        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Hello')

    def test_send_email_failure_returns_false_and_logs(self):
        with patch('notifications_app.controllers.messenger.EmailMessage') as mock_message:
            mock_message.return_value.send.side_effect = OSError('smtp down')
            with self.assertLogs('notifications_app.controllers.messenger', level='ERROR') as logs:
                result = Messenger().send_email(
                    to=['staff@example.com'], cc=[], subject='Hello', message='hi'
                )
        self.assertFalse(result)
        self.assertTrue(any('Email send failed' in line for line in logs.output))


class SendTestSmsCommandTests(TestCase):

    def test_refuses_without_any_target(self):
        # No --to and no TEST_SMS_RECIPIENT → a clear, hard error.
        with patch(
            'notifications_app.management.commands.send_test_sms.config',
            side_effect=lambda key, **kwargs: kwargs.get('default'),
        ):
            with self.assertRaises(CommandError):
                call_command('send_test_sms')

    def test_bypasses_env_gate_and_prints_the_raw_body(self):
        out = StringIO()
        with patch('notifications_app.controllers.sms.config', side_effect=_config('dev')), \
                patch('notifications_app.controllers.sms.requests.get', return_value=_response()) as mock_get:
            call_command('send_test_sms', to='256700000000', stdout=out)
        # dev ENV + a real transport call == the gate was bypassed.
        mock_get.assert_called_once()
        output = out.getvalue()
        self.assertIn(OK_BODY, output)
        self.assertIn('ybs_autocreate_status:  OK', output)
        self.assertIn('256700000000:SUBMITTED', output)


# ---------------------------------------------------------------------------
# D11 B1 — sender diagnostics are sanitized AT THE SOURCE.
#
# A log line may carry a fixed event category and bounded allowlisted metadata
# (numeric HTTP status, attempt number, a count, a closed status/error category).
# It may NOT carry the destination, the recipients, the subject, the message or
# OTP text, the gateway URL or query (which holds the account password), the
# response body, an arbitrary provider status, or the raw exception — not in the
# message, not in the arguments, not in extra context, not in a traceback.
# Masking or truncating the phone is not enough, and the canaries below would
# catch a partial fix. The `capture` dict and `send_test_sms`'s raw operator
# output are a SEPARATE, deliberate contract and stay unchanged (pinned below).
# ---------------------------------------------------------------------------
import logging  # noqa: E402

CANARY_DEST = '256700987654'
CANARY_MSG = 'Your Dinify OTP is 4821 MSG-CANARY'
CANARY_PW = 'PW-CANARY-yo'
CANARY_BODY = 'BODY-CANARY'


def _canary_config(env='prod'):
    values = {'ENV': env, 'YO_SMS_ACCOUNT_NO': 'ACCT-CANARY', 'YO_SMS_PASSWORD': CANARY_PW}

    def _cfg(key, **kwargs):
        if key in values:
            return values[key]
        return kwargs.get('default')

    return _cfg


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def everything(self):
        standard = set(vars(logging.LogRecord('x', 0, 'x', 0, 'x', None, None)))
        formatter = logging.Formatter('%(message)s')
        parts = []
        for r in self.records:
            parts += [str(r.msg), repr(r.args), r.getMessage(), formatter.format(r)]
            if r.exc_info:
                parts.append(formatter.formatException(r.exc_info))
            parts += [repr(v) for k, v in vars(r).items() if k not in standard]
            parts += [str(r.exc_text or ''), str(r.stack_info or '')]
        return '\n'.join(parts)

    def messages(self):
        return [r.getMessage() for r in self.records]


class _CaptureMixin:
    logger_name = 'notifications_app.controllers.sms'

    def setUp(self):
        super().setUp()
        self.capture = _Capture()
        log = logging.getLogger(self.logger_name)
        log.addHandler(self.capture)
        self.addCleanup(log.removeHandler, self.capture)

    def assertClean(self, *extra):
        text = self.capture.everything()
        for canary in (CANARY_DEST, '987654', 'MSG-CANARY', '4821', CANARY_PW, 'ACCT-CANARY',
                       CANARY_BODY, 'smgw1.yo.co.ug', 'sendsms', 'password=', *extra):
            self.assertNotIn(canary, text)


@patch('notifications_app.controllers.sms.time.sleep', return_value=None)
@patch('notifications_app.controllers.sms.config', side_effect=_canary_config('prod'))
@patch('notifications_app.controllers.sms.requests.get')
class SmsDiagnosticSanitizationTests(_CaptureMixin, TestCase):

    def send(self, **kwargs):
        return send_sms(message=CANARY_MSG, msisdn=CANARY_DEST, **kwargs)

    def test_success_logs_a_category_and_a_count_not_the_destination(self, mock_get, *_):
        mock_get.return_value = _response(
            body=f'ybs_autocreate_message={CANARY_DEST}%3ASUBMITTED-{CANARY_BODY}'
                 f'&ybs_autocreate_status=OK')
        self.assertTrue(self.send())
        self.assertClean()
        self.assertTrue(any('SMS accepted by gateway' in m for m in self.capture.messages()))

    def test_non_2xx_logs_the_numeric_status_only(self, mock_get, *_):
        mock_get.return_value = _response(status_code=503, body=f'{CANARY_BODY} {CANARY_DEST}')
        self.assertFalse(self.send())
        self.assertClean()
        self.assertTrue(any('503' in m for m in self.capture.messages()), self.capture.messages())

    def test_rejection_does_not_echo_an_arbitrary_provider_status_or_body(self, mock_get, *_):
        mock_get.return_value = _response(
            body=f'ybs_autocreate_status=STATUS-CANARY&ybs_autocreate_message='
                 f'{CANARY_DEST}%3A{CANARY_BODY}')
        self.assertFalse(self.send())
        self.assertClean('STATUS-CANARY')
        self.assertTrue(any('SMS gateway rejected' in m for m in self.capture.messages()))

    def test_a_known_gateway_status_is_kept_as_allowlisted_metadata(self, mock_get, *_):
        mock_get.return_value = _response(body=FAILED_BODY)
        self.assertFalse(self.send())
        self.assertTrue(any('ERROR' in m for m in self.capture.messages()), self.capture.messages())

    def test_malformed_body_logs_nothing_of_the_body(self, mock_get, *_):
        mock_get.return_value = _response(body=f'<html>{CANARY_BODY}{CANARY_DEST}</html>')
        self.assertFalse(self.send())
        self.assertClean()

    def test_transport_exception_url_and_query_never_reach_the_log(self, mock_get, *_):
        url = f'{YO_SMS_URL}?password={CANARY_PW}&destinations={CANARY_DEST}&sms_content=4821'
        mock_get.side_effect = [requests.ConnectionError(f'Max retries exceeded with url: {url}'),
                                requests.Timeout(f'Read timed out: {url}')]
        self.assertFalse(self.send())
        self.assertClean()
        self.assertEqual(mock_get.call_count, 2)
        messages = self.capture.messages()
        self.assertTrue(any('attempt 1' in m for m in messages), messages)
        self.assertTrue(any('attempt 2' in m for m in messages), messages)

    def test_positive_control_transport_parameters_are_exactly_unchanged(self, mock_get, *_):
        mock_get.return_value = _response()
        self.assertTrue(self.send(timeout=3))
        self.assertEqual(mock_get.call_args.args, (YO_SMS_URL,))
        self.assertEqual(mock_get.call_args.kwargs, {
            'params': {
                'ybsacctno': 'ACCT-CANARY', 'password': CANARY_PW, 'origin': 'Dinify',
                'sms_content': CANARY_MSG, 'destinations': CANARY_DEST, 'nostore': 0,
            },
            'timeout': 3,
        })

    def test_positive_control_capture_still_receives_the_raw_exchange(self, mock_get, *_):
        body = f'ybs_autocreate_message={CANARY_DEST}%3ASUBMITTED&ybs_autocreate_status=OK'
        mock_get.return_value = _response(body=body)
        exchange = {}
        self.assertTrue(self.send(capture=exchange))
        self.assertEqual(exchange, {'status_code': 200, 'body': body})
        self.assertClean()


class MessengerEmailDiagnosticSanitizationTests(_CaptureMixin, TestCase):
    logger_name = 'notifications_app.controllers.messenger'

    def test_failure_log_names_no_recipient_subject_or_exception(self):
        with patch('notifications_app.controllers.messenger.EmailMessage') as mock_message:
            mock_message.return_value.send.side_effect = OSError(
                f'SMTP refused rcpt canary.person@example.test {CANARY_BODY}')
            result = Messenger().send_email(
                to=['canary.person@example.test'], cc=['cc.canary@example.test'],
                subject='Dinify OTP SUBJ-CANARY', message=CANARY_MSG,
            )
        self.assertFalse(result)
        self.assertClean('canary.person', 'cc.canary', 'SUBJ-CANARY', 'SMTP refused')
        self.assertTrue(any('Email send failed' in m for m in self.capture.messages()))


class SendTestSmsOperatorContractTests(TestCase):
    """The operator command's raw stdout report is deliberately unchanged."""

    def test_raw_body_still_printed_under_a_transport_stub(self):
        out = StringIO()
        body = f'ybs_autocreate_message={CANARY_DEST}%3ASUBMITTED&ybs_autocreate_status=OK'
        with patch('notifications_app.controllers.sms.config', side_effect=_canary_config('dev')), \
                patch('notifications_app.controllers.sms.requests.get',
                      return_value=_response(body=body)):
            call_command('send_test_sms', to=CANARY_DEST, stdout=out)
        self.assertIn(f'raw body:               {body}', out.getvalue())
        self.assertIn(f'{CANARY_DEST}:SUBMITTED', out.getvalue())
