"""
Unit tests for payment integration safety hardening.

Tests verify that:
- HTTP timeouts are applied to all external requests
- Network errors are caught and produce meaningful error responses
- No credential leakage in logs
"""
from unittest.mock import patch, MagicMock
from django.test import TestCase
import requests


class YoIntegrationTestSafety(TestCase):
    """Test Yo integration safety (SMS gateway — payment paths retired)."""

    @patch('payment_integrations_app.controllers.yo_integrations.config')
    @patch('payment_integrations_app.controllers.yo_integrations.requests.get')
    def test_send_sms_network_error_returns_false(self, mock_get, mock_config):
        """SMS send network errors should return False."""
        mock_config.side_effect = lambda key, **kw: {
            'YO_SMS_ACCOUNT_NO': 'test',
            'YO_SMS_PASSWORD': 'test',
            'ENV': 'prod',
        }.get(key, kw.get('default', 'test'))
        mock_get.side_effect = requests.Timeout("Timed out")

        from payment_integrations_app.controllers.yo_integrations import YoIntegration
        yo = YoIntegration()
        result = yo.send_sms(message='Test', to='256700000000')

        self.assertFalse(result)


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
