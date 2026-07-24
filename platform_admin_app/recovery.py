"""
One-shot break-glass recovery codes for platform-staff accounts.

At n=1 administrator, losing the TOTP device must not mean losing the platform.
Ten codes are generated at enrolment, displayed EXACTLY ONCE, and stored only as
hashes in ``PlatformStaffAuth.recovery_code_hashes``. A code is accepted anywhere a
TOTP code is (login verification and step-up elevation) and is consumed on use, so
each works at most once. Regeneration is a management-command operation only.

HASHING: bare SHA-256, matching ``platform_admin_app.sessions.hash_token``. That is
the right precedent here — and NOT the salted/peppered HMAC used for the 4-digit
restaurant OTP — because these codes carry 128 bits of ``secrets`` entropy, which
makes them infeasible to brute-force or precompute from a database read alone; a
salt or a KDF would add cost without adding resistance. Unlike the session token,
though, verification is a scan over a candidate list rather than an indexed lookup,
so comparison uses ``hmac.compare_digest``.
"""
import hashlib
import hmac
import secrets

# 16 bytes -> 22 urlsafe-base64 chars, ~128 bits. Long enough that bare SHA-256
# storage is sound; short enough to write on paper and type once.
RECOVERY_CODE_BYTES = 16
RECOVERY_CODE_COUNT = 10


def hash_code(code):
    """SHA-256 hex of a recovery code — the only form ever stored or compared."""
    return hashlib.sha256(code.encode()).hexdigest()


def generate_codes(count=RECOVERY_CODE_COUNT):
    """
    Return ``(plaintext_codes, hashes)``.

    The plaintext list is the caller's only chance to show them; it is never
    persisted. Only ``hashes`` goes to the database.
    """
    codes = [secrets.token_urlsafe(RECOVERY_CODE_BYTES) for _ in range(count)]
    return codes, [hash_code(code) for code in codes]


def find_matching_hash(stored_hashes, code):
    """
    The stored hash matching ``code``, or ``None``.

    Constant-time per candidate so a near-miss cannot be distinguished by timing.
    The whole list is scanned even after a match for the same reason.
    """
    if not code or not stored_hashes:
        return None
    candidate = hash_code(str(code).strip())
    matched = None
    for stored in stored_hashes:
        if hmac.compare_digest(str(stored), candidate):
            matched = stored
    return matched


def consume(auth, code):
    """
    Consume a recovery code on a ``PlatformStaffAuth`` row.

    Returns True and removes the used hash (one-shot). The caller runs this inside
    the same transaction as the session mint, so a code can never be spent without
    the login it paid for actually completing.
    """
    matched = find_matching_hash(auth.recovery_code_hashes or [], code)
    if matched is None:
        return False

    auth.recovery_code_hashes = [
        stored for stored in auth.recovery_code_hashes if stored != matched
    ]
    auth.save(update_fields=['recovery_code_hashes'])
    return True


def remaining(auth):
    """How many unused recovery codes the account still holds."""
    return len(auth.recovery_code_hashes or [])
