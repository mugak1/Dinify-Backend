"""
Domain errors for commercial mutations (Phase 1, Step 3C).

ONE exception type with a stable machine-readable ``code``, mirroring
``platform_admin_app.onboarding_adoption.AdoptionError`` and
``platform_admin_app.delegation.DelegationValidationError``.

WHY A NARROW TYPE RATHER THAN WHATEVER THE ORM RAISES. ``DoesNotExist``,
``IntegrityError``, ``ValidationError``, ``ValueError`` and ``decimal.InvalidOperation``
all mean something specific in this domain — a mistyped target, a lost race, a
malformed amount — and letting them escape would hand a future HTTP adapter nothing
to branch on but exception classes that mean different things in different places. It
would also turn an ordinary, expected concurrency outcome into a 500.

``details`` carries UUIDs and machine values ONLY. These messages reach logs and
operator-facing surfaces, and an owner's email, phone or name has no business in
either — an identifier is enough to investigate with.
"""

# --- error codes -------------------------------------------------------------
#
# Distinct because each calls for a different reaction: fix the identifier, pick a
# real tenant, fix the invocation, reload the screen, or use a different operation.

# Targeting.
INVALID_RESTAURANT_ID = 'invalid_restaurant_id'
RESTAURANT_NOT_FOUND = 'restaurant_not_found'
RESTAURANT_DELETED = 'restaurant_deleted'
INVALID_ACTOR = 'invalid_actor'

# Service configuration.
INVALID_PAYMENT_TIMING = 'invalid_payment_timing'
INVALID_PAYMENT_COLLECTION_MODE = 'invalid_payment_collection_mode'
# The optimistic-concurrency refusal: the caller's belief about the current value no
# longer matches the locked row. Its own code because the correct reaction is
# "reload and look again", not "fix your input".
STALE_SERVICE_CONFIGURATION = 'stale_service_configuration'

# Subscription terms.
INVALID_SUBSCRIPTION_TERMS = 'invalid_subscription_terms'
SUBSCRIPTION_TERMS_ALREADY_OPEN = 'subscription_terms_already_open'
SUBSCRIPTION_TERMS_NOT_FOUND = 'subscription_terms_not_found'
STALE_SUBSCRIPTION_TERMS = 'stale_subscription_terms'
NO_OPEN_SUBSCRIPTION_TERMS = 'no_open_subscription_terms'
# Covers every future-dated terms BOUNDARY — an effective_from ahead of now, and an
# ended_at ahead of now. One rule ("this domain does not schedule"), so one code:
# the open-row invariant is `ended_at IS NULL`, not a clock-dependent state machine,
# and there is no scheduler in this repository to maintain one.
FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED = 'future_effective_terms_not_supported'

COMMERCIAL_ERROR_CODES = frozenset({
    INVALID_RESTAURANT_ID,
    RESTAURANT_NOT_FOUND,
    RESTAURANT_DELETED,
    INVALID_ACTOR,
    INVALID_PAYMENT_TIMING,
    INVALID_PAYMENT_COLLECTION_MODE,
    STALE_SERVICE_CONFIGURATION,
    INVALID_SUBSCRIPTION_TERMS,
    SUBSCRIPTION_TERMS_ALREADY_OPEN,
    SUBSCRIPTION_TERMS_NOT_FOUND,
    STALE_SUBSCRIPTION_TERMS,
    NO_OPEN_SUBSCRIPTION_TERMS,
    FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED,
})


class CommercialMutationError(Exception):
    """A refused commercial mutation. ``code`` is one of ``COMMERCIAL_ERROR_CODES``."""

    def __init__(self, code, message='', details=None):
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)

    def __str__(self):
        return self.message
