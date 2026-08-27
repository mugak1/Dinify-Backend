"""
Models for the platform-admin identity layer.

`PlatformStaffAuth` is the per-platform-staff authentication adjunct that stores
the (encrypted) TOTP secret and hashed recovery codes. It holds credential STATE
only — enrolment / verification / lockout logic lands in PR-2b. No model method
mints or verifies a secret here.

`AdminSession` is the opaque server-side session; `AdminAuditLog` is the
append-only record of what the admin plane did. None of them inherit
``users_app.BaseModel`` — its soft-delete / archival semantics are wrong here.

`RestaurantOnboarding` and `OwnerInvitation` (bottom of the file) are the
onboarding domain: control-plane PROVENANCE for how a canonical `Restaurant`
entered Admin, and the credential record for asking its owner to confirm control.
They live here rather than in `restaurants_app` for the same reason as the rest of
this module — they must not be tenant-editable, and `BaseModel`'s soft-delete /
vacuum semantics are wrong for evidence. Nothing creates either automatically.
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


# --- the onboarding domain ---------------------------------------------------
#
# ``RestaurantOnboarding.source`` — HOW a canonical Restaurant entered the Admin
# onboarding domain. A closed vocabulary with NO default: a row that cannot say
# how the restaurant arrived is not a provenance record, so the absence of an
# explicit choice must fail rather than quietly become one of the two.
ONBOARDING_SOURCE_ADMIN_CREATED = 'admin_created'
ONBOARDING_SOURCE_LEGACY_ADOPTED = 'legacy_adopted'
ONBOARDING_SOURCE_CHOICES = [
    (ONBOARDING_SOURCE_ADMIN_CREATED, ONBOARDING_SOURCE_ADMIN_CREATED),
    (ONBOARDING_SOURCE_LEGACY_ADOPTED, ONBOARDING_SOURCE_LEGACY_ADOPTED),
]
ONBOARDING_SOURCE_VALUES = [value for value, _label in ONBOARDING_SOURCE_CHOICES]


class RestaurantOnboarding(models.Model):
    """
    The durable platform record of HOW one canonical ``Restaurant`` entered the
    Admin onboarding domain.

    IT IS NOT A SECOND RESTAURANT. Baba House already exists with a real owner,
    membership, menu, tables, QR state, orders and lifecycle; a shadow copy of any
    of that would immediately be a second value able to drift from the first. This
    row holds ONLY facts that have no canonical home elsewhere — provenance, and
    the administrative attestation described below. Admin manages the canonical
    Restaurant regardless of how it entered Dinify.

    Nor is it a readiness checklist, a billing record, an invitation, a lifecycle
    state (``Restaurant.status`` owns that) or an owner identity (``Restaurant.owner``
    plus the owner-role ``RestaurantEmployee`` own that, jointly — see
    ``platform_admin_app.onboarding``).

    ABSENCE IS MEANINGFUL. Nothing creates these rows automatically: no signal, no
    ``get_or_create``, no migration backfill. A restaurant with no row here has
    simply not been brought into the Admin onboarding model yet, which is the
    truthful statement about every restaurant that exists today.

    CLAIM STATE IS NOT STORED HERE. There is deliberately no ``claimed`` /
    ``claim_status`` / ``owner_claimed_at`` column, because claim is DERIVED from
    evidence: a consumed ``OwnerInvitation`` (observed claim), or the attestation
    pair below (an administrator vouching for a legacy relationship). A restaurant
    with neither has not established control in this domain, and that is a third
    honest answer rather than a missing value.

    Owner go-live APPROVAL is deliberately absent too — it is a readiness input and
    its reset semantics are not frozen. Step 3 owns it.

    No ``users_app.BaseModel``: ``deleted`` / ``archived`` / ``vacuumed`` assert that
    rows get hidden or reaped, which is wrong for provenance evidence.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # The canonical tenant. OneToOne because a restaurant enters the onboarding
    # domain exactly once — expressed as a database uniqueness fact rather than a
    # convention a future service is trusted to keep. PROTECT because restaurants
    # are normally SOFT-deleted: a hard delete is not a routine operation here, and
    # it must not silently erase how a tenant entered Dinify.
    restaurant = models.OneToOneField(
        'restaurants_app.Restaurant',
        on_delete=models.PROTECT,
        related_name='admin_onboarding',
    )

    # No default — see ONBOARDING_SOURCE_CHOICES above. `choices=` is a form/admin
    # nicety, NOT an integrity boundary, so the vocabulary is also a CheckConstraint.
    source = models.CharField(max_length=32, choices=ONBOARDING_SOURCE_CHOICES)

    # --- source=admin_created ------------------------------------------------
    # The platform-staff actor who created the restaurant through the (future)
    # Admin creation workflow. NOT ``Restaurant.owner`` and never a substitute for
    # it: this names who at Dinify pressed the button, not who owns the business.
    # PROTECT so deleting the actor cannot erase the attribution.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='restaurant_onboardings_created',
    )

    # --- source=legacy_adopted -----------------------------------------------
    # WHEN an administrator reconciled a pre-existing Restaurant into this domain,
    # and WHO did it. This is a real future event with a real timestamp. It says
    # nothing about when the restaurant was created or when its owner gained
    # control — neither of which this platform knows for a legacy tenant.
    adopted_at = models.DateTimeField(null=True, blank=True)
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='restaurant_onboardings_adopted',
    )

    # --- administrative owner-control attestation (legacy only) --------------
    # A THREE-PART FACT, and it needs all three parts to stay true. It reads:
    #
    #   "at THIS time (`_at`), THIS administrator (`_by`) explicitly attested that
    #    THIS user (`_user`) genuinely controls this restaurant."
    #
    # It is emphatically NOT "the owner claimed the account at this historical
    # timestamp" — we do not know that for a legacy restaurant, and a fabricated
    # claim timestamp would be indistinguishable from an observed one for every
    # future reader. The attesting administrator, the moment of attestation and the
    # SUBJECT of the attestation are the facts we actually have, so those are the
    # facts stored.
    #
    # WHY THE SUBJECT IS STORED RATHER THAN READ OFF ``Restaurant.owner``. An
    # attestation certifies ONE person's control. If the subject were implicit —
    # "whoever the owner FK points at" — then reassigning the owner would silently
    # re-point the evidence, and the replacement would inherit control evidence
    # nobody ever gave them. That is precisely the fabricated-evidence failure this
    # whole design exists to avoid, arriving through the back door. Making
    # invalidation the job of every future owner-write path is not enforceable (a
    # cross-table rule cannot be a CheckConstraint, and this domain adds no
    # signals), so the binding is stored instead: a reader compares
    # ``owner_control_attested_user_id`` with the CURRENT ``restaurant.owner_id``
    # and a stale attestation stops counting on its own.
    #
    # THIS IS NOT A SECOND OWNER OF RECORD. ``Restaurant.owner`` remains the only
    # answer to "who owns this restaurant"; this column is a historical snapshot of
    # who was VOUCHED FOR, and it must NEVER be kept in step with the FK — drifting
    # apart is the signal, not a defect.
    #
    # Nothing here may be inferred from ``User.last_login``, ``prompt_password_change``,
    # an OTP row, ``User.is_active``, ``Restaurant.owner`` existing, or an owner
    # ``RestaurantEmployee`` existing. None of those is proof of claim.
    #
    # ALL THREE NULL is a legitimate, honest state: the ownership relationship
    # exists technically but nobody has vouched for it. The three move together
    # (`restaurant_onboarding_attestation_triple`), and only ``legacy_adopted``
    # provenance may carry them — an admin-created restaurant's owner is established
    # by an invitation that was actually consumed, never by attestation.
    owner_control_attested_at = models.DateTimeField(null=True, blank=True)
    # The SUBJECT — the owner whose control was certified. Distinct from `_by`
    # below, which is the ADMINISTRATOR who certified it; the two are different
    # people playing different roles in the same sentence.
    owner_control_attested_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='restaurant_onboardings_attested_as_owner',
    )
    owner_control_attested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='restaurant_onboardings_attested',
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'restaurant_onboarding'
        ordering = ['-created_at']
        constraints = [
            # The vocabulary as a database fact. Stated separately from the shape
            # constraints below so a violation names WHICH invariant failed, and so
            # that an unknown source (including the empty string a caller who simply
            # forgot ``source`` would insert) is refused on its own terms.
            models.CheckConstraint(
                condition=models.Q(source__in=ONBOARDING_SOURCE_VALUES),
                name='restaurant_onboarding_source_vocabulary',
            ),
            # An admin-created restaurant: Dinify pressed the button, so there is a
            # creating actor and there is nothing to adopt. Attestation is refused
            # here because control over an admin-created tenant is established by a
            # consumed invitation, not by an administrator vouching for it.
            models.CheckConstraint(
                condition=(
                    ~models.Q(source=ONBOARDING_SOURCE_ADMIN_CREATED)
                    | models.Q(
                        created_by__isnull=False,
                        adopted_at__isnull=True,
                        adopted_by__isnull=True,
                        owner_control_attested_at__isnull=True,
                        owner_control_attested_user__isnull=True,
                        owner_control_attested_by__isnull=True,
                    )
                ),
                name='restaurant_onboarding_admin_created_shape',
            ),
            # A legacy-adopted restaurant: Dinify did not create it, so there is no
            # creating actor — but somebody reconciled it, at a knowable moment.
            # The attestation pair is optional here and governed by its own rule.
            models.CheckConstraint(
                condition=(
                    ~models.Q(source=ONBOARDING_SOURCE_LEGACY_ADOPTED)
                    | models.Q(
                        created_by__isnull=True,
                        adopted_at__isnull=False,
                        adopted_by__isnull=False,
                    )
                ),
                name='restaurant_onboarding_legacy_adopted_shape',
            ),
            # All three or none. Each missing part breaks the sentence a different
            # way: a timestamp with nobody behind it is an unattributable assertion;
            # an attestor with no timestamp is an assertion about no particular
            # moment; and an attestation with no SUBJECT is the dangerous one — it
            # would silently certify whoever `Restaurant.owner` points at next. Any
            # partial triple is worse than silence.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        owner_control_attested_at__isnull=True,
                        owner_control_attested_user__isnull=True,
                        owner_control_attested_by__isnull=True,
                    )
                    | models.Q(
                        owner_control_attested_at__isnull=False,
                        owner_control_attested_user__isnull=False,
                        owner_control_attested_by__isnull=False,
                    )
                ),
                name='restaurant_onboarding_attestation_triple',
            ),
        ]

    def __str__(self):
        return f'RestaurantOnboarding<{self.restaurant_id}:{self.source}>'


# THE INVITATION-LEVEL FAILED-VERIFICATION BUDGET.
#
# A CLOSED SERVER POLICY, not a per-request or per-invitation setting: it caps how many
# times a bearer claim credential may be used to guess an owner-claim OTP, for the whole
# life of that credential.
#
# WHY THE OTP ROW'S OWN CAP IS NOT ENOUGH. `UserOtp.attempts` is capped at
# `OTP_MAX_ATTEMPTS`, but `OtpManager.make_otp` DELETES the previous challenge and
# INSERTS a fresh row with `attempts=0` — so requesting another challenge resets that
# budget. Safe enough for ordinary login UX, and wrong for converting a stolen bearer
# token into restaurant authority: five guesses, request another code, five more, and a
# four-digit space is exhausted. This counter lives on the CREDENTIAL, so re-issuing the
# second factor cannot reset it. Only minting a NEW invitation does — which is an
# elevated, reasoned, audited Admin decision (Step 2E reissue).
#
# It is deliberately NOT configurable per request or per invitation: a per-row limit is
# a per-row way to raise it.
OWNER_CLAIM_MAX_FAILED_ATTEMPTS = 5


class OwnerInvitation(models.Model):
    """
    One persisted, single-use attempt to have ONE specific ``User`` confirm control
    of the owner relationship for ONE ``RestaurantOnboarding``.

    A CREDENTIAL LIFECYCLE RECORD — nothing more. It is not the owner identity (the
    canonical ``Restaurant.owner`` plus the owner-role membership is), not a second
    owner field, not an email delivery log, not a password and not an OTP row.

    WHAT WRITES THIS ROW, as of Step 2E. Minting is
    ``onboarding_invitations.mint_owner_invitation`` — the ONE credential primitive,
    called both by Step-2D creation and by Step-2E reissue, so an initial credential
    and a reissued one are indistinguishable in entropy, hashing and window.
    ``superseded_at`` is stamped by ``reissue_owner_invitation`` and
    ``cancelled_at``/``cancelled_by`` by ``cancel_owner_invitation``. REDEMPTION IS
    STILL NOT BUILT: nothing writes ``consumed_at``, and Step 2F owns that transaction.

    STILL NO DELIVERY COLUMNS (``delivery_channel`` / ``delivered_at`` / provider ids),
    and still deliberately. The recon found today's transactional delivery
    infrastructure too unreliable to freeze a contract around; the raw token is handed
    to the authenticated, elevated operator who asked for it, and delivery can be added
    additively once its architecture is chosen and proven. Baking the current
    notification system into this schema would be the expensive mistake. This is also
    why the Admin operation is called REISSUE rather than "resend" — there is no send
    to re-do.

    THE RESTAURANT IS DERIVED, never stored: invitation -> onboarding -> restaurant.
    A second FK would be a second value able to drift from the first.

    IDENTITY IS NOT SNAPSHOTTED. No email / phone / name copied onto the row —
    ``invited_user`` is the canonical identity, and a snapshot would go stale the
    moment the user edits their profile. (A future delivery-attempt record may
    legitimately snapshot the destination it actually sent to; that is a different
    fact, and a different table.)
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # PROTECT: the onboarding row is the context this credential is meaningful in.
    onboarding = models.ForeignKey(
        RestaurantOnboarding,
        on_delete=models.PROTECT,
        related_name='owner_invitations',
    )

    # The exact Dinify identity being asked to confirm ownership. PROTECT because
    # this row is evidence of who was invited.
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='owner_invitations',
    )
    # The platform-staff account that issued it. Whether an issuer is ELIGIBLE
    # (account_type, session, elevation) is a service-layer question; the model
    # records who it was, and nothing here encodes account_type logic.
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='owner_invitations_issued',
    )

    # SHA-256 hex of the raw claim token (64 chars). The RAW token is NEVER stored —
    # the same convention as AdminSession, AdminLoginChallenge and DelegationGrant.
    # unique=True is also the lookup index redemption will hit.
    token_hash = models.CharField(max_length=64, unique=True)

    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    # The three TERMINAL stamps, mutually exclusive (see the constraints below).
    # EXPIRY IS NOT AMONG THEM: "expired" is `expires_at <= now`, derived on read.
    # Persisting `status='expired'` would need a sweeper to maintain it, and this
    # repository has no scheduler running at all — a status column nothing updates
    # is a lie with a timestamp on it.
    consumed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='owner_invitations_cancelled',
    )
    superseded_at = models.DateTimeField(null=True, blank=True)

    # HOW MANY TIMES THIS CREDENTIAL HAS FAILED OWNER-CLAIM VERIFICATION.
    #
    # Incremented by `owner_claim_redemption` and by nothing else, and only when a
    # purpose- and destination-bound owner-claim OTP verification FAILS. Never by a
    # refused claim state, a malformed body, a rejected password or a throttle.
    #
    # `db_default` as well as `default`, for the same rollback reason as
    # `User.customer_access_state` (see migration `users_app/0014`): Django adds a
    # column WITH a default and immediately drops it, so a NOT NULL column ends up with
    # no database default. Under the expand-only rule a rollback lands OLD CODE ON NEW
    # SCHEMA, and `mint_owner_invitation` in that older code INSERTs an invitation
    # without naming this column. `db_default` keeps a real database default so that
    # insert succeeds and lands on zero — the correct value for a credential minted by
    # code that predates the budget.
    #
    # NOTHING ELSE ABOUT THE ATTEMPT IS STORED: not the submitted code, not the client
    # IP, not a timestamp. A counter is the whole fact needed to stop guessing; the rest
    # would be a credential-adjacent audit trail nobody asked for on an anonymous route.
    claim_failed_attempts = models.PositiveSmallIntegerField(
        default=0,
        db_default=0,
    )

    class Meta:
        db_table = 'owner_invitation'
        ordering = ['-issued_at']
        indexes = [
            # "What has been tried for this tenant?" — the per-onboarding history.
            models.Index(fields=['onboarding', 'issued_at']),
        ]
        constraints = [
            # AT MOST ONE UNRESOLVED INVITATION PER ONBOARDING.
            #
            # NOTE what the predicate does NOT say: there is no `expires_at > now()`
            # term, because a PostgreSQL partial-index predicate must be IMMUTABLE
            # and a clock comparison is not. An expired-but-unsuperseded invitation
            # therefore keeps occupying the slot — deliberately. That is what makes
            # `onboarding_invitations.reissue_owner_invitation`'s supersede step
            # load-bearing: it stamps the old row before inserting its replacement,
            # inside one transaction under the `Restaurant` lock, exactly as
            # `challenges.create_challenge` consumes before it inserts under
            # `one_live_admin_challenge_per_user`. Reissuing out of an EXPIRED head is
            # the case that proves it: the row reads "expired" but is still unresolved,
            # so without the supersede the insert would violate this index.
            models.UniqueConstraint(
                fields=['onboarding'],
                condition=models.Q(
                    consumed_at__isnull=True,
                    cancelled_at__isnull=True,
                    superseded_at__isnull=True,
                ),
                name='one_unresolved_owner_invitation_per_onboarding',
            ),
            # Terminal-state exclusivity, as three small named rules rather than one
            # clever expression — a violation should name the pair that collided.
            # An invitation resolves exactly once and in exactly one way; a row that
            # was both consumed and cancelled cannot be reported honestly.
            models.CheckConstraint(
                condition=(
                    models.Q(consumed_at__isnull=True)
                    | models.Q(cancelled_at__isnull=True)
                ),
                name='owner_invitation_not_consumed_and_cancelled',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(consumed_at__isnull=True)
                    | models.Q(superseded_at__isnull=True)
                ),
                name='owner_invitation_not_consumed_and_superseded',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cancelled_at__isnull=True)
                    | models.Q(superseded_at__isnull=True)
                ),
                name='owner_invitation_not_cancelled_and_superseded',
            ),
            # Both or neither, for the same reason as the attestation pair: a
            # cancellation nobody is attached to is unattributable.
            models.CheckConstraint(
                condition=(
                    models.Q(cancelled_at__isnull=True, cancelled_by__isnull=True)
                    | models.Q(cancelled_at__isnull=False, cancelled_by__isnull=False)
                ),
                name='owner_invitation_cancellation_pair',
            ),
            # A credential whose expiry precedes its issue is born dead and would
            # read as "expired" forever while still holding the unresolved slot.
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F('issued_at')),
                name='owner_invitation_expires_after_issue',
            ),
            # THE BUDGET AS A DATABASE FACT, matching how every other closed policy in
            # this repo is held (`Restaurant.status`, `RestaurantOnboarding.source`,
            # `User.customer_access_state`). The service already caps the increment; this
            # is the backstop for the day it stops doing so, on the one counter whose
            # whole job is to stop guessing.
            #
            # The bound is written as a LITERAL matching `OWNER_CLAIM_MAX_FAILED_ATTEMPTS`
            # rather than interpolated, because a migration must not change meaning when a
            # module constant moves. `tests_owner_claim_redemption` asserts the two agree,
            # so raising the policy requires a deliberate migration rather than silently
            # turning the sixth increment into an IntegrityError.
            models.CheckConstraint(
                condition=models.Q(claim_failed_attempts__lte=5),
                name='owner_invitation_claim_attempts_bounded',
            ),
        ]

    def __str__(self):
        return f'OwnerInvitation<{self.onboarding_id}:{self.invited_user_id}>'

    @property
    def is_resolved(self):
        """A terminal stamp is set. Pure — no clock, no database, no side effects."""
        return (
            self.consumed_at is not None
            or self.cancelled_at is not None
            or self.superseded_at is not None
        )

    @property
    def is_expired(self):
        """
        Derived, never stored: the issue window has passed.

        An expired invitation is still UNRESOLVED — it holds the per-onboarding
        slot until something supersedes, cancels or consumes it.
        """
        return self.expires_at <= timezone.now()

    @property
    def is_verification_locked(self):
        """
        Derived, never stored: this credential has spent its owner-claim guess budget.

        DERIVED FOR THE SAME REASON EXPIRY IS. A persisted ``claim_locked_at`` or
        ``status='verification_locked'`` would be a second representation of a fact the
        counter already states, and nothing in this repository runs on a schedule to
        keep one honest.

        A verification-locked invitation is STILL UNRESOLVED: it holds the
        per-onboarding slot, it can be superseded by a reissue and it can be cancelled.
        What it cannot do is be challenged or redeemed. That is exactly the shape of
        expiry, one axis over — the credential has not been consumed, cancelled or
        superseded, and it cannot currently be used.
        """
        return self.claim_failed_attempts >= OWNER_CLAIM_MAX_FAILED_ATTEMPTS

    @property
    def is_claimable(self):
        """
        The only state a token may be redeemed in: unresolved, unexpired, and with
        verification budget left.
        """
        return (
            not self.is_resolved
            and not self.is_expired
            and not self.is_verification_locked
        )
