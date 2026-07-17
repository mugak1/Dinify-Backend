"""
Fail-closed resolution of the diner-capability signing key (``DINER_CAP_KEY``).

Anonymous diner capabilities — the QR credential and the table session
(``restaurants_app/controllers/diner_capability.py``) — are signed with a
DEDICATED key, secret-separated from the staff JWT signing key. A deployed
(``DEBUG=False``) environment MUST configure an explicit, strong
``DINER_CAP_KEY``: it may never silently derive from, or fall back to,
``SECRET_KEY``. A shared or derived key would let anyone able to read
``SECRET_KEY`` forge diner authority for any table.

This module is imported at settings-load time, so it stays import-safe: stdlib
plus ``django.core.exceptions`` only, and it NEVER imports ``django.conf.settings``
(the resolver takes ``secret_key`` / ``debug`` as plain arguments).

Security note: no code path here logs, echoes, or interpolates the key value —
error messages name the variable but never reveal its contents.
"""
import hashlib
import hmac
import warnings

from django.core.exceptions import ImproperlyConfigured

# Minimum secret material for an explicitly-configured key.
MIN_DINER_CAP_KEY_LENGTH = 32

# Exact (whole-string, case-insensitive) placeholder values that must never be
# accepted as a real key. Deliberately NOT substring matching — a legitimate
# random key may happen to contain a fragment like "test" or "secret".
_PLACEHOLDER_KEYS = frozenset({
    'changeme', 'change-me', 'change_me', 'placeholder', 'your-key-here',
    'your_key_here', 'secret', 'password', 'diner-cap-key', 'diner_cap_key',
    'xxx', 'todo', 'none', 'null', 'example',
})

# Info string for the DEBUG-only derived development key. Matches the historical
# derivation so a developer with no key set keeps stable dev tokens across this
# change (and so an operator can reproduce the pre-change key for continuity —
# see BREAKING_CHANGES.md).
_DEV_DERIVE_INFO = b'diner-capability'


def _validate_explicit_key(key, secret_key):
    """
    Raise ``ImproperlyConfigured`` if an explicitly-configured key is unsafe.
    Never echoes the key value into the message. Placeholder is checked first so
    a known placeholder gets the clearest message even though it is also short.
    """
    if key.lower() in _PLACEHOLDER_KEYS:
        raise ImproperlyConfigured(
            'DINER_CAP_KEY is set to an obvious placeholder value; configure a '
            'real random secret.'
        )
    if len(key) < MIN_DINER_CAP_KEY_LENGTH:
        raise ImproperlyConfigured(
            f'DINER_CAP_KEY must be at least {MIN_DINER_CAP_KEY_LENGTH} '
            'characters of secret material.'
        )
    if key == secret_key:
        raise ImproperlyConfigured(
            'DINER_CAP_KEY must not be equal to SECRET_KEY — the diner '
            'capability key must be secret-separated from the Django/JWT '
            'signing key.'
        )


def resolve_diner_cap_key(explicit_key, secret_key, debug):
    """
    Resolve the diner-capability signing key, failing closed in production.

    * An explicitly-configured key is validated (length, not equal to
      ``SECRET_KEY``, not a known placeholder) and returned.
    * With no explicit key AND ``debug`` truthy, a development-only key derived
      from ``SECRET_KEY`` is returned, accompanied by a visible warning. This
      fallback is IMPOSSIBLE under ``debug`` falsy.
    * With no explicit key AND ``debug`` falsy, ``ImproperlyConfigured`` is
      raised so the application fails to boot rather than run on an unspecified,
      derivable key.

    Error messages may name the variable but never contain its value.
    """
    key = (explicit_key or '').strip()
    if key:
        _validate_explicit_key(key, secret_key)
        return key

    if debug:
        warnings.warn(
            'DINER_CAP_KEY is not set; falling back to an INSECURE '
            'development-only key derived from SECRET_KEY. This fallback is '
            'disabled when DEBUG=False. Set an explicit DINER_CAP_KEY before '
            'deploying.',
            stacklevel=2,
        )
        return hmac.new(
            secret_key.encode(), _DEV_DERIVE_INFO, hashlib.sha256
        ).hexdigest()

    raise ImproperlyConfigured(
        'DINER_CAP_KEY must be set in a deployed (DEBUG=False) environment. It '
        'signs anonymous diner capabilities and must be secret-separated from '
        'SECRET_KEY. Generate one with: '
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
