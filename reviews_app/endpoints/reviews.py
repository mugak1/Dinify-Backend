"""
Reviews endpoints.

Two endpoint classes on two paths so the public/authenticated boundary is
explicit:
- ``ReviewSubmissionEndpoint``  -> diner submission, AllowAny.
- ``RestaurantReviewsEndpoint`` -> reviews-module retrieval, JWT (inherits the
  project IsAuthenticated default). Mirrors ``RestaurantIssuesEndpoint.get``.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from misc_app.controllers.secretary import Secretary
from users_app.controllers.permissions_check import get_module_restaurant_ids
from dinify_backend.configss.string_definitions import MODULE_REVIEWS
from reviews_app.models import PUBLIC_RATING_THRESHOLD
from reviews_app.serializers import ReviewRestaurantReadSerializer
from reviews_app.controllers.submit_review import submit_review, RATING_FIELDS
from reviews_app.controllers.resolve_review import resolve_review
from restaurants_app.controllers.diner_capability import (
    require_table_session, DinerCapabilityError,
)


class ReviewSubmissionEndpoint(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # A review is authorised by a diner table SESSION bound to the order's
        # table — not by order-UUID knowledge alone. The completed-service
        # (SALE_STATUSES) gate and one-per-order uniqueness are preserved in
        # submit_review.
        try:
            table = require_table_session(request)
        except DinerCapabilityError as exc:
            return Response(
                {'status': exc.status, 'message': exc.message}, status=exc.status,
            )
        rating_fields = {
            field: request.data.get(field) for field in RATING_FIELDS
        }
        response = submit_review(
            order_id=request.data.get('order'),
            rating_fields=rating_fields,
            comment=request.data.get('comment'),
            tags=request.data.get('tags'),
            session_restaurant_id=table.restaurant_id,
            session_table_id=table.id,
        )
        return Response(response, status=response['status'])


class RestaurantReviewsEndpoint(APIView):
    def get(self, request):
        allowed = get_module_restaurant_ids(request.user, MODULE_REVIEWS)
        # Review has NO soft-delete column, so (unlike support issues) the filter
        # starts empty — never add {'deleted': False} or it would FieldError.
        orm_filter = {}
        if allowed is not None:
            # Non-admin: bind to the caller's own restaurants. A client
            # ?restaurant= may only narrow within that set, never widen it.
            client_restaurant = request.GET.get('restaurant')
            if client_restaurant is not None and str(client_restaurant) in allowed:
                orm_filter['restaurant__in'] = [str(client_restaurant)]
            else:
                orm_filter['restaurant__in'] = list(allowed)

        # Optional filters — all defensive so a bad value can never 500.
        rating = request.GET.get('rating')
        if rating is not None and rating.isdigit():
            orm_filter['overall_rating'] = int(rating)

        resolution_status = request.GET.get('resolution_status')
        if resolution_status in ('open', 'resolved'):
            orm_filter['resolution_status'] = resolution_status

        if request.GET.get('critical') == 'true':
            # is_critical is a Python property, so filter the underlying column.
            # This ANDs cleanly with the exact ``rating`` filter above (different
            # ORM keys on the same column).
            orm_filter['overall_rating__lt'] = PUBLIC_RATING_THRESHOLD

        secretary_args = {
            'request': request,
            'serializer': ReviewRestaurantReadSerializer,
            'filter': orm_filter,
            'paginate': True,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The reviews have been retrieved successfully.',
            'error_message': 'Sorry, an error occurred while retrieving the reviews. Please try again later.',  # noqa: E501
        }
        response = Secretary(secretary_args).read()
        return Response(response, status=response['status'])


class ReviewResolutionEndpoint(APIView):
    """
    Owner/manager mark-handled write: toggle resolution_status between 'open' and
    'resolved'. A controlled state transition — a direct update, not a Secretary
    edit. JWT (inherits the project IsAuthenticated default).
    """

    def patch(self, request, review_id):
        response = resolve_review(
            request.user,
            review_id,
            request.data.get('resolution_status'),
            note=request.data.get('resolution_note'),
        )
        return Response(response, status=response['status'])
