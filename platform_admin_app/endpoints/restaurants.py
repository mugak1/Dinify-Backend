"""
Restaurant admin-plane endpoints — the directory/detail reads, and the transition.

READS ARE NOT ELEVATION-GATED, AND THAT IS DELIBERATE. ``IsRecentlyElevated`` exists
for actions whose blast radius justifies re-proving a second factor mid-session —
stopping a tenant trading, minting a delegation. Reading the operator's own portfolio
is the ordinary work of the control plane: it is what the operator does on arriving,
and demanding a TOTP code to look at a list would train them to re-elevate reflexively,
which is precisely the habit step-up authentication depends on them NOT having. A
valid session is the right bar for a read.

ORDINARY GETS ARE NOT AUDITED. ``AdminAuditLog`` is the record of administrative
ACTION, not an access log; the "exactly one entry per unsafe request" convention on
``AdminAuditLog`` covers POST/PUT/PATCH/DELETE. Auditing directory views would bury
the transitions and delegations the log exists to make findable under a drift of page
loads — and would then feed itself, since ``last_activity_at`` reads that same log.

THE VIEWS ARE THIN. The read model lives in ``platform_admin_app.restaurant_reads``
and every lifecycle rule in ``restaurants_app.controllers.lifecycle``, for the same
reason the transition view delegates: a second caller should inherit behaviour rather
than re-implement it.

ELEVATION. Changing a tenant's operational state stops their diners ordering, their
kitchen working and their staff signing in, so the whole endpoint is step-up gated:
a valid session is not enough, the administrator must have cleared a second factor
within the elevation window. That covers the specification's "elevated confirmation"
requirement for offboarding and applies the same bar to suspension, which is just as
disruptive to a restaurant mid-service.

THE VIEW IS THIN. Every rule — the matrix, the reason requirement, the readiness and
receivables preconditions, the row lock and the audit entry — lives in
``restaurants_app.controllers.lifecycle``. This translates HTTP to that call and back,
so a second caller (a management command, Phase-1 automation) inherits identical
behaviour rather than a re-implementation.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from platform_admin_app import restaurant_reads
from platform_admin_app.audit_actions import ADMIN_RESTAURANT_TRANSITION_DENIED
from platform_admin_app.models import RESULT_DENIED
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant


def _not_found():
    """404 for a missing OR soft-deleted restaurant — the plane never distinguishes."""
    return Response({'status': 404, 'message': 'Restaurant not found.'}, status=404)


class AdminRestaurantListView(AdminAPIView):
    """
    ``GET`` the restaurant directory: filtered, paginated, one query for the page.

    Query parameters: ``search``, ``status``, ``attention``, ``page``, ``page_size``.
    A malformed one is a 400 with field-keyed errors, never an empty 200 — an empty
    page means "nothing matches", and returning it for a typo would let the operator
    conclude something false about the portfolio.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            params = restaurant_reads.parse_directory_params(request.query_params)
        except restaurant_reads.QueryParamError as exc:
            return Response(
                {'status': 400, 'message': 'Invalid query parameters.',
                 'errors': exc.errors},
                status=400,
            )

        queryset = restaurant_reads.apply_directory_filters(
            restaurant_reads.directory_queryset(), params,
        )

        page, page_size = params['page'], params['page_size']
        count = queryset.count()
        # Ceiling division. `pages` is 1 for an empty result rather than 0, so the
        # portal always has a page to render and never divides by it.
        pages = max(1, -(-count // page_size))
        offset = (page - 1) * page_size
        # A page beyond the end returns an empty `results` with honest metadata
        # rather than a 404: the page number is well-formed, the portfolio simply
        # does not extend that far, and the client can see that from `pages`.
        rows = list(queryset[offset:offset + page_size])

        return Response(
            {
                'status': 200,
                'data': {
                    'results': [restaurant_reads.serialize_row(r) for r in rows],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'count': count,
                        'pages': pages,
                    },
                },
            },
            status=200,
        )


class AdminRestaurantDetailView(AdminAPIView):
    """``GET`` one restaurant: the workspace header and Overview tab."""

    permission_classes = [IsAuthenticated]

    def get(self, request, restaurant_id):
        restaurant = (
            restaurant_reads.directory_queryset()
            .filter(id=restaurant_id)
            .first()
        )
        if restaurant is None:
            return _not_found()

        return Response(
            {'status': 200, 'data': restaurant_reads.serialize_detail(restaurant)},
            status=200,
        )


def _serialize(restaurant):
    """The restaurant's lifecycle position, plus where it can go next."""
    return {
        'id': str(restaurant.id),
        'name': restaurant.name,
        'status': restaurant.status,
        'allowed_transitions': lifecycle.allowed_targets(restaurant.status),
    }


class AdminRestaurantTransitionView(AdminAPIView):
    """``POST`` a lifecycle transition. Body: ``to_state``, ``reason``."""

    permission_classes = [IsAuthenticated, IsRecentlyElevated]

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused transition before DRF turns it into a 403.

        DRF rejects a failed permission inside ``check_permissions``, before the
        handler runs — so a stale-elevation attempt would otherwise 403 with no
        entry, breaking ``AdminAPIView``'s "exactly one entry per unsafe request,
        including denials". Only for a request that AUTHENTICATED: an anonymous call
        is a 401 about identity, not a lifecycle decision, and must not manufacture a
        denial row for a caller we cannot name.
        """
        if getattr(request, 'successful_authenticator', None):
            self.audit(
                request,
                ADMIN_RESTAURANT_TRANSITION_DENIED,
                result=RESULT_DENIED,
                resource_type='Restaurant',
                resource_id=str(self.kwargs.get('restaurant_id') or ''),
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    def post(self, request, restaurant_id):
        # Soft-deleted rows are excluded: `deleted` is the technical soft-delete and
        # a deleted restaurant has no lifecycle left to move. 404 rather than 400 —
        # the admin plane has no reason to distinguish "gone" from "never existed".
        restaurant = Restaurant.objects.filter(
            id=restaurant_id, deleted=False,
        ).first()
        if restaurant is None:
            # No audit entry: nothing was denied and no resource was touched. The
            # request never reached a decision about a real tenant.
            return _not_found()

        try:
            updated = lifecycle.transition_restaurant(
                restaurant=restaurant,
                to_state=request.data.get('to_state'),
                reason=request.data.get('reason'),
                request=request,
            )
        except lifecycle.LifecycleTransitionError as exc:
            # The service already wrote the `transition_denied` entry — it owns that
            # record because it is the only layer that knows the real from-state.
            return Response(
                {'status': 400, 'message': 'This transition was refused.',
                 'errors': exc.errors, 'code': exc.code},
                status=400,
            )

        return Response(
            {'status': 200, 'message': 'Lifecycle state updated.',
             'data': _serialize(updated)},
            status=200,
        )
