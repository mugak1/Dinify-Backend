"""
Fernet symmetric-encryption helper for platform-admin secrets at rest.

The key comes from the ``ADMIN_SECRET_ENCRYPTION_KEY`` environment variable via
the house ``python-decouple`` config pattern, read LAZILY at call time. The helper
FAILS CLOSED: encrypting or decrypting with a missing/invalid key raises
``ImproperlyConfigured`` immediately (the error text never echoes the key).

Creating a ``PlatformStaffAuth`` row with no secret does NOT touch this module, so
an unconfigured key never blocks the inert identity layer — only an actual
encrypt/decrypt (PR-2b enrolment) requires the key to be present and valid.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet
from decouple import config
from django.core.exceptions import ImproperlyConfigured


def _fernet() -> Fernet:
    """Build a Fernet from ADMIN_SECRET_ENCRYPTION_KEY. Fail closed on use."""
    key = config('ADMIN_SECRET_ENCRYPTION_KEY', default=None)
    if not key:
        raise ImproperlyConfigured(
            'ADMIN_SECRET_ENCRYPTION_KEY is not set; cannot encrypt or decrypt '
            'platform-admin secrets.'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:  # invalid key material (wrong length / not base64)
        raise ImproperlyConfigured(
            'ADMIN_SECRET_ENCRYPTION_KEY is invalid; expected a urlsafe-base64 '
            'Fernet key.'
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string, returning a Fernet token (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token back to the plaintext secret string."""
    return _fernet().decrypt(token.encode()).decode()
