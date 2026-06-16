from django.urls import path

from reviews_app.endpoints.reviews import (
    ReviewSubmissionEndpoint,
    RestaurantReviewsEndpoint,
)


urlpatterns = [
    # Diner-facing submission (AllowAny).
    path('submit/', ReviewSubmissionEndpoint.as_view()),
    # Owner/manager retrieval (JWT).
    path('', RestaurantReviewsEndpoint.as_view()),
]
