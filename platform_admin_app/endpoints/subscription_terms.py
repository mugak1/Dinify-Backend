"""
Admin SUBSCRIPTION-TERMS write endpoints (Phase 1, Step 3D.2b).

Three routes, one per Step 3C domain operation:

    POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/
    POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/replace/
    POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/end/

THREE EXPLICIT ROUTES, not one with an ``action``. Recording first terms, superseding
the open ones and closing them are materially different decisions with different
preconditions, different concurrency tokens and different histories left behind; a
``subscription-terms/<str:action>/`` route would make "what did this operator do?" a
question about a path segment, and a single serializer with conditionally-required
fields would make the accepted body a question about a value inside it. The routes
name the operations, which is what makes an audit log readable a year later.

━━ WHAT THESE ROWS ARE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The recurring SOFTWARE-SUBSCRIPTION terms Dinify has RECORDED for a restaurant —
restaurant → Dinify money, entirely separate from diner → restaurant payments.

They are not an invoice, not a payment, not paid status, not entitlement, not good
standing, not PSP state and not owner agreement. Recording terms charges nobody and
proves nothing about the owner's consent. "Open" means ``ended_at IS NULL`` and
nothing more; ``current`` is used only in the established sense of *the terms record
currently in force*.

━━ THE ENDPOINTS ARE ADAPTERS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing here creates, saves, updates or deletes a ``RestaurantSubscriptionTerms``
row. Every mutation goes through ``commercial_app.subscription_terms``, which owns
the ``Restaurant`` lock, the monotonic timeline rule, open-row selection, the
close-then-insert boundary, the exact-retry proofs and every no-op rule. Those are
subtle enough that a second implementation would not merely duplicate them — it
would disagree with them.

━━ THE AUDIT RULE THAT IS EASIEST TO GET WRONG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each endpoint's before/after states describe THE CANONICAL CURRENT CONFIGURATION as
this request found it and left it — never a historical transition replayed.

That matters most on a retry. ``replace_subscription_terms`` returns
``previous_terms`` on an exact retry, as the evidence that the replacement it is
being asked to repeat already happened. Using that row as this request's
``before_state`` would write the old → new transition into the log a SECOND time, as
though it had occurred twice. It did not: this request moved nothing. So a replace
no-op audits ``before == after == the current open terms``, and an end no-op audits
``null → null``.
"""
import uuid

from django.db import transaction
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from commercial_app import errors, subscription_terms
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    RestaurantSubscriptionTerms,
)
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED,
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED,
    ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED,
)
from platform_admin_app.endpoints.commercial_base import (
    NOT_FOUND_CODES,
    ElevatedCommercialWriteView,
    ReasonedRequestSerializer,
    not_found,
)
from platform_admin_app.models import RESULT_FAILURE, RESULT_SUCCESS

# The audit key every one of these operations reports under. One name, so no event
# can describe a different fact from the one it moved.
STATE_KEY = 'subscription_terms'


# --- strict input primitives -------------------------------------------------

class StrictDecimalStringField(serializers.Field):
    """
    A decimal amount that MUST arrive as a JSON string.

    ``"150000.00"`` is accepted; ``150000``, ``150000.0``, ``true`` and ``null`` are
    not. That strictness is the point rather than fussiness: the Step 3C domain
    refuses binary-float ambiguity outright and the Step 3D.1 read contract already
    emits ``recurring_amount`` as a decimal string, so accepting a JSON number here
    would put a float in the middle of a round trip that is otherwise exact — and
    ``0.00`` versus ``0.0`` is precisely the distinction that would be lost.

    A DRF ``CharField`` would NOT do: it coerces a JSON number to its string form and
    the contract would be silently bypassed by the very input it exists to refuse.

    The value is passed to the domain UNPARSED. Canonical ``Decimal`` validation —
    scale, magnitude, negativity — belongs to ``commercial_app`` and is not
    reimplemented here.
    """

    default_error_messages = {
        'not_a_string': 'Send the amount as a decimal string, e.g. "150000.00".',
    }

    def to_internal_value(self, data):
        # `bool` is not a `str`, so True/False are refused here rather than reaching
        # the domain as something that could later look like a number.
        if not isinstance(data, str):
            self.fail('not_a_string')
        return data

    def to_representation(self, value):
        return value


class StrictUUIDStringField(serializers.Field):
    """
    A concurrency token that MUST arrive as a JSON string holding a UUID.

    ``"7f1c…"`` is accepted; ``42``, ``true`` and ``null`` are not.

    A DRF ``UUIDField`` would NOT do, and the reason is specific rather than
    stylistic: given a JSON number it evaluates ``uuid.UUID(int=data)``, so ``42``
    becomes ``00000000-0000-0000-0000-00000000002a`` — a perfectly well-formed UUID
    that no row has ever carried. The request then reaches the domain, misses, and
    comes back as a **409 saying the terms changed since they were loaded**. Nothing
    changed; the body was malformed. A concurrency conflict is the one error here
    that tells an operator the world moved under them, and it must never be the
    answer to a typo.

    The token is only ever produced by this API — ``subscription_terms.current.id``
    in the canonical read, a string — so a number is never something a legitimate
    client has.

    The parsed ``UUID`` is handed to the domain, which treats it as identity.
    """

    default_error_messages = {
        'not_a_string': 'Send the terms id as the UUID string the read returns.',
        'invalid': 'Enter a valid UUID.',
    }

    def to_internal_value(self, data):
        # `bool` is not a `str`, so True/False are refused here rather than being
        # reinterpreted as an integer and, from there, as a UUID.
        if not isinstance(data, str):
            self.fail('not_a_string')
        try:
            return uuid.UUID(data.strip())
        except (ValueError, AttributeError, TypeError):
            self.fail('invalid')

    def to_representation(self, value):
        return str(value)


class AwareDateTimeField(serializers.Field):
    """
    An ISO-8601 datetime that MUST carry an explicit timezone offset.

    ``2026-08-24T12:00:00Z`` and ``2026-08-24T15:00:00+03:00`` are accepted;
    ``2026-08-24T12:00:00`` and ``2026-08-24`` are not.

    DRF's own ``DateTimeField`` cannot express this. With ``USE_TZ`` on it makes a
    naive value aware using the CURRENT timezone, silently converting an omission
    into an assumption; with ``default_timezone=None`` it strips the zone off aware
    values instead. Both are wrong for a commercial boundary — the difference between
    midnight EAT and midnight UTC is three hours of "which terms were in force", and
    an operator in another timezone would never see the substitution happen.

    Step 3C already refuses naive datetimes for exactly this reason. This field makes
    the same refusal at the edge, on the RAW input, before any framework coercion can
    hide the omission. A date-only value is refused too: it is a naive midnight
    wearing a different shape.
    """

    default_error_messages = {
        'invalid': 'Enter a valid ISO-8601 datetime, e.g. 2026-08-24T12:00:00Z.',
        'naive': (
            'Include an explicit timezone offset, e.g. 2026-08-24T12:00:00Z or '
            '2026-08-24T15:00:00+03:00.'
        ),
    }

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('invalid')
        try:
            parsed = parse_datetime(data.strip())
        except ValueError:
            # Well-formed shape, impossible value (month 13, day 32...).
            self.fail('invalid')
        if parsed is None:
            self.fail('invalid')
        if parsed.tzinfo is None:
            self.fail('naive')
        return parsed

    def to_representation(self, value):
        return value.isoformat()


# --- the audit snapshot ------------------------------------------------------

def terms_snapshot(terms):
    """
    One terms row as an audit event should record it, or ``None``.

    NARROW ON PURPOSE. It carries the immutable COMMERCIAL FACTS and the identity
    that a future invoice or owner approval will reference — and nothing else.

    Deliberately absent: ``recorded_by`` and ``recorded_at`` (the audit row already
    says who made this request and when, and the terms' own provenance is not what
    the event is about), ``ended_at`` (a terminal stamp is not a commercial fact, and
    including it on a replacement's outgoing row would make the before-state describe
    the closure rather than the terms), and every word this domain does not have —
    no status, active, paid, valid, good standing, invoice, PSP or transaction.
    """
    if terms is None:
        return None
    return {
        'id': str(terms.id),
        # A decimal STRING, matching the canonical read. A bare Decimal would be
        # rendered as a float by the audit's JSON normalisation and lose its scale.
        'recurring_amount': str(terms.recurring_amount),
        'currency': terms.currency,
        'billing_interval': {
            'unit': terms.billing_interval_unit,
            'count': terms.billing_interval_count,
        },
        'effective_from': terms.effective_from.isoformat(),
    }


def _state(terms):
    return {STATE_KEY: terms_snapshot(terms)}


# --- request serializers -----------------------------------------------------

class _TermsFactsSerializer(ReasonedRequestSerializer):
    """
    The five commercial facts a terms row is made of, plus the reason.

    Shared by record and replace because a terms row IS these facts however it comes
    into existence; replace adds the concurrency token on top. Every one of them is
    required — there is no partial terms row, and a default for any of them would be
    Dinify inventing a commercial term nobody stated.
    """

    recurring_amount = StrictDecimalStringField(required=True)
    # Shape and canonical case belong to the domain (trim, upper, three letters), so
    # the raw string is passed through rather than half-normalised here. There is
    # deliberately no currency ALLOWLIST at either layer: this repository has no
    # authoritative source for one.
    currency = serializers.CharField(
        required=True, allow_blank=False, trim_whitespace=False, max_length=16,
    )
    # Matched exactly. `monthly`, `MONTH`, `annually` and `per_order` are all refused
    # rather than coerced — the stored value has to read back unambiguously, and
    # `per_order` in particular would restate the per-order-commission framing the
    # non-custodial posture exists to keep out.
    billing_interval_unit = serializers.ChoiceField(
        choices=BILLING_INTERVAL_UNIT_VALUES, required=True, allow_null=False,
    )
    # The upper bound is the domain's (a 32-bit column) and is not respelled here;
    # an oversized count is refused there as a named 400 rather than a DataError.
    billing_interval_count = serializers.IntegerField(required=True, min_value=1)
    effective_from = AwareDateTimeField(required=True)


class RecordSubscriptionTermsRequestSerializer(_TermsFactsSerializer):
    """
    Record terms for a restaurant that has none open.

    THERE IS DELIBERATELY NO ``expected_terms_id``. The operation means "record terms
    only if none are open", and Step 3C enforces that under the ``Restaurant`` lock:
    identical open terms are a safe no-op, different ones are
    ``subscription_terms_already_open``. A concurrency token the domain does not
    consult would be a field the caller has to supply and nothing would check.
    """

    _FIELD_ORDER = (
        'recurring_amount', 'currency', 'billing_interval_unit',
        'billing_interval_count', 'effective_from', 'reason',
    )


class ReplaceSubscriptionTermsRequestSerializer(_TermsFactsSerializer):
    """
    Supersede the exact currently-open terms.

    ``expected_terms_id`` is the UUID the canonical read publishes as
    ``commercial.subscription_terms.current.id``. It is REQUIRED and non-null: a
    replacement without one would be "supersede whatever happens to be open", which
    is precisely the stale-screen overwrite the token exists to prevent. It is never
    pre-compared here — the authoritative comparison happens under the row lock.
    """

    expected_terms_id = StrictUUIDStringField(required=True, allow_null=False)

    _FIELD_ORDER = (
        'expected_terms_id', 'recurring_amount', 'currency',
        'billing_interval_unit', 'billing_interval_count', 'effective_from',
        'reason',
    )


class EndSubscriptionTermsRequestSerializer(ReasonedRequestSerializer):
    """
    Close the exact currently-open terms, leaving the restaurant with none.

    ``ended_at`` is REQUIRED and never defaulted to "now". The operator is recording
    the boundary at which the terms stopped applying, which is frequently not the
    moment they got round to typing it — backdating is an ordinary, truthful
    operation here, and inferring the current time would quietly record a different
    fact from the one they meant.
    """

    expected_terms_id = StrictUUIDStringField(required=True, allow_null=False)
    ended_at = AwareDateTimeField(required=True)

    _FIELD_ORDER = ('expected_terms_id', 'ended_at', 'reason')


# --- the shared control-plane surface ----------------------------------------

class _SubscriptionTermsWriteView(ElevatedCommercialWriteView):
    """
    Authentication, elevation, status mapping and error vocabulary shared by the
    three operations. Each subclass writes its OWN ``post()``: they call different
    writers, carry different tokens, and — most importantly — build different audit
    states, and that is exactly the part that must stay visible.
    """

    STATUS_BY_CODE = {
        # The caller's own input is wrong: bad amount, currency, interval, or a
        # boundary that would overlap or invert the timeline.
        errors.INVALID_SUBSCRIPTION_TERMS: 400,
        errors.FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED: 400,
        # STATE conflicts. The body was well-formed; the world is not what the caller
        # believed it to be, and the fix is to reload rather than to edit the request.
        errors.SUBSCRIPTION_TERMS_ALREADY_OPEN: 409,
        errors.STALE_SUBSCRIPTION_TERMS: 409,
        errors.NO_OPEN_SUBSCRIPTION_TERMS: 409,
        # 409 rather than 404, deliberately. This route's target is the RESTAURANT,
        # not the terms row; `expected_terms_id` is a concurrency assertion about
        # that restaurant's current state, and an assertion that no longer resolves
        # means the operator's view is stale. It also means a UUID belonging to
        # ANOTHER tenant is answered identically to a UUID that never existed, so the
        # response cannot be used to probe for other restaurants' terms.
        errors.SUBSCRIPTION_TERMS_NOT_FOUND: 409,
    }
    CONFLICT_MESSAGES = {
        errors.SUBSCRIPTION_TERMS_ALREADY_OPEN:
            'This restaurant already has different open subscription terms.',
        errors.STALE_SUBSCRIPTION_TERMS:
            'Subscription terms changed since they were loaded.',
        errors.NO_OPEN_SUBSCRIPTION_TERMS:
            'This restaurant has no open subscription terms.',
        errors.SUBSCRIPTION_TERMS_NOT_FOUND:
            'Subscription terms changed since they were loaded.',
    }
    ERROR_FIELDS = frozenset({
        'recurring_amount', 'currency', 'billing_interval_unit',
        'billing_interval_count', 'effective_from', 'ended_at',
        'expected_terms_id',
    })

    def audit_failure(self, request, restaurant, exc, reason, before_terms):
        """
        One failure row for a refused mutation.

        ``before_terms`` is THIS restaurant's actual current open terms, read by the
        caller — never anything reconstructed from ``exc.details``, which may name a
        row the caller has no business being told about. No ``after_state``: nothing
        was applied, and inventing one would record a change that never happened.
        """
        self.audit(
            request,
            self.audit_action,
            result=RESULT_FAILURE,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            reason=reason,
            error_code=exc.code,
            before_state=_state(before_terms),
        )

    def audit_success(self, request, restaurant, reason, before_terms, after_terms):
        """One success row. Equal states on a no-op — the request happened, nothing moved."""
        self.audit(
            request,
            self.audit_action,
            result=RESULT_SUCCESS,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            reason=reason,
            before_state=_state(before_terms),
            after_state=_state(after_terms),
        )

    def open_terms(self, restaurant):
        """
        This restaurant's open terms, or ``None`` — for a failure audit's before-state.

        ``ended_at IS NULL`` is not a heuristic this module is choosing: it is the
        predicate of ``one_open_subscription_terms_per_restaurant``, the partial
        unique index that makes "at most one open row" a database fact. Reading the
        invariant directly is therefore not a second opinion about what "open" means,
        and it avoids reaching into the domain's private selector.

        Only ever called on a refusal path, where the mutation did not happen (the
        domain's savepoint has rolled back) and one extra read costs nothing.
        """
        return (
            RestaurantSubscriptionTerms.objects
            .filter(restaurant=restaurant, ended_at__isnull=True)
            .first()
        )

    def _facts(self, data):
        """The five commercial facts, as the writers name them."""
        return {
            'recurring_amount': data['recurring_amount'],
            'currency': data['currency'],
            'billing_interval_unit': data['billing_interval_unit'],
            'billing_interval_count': data['billing_interval_count'],
            'effective_from': data['effective_from'],
        }

    def prepare(self, request, restaurant_id):
        """
        Resolve, parse and validate. Returns ``(restaurant, data, error_response)``.

        Ordering is load-bearing and matches the service-configuration endpoints: the
        TARGET is resolved first, so an unreadable or invalid body aimed at a
        restaurant that does not exist is still a silent 404 rather than an audit row
        about a tenant that was never touched.
        """
        restaurant = self.resolve_restaurant(restaurant_id)
        if restaurant is None:
            return None, None, not_found()

        payload, parse_error = self.read_body(request, restaurant)
        if parse_error is not None:
            return restaurant, None, parse_error

        serializer = self.serializer_class(data=payload)
        if not serializer.is_valid():
            return restaurant, None, self.reject_invalid(
                request, restaurant, serializer,
            )
        return restaurant, serializer.validated_data, None


class AdminRestaurantRecordSubscriptionTermsView(_SubscriptionTermsWriteView):
    """
    ``POST`` the restaurant's subscription terms, when it has none open.

    Also the route for reopening after a previous set was ended — subject to the
    domain's monotonic timeline rule, which refuses an ``effective_from`` earlier
    than the latest closure so two windows cannot overlap.
    """

    serializer_class = RecordSubscriptionTermsRequestSerializer
    audit_action = ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_RECORDED

    def post(self, request, restaurant_id):
        restaurant, data, error = self.prepare(request, restaurant_id)
        if error is not None:
            return error

        with transaction.atomic():
            try:
                result = subscription_terms.record_subscription_terms(
                    restaurant_id=restaurant.id,
                    actor=request.user,
                    **self._facts(data),
                )
            except CommercialMutationError as exc:
                if exc.code in NOT_FOUND_CODES:
                    return not_found()
                status = self.domain_status(exc)
                if status is None:
                    raise
                self.audit_failure(
                    request, restaurant, exc, data['reason'],
                    self.open_terms(restaurant),
                )
                return self.domain_error_response(exc, status)

            # A real recording moves null -> the new terms. An exact retry moves
            # nothing, and says so with equal states rather than pretending the terms
            # were created a second time.
            before = None if result.changed else result.terms
            self.audit_success(
                request, restaurant, data['reason'], before, result.terms,
            )
            commercial = self.read_commercial(restaurant)

        return self.success(
            result.changed, commercial, 'Subscription terms recorded.',
        )


class AdminRestaurantReplaceSubscriptionTermsView(_SubscriptionTermsWriteView):
    """
    ``POST`` a replacement for the exact currently-open terms.

    One atomic operation in the domain: the outgoing row is closed at exactly the
    replacement's ``effective_from`` and the successor inserted, so the history has
    no gap in which the restaurant had no terms and no overlap in which it had two.
    """

    serializer_class = ReplaceSubscriptionTermsRequestSerializer
    audit_action = ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_REPLACED

    def post(self, request, restaurant_id):
        restaurant, data, error = self.prepare(request, restaurant_id)
        if error is not None:
            return error

        with transaction.atomic():
            try:
                result = subscription_terms.replace_subscription_terms(
                    restaurant_id=restaurant.id,
                    expected_terms_id=data['expected_terms_id'],
                    actor=request.user,
                    **self._facts(data),
                )
            except CommercialMutationError as exc:
                if exc.code in NOT_FOUND_CODES:
                    return not_found()
                status = self.domain_status(exc)
                if status is None:
                    raise
                self.audit_failure(
                    request, restaurant, exc, data['reason'],
                    self.open_terms(restaurant),
                )
                return self.domain_error_response(exc, status)

            # THE TRAP. On an exact retry the domain hands back `previous_terms` as
            # PROOF that the replacement it is being asked to repeat already
            # happened. Using it as this request's before-state would write the
            # old -> new transition into the log a second time, as though it had
            # occurred twice. It did not: this request moved nothing, so both states
            # are the terms that are open right now.
            before = result.previous_terms if result.changed else result.terms
            self.audit_success(
                request, restaurant, data['reason'], before, result.terms,
            )
            commercial = self.read_commercial(restaurant)

        return self.success(
            result.changed, commercial, 'Subscription terms replaced.',
        )


class AdminRestaurantEndSubscriptionTermsView(_SubscriptionTermsWriteView):
    """
    ``POST`` to close the exact currently-open terms, creating no replacement.

    Afterwards the restaurant has NO open terms, which a future readiness rule will
    truthfully read as commercially unconfigured. Nothing is auto-created to fill the
    gap: deciding the next terms is a separate decision somebody has to make.

    NO ACTOR reaches the domain, deliberately — Step 3B added no ``ended_by`` column,
    and adding schema purely to duplicate what this audit row already records would
    create a second home for the same fact. Who ended the terms, under what session
    and why is exactly what ``AdminAuditLog`` is for.
    """

    serializer_class = EndSubscriptionTermsRequestSerializer
    audit_action = ADMIN_RESTAURANT_SUBSCRIPTION_TERMS_ENDED

    def post(self, request, restaurant_id):
        restaurant, data, error = self.prepare(request, restaurant_id)
        if error is not None:
            return error

        with transaction.atomic():
            try:
                result = subscription_terms.end_subscription_terms(
                    restaurant_id=restaurant.id,
                    expected_terms_id=data['expected_terms_id'],
                    ended_at=data['ended_at'],
                )
            except CommercialMutationError as exc:
                if exc.code in NOT_FOUND_CODES:
                    return not_found()
                status = self.domain_status(exc)
                if status is None:
                    raise
                self.audit_failure(
                    request, restaurant, exc, data['reason'],
                    self.open_terms(restaurant),
                )
                return self.domain_error_response(exc, status)

            # A real end moves the open terms -> none. An exact retry finds none open
            # already and moves nothing: null -> null. Stuffing the requested UUID
            # into the state to make the no-op look busier would replay the original
            # open -> none transition, which is not what this request did.
            before = result.terms if result.changed else None
            self.audit_success(request, restaurant, data['reason'], before, None)
            commercial = self.read_commercial(restaurant)

        return self.success(result.changed, commercial, 'Subscription terms ended.')
