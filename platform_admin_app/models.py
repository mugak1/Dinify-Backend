"""
Models for the platform-admin identity layer.

`PlatformStaffAuth` is the per-platform-staff authentication adjunct that stores
the (encrypted) TOTP secret and hashed recovery codes. It holds credential STATE
only — enrolment / verification / lockout logic lands in PR-2b. No model method
mints or verifies a secret here.

`AdminSession` is the opaque server-side session; `AdminAuditLog` is the
append-only record of what the admin plane did. None of them inherit
``users_app.BaseModel`` — its soft-delete / archival semantics are wrong here.
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

    # Failed-verification / lockout state.
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # Highest TOTP time-step already accepted for this account. A TOTP code stays
    # valid for its whole ±1-step window, so without this a code observed in
    # transit could be replayed within ~90s; verification requires a STRICTLY
    # greater counter, which makes every code single-use.
    last_totp_counter = models.BigIntegerField(null=True, blank=True)

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


class AdminLoginChallenge(models.Model):
    """
    The short-lived first-factor receipt in the two-step admin login.

    Password verification alone must never produce something that can act. Rather
    than adding an "unverified" state to ``AdminSession`` — which would make the
    existence of a session stop meaning "fully authenticated" — the partial state
    lives here, in its own row behind its own short-lived cookie. Only the second
    factor converts a challenge into a session.

    Like the session token, only the SHA-256 hash of the challenge token is stored.
    The FK CASCADEs (unlike ``AdminSession``'s PROTECT): a challenge is ephemeral
    scaffolding with no forensic value once consumed or expired, and a five-minute
    row must never block deleting a user — the audit log is the durable record.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_login_challenges',
    )
    token_hash = models.CharField(max_length=64, unique=True)

    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'admin_login_challenge'

    def __str__(self):
        return f'AdminLoginChallenge<{self.user_id}>'


class AppendOnlyViolation(Exception):
    """
    Raised on any attempt to mutate or delete an append-only audit row.

    A plain ``Exception`` on purpose: it must NOT subclass DRF's ``APIException``
    (which the exception handler would turn into a tidy 4xx) — tampering with the
    audit log is a programming error that deserves a loud 500, not a handled
    response.
    """


# ``AdminAuditLog.result`` — the outcome axis, written from these constants only.
RESULT_SUCCESS = 'success'
RESULT_FAILURE = 'failure'
RESULT_DENIED = 'denied'
RESULT_CHOICES = [
    (RESULT_SUCCESS, RESULT_SUCCESS),
    (RESULT_FAILURE, RESULT_FAILURE),
    (RESULT_DENIED, RESULT_DENIED),
]


class AdminAuditLogQuerySet(models.QuerySet):
    """Queryset that refuses the bulk mutation paths, which bypass ``save()``."""

    def update(self, *args, **kwargs):
        raise AppendOnlyViolation(
            'AdminAuditLog is append-only: bulk update() is not permitted.'
        )

    def delete(self, *args, **kwargs):
        raise AppendOnlyViolation(
            'AdminAuditLog is append-only: bulk delete() is not permitted.'
        )


class AdminAuditLog(models.Model):
    """
    Append-only record of administrative action — the admin plane's system of record.

    The deliberate INVERSE of the legacy Mongo ``save_action`` path (which is
    daemon-threaded, swallows every exception, and escapes the caller's
    transaction): rows here are written synchronously, inside the caller's
    transaction, and a failed write raises. See ``platform_admin_app.audit``.

    APPEND-ONLY ENFORCEMENT is service-layer, in three places: ``save()`` refuses
    to update an already-persisted row, ``delete()`` refuses outright, and the
    manager's queryset refuses ``update()`` / ``delete()``. Known residual bypasses
    — ``bulk_create`` (never calls ``save()``), ``QuerySet.raw`` and direct SQL —
    are NOT closed here. A Postgres rule or ``BEFORE UPDATE OR DELETE`` trigger is
    the strictly stronger option and the natural next step if the log ever needs
    to resist a compromised application process; it is deliberately not added in
    this PR.

    No ``BaseModel`` inheritance and no soft-delete fields: ``deleted`` /
    ``deletion_reason`` / ``deleted_by`` / ``archived`` / ``vacuumed`` /
    ``time_last_updated`` all assert that rows get mutated, archived or hidden,
    which is precisely what an audit log must not do.

    RETENTION: an append-only log grows unbounded. A retention/partitioning
    decision is owed before scale, but no purge mechanism is built now — deleting
    audit rows is exactly the operation this model exists to prevent, so it needs
    a deliberate policy rather than an incidental helper.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # The acting account. Nullable because a failed authentication has no resolved
    # user; PROTECT so history is never destroyed by deleting the actor.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='admin_audit_entries',
    )
    # The identifier supplied on a failed attempt, stored VERBATIM for forensics.
    # It is a record of what was typed — never an assertion that the account exists.
    actor_label = models.CharField(max_length=255, blank=True, default='')

    # Nullable: authentication events precede session creation.
    session = models.ForeignKey(
        AdminSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='audit_entries',
    )

    action = models.CharField(max_length=100, db_index=True)

    # CharField (not UUIDField): resource pks vary across the codebase — UUIDs,
    # integers (reviews) and sequential references all have to fit.
    resource_type = models.CharField(max_length=100, blank=True, default='')
    resource_id = models.CharField(max_length=100, blank=True, default='')

    # A plain UUID, NOT an FK: the log must survive a restaurant row's deletion.
    restaurant_id = models.UUIDField(null=True, blank=True, db_index=True)
    # Forward-compatible placeholder — DelegationGrant arrives in PR-4, so no FK yet.
    delegation_id = models.UUIDField(null=True, blank=True)

    reason = models.TextField(blank=True, default='')

    # Always redacted by the service before persistence — never trust a caller to
    # have scrubbed them (the PR-0B lesson).
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)

    result = models.CharField(max_length=16, choices=RESULT_CHOICES, db_index=True)
    error_code = models.CharField(max_length=100, blank=True, default='')

    # Server-generated per request by RequestIDMiddleware; never client-supplied.
    request_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    # Nullable: ClientIPMiddleware yields None when REMOTE_ADDR is absent.
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = models.Manager.from_queryset(AdminAuditLogQuerySet)()

    class Meta:
        db_table = 'admin_audit_log'
        ordering = ['-created_at']
        indexes = [
            # The two query shapes the portal will use: per-restaurant history and
            # per-actor history, both newest-first.
            models.Index(fields=['restaurant_id', 'created_at']),
            models.Index(fields=['actor', 'created_at']),
        ]

    def __str__(self):
        return f'AdminAuditLog<{self.action}:{self.result}>'

    def save(self, *args, **kwargs):
        """Permit the INSERT only — any later save is an update, so it raises."""
        if not self._state.adding:
            raise AppendOnlyViolation(
                'AdminAuditLog is append-only: an existing entry cannot be modified.'
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyViolation(
            'AdminAuditLog is append-only: entries cannot be deleted.'
        )
