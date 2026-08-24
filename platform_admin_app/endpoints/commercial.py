"""
Admin SERVICE-CONFIGURATION write endpoints (Phase 1, Step 3D.2a).

Two routes, one per axis of ``RestaurantServiceConfiguration``:

    POST admin/v1/restaurants/<uuid>/commercial/payment-timing/
    POST admin/v1/restaurants/<uuid>/commercial/payment-collection-mode/

TWO ROUTES, NOT ONE, and no ``<str:field>`` parameter between them. These are two
different decisions — a SERVICE-MODEL fact (must settlement be recorded before the
kitchen may fire?) and a CUSTODY fact (does Dinify initiate the diner payment at
all?) — with different consequences and plausibly different future write authority.
A single parameterised route would make "what did this operator change?" a question
about a URL segment rather than about which endpoint was called, and would let one
grant of access reach both. It is the same reasoning that keeps
``service_configuration._set_axis`` private behind two named public writers.

THE ENDPOINTS ARE ADAPTERS, NOT SECOND WRITERS. Nothing here assigns
``RestaurantServiceConfiguration`` fields, and nothing here calls ``save`` /
``create`` / ``update_or_create`` on commercial state. Every write goes through
``commercial_app.service_configuration``, which already owns the ``Restaurant`` row
lock, the soft-delete re-check on the locked row, actor resolution, vocabulary
validation, optimistic concurrency, the same-state no-op and attribution stamping.

The control-plane half — authentication, elevation, CSRF, the reason, the guarded
body parse, the audit entry, the status mapping and the transaction that binds the
mutation to its audit row — lives in ``commercial_base`` and is shared with the
subscription-terms endpoints (Step 3D.2b), so a correction to any of it lands in one
place rather than in two copies that drift.

ONE OUTER TRANSACTION: MUTATION + AUDIT. The domain call and its ``AdminAuditLog``
entry share one ``transaction.atomic()`` here. The writer opens its own atomic
block, which becomes a nested savepoint inside this one — that composition is
exactly why the audit write does not live in ``commercial_app``. If the audit insert
fails, the commercial mutation rolls back with it: an administrative action that
cannot be attributed must not be allowed to stand. The same structure is what lets a
REFUSED mutation still be recorded, since the domain exception unwinds only its own
savepoint.
"""
from django.db import transaction
from rest_framework import serializers

from commercial_app import errors, service_configuration
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    PAYMENT_COLLECTION_MODE_VALUES,
    PAYMENT_TIMING_VALUES,
)
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET,
    ADMIN_RESTAURANT_PAYMENT_TIMING_SET,
)
from platform_admin_app.endpoints.commercial_base import (
    NOT_FOUND_CODES,
    ElevatedCommercialWriteView,
    ReasonedRequestSerializer,
    not_found,
)
from platform_admin_app.models import RESULT_FAILURE, RESULT_SUCCESS


# --- request serializers -----------------------------------------------------

class _CommercialAxisSerializer(ReasonedRequestSerializer):
    """
    Shared request contract for both axes.

    ``value`` and ``expected_current`` are declared by the subclasses, because their
    vocabularies differ and spelling them out per axis is what makes each endpoint's
    accepted set readable at the class it belongs to.
    """

    _FIELD_ORDER = ('value', 'expected_current', 'reason')


class PaymentTimingRequestSerializer(_CommercialAxisSerializer):
    """``pay_first`` | ``pay_after``, plus the concurrency assertion and a reason."""

    value = serializers.ChoiceField(
        choices=PAYMENT_TIMING_VALUES, required=True, allow_null=False,
    )
    # REQUIRED, and separately NULLABLE — the distinction is the whole point.
    #
    # An explicit `null` is a real assertion: "I believe nobody has configured this
    # yet", and it is the ONLY assertion that succeeds against a fresh restaurant.
    # A client that simply omitted the key made no assertion at all, and treating
    # omission as null would hand it that claim by accident — silently defeating the
    # optimistic concurrency it was supposed to be exercising. `required=True` makes
    # omission a 400; `allow_null=True` keeps the explicit null legal.
    expected_current = serializers.ChoiceField(
        choices=PAYMENT_TIMING_VALUES, required=True, allow_null=True,
    )


class PaymentCollectionModeRequestSerializer(_CommercialAxisSerializer):
    """
    ``offline`` | ``psp_online``, plus the concurrency assertion and a reason.

    The vocabulary is CLOSED and is the domain's own. Tender words (``cash``,
    ``card``, ``momo``) and provider names (``flutterwave``, ``pesapal``) are
    refused, and no alias is accepted: collection mode is who initiates the payment,
    which is a different question from what the diner tapped or who processed it.
    """

    value = serializers.ChoiceField(
        choices=PAYMENT_COLLECTION_MODE_VALUES, required=True, allow_null=False,
    )
    expected_current = serializers.ChoiceField(
        choices=PAYMENT_COLLECTION_MODE_VALUES, required=True, allow_null=True,
    )


# --- the shared endpoint body ------------------------------------------------

class _CommercialAxisWriteView(ElevatedCommercialWriteView):
    """
    The shared flow for one service-configuration axis.

    ABSTRACT — the two concrete subclasses below supply the axis. Sharing the body is
    deliberate: the two axes differ ONLY in which writer they call and which key they
    audit, so two hand-copied implementations would be two places for the audit and
    transaction discipline to drift apart. The subclasses stay tiny and fully
    explicit about which decision they make, which is what the route naming exists to
    preserve.
    """

    STATUS_BY_CODE = {
        # The world moved under the caller. Their request was well-formed, so this is
        # not a 400: 409 is the status that means "reload and look again".
        errors.STALE_SERVICE_CONFIGURATION: 409,
        # Defensive. The serializer already rejects anything outside the vocabulary,
        # so these are unreachable through HTTP — kept mapped so a future caller that
        # bypasses the serializer still gets an honest status.
        errors.INVALID_PAYMENT_TIMING: 400,
        errors.INVALID_PAYMENT_COLLECTION_MODE: 400,
    }
    CONFLICT_MESSAGES = {
        errors.STALE_SERVICE_CONFIGURATION:
            'Commercial configuration changed since it was loaded.',
    }
    ERROR_FIELDS = frozenset({'value', 'expected_current'})

    # --- supplied by subclasses ---
    # The audited before/after key, and the attribute the domain result reports it
    # under. One name, so the audit payload cannot describe a different field from
    # the one that moved.
    state_key = None
    writer = None
    success_message = ''

    def post(self, request, restaurant_id):
        restaurant = self.resolve_restaurant(restaurant_id)
        if restaurant is None:
            return not_found()

        payload, parse_error = self.read_body(request, restaurant)
        if parse_error is not None:
            return parse_error

        serializer = self.serializer_class(data=payload)
        if not serializer.is_valid():
            return self.reject_invalid(request, restaurant, serializer)

        data = serializer.validated_data
        reason = data['reason']

        with transaction.atomic():
            try:
                result = self.writer(
                    restaurant_id=restaurant.id,
                    value=data['value'],
                    actor=request.user,
                    # The caller's exact assertion, passed straight through. The
                    # authoritative comparison happens UNDER THE ROW LOCK inside the
                    # writer; comparing here would be a check against a value that
                    # can change before the lock is taken.
                    expected_current=data['expected_current'],
                )
            except CommercialMutationError as exc:
                if exc.code in NOT_FOUND_CODES:
                    return not_found()
                status = self.domain_status(exc)
                if status is None:
                    raise
                self.audit(
                    request,
                    self.audit_action,
                    result=RESULT_FAILURE,
                    resource_type='Restaurant',
                    resource_id=str(restaurant.id),
                    restaurant_id=restaurant.id,
                    reason=reason,
                    error_code=exc.code,
                    # The value actually stored, taken deliberately from the one
                    # detail key that carries it — never by copying the exception's
                    # dict wholesale. NO after_state: nothing was applied, and
                    # inventing one would record a change that never happened.
                    before_state={self.state_key: exc.details.get('actual_current')},
                )
                return self.domain_error_response(exc, status)

            # Audited on a no-op too. `changed=False` states that no domain row
            # moved; the audit row states that an administrator asked for this,
            # which is the fact a control-plane log exists to keep. before == after
            # says both things at once without re-stamping the attribution — the
            # writer already refuses to do that.
            self.audit(
                request,
                self.audit_action,
                result=RESULT_SUCCESS,
                resource_type='Restaurant',
                resource_id=str(restaurant.id),
                restaurant_id=restaurant.id,
                reason=reason,
                before_state={self.state_key: result.previous_value},
                after_state={self.state_key: result.current_value},
            )

            # Re-read INSIDE the transaction, so the client is handed the state the
            # mutation actually produced rather than an echo of what it asked for.
            commercial = self.read_commercial(restaurant)

        return self.success(result.changed, commercial, self.success_message)


class AdminRestaurantPaymentTimingView(_CommercialAxisWriteView):
    """
    ``POST`` this restaurant's PAYMENT TIMING — ``pay_first`` | ``pay_after``.

    A service-model decision: must settlement be recorded before the kitchen may
    fire the order, or does the order fire immediately and the tab settle at the
    end? It says nothing about custody, tender or provider.

    ELEVATION-GATED. Nothing in the order or kitchen path reads this value yet, and
    that is not a reason to gate it lightly: the decision is consequential the
    moment it is recorded, because the enforcement that will eventually consume it
    is built against whatever the configuration then says. A step-up requirement
    added later would also have to explain why the setting was casual first.
    """

    serializer_class = PaymentTimingRequestSerializer
    audit_action = ADMIN_RESTAURANT_PAYMENT_TIMING_SET
    state_key = 'payment_timing'
    writer = staticmethod(service_configuration.set_payment_timing)
    success_message = 'Payment timing recorded.'


class AdminRestaurantPaymentCollectionModeView(_CommercialAxisWriteView):
    """
    ``POST`` this restaurant's PAYMENT COLLECTION MODE — ``offline`` | ``psp_online``.

    A custody decision: does Dinify initiate the diner payment through a licensed
    provider, or does the restaurant collect it itself?

    ``offline`` IS AN ORDINARY, PERMANENT, FULLY CONFIGURED VALUE. It is not a
    fallback, not degraded and not pre-launch-only — the first commercial restaurant
    must be able to go live on it — so this endpoint accepts it as unremarkably as
    the other.

    ``psp_online`` performs EXACTLY ONE commercial configuration mutation. It
    contacts no provider, creates no merchant record or merchant id, validates no
    provider onboarding, initiates no payment and writes no transaction or webhook
    row. There is no PSP integration in this repository; the value records a
    commercial decision, and pairing it with provider-authoritative merchant state
    is a future readiness question.
    """

    serializer_class = PaymentCollectionModeRequestSerializer
    audit_action = ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET
    state_key = 'payment_collection_mode'
    writer = staticmethod(service_configuration.set_payment_collection_mode)
    success_message = 'Payment collection mode recorded.'
