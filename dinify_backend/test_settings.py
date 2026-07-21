"""
Test settings — overrides settings.py for CI and local test runs.

When DATABASE_ENGINE is set in the environment (e.g. by CI), those values are
used as-is.  Otherwise falls back to SQLite in-memory so local runs don't need
a running Postgres instance.
"""
import os

# Core Django
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('ALLOWED_HOSTS', '*')
os.environ.setdefault('CORS_ORIGIN_ALLOW_ALL', 'True')
os.environ.setdefault('ENV', 'dev')
# Diner-capability signing key — an explicit, non-production test-only value so
# the fail-closed resolver (dinify_backend/diner_cap_config.py) uses a real key
# instead of the DEBUG-derived fallback during CI/local runs. >=32 chars and
# deliberately different from SECRET_KEY.
os.environ.setdefault(
    'DINER_CAP_KEY', 'diner-cap-test-key-0123456789abcdef0123456789abcdef'
)

# Database — CI sets these to point at the PostgreSQL service container;
# locally they fall back to SQLite in-memory.
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

# SMS gateway + test-MSISDN stubs (settings-import defaults; SMS is mocked in tests)
os.environ.setdefault('YO_SMS_ACCOUNT_NO', 'test-yo-sms-account')
os.environ.setdefault('YO_SMS_PASSWORD', 'test-yo-sms-password')
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
mongo_mock.COL_NOTIFICATIONS = 'notifications'
mongo_mock.COL_DPO_RESPONSES = 'dpo_responses'
mongo_mock.COL_YO_RESPONSES = 'yo_responses'

from dinify_backend.settings import *  # noqa: F401,F403,E402

# The suite must NEVER touch SMTP: locmem stores sends in django.core.mail.outbox.
# (Before this, tests nominally ran the real smtp backend and were saved only by
# fail_silently=True — which send_email no longer uses.)
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Use whatever DATABASE_* env vars are active.  In CI this is PostgreSQL
# (set by the workflow); locally it falls back to the SQLite defaults above.
DATABASES = {
    'default': {
        'ENGINE': os.environ['DATABASE_ENGINE'],
        'NAME': os.environ['DATABASE_NAME'],
        'USER': os.environ.get('DATABASE_USER', ''),
        'PASSWORD': os.environ.get('DATABASE_PASSWORD', ''),
        'HOST': os.environ.get('DATABASE_HOST', ''),
        'PORT': os.environ.get('DATABASE_PORT', ''),
    }
}
