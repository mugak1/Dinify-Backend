"""
Root urlconf for the admin control plane (``ROOT_URLCONF`` under
``dinify_backend.settings_admin``).

This is the isolation boundary: it contains ONLY the admin include — no customer
routes. The customer WSGI process (``dinify_backend.urls``) has no admin routes and
this admin process has no customer routes. Apache mounts the app at ``/api`` and
strips it, so routes register WITHOUT a leading ``api/`` — ``admin/v1/…`` here is
reached as the browser path ``/api/admin/v1/…``.
"""
from django.urls import include, path

urlpatterns = [
    path('admin/v1/', include('platform_admin_app.urls')),
]
