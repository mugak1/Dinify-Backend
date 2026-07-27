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
from datetime import timedelta

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

    # Issued while the account was locked out, so it accepts ONLY a recovery code —
    # the break-glass path out of a nuisance lockout. A TOTP code is refused on such
    # a challenge, because TOTP is what an attacker can make you fail; a recovery
    # code is a secret they do not hold. See endpoints/auth.py.
    recovery_only = models.BooleanField(default=False)

    class Meta:
        db_table = 'admin_login_challenge'
        constraints = [
            # "A new challenge invalidates every prior one" as a database fact
            # rather than a convention `challenges.create_challenge` is trusted to
            # keep. NOTE the condition is `consumed_at IS NULL` and NOT "unexpired":
            # a partial index predicate must be immutable, so it cannot reference
            # `now()`. An expired-but-unconsumed row therefore still holds the slot,
            # which is exactly why create_challenge's consume step stays
            # load-bearing — it is what frees it.
            models.UniqueConstraint(
                fields=['user'],
                condition=models.Q(consumed_at__isnull=True),
                name='one_live_admin_challenge_per_user',
            ),
        ]

    def __str__(self):
        return f'AdminLoginChallenge<{self.user_id}>'


# ``DelegationGrant.scope`` — how much authority a delegated session carries.
# PR-4b enforces these on the customer plane; this module only records the choice.
SCOPE_VIEW = 'view'
SCOPE_SUPPORT = 'support'
SCOPE_CHOICES = [
    (SCOPE_VIEW, SCOPE_VIEW),
    (SCOPE_SUPPORT, SCOPE_SUPPORT),
]


class DelegationGrant(models.Model):
    """
    A scoped, time-boxed, reasoned, revocable grant of access into one restaurant.

    This replaces ambient authority. The Falcon-era admin portal drilled into a
    tenant through an in-process embed carrying the administrator's full authority,
    which in the data was indistinguishable from the owner acting. A grant is the
    opposite: it names WHO, WHICH restaurant, HOW MUCH (``scope``), FOR HOW LONG,
    and WHY — and can be killed at any moment.

    Two independent clocks, deliberately: ``code_expires_at`` bounds only the
    handoff window in which the one-time exchange code may be redeemed (minutes),
    while ``session_ttl_seconds`` bounds the delegated session that redemption buys
    (PR-4b mints it). A code that is never redeemed simply lapses.

    Only the SHA-256 hash of the exchange code is stored — the raw value is returned
    to the minting admin exactly once and never again, matching ``AdminSession`` and
    ``AdminLoginChallenge``.

    NOT append-only, unlike ``AdminAuditLog``: ``redeemed_at`` and ``revoked_at`` are
    legitimate later writes, because this row tracks a lifecycle. Its immutable
    history lives in the audit log, which records every mint, supersession and
    revocation as its own entry.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # The platform-staff account that minted it. PROTECT: a grant is evidence of
    # who reached into a tenant, so deleting the actor must never erase it.
    administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='delegation_grants',
        db_index=True,
    )
    # Which admin session minted it. Nullable so the grant outlives session expiry.
    admin_session = models.ForeignKey(
        AdminSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='delegation_grants',
    )
    # A real FK, unlike AdminAuditLog.restaurant_id (a loose UUID so the log can
    # outlive the row). The trade-off inverts here: restaurants are only ever
    # SOFT-deleted, and a grant is meaningless without its target — so referential
    # integrity is worth more than independence. String reference because this is
    # the first model-level dependency platform_admin_app -> restaurants_app.
    restaurant = models.ForeignKey(
        'restaurants_app.Restaurant',
        on_delete=models.PROTECT,
        related_name='delegation_grants',
        db_index=True,
    )

    # No default: the minting admin must choose how much authority to hand over.
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES)
    # Required and substantive (non-blank, >= 10 chars, enforced at mint). A
    # delegation without a stated reason is precisely what this feature prevents.
    reason = models.TextField()

    # SHA-256 hex of the one-time code (64 chars). The raw code is NEVER stored,
    # logged or audited. unique=True also provides the lookup index PR-4b needs.
    exchange_code_hash = models.CharField(max_length=64, unique=True)

    # The handoff window only — how long the code may be redeemed for, not how long
    # the resulting session lives. Set at mint from ADMIN_DELEGATION_CODE_TTL.
    code_expires_at = models.DateTimeField()
    # How long the delegated session PR-4b mints will live. Bounded at mint.
    session_ttl_seconds = models.PositiveIntegerField(default=900)

    issued_at = models.DateTimeField(default=timezone.now)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=255, blank=True, default='')

    issued_ip = models.GenericIPAddressField(null=True, blank=True)
    issued_user_agent = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'delegation_grant'
        ordering = ['-issued_at']
        indexes = [
            # "What has this administrator been doing?" — the per-actor history.
            models.Index(fields=['administrator', 'issued_at']),
            # "Who has been in this tenant?" — the per-restaurant history.
            models.Index(fields=['restaurant', 'issued_at']),
        ]

    def __str__(self):
        return f'DelegationGrant<{self.administrator_id}:{self.restaurant_id}>'

    @property
    def is_code_live(self):
        """The exchange code may still be redeemed. Pure — no side effects."""
        if self.redeemed_at is not None or self.revoked_at is not None:
            return False
        return timezone.now() < self.code_expires_at

    @property
    def is_session_live(self):
        """
        The delegated session bought by redemption is still within its TTL.

        Revocation wins immediately — PR-4b treats a revoked grant as killing the
        session outright, not merely preventing future redemption.
        """
        if self.redeemed_at is None or self.revoked_at is not None:
            return False
        expiry = self.redeemed_at + timedelta(seconds=self.session_ttl_seconds)
        return timezone.now() < expiry


class DelegatedSession(models.Model):
    """
    The credential a redeemed ``DelegationGrant`` buys — the thing that actually
    travels on the CUSTOMER plane.

    ``OneToOneField`` on purpose: one grant yields at most one session, ever. The
    grant's ``redeemed_at`` already makes redemption single-use, but stating it as a
    database constraint means a bug in the exchange path cannot quietly mint a
    second credential for authority that was handed over once.

    Only the SHA-256 hash of the session token is stored (``hash_token``, imported
    from ``sessions`` — never redefined). The raw token is returned by the exchange
    exactly once and cannot be recovered from this row.

    Deliberately NO ``last_seen_at``: a write on every delegated request would buy
    nothing that ``AdminAuditLog`` does not already record, at the cost of a write
    amplification on a read path.

    Liveness is NEVER cached and is never read from this row alone — it is
    recomputed per request across this row AND the grant (revocation must take
    effect immediately), which is why there is no ``is_live`` property here to
    tempt a caller into asking only half the question.

    NOT append-only (``ended_at`` is a legitimate later write); its immutable
    history lives in ``AdminAuditLog``.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # PROTECT: the grant is the authority this session rests on; deleting it must
    # never orphan a live credential.
    grant = models.OneToOneField(
        DelegationGrant,
        on_delete=models.PROTECT,
        related_name='delegated_session',
    )

    # SHA-256 hex of the session token (64 chars). unique=True is also the lookup
    # index every delegated request hits.
    token_hash = models.CharField(max_length=64, unique=True)

    issued_at = models.DateTimeField(default=timezone.now)
    # Set at exchange to redeemed_at + grant.session_ttl_seconds — the SAME
    # arithmetic as DelegationGrant.is_session_live, so the admin plane's listing
    # and the customer plane can never disagree about when this expires.
    expires_at = models.DateTimeField()

    # Voluntary end (the administrator leaving the tenant), distinct from the
    # grant's revocation, which is the admin-plane kill switch.
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=64, blank=True, default='')

    issued_ip = models.GenericIPAddressField(null=True, blank=True)
    issued_user_agent = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'delegated_session'
        ordering = ['-issued_at']

    def __str__(self):
        return f'DelegatedSession<{self.grant_id}>'


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

    APPEND-ONLY ENFORCEMENT IS APPLICATION-ENFORCED, **NOT DATABASE-ENFORCED**. Say
    it plainly, because "append-only" otherwise reads as a stronger guarantee than
    the code delivers. Three service-layer places enforce it: ``save()`` refuses to
    update an already-persisted row, ``delete()`` refuses outright, and the manager's
    queryset refuses ``update()`` / ``delete()``. Known residual bypasses —
    ``bulk_create`` (never calls ``save()``), ``QuerySet.raw``, direct SQL, and
    anything holding the database credentials — are NOT closed here and cannot be
    closed at this layer. The log therefore resists mistakes and ordinary code paths;
    it does NOT resist a compromised application process or anyone with database
    access. A Postgres rule or ``BEFORE UPDATE OR DELETE`` trigger is the strictly
    stronger option and the natural next step if it ever needs to; it is deliberately
    not added here.

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
