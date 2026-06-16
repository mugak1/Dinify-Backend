"""
Reviews analytics endpoints (read-only, JWT).

Two GET endpoints, each per-restaurant: a lean summary for the dashboard card and
a windowed analytics report for the reviews Overview. Both inherit the project
``IsAuthenticated`` default (like ``RestaurantReviewsEndpoint``) and share one
single-restaurant, fail-closed guard.
"""
import uuid

from rest_framework.views import APIView
from rest_framework.response import Response

from users_app.controllers.permissions_check import can_read_restaurant
from reviews_app.controllers.review_analytics import (
    review_analytics,
    review_summary,
)


def _resolve_restaurant(request):
    """
    Resolve and authorize the single ``?restaurant=`` these analytics require.

    Returns ``(restaurant_id, None)`` on success, or ``(None, error_dict)``. The
    checks run require -> well-formed -> authorized, and crucially the UUID-format
    check precedes ``can_read_restaurant``: a dinify admin is authorized for any
    id, so without it a malformed ``?restaurant=`` would reach the ORM and 500 on
    Postgres (the Restaurant PK is a UUID). Failing closed with a uniform 400
    mirrors submit_review's malformed-id handling.
    """
    restaurant_id = request.GET.get('restaurant')
    if not restaurant_id:
        return None, {'status': 400, 'message': 'restaurant is required'}
    try:
        uuid.UUID(str(restaurant_id))
    except (ValueError, TypeError):
        return None, {'status': 400, 'message': 'restaurant is invalid'}
    if not can_read_restaurant(request.user, restaurant_id):
        return None, {
            'status': 403,
            'message': 'You do not have permission to view analytics for this restaurant.',
        }
    return restaurant_id, None


class ReviewSummaryEndpoint(APIView):
    def get(self, request):
        restaurant_id, error = _resolve_restaurant(request)
        if error is not None:
            return Response(error, status=error['status'])
        response = review_summary(restaurant_id)
        return Response(response, status=response['status'])


class ReviewAnalyticsEndpoint(APIView):
    def get(self, request):
        restaurant_id, error = _resolve_restaurant(request)
        if error is not None:
            return Response(error, status=error['status'])
        response = review_analytics(
            restaurant_id,
            request.GET.get('from'),
            request.GET.get('to'),
            request.GET.get('category', 'weekly'),
        )
        return Response(response, status=response['status'])
