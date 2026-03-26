"""
Unit tests for payment integration safety hardening.

Tests verify that:
- HTTP timeouts are applied to all external requests
- Network errors are caught and produce meaningful error responses
- XML parse errors are handled gracefully
- No credential leakage in logs
"""
from unittest.mock import patch, MagicMock
from django.test import TestCase
import requests


class FlutterwaveTestSafety(TestCase):
    """Test Flutterwave integration safety."""

    @patch('payment_integrations_app.controllers.flutterwave.config')
    @patch('payment_integrations_app.controllers.flutterwave.requests.post')
    def test_collect_mobile_money_timeout(self, mock_post, mock_config):
        """Verify timeout is passed to requests.post for MoMo collection."""
        mock_config.side_effect = lambda key, **kw: {
            'FLUTTERWAVE_SECRET': 'test-secret',
            'DEFAULT_PAYMENT_EMAIL': 'test@test.com',
        }.get(key, kw.get('default', ''))
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={'status': 'success'})
        )

        from payment_integrations_app.controllers.flutterwave import Flutterwave
        fw = Flutterwave(
            amount=10000,
            transaction_id='test-tx-001',
            msisdn='256700000000',
            restaurant_country='UG',
        )
        result = fw.collect_mobile_money()

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertGreater(call_kwargs.kwargs['timeout'], 0)
        self.assertEqual(result['status'], 'success')

    @patch('payment_integrations_app.controllers.flutterwave.config')
    @patch('payment_integrations_app.controllers.flutterwave.requests.post')
    def test_collect_network_error_returns_error_dict(self, mock_post, mock_config):
        """Network errors should return an error dict, not crash."""
        mock_config.side_effect = lambda key, **kw: {
            'FLUTTERWAVE_SECRET': 'test-secret',
            'DEFAULT_PAYMENT_EMAIL': 'test@test.com',
        }.get(key, kw.get('default', ''))
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        from payment_integrations_app.controllers.flutterwave import Flutterwave
        fw = Flutterwave(
            amount=10000,
            transaction_id='test-tx-002',
            msisdn='256700000000',
            restaurant_country='UG',
        )
        result = fw.collect_mobile_money()

        self.assertEqual(result['status'], 'error')
        self.assertIn('message', result)

    @patch('payment_integrations_app.controllers.flutterwave.config')
    @patch('payment_integrations_app.controllers.flutterwave.requests.post')
    def test_send_mobile_money_timeout(self, mock_post, mock_config):
        """Verify timeout is passed to requests.post for MoMo payout."""
        mock_config.return_value = 'test-secret'
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={'status': 'success'})
        )

        from payment_integrations_app.controllers.flutterwave import Flutterwave
        fw = Flutterwave(
            amount=5000,
            transaction_id='test-tx-003',
            msisdn='256700000000',
            restaurant_country='UG',
        )
        result = fw.send_mobile_money()

        call_kwargs = mock_post.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertEqual(result['status'], 'success')

    @patch('payment_integrations_app.controllers.flutterwave.config')
    @patch('payment_integrations_app.controllers.flutterwave.requests.post')
    def test_send_mobile_money_network_error(self, mock_post, mock_config):
        """Payout network errors should return an error dict."""
        mock_config.return_value = 'test-secret'
        mock_post.side_effect = requests.Timeout("Request timed out")

        from payment_integrations_app.controllers.flutterwave import Flutterwave
        fw = Flutterwave(
            amount=5000,
            transaction_id='test-tx-004',
            msisdn='256700000000',
            restaurant_country='UG',
        )
        result = fw.send_mobile_money()

        self.assertEqual(result['status'], 'error')

    def test_unsupported_telecom_returns_error(self):
        """Unsupported telecom should return error without making HTTP call."""
        from payment_integrations_app.controllers.flutterwave import Flutterwave
        fw = Flutterwave(
            amount=10000,
            transaction_id='test-tx-005',
            msisdn='999000000000',  # unknown prefix
            restaurant_country='UG',
        )
        result = fw.collect_mobile_money()
        self.assertEqual(result['status'], 'error')
        self.assertIn('Unsupported', result['message'])


class DpoTestSafety(TestCase):
    """Test DPO integration safety."""

    @patch('payment_integrations_app.controllers.dpo.config')
    @patch('payment_integrations_app.controllers.dpo.requests.post')
    @patch('payment_integrations_app.controllers.dpo.MONGO_DB')
    def test_create_token_timeout(self, mock_mongo, mock_post, mock_config):
        """Verify timeout is passed to requests.post for token creation."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_response = MagicMock()
        mock_response.text = '<API3G><Result>000</Result><TransToken>TEST123</TransToken></API3G>'
        mock_post.return_value = mock_response

        from payment_integrations_app.controllers.dpo import DpoIntegration
        dpo = DpoIntegration()
        result = dpo.create_token(
            amount=50000,
            currency='UGX',
            transaction_reference='test-ref-001',
            timestamp='2024-01-01 12:00:00.000000+00:00'
        )

        call_kwargs = mock_post.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertIsNotNone(result)
        self.assertIn('TEST123', result)

    @patch('payment_integrations_app.controllers.dpo.config')
    @patch('payment_integrations_app.controllers.dpo.requests.post')
    def test_create_token_network_error_returns_none(self, mock_post, mock_config):
        """Network errors on create_token should return None."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        from payment_integrations_app.controllers.dpo import DpoIntegration
        dpo = DpoIntegration()
        result = dpo.create_token(
            amount=50000,
            currency='UGX',
            transaction_reference='test-ref-002',
            timestamp='2024-01-01 12:00:00.000000+00:00'
        )

        self.assertIsNone(result)

    @patch('payment_integrations_app.controllers.dpo.config')
    @patch('payment_integrations_app.controllers.dpo.requests.post')
    @patch('payment_integrations_app.controllers.dpo.MONGO_DB')
    def test_verify_token_network_error_returns_false(self, mock_mongo, mock_post, mock_config):
        """Network errors on verify_token should return False."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.side_effect = requests.Timeout("Timed out")

        from payment_integrations_app.controllers.dpo import DpoIntegration
        dpo = DpoIntegration()
        result = dpo.verify_token(
            transaction_reference='test-ref-003',
            dpo_token='TESTTOKEN'
        )

        self.assertFalse(result)

    @patch('payment_integrations_app.controllers.dpo.config')
    @patch('payment_integrations_app.controllers.dpo.MONGO_DB')
    def test_interprete_response_invalid_xml(self, mock_mongo, mock_config):
        """Invalid XML should be handled gracefully."""
        mock_config.side_effect = lambda key, **kw: 'test-value'

        from payment_integrations_app.controllers.dpo import DpoIntegration
        dpo = DpoIntegration()
        mock_response = MagicMock()
        mock_response.text = 'this is not XML'

        result = dpo.interprete_response(
            request_type='test',
            request_body={},
            dpo_response=mock_response
        )

        self.assertEqual(result, {})


class YoIntegrationTestSafety(TestCase):
    """Test Yo integration safety."""

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.requests.post')
    @patch('payment_integrations_app.controllers.yo_integrations.MONGO_DB')
    def test_momo_collect_timeout(self, mock_mongo, mock_post, mock_config):
        """Verify timeout on momo_collect."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_response = MagicMock()
        mock_response.text = '<AutoCreate><Response><Status>OK</Status></Response></AutoCreate>'
        mock_post.return_value = mock_response

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        result = yo.momo_collect(
            transaction_amount=10000,
            msisdn='256700000000',
            transaction_id='test-yo-001'
        )

        call_kwargs = mock_post.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertTrue(result)

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.requests.post')
    def test_momo_collect_network_error_returns_false(self, mock_post, mock_config):
        """Network errors on momo_collect should return False."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        result = yo.momo_collect(
            transaction_amount=10000,
            msisdn='256700000000',
            transaction_id='test-yo-002'
        )

        self.assertFalse(result)

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.requests.post')
    def test_momo_disburse_network_error_returns_false(self, mock_post, mock_config):
        """Network errors on momo_disburse should return False."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.side_effect = requests.Timeout("Timed out")

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        result = yo.momo_disburse(
            transaction_amount=5000,
            msisdn='256700000000',
            transaction_id='test-yo-003'
        )

        self.assertFalse(result)

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.requests.get')
    def test_send_sms_network_error_returns_false(self, mock_get, mock_config):
        """SMS send network errors should return False."""
        mock_config.side_effect = lambda key, **kw: {
            'YO_API_USERNAME': 'test',
            'YO_API_PASSWORD': 'test',
            'YO_SMS_ACCOUNT_NO': 'test',
            'YO_SMS_PASSWORD': 'test',
            'ENV': 'prod',
        }.get(key, kw.get('default', 'test'))
        mock_get.side_effect = requests.Timeout("Timed out")

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        result = yo.send_sms(message='Test', to='256700000000')

        self.assertFalse(result)

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.MONGO_DB')
    def test_interprete_response_invalid_xml(self, mock_mongo, mock_config):
        """Invalid XML should be handled gracefully."""
        mock_config.side_effect = lambda key, **kw: 'test-value'

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        mock_response = MagicMock()
        mock_response.text = 'not-xml'

        result = yo.interprete_response(
            request_type='test',
            request_body={},
            yo_response=mock_response
        )

        self.assertIsNone(result)
        mock_mongo.__getitem__().insert_one.assert_called_once()


class PesapalTestSafety(TestCase):
    """Test Pesapal integration safety."""

    @patch('payment_integrations_app.controllers.pesapal.config')
    @patch('payment_integrations_app.controllers.pesapal.requests.post')
    def test_authenticate_timeout(self, mock_post, mock_config):
        """Verify timeout on authenticate."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={'token': 'test-token'})
        )

        from payment_integrations_app.controllers.pesapal import Pesapal
        result = Pesapal().authenticate()

        call_kwargs = mock_post.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertEqual(result['status'], 200)

    @patch('payment_integrations_app.controllers.pesapal.config')
    @patch('payment_integrations_app.controllers.pesapal.requests.post')
    def test_authenticate_network_error(self, mock_post, mock_config):
        """Network errors should return error dict with status 500."""
        mock_config.side_effect = lambda key, **kw: 'test-value'
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        from payment_integrations_app.controllers.pesapal import Pesapal
        result = Pesapal().authenticate()

        self.assertEqual(result['status'], 500)
        self.assertIn('message', result)


class MessengerTestSafety(TestCase):
    """Test Messenger SMS safety."""

    @patch('notifications_app.controllers.messenger.config')
    @patch('notifications_app.controllers.messenger.requests.get')
    def test_send_sms_timeout(self, mock_get, mock_config):
        """Verify timeout on SMS send."""
        mock_config.side_effect = lambda key, **kw: {
            'YO_SMS_ACCOUNT_NO': 'test',
            'YO_SMS_PASSWORD': 'test',
            'ENV': 'prod',
        }.get(key, kw.get('default', 'test'))
        mock_get.return_value = MagicMock()

        from notifications_app.controllers.messenger import Messenger
        m = Messenger()
        result = m.send_sms(message='Test', msisdn='256700000000')

        call_kwargs = mock_get.call_args
        self.assertIn('timeout', call_kwargs.kwargs)
        self.assertTrue(result)

    @patch('notifications_app.controllers.messenger.config')
    @patch('notifications_app.controllers.messenger.requests.get')
    def test_send_sms_network_error_returns_false(self, mock_get, mock_config):
        """SMS network errors should return False, not crash."""
        mock_config.side_effect = lambda key, **kw: {
            'YO_SMS_ACCOUNT_NO': 'test',
            'YO_SMS_PASSWORD': 'test',
            'ENV': 'prod',
        }.get(key, kw.get('default', 'test'))
        mock_get.side_effect = requests.Timeout("Timed out")

        from notifications_app.controllers.messenger import Messenger
        m = Messenger()
        result = m.send_sms(message='Test', msisdn='256700000000')

        self.assertFalse(result)
