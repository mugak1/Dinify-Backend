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
from platform_admin_app.endpoints.restaurants import (
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

    # Restaurant lifecycle. The ONLY route that writes Restaurant.status;
    # elevation-gated, because it can stop a tenant trading.
    path(
        'restaurants/<uuid:restaurant_id>/transition/',
        AdminRestaurantTransitionView.as_view(),
        name='admin-restaurant-transition',
    ),
]
