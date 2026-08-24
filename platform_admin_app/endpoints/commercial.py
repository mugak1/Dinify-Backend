"""
Admin commercial WRITE endpoints (Phase 1, Step 3D.2a).

The first supported HTTP path for changing a restaurant's canonical commercial
configuration. Two routes, one per axis:

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

━━ THE ENDPOINT IS AN ADAPTER, NOT A SECOND WRITER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing here assigns ``RestaurantServiceConfiguration`` fields, and nothing here
calls ``save`` / ``create`` / ``update_or_create`` on commercial state. Every write
goes through ``commercial_app.service_configuration``, which already owns the
``Restaurant`` row lock, the soft-delete re-check on the locked row, actor
resolution, vocabulary validation, optimistic concurrency, the same-state no-op and
attribution stamping. Re-implementing any of that here would create a second set of
rules that could drift from the one the concurrency tests pin.

What this layer owns is the CONTROL-PLANE half: authentication, step-up elevation,
CSRF, a substantive reason, the audit entry, the HTTP status mapping, and the
transaction that binds the mutation to its audit row.

━━ ONE OUTER TRANSACTION: MUTATION + AUDIT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The domain call and its ``AdminAuditLog`` entry share ONE ``transaction.atomic()``
here. The writer opens its own atomic block, which becomes a nested savepoint inside
this one — that composition is exactly why the audit write does not live in
``commercial_app``. If the audit insert fails, the commercial mutation rolls back
with it: an administrative action that cannot be attributed must not be allowed to
stand.

The same structure is what lets a REFUSED mutation still be recorded. A
``CommercialMutationError`` raised inside the writer unwinds only its savepoint, so
the outer transaction survives and the failure audit written afterwards commits
normally.

━━ WHAT THE RESPONSE RETURNS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The canonical Step 3D.1 ``commercial`` object, re-read from the database inside the
same transaction — never assembled from the request, and never a second projection
written for the write path. So a successful write hands the client the exact
``expected_current`` / terms-id tokens its next edit will need, in the identical
shape a GET would have returned.

The transitional compatibility keys (``payment_mode``, ``payment_mode_configured``,
``subscription``) are deliberately ABSENT here. They exist so the currently deployed
Admin frontend keeps working against the GET contract; teaching a brand-new write
surface to emit them would recruit a new consumer for fields that are on their way
out.
"""
from django.db import transaction
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from commercial_app import errors, service_configuration
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    PAYMENT_COLLECTION_MODE_VALUES,
    PAYMENT_TIMING_VALUES,
)
from platform_admin_app import commercial_reads
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_PAYMENT_COLLECTION_MODE_SET,
    ADMIN_RESTAURANT_PAYMENT_TIMING_SET,
)
from platform_admin_app.delegation import MIN_REASON_LENGTH
from platform_admin_app.models import RESULT_DENIED, RESULT_FAILURE, RESULT_SUCCESS
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.models import Restaurant

# A generous upper bound on the stated reason. Not a product rule — a bound, so one
# caller cannot push an unbounded blob into the audit table. No honest operator
# explanation reaches it.
MAX_REASON_LENGTH = 1000

# The domain codes this adapter is prepared to translate into a client answer.
# Anything else — `invalid_actor`, say, which cannot occur for an authenticated
# request — is deliberately NOT caught: an unexpected internal condition should
# surface as a 500 and roll the transaction back, not be relabelled a client error
# to keep the error rate tidy.
_STATUS_BY_CODE = {
    # The world moved under the caller. Their request was well-formed, so this is
    # not a 400: 409 is the status that means "reload and look again".
    errors.STALE_SERVICE_CONFIGURATION: 409,
    # Defensive. The serializer already rejects anything outside the vocabulary, so
    # these are unreachable through HTTP — kept mapped so a future caller that
    # bypasses the serializer still gets an honest status.
    errors.INVALID_PAYMENT_TIMING: 400,
    errors.INVALID_PAYMENT_COLLECTION_MODE: 400,
}

# Targeting failures from the writer. Unreachable in practice — the view resolves the
# restaurant first — but a soft-delete committing between that check and the row lock
# lands here. Answered 404 and NOT audited, matching how the transition endpoint
# treats a target that is not there: nothing was denied and no tenant was touched.
_NOT_FOUND_CODES = frozenset({
    errors.INVALID_RESTAURANT_ID,
    errors.RESTAURANT_NOT_FOUND,
    errors.RESTAURANT_DELETED,
})


# --- request serializers -----------------------------------------------------

class _CommercialAxisSerializer(serializers.Serializer):
    """
    Shared request contract for both axes. Plain ``Serializer``, never a
    ``ModelSerializer``.

    A ``ModelSerializer`` over ``RestaurantServiceConfiguration`` would expose the
    model's own fields — including the ``*_set_by`` attribution FKs — to DRF's
    generic mutation machinery, and would let a request body name columns this
    endpoint has no business writing. The attribution is the control plane's to
    stamp, from the authenticated session, and the only inputs are the three below.

    ``value`` and ``expected_current`` are declared by the subclasses, because their
    vocabularies differ and spelling them out per axis is what makes each endpoint's
    accepted set readable at the class it belongs to.
    """

    reason = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=MAX_REASON_LENGTH,
        # DRF's default, restated because it is load-bearing here: a whitespace-only
        # reason trims to '' and is then refused by `allow_blank=False`, and the
        # value that reaches the audit row is the trimmed one.
        trim_whitespace=True,
    )

    # Fixed order, so the audit error code for a request with several problems is
    # deterministic rather than dependent on dict iteration.
    _FIELD_ORDER = ('value', 'expected_current', 'reason')

    def validate_reason(self, value):
        """
        The house reason bar, imported rather than respelled.

        ``MIN_REASON_LENGTH`` comes from ``platform_admin_app.delegation``, which
        ``restaurants_app.controllers.lifecycle`` already mirrors — a reason is a
        reason, and a third standard on a third surface is how they start
        disagreeing. The messages match those surfaces too.
        """
        if len(value) < MIN_REASON_LENGTH:
            raise serializers.ValidationError(
                f'Please state a reason of at least {MIN_REASON_LENGTH} characters.',
                code='too_short',
            )
        return value

    def audit_error_code(self):
        """
        One stable machine code naming the first problem, for the audit row.

        Built from DRF's own error codes, so it stays in step with the validation
        rather than being a parallel vocabulary maintained by hand: a missing reason
        becomes ``reason_required`` and a short one ``reason_too_short``, which is
        exactly what the delegation and lifecycle surfaces already emit.
        """
        for field in self._FIELD_ORDER:
            detail = self.errors.get(field)
            if not detail:
                continue
            code = getattr(detail[0], 'code', '') if isinstance(detail, list) else ''
            return f'{field}_{code}' if code else f'invalid_{field}'
        return 'invalid_request'


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

def _not_found():
    """404 for a missing OR soft-deleted restaurant — the plane never distinguishes."""
    return Response({'status': 404, 'message': 'Restaurant not found.'}, status=404)


class _CommercialAxisWriteView(AdminAPIView):
    """
    The shared control-plane flow for one commercial axis.

    ABSTRACT — the two concrete subclasses below supply the axis. Sharing the body
    is deliberate: the transaction boundary, the exactly-one-audit rule and the
    status mapping are properties of "an elevated commercial write", not of one
    axis, and two hand-copied implementations are two places for that discipline to
    drift apart. The subclasses stay tiny and fully explicit about which decision
    they make, which is what the route naming exists to preserve.
    """

    permission_classes = [IsAuthenticated, IsRecentlyElevated]

    # --- supplied by subclasses ---
    serializer_class = None
    audit_action = None
    # The audited before/after key, and the attribute the domain result reports it
    # under. One name, so the audit payload cannot describe a different field from
    # the one that moved.
    state_key = None
    writer = None
    success_message = ''

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused write before DRF turns it into a 403.

        DRF evaluates permissions in ``check_permissions``, BEFORE the handler runs,
        so a stale-elevation request would otherwise 403 with no entry — breaking
        ``AdminAPIView``'s "exactly one entry per unsafe request, including
        denials". Mirrors the lifecycle transition and delegation-mint views.

        Only for a request that AUTHENTICATED. An anonymous call is a 401 about
        identity, and a CSRF failure is rejected inside authentication itself; both
        are refused before any administrative decision could exist, and neither may
        manufacture an audit actor we cannot name.
        """
        if getattr(request, 'successful_authenticator', None):
            self.audit(
                request,
                self.audit_action,
                result=RESULT_DENIED,
                resource_type='Restaurant',
                resource_id=str(self.kwargs.get('restaurant_id') or ''),
                restaurant_id=self.kwargs.get('restaurant_id'),
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    def post(self, request, restaurant_id):
        # Soft-deleted rows excluded here, exactly as the transition endpoint does.
        # A target that does not exist gets no audit row: nothing was denied and no
        # tenant was touched, so there is no administrative decision to record.
        restaurant = Restaurant.objects.filter(
            id=restaurant_id, deleted=False,
        ).first()
        if restaurant is None:
            return _not_found()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            # A malformed body from an authenticated, elevated administrator is
            # still an administrative ATTEMPT at a consequential change, so it is
            # audited rather than returned early and forgotten. `reason` is only
            # recorded when it validated — an unvalidated one is not a stated reason.
            self.audit(
                request,
                self.audit_action,
                result=RESULT_FAILURE,
                resource_type='Restaurant',
                resource_id=str(restaurant.id),
                restaurant_id=restaurant.id,
                reason=str(serializer.initial_data.get('reason') or '')[
                    :MAX_REASON_LENGTH
                ] if isinstance(serializer.initial_data, dict) else '',
                error_code=serializer.audit_error_code(),
            )
            return Response(
                {
                    'status': 400,
                    'message': 'The request could not be applied.',
                    'errors': serializer.errors,
                },
                status=400,
            )

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
                if exc.code in _NOT_FOUND_CODES:
                    return _not_found()
                status = _STATUS_BY_CODE.get(exc.code)
                if status is None:
                    # Not a client-facing outcome. Let it propagate: the outer
                    # transaction rolls back and the request fails honestly.
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
                    # inventing one would be a record of a change that never happened.
                    before_state={self.state_key: exc.details.get('actual_current')},
                )
                return Response(
                    {
                        'status': status,
                        'message': self._error_message(exc, status),
                        'code': exc.code,
                    },
                    status=status,
                )

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
            commercial = self._read_commercial(restaurant)

        return Response(
            {
                'status': 200,
                'message': self.success_message,
                'data': {'changed': result.changed, 'commercial': commercial},
            },
            status=200,
        )

    @staticmethod
    def _error_message(exc, status):
        """
        A stable operator-facing sentence. Never the raw exception text.

        The domain's own message is written for a domain caller and can name
        internals; the machine-readable ``code`` in the body is what a client should
        branch on.
        """
        if status == 409:
            return 'Commercial configuration changed since it was loaded.'
        return 'The request could not be applied.'

    @staticmethod
    def _read_commercial(restaurant):
        """
        The canonical Step 3D.1 projection for one restaurant.

        Delegates to ``commercial_reads`` rather than building a write-path shape:
        one representation for reads and successful writes, so the token a client
        gets back from a write is byte-identical to the one a GET would have given
        it. ``annotate_commercial`` composes onto any ``Restaurant`` queryset, which
        is why this needs neither the directory's aggregates nor its subquery.
        """
        row = commercial_reads.annotate_commercial(
            Restaurant.objects.filter(pk=restaurant.pk)
        ).get()
        return commercial_reads.commercial_summary(row)


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
