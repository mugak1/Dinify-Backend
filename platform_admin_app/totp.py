"""
TOTP (RFC 6238) second factor for platform-staff accounts.

Built on ``pyotp`` — the algorithm is never hand-rolled. The shared secret exists
in plaintext only in memory during enrolment and verification; at rest it is a
Fernet token in ``PlatformStaffAuth.totp_secret_encrypted`` via
``platform_admin_app.crypto``. It is never logged, never returned by an endpoint,
and never passed into an audit ``before_state`` / ``after_state``.

THIS MODULE NEVER CONSULTS ``ENV``. The restaurant/diner OTP flow has a deliberate
``ENV='dev'`` shortcut that makes every code the literal ``1234``
(``users_app/controllers/otp_manager.py``); nothing here shares a code path with it,
and no environment value may weaken admin verification. There is no dev bypass and
no hardcoded code — a wrong code fails under every ENV.

REPLAY: a TOTP code is valid for its whole acceptance window, so simply verifying
the code would let anyone who observed it reuse it for up to ~90 seconds. Every
successful verification records the matched time step in ``last_totp_counter`` and
the next one must be STRICTLY greater, which makes each code single-use.
"""
import hmac
import time

import pyotp

from platform_admin_app.crypto import decrypt_secret, encrypt_secret

# RFC 6238 default step. pyotp uses the same value; named here because the replay
# counter arithmetic below depends on it.
TOTP_STEP_SECONDS = 30

# Accept the current step plus one either side (±30s) to tolerate clock skew, and
# no more — a wider window multiplies an attacker's guessing surface.
TOTP_WINDOW_STEPS = 1

TOTP_ISSUER = 'Dinify Admin'


def generate_secret():
    """A fresh base32 TOTP secret. Plaintext — the caller must encrypt it."""
    return pyotp.random_base32()


def encrypt_for_storage(secret):
    """Fernet-encrypt a plaintext secret for ``totp_secret_encrypted``."""
    return encrypt_secret(secret)


def provisioning_uri(account_name, secret):
    """
    The ``otpauth://`` URI for an authenticator app (QR or manual entry).

    ``account_name`` is the admin username — it is shown in the authenticator, so
    it must identify the account without being a credential.
    """
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name, issuer_name=TOTP_ISSUER,
    )


def _current_counter(for_time=None):
    now = int(for_time if for_time is not None else time.time())
    return now // TOTP_STEP_SECONDS


def match_counter(secret, code, for_time=None):
    """
    Return the time step whose code equals ``code``, or ``None``.

    Walks the accepted window explicitly rather than using ``TOTP.verify`` because
    the caller needs to know WHICH step matched in order to enforce single use.
    Compared with ``hmac.compare_digest`` so a wrong code cannot be narrowed by
    timing.
    """
    if not code:
        return None
    candidate = str(code).strip()
    if not candidate:
        return None

    totp = pyotp.TOTP(secret)
    current = _current_counter(for_time)
    for offset in range(-TOTP_WINDOW_STEPS, TOTP_WINDOW_STEPS + 1):
        counter = current + offset
        expected = totp.at(counter * TOTP_STEP_SECONDS)
        if hmac.compare_digest(expected, candidate):
            return counter
    return None


def verify(auth, code, for_time=None):
    """
    Verify ``code`` against the enrolled secret on a ``PlatformStaffAuth`` row.

    Returns True and advances ``last_totp_counter`` on success. Returns False for a
    wrong code, an unenrolled account, or a code from a step already consumed (the
    replay guard) — the caller owns lockout accounting and audit.
    """
    if not auth or not auth.totp_secret_encrypted:
        return False

    secret = decrypt_secret(auth.totp_secret_encrypted)
    counter = match_counter(secret, code, for_time=for_time)
    if counter is None:
        return False

    # Strictly greater: re-presenting a code from the same (or an earlier) step —
    # still inside its validity window — is a replay and must fail.
    if auth.last_totp_counter is not None and counter <= auth.last_totp_counter:
        return False

    auth.last_totp_counter = counter
    auth.save(update_fields=['last_totp_counter'])
    return True
