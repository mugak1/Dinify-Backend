"""
Restaurant lifecycle endpoint — the admin-plane door onto the transition service.

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

from platform_admin_app.audit_actions import ADMIN_RESTAURANT_TRANSITION_DENIED
from platform_admin_app.models import RESULT_DENIED
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant


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
            return Response(
                {'status': 404, 'message': 'Restaurant not found.'}, status=404,
            )

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
