from django.urls import path

from support_app.endpoints.issues import (
    RestaurantIssuesEndpoint,
    RestaurantIssueDetailEndpoint,
)


# Phase 1: cross-tenant support triage is admin-plane functionality, built
# natively on /api/admin/v1. The `admin/issues/` route served a Dinify-admin
# endpoint whose only gate was a `dinify_admin` string in the caller's
# User.roles, and whose write queryset was an unrestricted
# `SupportIssue.objects.all()` — it is REMOVED, not ported. A delegated
# administrator still reaches ONE restaurant's issues through the routes below,
# which is what delegation is for.
urlpatterns = [
    path('issues/', RestaurantIssuesEndpoint.as_view()),
    path('issues/<uuid:issue_id>/', RestaurantIssueDetailEndpoint.as_view()),
]
