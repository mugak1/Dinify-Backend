"""
Shared control-plane mechanics for the elevated commercial write endpoints.

Deliberately SMALL, and deliberately not a framework. What lives here is the part
that is identical for every elevated commercial write — authentication, step-up
elevation, target resolution, the guarded body parse, the audit error vocabulary, the
outer-transaction canonical re-read — because those are properties of *an elevated
commercial write*, not of any one operation. (The reason contract itself moved one
level out, to ``reasoned_request``, once a non-commercial surface needed it; it is
re-exported here so existing imports are unchanged.)

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
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from commercial_app import errors
from platform_admin_app import commercial_reads
from platform_admin_app.endpoints.reasoned_request import (  # noqa: F401
    MAX_REASON_LENGTH,
    ReasonedRequestSerializer,
    read_request_body,
)
from platform_admin_app.models import RESULT_DENIED, RESULT_FAILURE
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.models import Restaurant

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
#
# MOVED, NOT COPIED. ``ReasonedRequestSerializer`` and ``MAX_REASON_LENGTH`` now live
# in ``platform_admin_app.endpoints.reasoned_request`` because they belong to *an
# audited admin write* rather than to a commercial one — the onboarding creation
# endpoint (Step 2D) needs the identical contract, and a second copy of the
# audit-reason rule is the drift this module's own docstring warns about. Re-exported
# here so every existing import site keeps working unchanged.

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

        The classification — which exception means which status and which error code,
        and why the two must not be folded together — lives in
        ``reasoned_request.read_request_body``. What stays here is what only this
        surface knows: that the resource is a ``Restaurant`` that already exists, and
        so which ids the audit row carries.
        """
        payload, unreadable = read_request_body(request)
        if unreadable is None:
            return payload, None
        status, error_code, detail = unreadable
        self.audit(
            request,
            self.audit_action,
            result=RESULT_FAILURE,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            # No reason can be read from a body that would not parse, and one must
            # never be invented.
            reason='',
            error_code=error_code,
        )
        return None, Response(
            {
                'status': status,
                'message': 'The request could not be applied.',
                'errors': {'__all__': [detail]},
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
