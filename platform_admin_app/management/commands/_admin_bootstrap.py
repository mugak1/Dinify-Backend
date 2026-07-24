"""
Shared helpers for the platform-admin bootstrap commands.

Kept out of the command modules themselves so ``create_platform_admin`` and
``reset_platform_admin_totp`` provision credentials identically — the two must never
drift, because one is the setup path and the other is the break-glass path.

Note the leading underscore: Django's command discovery ignores modules starting with
one, so this is not exposed as a ``manage.py`` subcommand.
"""
import io
import sys

import qrcode
from django.core.management.base import CommandError

from platform_admin_app import recovery, totp


def require_encryption_key():
    """
    Fail closed, and say so plainly, when the Fernet key is missing or invalid.

    Without it the TOTP secret cannot be encrypted, so provisioning must not
    half-complete: better to refuse before creating anything than to leave an admin
    account that can never log in.
    """
    try:
        totp.encrypt_for_storage('probe')
    except Exception as exc:
        raise CommandError(
            'ADMIN_SECRET_ENCRYPTION_KEY is not set or is invalid, so the TOTP '
            'secret cannot be encrypted. Add a urlsafe-base64 Fernet key to the '
            'project .env (see .env.example) and re-run. Generate one with:\n'
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def require_tty():
    """
    Refuse to run without an interactive terminal.

    The password is prompted, never passed as an argument (an argv password lands in
    shell history and the process table). On a non-TTY — CI, a pipe, a cron job —
    prompting would hang, so fail fast with an explanation instead.
    """
    if not sys.stdin.isatty():
        raise CommandError(
            'This command is interactive (it prompts for a password) and needs a '
            'terminal. Run it directly over SSH, not from a script or pipeline.'
        )


def ascii_qr(data):
    """Render ``data`` as an ASCII QR code for scanning straight from the terminal."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    buffer = io.StringIO()
    qr.print_ascii(out=buffer)
    return buffer.getvalue()


def provision_credentials(auth, account_name):
    """
    Generate and persist a fresh TOTP secret + recovery codes for ``auth``.

    Returns ``(provisioning_uri, plaintext_recovery_codes)`` — the caller's ONLY
    chance to display them. Only the encrypted secret and the code hashes are
    stored; the plaintext exists in memory and on the operator's screen, nowhere else.
    """
    from django.utils import timezone

    secret = totp.generate_secret()
    codes, hashes = recovery.generate_codes()
    now = timezone.now()

    auth.totp_secret_encrypted = totp.encrypt_for_storage(secret)
    auth.totp_enrolled_at = now
    auth.recovery_code_hashes = hashes
    auth.recovery_generated_at = now
    # A re-provisioned secret starts a fresh replay timeline.
    auth.last_totp_counter = None
    auth.failed_attempts = 0
    auth.locked_until = None
    auth.save(update_fields=[
        'totp_secret_encrypted', 'totp_enrolled_at', 'recovery_code_hashes',
        'recovery_generated_at', 'last_totp_counter', 'failed_attempts',
        'locked_until',
    ])

    return totp.provisioning_uri(account_name, secret), codes


def write_enrolment_block(command, uri, codes):
    """Print the provisioning URI, the QR, and the one-time recovery codes."""
    out = command.stdout
    out.write('')
    out.write('=== Scan this with your authenticator app ===')
    out.write(ascii_qr(uri))
    out.write('If you cannot scan, enter this URI manually:')
    out.write(f'  {uri}')
    out.write('')
    out.write('=== Recovery codes — shown ONCE, never again ===')
    for code in codes:
        out.write(f'  {code}')
    out.write('')
    out.write(command.style.SUCCESS(
        'Store these codes OFFLINE (print them, or write them down and put them '
        'somewhere physically safe). Each works exactly once and they are the ONLY '
        'way back in if the authenticator device is lost. They are not recoverable '
        'from the database — only their hashes are stored.'
    ))
