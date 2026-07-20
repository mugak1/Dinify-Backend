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

    def test_per_destination_states_are_parsed_and_logged(self, mock_get, mock_config, _sleep):
        mock_config.side_effect = _config('prod')
        mock_get.return_value = _response()
        with self.assertLogs('notifications_app.controllers.sms', level='INFO') as logs:
            self.assertTrue(send_sms(message='Hi', msisdn='256700000000'))
        # %3A decodes to ':' — the per-destination <msisdn>:<STATE> pair.
        self.assertTrue(any('256700000000:SUBMITTED' in line for line in logs.output))

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
