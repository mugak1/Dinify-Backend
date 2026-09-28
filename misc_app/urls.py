from django.urls import path
from misc_app.endpoints.health import HealthCheckView
from misc_app.endpoints.readiness import readiness

urlpatterns = [
    path('', HealthCheckView.as_view()),
    # D15 R1 — bounded readiness: 200 ready / 503 not_ready. Additive; the route above is
    # unchanged. See misc_app/endpoints/readiness.py.
    path('ready/', readiness, name='readiness'),
]
