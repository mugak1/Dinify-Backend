"""
Test settings — overrides settings.py for CI and local test runs.
Uses SQLite in-memory so tests don't need a running Postgres instance.
"""
import os

# Core Django
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('ALLOWED_HOSTS', '*')
os.environ.setdefault('CORS_ORIGIN_ALLOW_ALL', 'True')
os.environ.setdefault('ENV', 'dev')

# Database
os.environ.setdefault('DATABASE_ENGINE', 'django.db.backends.sqlite3')
os.environ.setdefault('DATABASE_NAME', ':memory:')
os.environ.setdefault('DATABASE_USER', '')
os.environ.setdefault('DATABASE_PASSWORD', '')
os.environ.setdefault('DATABASE_HOST', '')
os.environ.setdefault('DATABASE_PORT', '')

# Email
os.environ.setdefault('EMAIL_HOST', 'localhost')
os.environ.setdefault('EMAIL_ACCOUNT', 'test@test.com')
os.environ.setdefault('EMAIL_PASSWORD', '')
os.environ.setdefault('EMAIL_PORT', '587')

# MongoDB
os.environ.setdefault('MONGO_HOST', 'mongodb://localhost:27017')
os.environ.setdefault('MONGO_DATABASE', 'dinify_test')

# Payment integrations (stubs — never used in tests)
os.environ.setdefault('FLUTTERWAVE_SECRET', 'test-flutterwave-secret')
os.environ.setdefault('DEFAULT_PAYMENT_EMAIL', 'test@test.com')
os.environ.setdefault('DPO_COMPANY_TOKEN', 'test-dpo-token')
os.environ.setdefault('DPO_SERVICE_TYPE', 'test-dpo-service')
os.environ.setdefault('YO_API_USERNAME', 'test-yo-username')
os.environ.setdefault('YO_API_PASSWORD', 'test-yo-password')
os.environ.setdefault('YO_SMS_ACCOUNT_NO', 'test-yo-sms-account')
os.environ.setdefault('YO_SMS_PASSWORD', 'test-yo-sms-password')
os.environ.setdefault('PESAPAL_CONSUMER_KEY', 'test-pesapal-key')
os.environ.setdefault('PESAPAL_CONSUMER_SECRET', 'test-pesapal-secret')
os.environ.setdefault('TEST_MSISDN', '256700000000')

from unittest.mock import MagicMock  # noqa: E402
import sys  # noqa: E402

# Stub out MongoDB before any app module tries to import it.
# This avoids a real MongoDB connection during tests.
mongo_mock = MagicMock()
sys.modules['dinify_backend.mongo_db'] = mongo_mock
mongo_mock.MONGO_DB = MagicMock()
mongo_mock.ACTION_LOGS = 'action_logs'
mongo_mock.NOTIFICATIONS = 'notifications'
mongo_mock.COL_DPO_TOKENS = 'dpo_tokens'
mongo_mock.COL_DPO_TOKEN_VERIFICATION = 'dpo_token_verification'
mongo_mock.COL_PROFILE_UPDATE_APPROVALS = 'profile_update_approvals'
mongo_mock.COL_NOTIFICATIONS = 'notifications'
mongo_mock.COL_DPO_RESPONSES = 'dpo_responses'
mongo_mock.COL_YO_RESPONSES = 'yo_responses'

from dinify_backend.settings import *  # noqa: F401,F403,E402

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
