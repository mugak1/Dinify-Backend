"""
The commercial / service-configuration domain (Phase 1, Step 3B).

Two durable facts about a restaurant that had no authoritative home before this
app existed, and that readiness, Admin, future subscription billing, future
invoices/receivables, future PSP integration and future owner go-live approval all
have to be able to read:

    RestaurantServiceConfiguration  — how this restaurant takes money from DINERS
    RestaurantSubscriptionTerms     — what this restaurant pays DINIFY for software

THIS MODULE IS SCHEMA ONLY. There is no writer, no service, no serializer, no
endpoint and no management command, deliberately — the same shape Step 2A used for
the onboarding domain. Absence of a row means "not yet recorded in the commercial
domain", which is the truthful state of every restaurant today.

━━ FOUR CONCEPTS THAT ARE ROUTINELY CONFUSED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Read this before adding a field, because the legacy tree contains remnants of two
earlier, incomplete payment designs and it is easy to reverse-engineer the wrong
architecture from them.

1. PAYMENT TIMING — restaurant-level, `pay_first` | `pay_after`. A SERVICE-MODEL
   fact: must settlement be recorded before the kitchen may fire the order
   (counter cafe, QSR, nightlife), or does the order fire immediately and the tab
   settle at the end (full-service dining)? It says NOTHING about who moves the
   funds, whether Dinify initiates anything, whether the payment is digital, which
   tender the diner uses, or which provider is involved.

2. PAYMENT COLLECTION MODE — restaurant-level, `offline` | `psp_online`. A CUSTODY
   fact: does Dinify initiate the diner payment at all? `offline` means it does not
   — the restaurant collects the money itself (cash, its own MTN/Airtel merchant
   till, its own card terminal, some other external mechanism) and Dinify may
   record the resulting settlement for operational reporting without ever
   executing it. `psp_online` means Dinify initiates payment through a licensed
   provider on the restaurant's behalf; the RESTAURANT remains merchant of record
   and funds settle directly to it. Dinify never holds, controls, pools or
   disburses diner money in either mode.

3. PAYMENT METHOD / TENDER — TRANSACTION-level, `cash` | `momo` | `card`. This is
   ``finance_app.DinifyTransaction.payment_mode`` and it stays exactly where it is.
   A mobile-money payment can occur under EITHER collection mode; the difference is
   who initiated it, not what the diner tapped.

4. DINIFY SUBSCRIPTION — restaurant -> Dinify, a recurring SOFTWARE fee. A
   completely separate money flow from diner -> restaurant, and Dinify's actual
   revenue model. Dinify does NOT earn commission, a surcharge, a percentage of
   GMV, a per-order fee, or anything netted from diner settlements.

THE AXES ARE INDEPENDENT. All four timing x collection combinations are legitimate
and there is deliberately no constraint coupling them. A counter cafe taking cash
is `pay_first` + `offline`; a full-service restaurant on Dinify-initiated mobile
money is `pay_after` + `psp_online`; the other two are equally real.

`offline` IS A PERMANENT, FIRST-CLASS COMMERCIAL MODE — not degraded, fallback,
temporary, pre-launch or test-only. PSP-backed collection AUGMENTS Dinify later and
is not a prerequisite for anybody to launch: the first commercial restaurant must
be able to go live in `offline`.

━━ WHAT THE LEGACY FIELDS ARE NOT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

None of the following is authoritative for this domain, and NOTHING here is derived
from any of them (there is no backfill, no signal, no inference):

  ``Restaurant.require_order_prepayments``  — a settings toggle with ZERO runtime
      readers. It is not canonical payment timing; a stored intention that nothing
      enforces cannot establish a service model.
  ``Table.prepayment_required``             — per-table, copied onto the order and
      then never gated on. Also not payment timing.
  ``Restaurant.preferred_subscription_method`` / ``flat_fee`` /
  ``subscription_validity`` / ``subscription_expiry_date``
                                            — remnants of the old restaurant ->
      Dinify billing flow. They are not diner payment configuration, and
      `subscription_validity` in particular is a bare boolean defaulting True whose
      only writer was deleted, so it is evidence of nothing.
  ``DinifyTransaction.payment_mode``        — transaction tender (concept 3), not
      restaurant collection mode (concept 2).

━━ NO PSP STATE, DELIBERATELY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No provider name, merchant id, provider account, merchant status or webhook state
appears anywhere in this app, and `psp_online` is deliberately provider-agnostic.
There is no PSP integration in this repository and therefore no provider-
authoritative state to project; a local `ready` flag nobody writes would either
read `not_configured` forever or be an operator asserting a fact only the provider
can know. PSP merchant state arrives WITH the first integration. Until then
readiness can say `not_applicable` for `offline` and "required but unavailable" for
`psp_online` from the collection mode alone.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

# --- payment timing (concept 1) ---------------------------------------------
#
# A closed vocabulary. `choices=` below is a form/admin nicety; the INTEGRITY
# boundary is the CheckConstraint, because a direct ORM write never runs model
# validation and `Model.save()` does not call `full_clean()`.
PAYMENT_TIMING_PAY_FIRST = 'pay_first'
PAYMENT_TIMING_PAY_AFTER = 'pay_after'
PAYMENT_TIMING_CHOICES = [
    (PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_FIRST),
    (PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_AFTER),
]
PAYMENT_TIMING_VALUES = [value for value, _label in PAYMENT_TIMING_CHOICES]

# --- payment collection mode (concept 2) ------------------------------------
PAYMENT_COLLECTION_MODE_OFFLINE = 'offline'
PAYMENT_COLLECTION_MODE_PSP_ONLINE = 'psp_online'
PAYMENT_COLLECTION_MODE_CHOICES = [
    (PAYMENT_COLLECTION_MODE_OFFLINE, PAYMENT_COLLECTION_MODE_OFFLINE),
    (PAYMENT_COLLECTION_MODE_PSP_ONLINE, PAYMENT_COLLECTION_MODE_PSP_ONLINE),
]
PAYMENT_COLLECTION_MODE_VALUES = [
    value for value, _label in PAYMENT_COLLECTION_MODE_CHOICES
]

# --- subscription recurrence (concept 4) ------------------------------------
#
# A GENERIC recurrence, not a plan catalogue. `month + 1`, `year + 1` and
# `week + 2` are all expressible, so the schema can carry real commercial terms
# without pretending a fixed set of named plans has been chosen. `per_order` is
# deliberately absent: Dinify's revenue model is a recurring software subscription,
# and reviving that word would reintroduce the per-order-commission framing the
# non-custodial posture exists to keep out.
BILLING_INTERVAL_DAY = 'day'
BILLING_INTERVAL_WEEK = 'week'
BILLING_INTERVAL_MONTH = 'month'
BILLING_INTERVAL_YEAR = 'year'
BILLING_INTERVAL_UNIT_CHOICES = [
    (BILLING_INTERVAL_DAY, BILLING_INTERVAL_DAY),
    (BILLING_INTERVAL_WEEK, BILLING_INTERVAL_WEEK),
    (BILLING_INTERVAL_MONTH, BILLING_INTERVAL_MONTH),
    (BILLING_INTERVAL_YEAR, BILLING_INTERVAL_YEAR),
]
BILLING_INTERVAL_UNIT_VALUES = [
    value for value, _label in BILLING_INTERVAL_UNIT_CHOICES
]


class RestaurantServiceConfiguration(models.Model):
    """
    How ONE restaurant takes money from its diners: payment timing and collection
    mode, each independently configured, each independently attributed.

    ONE ROW PER RESTAURANT, and only once somebody has actually decided something.
    Nothing creates this row automatically — no signal, no ``post_save``, no
    ``get_or_create`` on a read path, no migration backfill. A newly created
    restaurant gets zero commercial rows until a future onboarding service
    deliberately records them.

    BOTH AXES ARE NULLABLE WITH NO DEFAULT, and that is the load-bearing decision
    in this model. "Not configured" has to be distinguishable from a decision, or
    the first reader cannot tell a choice from a column default — which is exactly
    what makes ``Restaurant.subscription_validity`` (``default=True``, one deleted
    writer) unable to mean anything today. Defaulting `payment_collection_mode` to
    `offline` would be the same mistake in a new table: `offline` is a perfectly
    valid mode, but VALID and CHOSEN are different facts.

    NO TENDER FIELD. There is no `cash` / `momo` / `card` here and there must never
    be one: tender is a property of an individual transaction
    (``DinifyTransaction.payment_mode``), not of a restaurant. A restaurant on
    `offline` may take cash from one diner and mobile money from the next.

    NOT ``users_app.BaseModel``. That base carries ``deleted`` / ``archived`` /
    ``vacuumed`` / ``deletion_reason``, all of which assert that rows get hidden or
    reaped. Commercial configuration is not soft-deleted; it is superseded by being
    rewritten, with the change history living in ``AdminAuditLog``. The same
    reasoning ``RestaurantOnboarding`` gives.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # OneToOne because a restaurant has exactly one current service configuration —
    # expressed as a database uniqueness fact rather than a convention some future
    # service is trusted to keep. PROTECT because restaurants are normally
    # SOFT-deleted: a hard delete is not routine here, and it must not silently
    # erase how a tenant was configured to trade.
    restaurant = models.OneToOneField(
        'restaurants_app.Restaurant',
        on_delete=models.PROTECT,
        related_name='service_configuration',
    )

    # --- concept 1: payment timing ------------------------------------------
    payment_timing = models.CharField(
        max_length=32,
        choices=PAYMENT_TIMING_CHOICES,
        null=True,
        blank=True,
    )
    payment_timing_set_at = models.DateTimeField(null=True, blank=True)
    # WHO last established the CURRENT value. Not owner approval, and not a
    # substitute for change history — AdminAuditLog keeps that. PROTECT so deleting
    # an account cannot erase the attribution.
    #
    # Note what is deliberately NOT here: any "must be platform staff" rule. Write
    # authority belongs to the future domain service, and the two axes may well end
    # up with DIFFERENT authority (a restaurant plausibly gets a say in its own
    # service model; the custody decision is Dinify's). Encoding one answer in the
    # column would pre-empt that.
    payment_timing_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='service_configurations_payment_timing_set',
    )

    # --- concept 2: payment collection mode ---------------------------------
    payment_collection_mode = models.CharField(
        max_length=32,
        choices=PAYMENT_COLLECTION_MODE_CHOICES,
        null=True,
        blank=True,
    )
    payment_collection_mode_set_at = models.DateTimeField(null=True, blank=True)
    payment_collection_mode_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='service_configurations_payment_collection_mode_set',
    )

    # OPERATIONAL METADATA ONLY — row bookkeeping, nothing more.
    #
    # ``updated_at`` MUST NOT become the fingerprint a future owner go-live
    # approval binds to. An approval says "the owner approved launching under THESE
    # terms, THIS timing and THIS collection mode"; binding it to a generic row
    # timestamp would let an unrelated future edit to this row (a column added in
    # some later PR, a re-stamp of an attribution) silently invalidate a valid
    # approval, and would equally fail to invalidate one if a value were changed
    # without the timestamp moving. The approval binds to the EXACT approved facts:
    # the owner id, the exact ``RestaurantSubscriptionTerms.id``, the exact
    # ``payment_timing`` and the exact ``payment_collection_mode``.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'restaurant_service_configuration'
        ordering = ['-created_at']
        constraints = [
            # The closed vocabularies as DATABASE facts. NULL is explicitly allowed
            # on both — it is the honest "nobody has decided" state, not a violation.
            models.CheckConstraint(
                condition=(
                    models.Q(payment_timing__isnull=True)
                    | models.Q(payment_timing__in=PAYMENT_TIMING_VALUES)
                ),
                name='service_configuration_payment_timing_vocabulary',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(payment_collection_mode__isnull=True)
                    | models.Q(
                        payment_collection_mode__in=PAYMENT_COLLECTION_MODE_VALUES
                    )
                ),
                name='service_configuration_payment_collection_mode_vocabulary',
            ),
            # ALL THREE MOVE TOGETHER, per axis. Each partial state breaks the
            # sentence a different way: a value with no timestamp or actor is an
            # unattributable assertion, and a stamp with no value attributes a
            # decision that was never made. The same all-or-none rule
            # ``restaurant_onboarding_attestation_triple`` enforces, applied twice
            # because these are two independent decisions.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        payment_timing__isnull=True,
                        payment_timing_set_at__isnull=True,
                        payment_timing_set_by__isnull=True,
                    )
                    | models.Q(
                        payment_timing__isnull=False,
                        payment_timing_set_at__isnull=False,
                        payment_timing_set_by__isnull=False,
                    )
                ),
                name='service_configuration_payment_timing_triple',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        payment_collection_mode__isnull=True,
                        payment_collection_mode_set_at__isnull=True,
                        payment_collection_mode_set_by__isnull=True,
                    )
                    | models.Q(
                        payment_collection_mode__isnull=False,
                        payment_collection_mode_set_at__isnull=False,
                        payment_collection_mode_set_by__isnull=False,
                    )
                ),
                name='service_configuration_payment_collection_mode_triple',
            ),
            # NOTE there is deliberately NO constraint coupling the two axes. All
            # four combinations are legitimate domain states and a cross-constraint
            # would encode a product opinion the domain does not hold.
        ]

    def __str__(self):
        return f'RestaurantServiceConfiguration<{self.restaurant_id}>'


class RestaurantSubscriptionTerms(models.Model):
    """
    The software-subscription TERMS Dinify has recorded for one restaurant: a
    recurring amount, in a currency, on a recurrence, effective from a moment.

    IT IS CALLED "TERMS", NOT "AGREEMENT", AND THE DIFFERENCE IS THE POINT. A
    platform administrator recording terms does not prove the restaurant's owner
    agreed to them. Naming the row `Agreement` would assert consent this platform
    has never observed — the same fabricated-evidence failure the onboarding
    domain's `legacy_adopted` provenance and its stored attestation SUBJECT exist to
    avoid. Evidence that the CURRENT owner accepted launching under the current
    terms will come from the explicit owner go-live approval, which is a separate
    future record that binds to this row's ``id``.

    WHAT IT IS NOT: payment received, an invoice, an invoice paid, good standing, a
    successful transaction, a trial, merchant readiness, or any diner payment state.
    There is no ``status`` / ``active`` / ``valid`` / ``paid`` / ``good_standing`` /
    ``expired`` column, because every one of those would need a maintainer and this
    repository runs nothing on a schedule (see BACKGROUND_TASKS.md). "Open" is
    DERIVED from an explicit terminal timestamp instead: ``ended_at IS NULL``.

    HISTORY IS THE POINT (0..N per restaurant). Terms change; when they do, the
    future writer CLOSES the open row and INSERTS a replacement rather than editing
    the amount in place. That is why this is a plain FK rather than a OneToOne, why
    the primary key is a stable UUID, and why at most one row may be open at a time.
    A future invoice or receivable will reference the exact terms it was raised
    under, and an approval will reference the exact terms that were approved —
    neither is expressible if the numbers are mutated underneath them.

    NO PLAN / TIER / COMMISSION. There is no ``Plan`` catalogue, no
    ``commission_rate``, no ``per_order_rate`` and no ``surcharge_percentage``.
    Dinify earns a recurring software subscription, never a percentage of diner
    money, and freezing a named plan catalogue would encode a product decision
    nobody has made yet.

    NO LINK TO A TRANSACTION OR AN ORDER, deliberately. Terms are commercial
    intent; ``DinifyTransaction`` is a payment record. An FK between them is how a
    terms row quietly becomes a payment-state model.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # PROTECT: commercial history must block a destructive hard delete rather than
    # vanish with it. A plain FK (not OneToOne) — history is the point.
    restaurant = models.ForeignKey(
        'restaurants_app.Restaurant',
        on_delete=models.PROTECT,
        related_name='subscription_terms',
    )

    # DecimalField, never float — the repository's money rule, enforced in CI by
    # scripts/check_money_fields.py. 12 digits covers any realistic UGX subscription
    # by several orders of magnitude.
    #
    # ZERO IS A LEGITIMATE, EXPLICIT PRICE: a test tenant rehearsing the real
    # onboarding path, a free pilot, a waived period. It is NOT the same fact as
    # "no terms exist" — that is the absence of a row — and keeping the two
    # distinguishable is why there is no separate `chargeable` boolean. A row with
    # ``recurring_amount=0`` already states a complete, unambiguous fact.
    recurring_amount = models.DecimalField(max_digits=12, decimal_places=2)

    # ISO-4217, stored exactly as given. NO DEFAULT: the launch market is Uganda,
    # but "somebody chose UGX" and "the column defaulted" must stay
    # distinguishable, for the same reason both service-configuration axes are
    # nullable. The constraint below enforces the SHAPE (three uppercase ASCII
    # letters); which currencies Dinify actually supports is a policy question for
    # the future domain writer, not a database fact.
    currency = models.CharField(max_length=3)

    # Generic recurrence — see BILLING_INTERVAL_* above for why this is not a plan.
    # max_length is STORAGE SHAPE, not the integrity boundary — the
    # CheckConstraint below is. Deliberately wider than the longest valid unit so a
    # rejected value fails as a constraint violation rather than as a varchar
    # overflow: `per_order` (9 chars) must be refused BY THE VOCABULARY RULE, which
    # is the fact worth asserting, not by happening not to fit.
    billing_interval_unit = models.CharField(
        max_length=16,
        choices=BILLING_INTERVAL_UNIT_CHOICES,
    )
    billing_interval_count = models.PositiveIntegerField()

    # WHEN THESE TERMS BECOME COMMERCIALLY APPLICABLE. Distinct from `recorded_at`
    # and not derivable from it: an operator may legitimately record on Tuesday
    # terms that took effect the previous month. Required, with no default — it is
    # never fabricated from ``Restaurant.time_created``, which answers a different
    # question entirely.
    effective_from = models.DateTimeField()

    # The single intended terminal mutation. NULL means OPEN.
    ended_at = models.DateTimeField(null=True, blank=True)

    # WHEN A PLATFORM-SIDE OPERATOR RECORDED THIS SNAPSHOT, and who. Emphatically
    # NOT `agreed_at` / `agreed_by` / `signed_at` / `paid_at` — this is Dinify
    # writing down what it believes the terms to be. Whether the recorder is
    # ELIGIBLE (platform staff, elevated, with a reason) is a service-layer
    # question; the model records who it was.
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='subscription_terms_recorded',
    )

    class Meta:
        db_table = 'restaurant_subscription_terms'
        ordering = ['-effective_from']
        indexes = [
            # "What has this restaurant been on, and when?" — the history read.
            models.Index(fields=['restaurant', '-effective_from']),
        ]
        constraints = [
            # AT MOST ONE OPEN TERMS ROW PER RESTAURANT — a hard database
            # invariant, not a convention. Historical (ended) rows are unlimited.
            #
            # As with ``one_unresolved_owner_invitation_per_onboarding``, the
            # predicate deliberately consults no clock: a partial-index predicate
            # must be IMMUTABLE, so "open" can only mean "has no terminal stamp".
            # That is what makes the future writer's close-then-insert step
            # load-bearing — it must stamp ``ended_at`` on the outgoing row before
            # inserting the replacement, in one transaction.
            models.UniqueConstraint(
                fields=['restaurant'],
                condition=models.Q(ended_at__isnull=True),
                name='one_open_subscription_terms_per_restaurant',
            ),
            # Zero is allowed; negative is not. A negative subscription fee is
            # Dinify paying the restaurant, which is not what this row can mean.
            models.CheckConstraint(
                condition=models.Q(recurring_amount__gte=0),
                name='subscription_terms_recurring_amount_non_negative',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    billing_interval_unit__in=BILLING_INTERVAL_UNIT_VALUES
                ),
                name='subscription_terms_billing_interval_unit_vocabulary',
            ),
            # "Every 0 months" is not a recurrence.
            models.CheckConstraint(
                condition=models.Q(billing_interval_count__gte=1),
                name='subscription_terms_billing_interval_count_positive',
            ),
            # Terms that ended before they applied never had a period of effect and
            # could not be reported honestly. Equality is allowed: recording terms
            # that were superseded the instant they took effect is unusual but real.
            models.CheckConstraint(
                condition=(
                    models.Q(ended_at__isnull=True)
                    | models.Q(ended_at__gte=models.F('effective_from'))
                ),
                name='subscription_terms_ended_after_effective_from',
            ),
            # Exactly three uppercase ASCII letters. The FIELD already caps the
            # length at three; this adds "not shorter, and not lowercase or
            # numeric", so `ugx`, `UG` and `U1X` are all refused at the database.
            models.CheckConstraint(
                condition=models.Q(currency__regex=r'^[A-Z]{3}$'),
                name='subscription_terms_currency_shape',
            ),
        ]

    def __str__(self):
        return f'RestaurantSubscriptionTerms<{self.restaurant_id}:{self.id}>'

    @property
    def is_open(self):
        """
        Whether these terms are the restaurant's current ones.

        DERIVED, never stored — there is no ``status`` column to go stale, and
        nothing in this repository runs on a schedule that could maintain one. Pure:
        no clock, no database, no side effects.
        """
        return self.ended_at is None
