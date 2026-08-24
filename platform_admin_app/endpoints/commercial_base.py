"""
Shared control-plane mechanics for the elevated commercial write endpoints.

Deliberately SMALL, and deliberately not a framework. What lives here is the part
that is identical for every elevated commercial write — authentication, step-up
elevation, target resolution, the guarded body parse, the reason contract, the audit
error vocabulary, the outer-transaction canonical re-read — because those are
properties of *an elevated commercial write*, not of any one operation.

What emphatically does NOT live here is what each operation MEANS. Every endpoint
writes its own ``post()``, names its own domain writer, and builds its own audit
before/after states, because the five operations differ in exactly those places and a
generic ``CommercialMutationView(action=..., writer=..., state_builder=...)`` would
bury the differences that matter most — a replacement's exact-retry rule is not an
"end"'s, and neither is a service-configuration axis's.

The split exists because the alternative is worse: the parse guard below was a real
defect found in review on the first commercial write endpoint (``request.data``
parses on ACCESS, so an unreadable body escaped the audit entirely), and the reason
rule was a second. Copying either into a new module is how the copies drift.
"""
from rest_framework import serializers
from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from commercial_app import errors
from platform_admin_app import commercial_reads
from platform_admin_app.delegation import MIN_REASON_LENGTH
from platform_admin_app.models import RESULT_DENIED, RESULT_FAILURE
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.models import Restaurant

# A generous upper bound on the stated reason. Not a product rule — a bound, so one
# caller cannot push an unbounded blob into the audit table. No honest operator
# explanation reaches it.
MAX_REASON_LENGTH = 1000

# Targeting failures raised by the domain writers. Unreachable in practice — every
# view resolves the restaurant first — but a soft-delete committing between that
# check and the row lock lands here. Answered 404 and NOT audited, matching how the
# transition endpoint treats a target that is not there: nothing was denied and no
# tenant was touched.
NOT_FOUND_CODES = frozenset({
    errors.INVALID_RESTAURANT_ID,
    errors.RESTAURANT_NOT_FOUND,
    errors.RESTAURANT_DELETED,
})


def not_found():
    """404 for a missing OR soft-deleted restaurant — the plane never distinguishes."""
    return Response({'status': 404, 'message': 'Restaurant not found.'}, status=404)


# --- the reason contract -----------------------------------------------------

class ReasonedRequestSerializer(serializers.Serializer):
    """
    Base for every elevated commercial request body: a substantive reason, plus the
    audit vocabulary derived from whatever else failed.

    Plain ``Serializer``, never ``ModelSerializer``. A model-bound serializer over a
    commercial table would expose its attribution columns to DRF's generic mutation
    machinery and let a request body name fields the control plane is supposed to
    stamp from the authenticated session.
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

    # Subclasses override to name their own fields FIRST. Fixed order, so the audit
    # error code for a body with several problems is deterministic rather than
    # dependent on dict iteration.
    _FIELD_ORDER = ('reason',)

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

    def audit_reason(self):
        """
        The reason to record on a REJECTED request: normalized, or empty.

        A reason is recorded ONLY when the reason field itself validated. Reading the
        raw ``initial_data`` instead — the first version of this did — records two
        wrong things: a reason that was itself rejected for being too short still
        lands in the log as though it were a stated reason, and a valid but padded
        one is stored untrimmed whenever some OTHER field is what failed.

        DRF mechanics matter here. ``validated_data`` is ``{}`` after ANY failure, so
        it cannot supply the surviving field's value. What it does give is
        ``self.errors``: if ``reason`` is absent from it, both the field's own
        validation and ``validate_reason`` succeeded — so re-running the field
        reproduces exactly the value the serializer would have kept, deterministically
        and with no second normalisation rule.
        """
        if not isinstance(self.initial_data, dict) or 'reason' in self.errors:
            return ''
        field = self.fields['reason']
        try:
            return field.run_validation(field.get_value(self.initial_data))
        except serializers.ValidationError:
            # Unreachable given the `errors` check above; fail closed anyway rather
            # than let an audit write raise.
            return ''

    def audit_error_code(self):
        """
        One stable machine code naming the first problem, for the audit row.

        Built from DRF's own error codes so it stays in step with the validation
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


# --- the shared view body ----------------------------------------------------

class ElevatedCommercialWriteView(AdminAPIView):
    """
    ABSTRACT. Authentication, elevation, target resolution, body parsing and the
    canonical re-read — everything an elevated commercial write needs before and
    after the part that differs.

    Subclasses supply ``serializer_class``, ``audit_action``, ``STATUS_BY_CODE`` and
    a ``post()`` that calls exactly one Step 3C domain writer.
    """

    permission_classes = [IsAuthenticated, IsRecentlyElevated]

    serializer_class = None
    audit_action = None
    # {domain error code: HTTP status}. Anything absent is deliberately re-raised:
    # an unexpected internal condition should surface as a 500 and roll the
    # transaction back, not be relabelled a client error to keep the error rate tidy.
    STATUS_BY_CODE = {}
    # Fixed operator-facing sentences per code, used for CONFLICTS. Never the
    # domain's own message there — it names internals like the open row's id.
    CONFLICT_MESSAGES = {}
    # Request-body field names a domain 400 may be attributed to. Whitelisted rather
    # than passed through, so a future detail key cannot become a response field.
    ERROR_FIELDS = frozenset()

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused write before DRF turns it into a 403.

        DRF evaluates permissions in ``check_permissions``, BEFORE the handler runs,
        so a stale-elevation request would otherwise 403 with no entry — breaking
        ``AdminAPIView``'s "exactly one entry per unsafe request, including denials".
        Mirrors the lifecycle transition and delegation-mint views.

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

    # --- request plumbing ---

    def resolve_restaurant(self, restaurant_id):
        """The live target, or ``None``. Soft-deleted rows are excluded here."""
        return Restaurant.objects.filter(id=restaurant_id, deleted=False).first()

    def read_body(self, request, restaurant):
        """
        ``(payload, error_response)``. Exactly one is ``None``.

        ``request.data`` PARSES ON ACCESS, and a body DRF cannot read raises here
        rather than reaching the serializer at all. Left unguarded, DRF answers with
        its own bare ``{"detail": ...}`` — a different shape from every other error
        these endpoints return — and, more importantly, the request never reaches
        ``self.audit``, so an elevated administrator's unsafe request would be absent
        from the control-plane log purely because it was unreadable.

        TWO DISTINCT EXCEPTIONS REACH HERE, and catching only the first is the easy
        mistake: a malformed JSON body raises ``ParseError``, but a body whose
        ``Content-Type`` has no parser at all raises ``UnsupportedMediaType``, which
        is NOT a subclass of it. Both are "the server could not read this request",
        so both must be audited — but they are answered differently, because the
        STATUS is the caller's remedy. A ``400`` says *the body was wrong*; a
        ``415`` says *send JSON*. Folding the second into the first would delete the
        one clue that tells the operator which mistake they made.

        (Note that an EMPTY body never reaches either branch — DRF only invokes a
        parser when there is content — so an empty ``text/plain`` request is an
        ordinary validation failure, not a media-type one.)

        The exception detail describes the CALLER'S OWN input and carries no server
        state, so it is passed through: hiding it would cost an operator the one clue
        they need without protecting anything.
        """
        try:
            return request.data, None
        except (ParseError, UnsupportedMediaType) as exc:
            unsupported = isinstance(exc, UnsupportedMediaType)
            status = 415 if unsupported else 400
            self.audit(
                request,
                self.audit_action,
                result=RESULT_FAILURE,
                resource_type='Restaurant',
                resource_id=str(restaurant.id),
                restaurant_id=restaurant.id,
                # No reason can be read from a body that would not parse, and one
                # must never be invented.
                reason='',
                error_code=(
                    'unsupported_media_type' if unsupported else 'malformed_body'
                ),
            )
            return None, Response(
                {
                    'status': status,
                    'message': 'The request could not be applied.',
                    'errors': {'__all__': [str(exc.detail)]},
                },
                status=status,
            )

    def reject_invalid(self, request, restaurant, serializer):
        """
        Audit and answer a body that failed validation.

        A malformed body from an authenticated, elevated administrator is still an
        administrative ATTEMPT at a consequential change, so it is audited rather
        than returned early and forgotten.
        """
        self.audit(
            request,
            self.audit_action,
            result=RESULT_FAILURE,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            reason=serializer.audit_reason(),
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

    # --- domain outcomes ---

    def domain_status(self, exc):
        """The mapped status, or ``None`` when this code is not client-facing."""
        return self.STATUS_BY_CODE.get(exc.code)

    def domain_error_body(self, exc, status):
        """
        The response body for a refused mutation.

        A 400 is about the CALLER'S OWN input, so the domain's explanation is passed
        through and attributed to the request field it names — but only when that
        field is one this endpoint actually accepts, so a future detail key cannot
        leak into the response by default.

        A 409 gets a fixed sentence instead. The domain's message there legitimately
        names internals (the id of the row that is actually open), and a conflict
        message must say only "reload and look again".
        """
        if status == 409:
            return {
                'status': status,
                'message': self.CONFLICT_MESSAGES.get(
                    exc.code, 'Commercial configuration changed since it was loaded.',
                ),
                'code': exc.code,
            }
        field = exc.details.get('field')
        key = field if field in self.ERROR_FIELDS else '__all__'
        return {
            'status': status,
            'message': 'The request could not be applied.',
            'code': exc.code,
            'errors': {key: [exc.message]},
        }

    def domain_error_response(self, exc, status):
        """The refusal, as an HTTP response. See ``domain_error_body``."""
        return Response(self.domain_error_body(exc, status), status=status)

    def read_commercial(self, restaurant):
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

    def success(self, changed, commercial, message):
        """
        The success envelope. Canonical ``commercial`` only.

        The transitional compatibility keys (``payment_mode``,
        ``payment_mode_configured``, ``subscription``) are deliberately ABSENT: they
        exist so the currently deployed Admin frontend keeps working against the GET
        contract, and teaching a write surface to emit them would recruit a new
        consumer for fields that are on their way out.
        """
        return Response(
            {
                'status': 200,
                'message': message,
                'data': {'changed': changed, 'commercial': commercial},
            },
            status=200,
        )
