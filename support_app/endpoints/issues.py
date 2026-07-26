"""
Restaurant-facing support issue endpoints (Secretary pattern).

Support is an UNGATED module, so authorization is ANY-active-employee (not
owner/manager-only) and bound SERVER-SIDE via `get_employed_restaurant_ids`
— a client-sent restaurant/issue id can only narrow within the caller's own
restaurants, never widen. A dinify admin is unrestricted; issue CREATE stays
admin-excluded (an admin doesn't raise issues on a restaurant's behalf).
"""
from rest_framework.views import APIView
from rest_framework.response import Response

from misc_app.controllers.secretary import Secretary
from users_app.controllers.permissions_check import get_employed_restaurant_ids
from support_app.models import SupportIssue
from support_app.serializers import (
    SupportIssueWriteSerializer,
    SupportIssueRestaurantReadSerializer,
)


# Secretary REQUIRED_INFORMATION — full crm dict shape (Secretary.create reads
# info['type'] / info['text_presentation'] via bracket subscript).
REQUIRED_INFORMATION = [
    {'key': 'category', 'label': 'CATEGORY', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa: E501
    {'key': 'impact', 'label': 'IMPACT', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa: E501
    {'key': 'title', 'label': 'TITLE', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa: E501
    {'key': 'description', 'label': 'DESCRIPTION', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa: E501
]

# Restaurant users may ONLY set these on create. status/assigned_to/
# internal_notes/resolution_summary are never accepted here.
ALLOWED_CREATE_FIELDS = (
    'category', 'impact', 'title', 'description',
    'contact_phone', 'contact_email', 'preferred_contact_method',
    'page_url', 'user_agent',
)


class RestaurantIssuesEndpoint(APIView):
    def post(self, request):
        restaurant_id = request.data.get('restaurant')
        allowed = get_employed_restaurant_ids(request.user)
        # ANY active employee of the restaurant may raise an issue (support is
        # ungated); nobody else may. `not allowed` is the deny-all case.
        if not allowed or restaurant_id is None or str(restaurant_id) not in allowed:
            return Response(
                {
                    'status': 403,
                    'message': 'You do not have permission to raise a support '
                               'issue for this restaurant.',
                },
                status=403,
            )

        # Build a fresh whitelist dict — never mutate/strip request.data, so a
        # forgotten key cannot become an escalation hole.
        data = {
            key: request.data.get(key)
            for key in ALLOWED_CREATE_FIELDS
            if key in request.data
        }
        # Kept in data so any restaurant-required check still passes; it is
        # read_only on the serializer, so the ACTUAL write comes from the trusted
        # server_values channel below (never the client payload).
        data['restaurant'] = str(restaurant_id)

        secretary_args = {
            'serializer': SupportIssueWriteSerializer,
            'data': data,
            'required_information': REQUIRED_INFORMATION,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'user': request.user,
            'success_message': 'Your support issue has been submitted successfully.',  # noqa: E501
            'error_message': 'Sorry, an error occurred while submitting your support issue. Please try again later.',  # noqa: E501
            # restaurant (read_only) + created_by (the actor) are server-derived.
            'server_values': {'restaurant_id': str(restaurant_id)},
        }
        response = Secretary(secretary_args).create()
        return Response(response, status=response['status'])

    def get(self, request):
        allowed = get_employed_restaurant_ids(request.user)
        orm_filter = {'deleted': False}
        # Bind to the caller's own (employed) restaurants, unconditionally: the
        # resolver always returns a set (an empty one denies), so no principal
        # skips this filter. A client ?restaurant= may only narrow within that
        # set, never widen it.
        client_restaurant = request.GET.get('restaurant')
        if client_restaurant is not None and str(client_restaurant) in allowed:
            orm_filter['restaurant__in'] = [str(client_restaurant)]
        else:
            orm_filter['restaurant__in'] = list(allowed)

        secretary_args = {
            'request': request,
            'serializer': SupportIssueRestaurantReadSerializer,
            'filter': orm_filter,
            'paginate': True,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The support issues have been retrieved successfully.',  # noqa: E501
            'error_message': 'Sorry, an error occurred while retrieving the support issues. Please try again later.',  # noqa: E501
        }
        response = Secretary(secretary_args).read()
        return Response(response, status=response['status'])


class RestaurantIssueDetailEndpoint(APIView):
    def get(self, request, issue_id):
        try:
            issue = SupportIssue.objects.get(id=issue_id, deleted=False)
        except SupportIssue.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Support issue not found.'},
                status=404,
            )
        # 404 (not 403) on cross-tenant access so existence is not confirmed.
        # Any active employee of the issue's restaurant may read it (support is
        # ungated); nobody else may.
        allowed = get_employed_restaurant_ids(request.user)
        if str(issue.restaurant_id) not in allowed:
            return Response(
                {'status': 404, 'message': 'Support issue not found.'},
                status=404,
            )
        return Response(
            {
                'status': 200,
                'message': 'The support issue has been retrieved successfully.',
                'data': SupportIssueRestaurantReadSerializer(issue).data,
            },
            status=200,
        )
