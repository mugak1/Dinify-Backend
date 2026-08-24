"""
Admin control-plane URLs (mounted at ``admin/v1/`` by ``dinify_backend.urls_admin``).

Because Apache mounts the admin Django app at ``/api`` and STRIPS that prefix, the
browser path ``/api/admin/v1/health/`` reaches Django as ``/admin/v1/health/``. Every
route is an explicit, deny-by-default path — never a ``<str:action>/`` catch-all.
"""
from django.urls import path

from platform_admin_app.endpoints.auth import (
    AdminElevateView,
    AdminLoginView,
    AdminLogoutView,
    AdminSessionView,
    AdminVerifyView,
)
from platform_admin_app.endpoints.delegation import (
    AdminDelegationRevokeView,
    AdminDelegationsView,
)
from platform_admin_app.endpoints.commercial import (
    AdminRestaurantPaymentCollectionModeView,
    AdminRestaurantPaymentTimingView,
)
from platform_admin_app.endpoints.restaurants import (
    AdminRestaurantDetailView,
    AdminRestaurantListView,
    AdminRestaurantTransitionView,
)
from platform_admin_app.views import AdminHealthView

urlpatterns = [
    path('health/', AdminHealthView.as_view(), name='admin-health'),

    # Two-step authentication: password -> challenge -> second factor -> session.
    path('auth/login/', AdminLoginView.as_view(), name='admin-auth-login'),
    path('auth/verify/', AdminVerifyView.as_view(), name='admin-auth-verify'),
    path('auth/logout/', AdminLogoutView.as_view(), name='admin-auth-logout'),
    path('auth/session/', AdminSessionView.as_view(), name='admin-auth-session'),
    path('auth/elevate/', AdminElevateView.as_view(), name='admin-auth-elevate'),

    # Delegated tenant access: mint (elevation-gated) / list / revoke. The exchange
    # path that redeems a code for a delegated session is PR-4b.
    path(
        'delegations/',
        AdminDelegationsView.as_view(),
        name='admin-delegations',
    ),
    path(
        'delegations/<uuid:grant_id>/revoke/',
        AdminDelegationRevokeView.as_view(),
        name='admin-delegation-revoke',
    ),

    # Restaurant directory + detail (Phase 1, Step 1). READ-ONLY and NOT
    # elevation-gated: reading the portfolio is ordinary authenticated work, not a
    # step-up operation. Listed before the transition route for readability only —
    # the paths are distinct, so ordering carries no routing meaning here.
    path(
        'restaurants/',
        AdminRestaurantListView.as_view(),
        name='admin-restaurant-list',
    ),
    path(
        'restaurants/<uuid:restaurant_id>/',
        AdminRestaurantDetailView.as_view(),
        name='admin-restaurant-detail',
    ),

    # Restaurant lifecycle. The ONLY route that writes Restaurant.status;
    # elevation-gated, because it can stop a tenant trading.
    path(
        'restaurants/<uuid:restaurant_id>/transition/',
        AdminRestaurantTransitionView.as_view(),
        name='admin-restaurant-transition',
    ),

    # Commercial / service configuration writes (Phase 1, Step 3D.2a). Both
    # elevation-gated and reason-required, and both delegate the mutation to
    # `commercial_app.service_configuration`.
    #
    # TWO EXPLICIT PATHS, never one route with a field parameter: these are two
    # different commercial decisions, and the route should tell a reviewer which one
    # a request made without them having to read a body.
    path(
        'restaurants/<uuid:restaurant_id>/commercial/payment-timing/',
        AdminRestaurantPaymentTimingView.as_view(),
        name='admin-restaurant-payment-timing',
    ),
    path(
        'restaurants/<uuid:restaurant_id>/commercial/payment-collection-mode/',
        AdminRestaurantPaymentCollectionModeView.as_view(),
        name='admin-restaurant-payment-collection-mode',
    ),
]
