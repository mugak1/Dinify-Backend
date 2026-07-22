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
