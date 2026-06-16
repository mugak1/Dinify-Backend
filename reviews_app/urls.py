from django.urls import path

from reviews_app.endpoints.reviews import (
    ReviewSubmissionEndpoint,
    RestaurantReviewsEndpoint,
    ReviewResolutionEndpoint,
)
from reviews_app.endpoints.analytics import (
    ReviewSummaryEndpoint,
    ReviewAnalyticsEndpoint,
)


urlpatterns = [
    # Diner-facing submission (AllowAny).
    path('submit/', ReviewSubmissionEndpoint.as_view()),
    # Owner/manager analytics (JWT, per-restaurant). Registered before the root.
    path('summary/', ReviewSummaryEndpoint.as_view()),
    path('analytics/', ReviewAnalyticsEndpoint.as_view()),
    # Owner/manager mark-handled write (JWT). The <int:> id is distinct from the
    # static paths above and the empty root, so nothing is shadowed.
    path('<int:review_id>/resolution/', ReviewResolutionEndpoint.as_view()),
    # Owner/manager retrieval (JWT).
    path('', RestaurantReviewsEndpoint.as_view()),
]
