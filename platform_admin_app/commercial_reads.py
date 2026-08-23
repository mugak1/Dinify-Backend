"""
The Admin COMMERCIAL read projection (Phase 1, Step 3D.1).

Step 3B made a restaurant's commercial facts storable and Step 3C made them
writable. This module is the third face: what the Admin plane is TOLD about them.
``platform_admin_app.restaurant_reads`` delegates to it exactly as it delegates the
onboarding object to ``onboarding_reads`` — a thin hand-off to whoever owns the
question, because what is computed here is COMMERCIAL semantics (what counts as
configured, what an open terms row does and does not prove), not directory
presentation.

It answers ONCE, for BOTH the directory row and the detail response. There is one
commercial read answer; a row that says "unconfigured" while the workspace header
says "offline" is how an operator stops trusting the screen.

━━ THE THREE FACTS ARE INDEPENDENT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Payment timing, payment collection mode and subscription terms are three separate
decisions with three separate lifetimes, and every partial combination is a real
state a restaurant can be in. They are therefore reported as three objects, each
carrying its own ``configured`` flag.

There is deliberately NO single ``commercial_configured`` boolean. Collapsing three
facts into one word would make "partially configured" unrepresentable, and the
readiness engine (Step 3) needs to distinguish exactly which of the three is
missing in order to name a blocker.

━━ WHAT "SUBSCRIPTION TERMS CONFIGURED" MEANS, AND WHAT IT DOES NOT ━━━━━━━━━━━━━

``subscription_terms.configured`` means: THIS RESTAURANT HAS AN OPEN TERMS ROW.
That is a statement about recorded pricing intent and nothing else.

It is NOT: active, paid, valid, current account standing, an invoice, an invoice
paid, good standing, a trial, a successful collection, or any diner payment state.
Dinify has never collected a subscription payment through this system — there is no
invoice model, no receivable and no collection path — so any word implying money
changed hands would be an assertion the database cannot support. The field is named
``current`` in the sense of "the terms record currently in force", and the key it
sits under says ``terms`` for the same reason the model is called
``RestaurantSubscriptionTerms`` and not ``RestaurantSubscriptionAgreement``.

The legacy ``subscription`` object on the same response reports the OLD
``Restaurant`` columns and is a separate, transitional contract. Where the two
disagree — and they will — this object is canonical. See
``restaurant_reads.subscription_summary``.

━━ WHAT "OPEN" MEANS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``ended_at IS NULL``, and nothing else. Not ``effective_from <= now``, not the
latest ``recorded_at``, not the latest ``effective_from``, not
``subscription_validity``, not ``subscription_expiry_date``, and not anything read
off ``DinifyTransaction``.

This is safe to state so flatly because two other layers already guarantee it. The
partial unique index ``one_open_subscription_terms_per_restaurant`` makes "at most
one open row" a DATABASE fact, so "the open row" is unambiguous rather than a
tie-break this module has to invent. And the Step 3C writers refuse future-dated
terms outright, so an open row is never one that has not taken effect yet — there
is no scheduled state for a reader to resolve.

━━ THE RESPONSE IS THE CONCURRENCY TOKEN ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``payment_timing.value``, ``payment_collection_mode.value`` and
``subscription_terms.current.id`` are not decoration. Step 3C's writers take
``expected_current`` and ``expected_terms_id``, so a future Admin write screen must
send back the exact facts it read in order to make an optimistic-concurrency
assertion. The domain facts ARE the tokens — there is deliberately no separate
version counter, which would be a second thing able to drift from the values it
claims to describe.

━━ NO INFERENCE, IN EITHER DIRECTION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing here is derived from ``Restaurant.require_order_prepayments``,
``Table.prepayment_required``, ``preferred_subscription_method``, ``flat_fee``,
``subscription_validity``, ``subscription_expiry_date``,
``DinifyTransaction.payment_mode``, order history, lifecycle state or ``is_test``.
Where a legacy field disagrees with the commercial domain, the commercial domain
wins and the legacy field is simply not consulted.

Values are emitted as the exact persisted MACHINE vocabulary. ``offline`` is a
fully configured, permanent, first-class mode — never rendered as unconfigured, and
never translated into a tender (``cash``) or a provider. ``psp_online`` likewise
carries no provider, merchant id or readiness verdict: this repository has no PSP
integration and therefore no provider-authoritative state to project. Turning
machine values into display prose is the portal's job.

━━ NO ACTOR IDENTITY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``payment_timing_set_by``, ``payment_collection_mode_set_by`` and
``recorded_by`` are NOT exposed. The portal needs to know WHEN a decision was last
established (and needs the value itself for concurrency); WHO established it is
``AdminAuditLog``'s question, and the Activity screen is where it is answered.

This is enforced structurally rather than by remembering not to serialize it: the
annotations below select the four service-configuration columns this projection
actually needs, so the ``*_set_by_id`` columns never enter the result set at all,
and no join to ``users`` is added for them. A test asserts the SQL does not mention
them.

━━ THE PROJECTION IS PURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No write, no ``get_or_create``, no ``save``, no lock, no transaction, no audit row,
no repair, no legacy synchronisation. Reading an unconfigured restaurant creates no
``RestaurantServiceConfiguration`` and no ``RestaurantSubscriptionTerms``; reading a
restaurant whose terms have all ended opens nothing. A GET leaves the database
byte-for-byte as it found it, and tests pin that.
"""
from django.db.models import F, FilteredRelation, Q

# --- annotations -------------------------------------------------------------
#
# ONE QUERY FOR THE PAGE, WHATEVER ITS SIZE. The directory's bounded-query contract
# (ADMIN-DIR-N1-00) predates this module and adding commercial state must not be
# what breaks it: a control plane that issues a query per row is unusable at the
# size it exists to manage.
#
# Two LEFT JOINs, no per-row access and no extra round trip:
#
#   service configuration  a reverse OneToOne, so the join can add at most one row
#                          per restaurant and cannot multiply the result set.
#
#   subscription terms     0..N per restaurant, so a NAIVE join would be a real
#                          defect: ten historical rows would return one restaurant
#                          ten times, inflate the support-issue aggregate, corrupt
#                          `.count()` and shuffle pagination. `FilteredRelation`
#                          puts `ended_at IS NULL` in the JOIN's ON clause, so
#                          historical rows never enter the result at all — and the
#                          partial unique index guarantees what survives is at most
#                          one row. That index is what makes this join safe; without
#                          it the same SQL would be a latent duplication bug.
#
# A `Prefetch` was NOT used: it would turn the page retrieval into two queries and
# give up the existing one-query invariant to solve a problem the join already
# solves. A correlated subquery per field would preserve the invariant too, but at
# seven correlated subqueries per row against one join.
#
# `restaurant_reads.directory_queryset()` applies this ONCE, which is also what
# makes the directory and detail responses structurally incapable of disagreeing:
# they read the same annotations off the same queryset.

_OPEN_TERMS = 'commercial_open_terms'

# The annotation names this module reads. Exported so a test can assert the
# projection consumes exactly what the queryset provides, rather than the two
# drifting until a directory row raises AttributeError in production.
COMMERCIAL_ANNOTATIONS = (
    'commercial_payment_timing',
    'commercial_payment_timing_set_at',
    'commercial_payment_collection_mode',
    'commercial_payment_collection_mode_set_at',
    'commercial_terms_id',
    'commercial_terms_recurring_amount',
    'commercial_terms_currency',
    'commercial_terms_billing_interval_unit',
    'commercial_terms_billing_interval_count',
    'commercial_terms_effective_from',
    'commercial_terms_recorded_at',
)


def annotate_commercial(queryset):
    """
    Add every column ``commercial_summary`` needs to a ``Restaurant`` queryset.

    Read-only and side-effect-free: it composes a queryset and evaluates nothing.

    The service-configuration columns are annotated INDIVIDUALLY rather than
    ``select_related``-ed, for two reasons. It keeps one uniform contract — this
    projection reads annotations and only annotations, so there is no second code
    path that could quietly become a per-row lazy load. And it narrows the SELECT to
    the four columns the response actually contains, which is what makes "no actor
    identity is exposed" a property of the query rather than of remembering not to
    serialize two fields.
    """
    return (
        queryset
        .annotate(
            **{_OPEN_TERMS: FilteredRelation(
                'subscription_terms',
                condition=Q(subscription_terms__ended_at__isnull=True),
            )}
        )
        .annotate(
            commercial_payment_timing=F('service_configuration__payment_timing'),
            commercial_payment_timing_set_at=F(
                'service_configuration__payment_timing_set_at'
            ),
            commercial_payment_collection_mode=F(
                'service_configuration__payment_collection_mode'
            ),
            commercial_payment_collection_mode_set_at=F(
                'service_configuration__payment_collection_mode_set_at'
            ),
            commercial_terms_id=F(f'{_OPEN_TERMS}__id'),
            commercial_terms_recurring_amount=F(f'{_OPEN_TERMS}__recurring_amount'),
            commercial_terms_currency=F(f'{_OPEN_TERMS}__currency'),
            commercial_terms_billing_interval_unit=F(
                f'{_OPEN_TERMS}__billing_interval_unit'
            ),
            commercial_terms_billing_interval_count=F(
                f'{_OPEN_TERMS}__billing_interval_count'
            ),
            commercial_terms_effective_from=F(f'{_OPEN_TERMS}__effective_from'),
            commercial_terms_recorded_at=F(f'{_OPEN_TERMS}__recorded_at'),
        )
    )


# --- serialization -----------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def _axis(value, set_at):
    """
    One configured-or-not commercial axis.

    ``configured`` is derived from the VALUE, which is the canonical fact — not from
    the timestamp, and not from the configuration row merely existing. A row can
    exist with one axis decided and the other still NULL, and that restaurant is
    genuinely unconfigured on the second axis.

    The database keeps ``value`` / ``set_at`` / ``set_by`` all-or-none per axis
    (``service_configuration_payment_*_triple``), so deriving from the value cannot
    disagree with the timestamp — but the value is the fact worth reading.
    """
    configured = value is not None
    return {
        'configured': configured,
        # The exact persisted machine vocabulary. No display prose, no tender, no
        # provider — see the module docstring.
        'value': value if configured else None,
        'set_at': _iso(set_at) if configured else None,
    }


def _current_terms(restaurant):
    """
    The sole OPEN terms row, projected — or ``None`` when the restaurant has none.

    ``recurring_amount`` is serialized with ``str()`` and that is load-bearing, not
    stylistic. DRF's JSON encoder turns a bare ``Decimal`` into a FLOAT, which is
    exactly the conversion this repository's money rule forbids: ``150000.00`` would
    survive it but a value that is not representable in binary floating point would
    not, and a price that renders differently than it is stored is a price nobody
    can reconcile. ``str()`` on the ``Decimal`` the database returned preserves the
    stored scale exactly, so a zero-priced tenant reads ``"0.00"`` — a real,
    deliberate price, not "free", not "trial" and not "unpaid".

    The ``id`` is REQUIRED, not decorative: it is the ``expected_terms_id`` a future
    replace/end write must assert against.
    """
    terms_id = restaurant.commercial_terms_id
    if terms_id is None:
        return None
    return {
        'id': str(terms_id),
        'recurring_amount': str(restaurant.commercial_terms_recurring_amount),
        'currency': restaurant.commercial_terms_currency,
        'billing_interval': {
            'unit': restaurant.commercial_terms_billing_interval_unit,
            'count': restaurant.commercial_terms_billing_interval_count,
        },
        'effective_from': _iso(restaurant.commercial_terms_effective_from),
        'recorded_at': _iso(restaurant.commercial_terms_recorded_at),
    }


def commercial_summary(restaurant):
    """
    The canonical ``commercial`` object, identical for a directory row and a detail.

    Expects a restaurant from a queryset that has been through
    ``annotate_commercial`` — ``restaurant_reads.directory_queryset()`` applies it,
    and both read paths go through that. Reading the annotations rather than the
    relations is what keeps the whole projection free of per-row database access.

    Pure: no query, no write, no lock, no audit.
    """
    current = _current_terms(restaurant)
    return {
        'payment_timing': _axis(
            restaurant.commercial_payment_timing,
            restaurant.commercial_payment_timing_set_at,
        ),
        'payment_collection_mode': _axis(
            restaurant.commercial_payment_collection_mode,
            restaurant.commercial_payment_collection_mode_set_at,
        ),
        'subscription_terms': {
            # "An open terms row exists." NOT active, paid, valid or in good
            # standing — see the module docstring.
            'configured': current is not None,
            'current': current,
        },
    }
