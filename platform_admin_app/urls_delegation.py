"""
Delegated-session routes on the CUSTOMER plane (mounted at ``api/v1/delegation/``).

Separate from ``platform_admin_app/urls.py``, which is the admin control plane —
these three are the only delegation routes the customer API serves, and they are
explicit paths, never a ``<str:action>/`` catch-all.

The route STRINGS here are matched by ``configs/delegation_scopes.ALLOWED_ROUTES``
(``session/`` and ``end/`` are on the allowlist; ``exchange/`` deliberately is not —
it is reached with a one-time code, not with a session). A test asserts every
allowlist entry resolves to a real route, so a rename cannot silently strand one.
"""
from django.urls import path

from platform_admin_app.endpoints.delegated_exchange import (
    DelegationEndView,
    DelegationExchangeView,
    DelegationSessionView,
)

urlpatterns = [
    path('exchange/', DelegationExchangeView.as_view(), name='delegation-exchange'),
    path('session/', DelegationSessionView.as_view(), name='delegation-session'),
    path('end/', DelegationEndView.as_view(), name='delegation-end'),
]
