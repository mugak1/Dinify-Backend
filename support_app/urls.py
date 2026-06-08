from django.urls import path

from support_app.endpoints.issues import (
    RestaurantIssuesEndpoint,
    RestaurantIssueDetailEndpoint,
)
from support_app.endpoints.admin_issues import AdminIssuesEndpoint


urlpatterns = [
    # Dinify-admin routes
    path('admin/issues/', AdminIssuesEndpoint.as_view()),
    # Restaurant-facing routes
    path('issues/', RestaurantIssuesEndpoint.as_view()),
    path('issues/<uuid:issue_id>/', RestaurantIssueDetailEndpoint.as_view()),
]
