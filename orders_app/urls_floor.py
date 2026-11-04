"""The floor view, mounted at ``api/v1/floor/`` (Step 2, PR BE-D).

Floor contract v1 (``docs/FLOOR_CONTRACT.md``). Four routes, every one gated on the
``tables`` module and none on the delegated ``ALLOWED_ROUTES``. Explicit routes,
never a ``<str:action>/`` catch-all: confirming and declining are opposite decisions,
and which one a request made should be readable from the path. ``<str:>`` rather than
``<uuid:>`` so a malformed id gets the same JSON 404 as an unknown one.
"""
from django.urls import path

from orders_app.endpoints_floor import (
    FloorTableView,
    FloorView,
    RoundConfirmView,
    RoundDeclineView,
)

urlpatterns = [
    path('', FloorView.as_view()),
    path('tables/<str:table_id>/', FloorTableView.as_view()),
    path('rounds/<str:order_id>/confirm/', RoundConfirmView.as_view()),
    path('rounds/<str:order_id>/decline/', RoundDeclineView.as_view()),
]
