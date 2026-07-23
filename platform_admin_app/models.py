"""
Models for the platform-admin identity layer.

`PlatformStaffAuth` is the per-platform-staff authentication adjunct that stores
the (encrypted) TOTP secret and hashed recovery codes. It holds credential STATE
only — enrolment / verification / lockout logic lands in PR-2b. No model method
mints or verifies a secret here.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class PlatformStaffAuth(models.Model):
    """
    Second-factor credential state for a platform-staff account (OneToOne to User).

    The recon (R11) established that secret columns cannot live on ``User`` itself
    (the removed full-User archival signal, PR-0B, would have exfiltrated them),
    so this adjunct table holds them separately. Secrets are stored encrypted at
    rest (``totp_secret_encrypted`` — a Fernet token) or individually hashed
    (``recovery_code_hashes``); nothing here reads or writes plaintext.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='platform_auth',
    )

    # TOTP: Fernet-encrypted secret + enrolment marker (verification is PR-2b).
    totp_secret_encrypted = models.TextField(null=True, blank=True)
    totp_enrolled_at = models.DateTimeField(null=True, blank=True)

    # Break-glass recovery codes: each hashed individually; one-shot semantics
    # are enforced later in PR-2b.
    recovery_code_hashes = models.JSONField(default=list)
    recovery_generated_at = models.DateTimeField(null=True, blank=True)

    # Failed-verification / lockout state (consumed by PR-2b).
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'platform_staff_auth'
        verbose_name = 'Platform staff auth'
        verbose_name_plural = 'Platform staff auth'

    def __str__(self):
        return f'PlatformStaffAuth<{self.user_id}>'


class AdminSession(models.Model):
    """
    An opaque, server-side admin session — the admin plane exits SimpleJWT entirely.

    The browser holds only a high-entropy random token in the ``__Host-`` session
    cookie; this row stores only its SHA-256 hash, never the raw value. Expiry is
    enforced server-side on every request (an 8h absolute lifetime plus a 30-min
    idle timeout), and a session can be revoked instantly — there is no
    stateless-token gap. Rows are minted / resolved / expired / revoked by
    ``platform_admin_app.sessions`` and consumed by ``AdminSessionAuthentication``;
    the login/logout endpoints that call ``create_session`` land in PR-2b.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='admin_sessions',
    )
    # SHA-256 hex of the raw token (64 chars). The raw token is NEVER stored.
    # unique=True also provides the index used by the hash lookup in resolve_session.
    token_hash = models.CharField(max_length=64, unique=True)

    issued_at = models.DateTimeField(default=timezone.now)
    absolute_expiry = models.DateTimeField()
    last_seen = models.DateTimeField(default=timezone.now)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=255, blank=True, default='')

    issued_ip = models.GenericIPAddressField(null=True, blank=True)
    issued_user_agent = models.TextField(blank=True, default='')

    # Set when the session most recently cleared a TOTP re-auth (PR-2b consumes it).
    elevated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'admin_session'
        indexes = [
            # Powers revoke_all_for_user and any per-user session listing.
            models.Index(fields=['user', 'revoked_at']),
        ]

    def __str__(self):
        return f'AdminSession<{self.user_id}>'
