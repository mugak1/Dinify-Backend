from django.urls import path

from reviews_app.endpoints.reviews import (
    ReviewSubmissionEndpoint,
    RestaurantReviewsEndpoint,
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
    # Owner/manager retrieval (JWT).
    path('', RestaurantReviewsEndpoint.as_view()),
]
