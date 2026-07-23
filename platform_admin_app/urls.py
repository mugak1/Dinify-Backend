"""
Admin control-plane URLs (mounted at ``admin/v1/`` by ``dinify_backend.urls_admin``).

Because Apache mounts the admin Django app at ``/api`` and STRIPS that prefix, the
browser path ``/api/admin/v1/health/`` reaches Django as ``/admin/v1/health/``. Only
the unauthenticated health path lives here in PR-2a; every future admin route is added
below it as an explicit, deny-by-default path (never a ``<str:action>/`` catch-all).
"""
from django.urls import path

from platform_admin_app.views import AdminHealthView

urlpatterns = [
    path('health/', AdminHealthView.as_view(), name='admin-health'),
]
