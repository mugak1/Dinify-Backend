from django.urls import path
from reports_app.endpoints.restaurant_reports import RestaurantReportsEndpoint


# Phase 1: cross-tenant platform reporting is admin-plane functionality, built
# natively on /api/admin/v1. The `dinify/<report_name>/` route served every
# restaurant's revenue, owner PII and the whole transaction ledger to anyone
# holding a `dinify_admin` string in User.roles — it is REMOVED, not ported.
urlpatterns = [
    path('restaurant/<str:report_name>/', RestaurantReportsEndpoint.as_view()),
]
