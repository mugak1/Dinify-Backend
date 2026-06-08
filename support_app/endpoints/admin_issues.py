"""
Dinify-admin support issue endpoints (Secretary pattern).

Gated to Dinify staff only — specifically `dinify_admin` via
`is_dinify_superuser`. Restaurant users are rejected with 403.
"""
from rest_framework.views import APIView
from rest_framework.response import Response

from misc_app.controllers.secretary import Secretary
from misc_app.controllers.define_filter_params import define_filter_params
from users_app.controllers.permissions_check import is_dinify_superuser
from support_app.serializers import (
    SupportIssueWriteSerializer,
    SupportIssueAdminReadSerializer,
)


# EDIT_INFORMATION — fields admins may change. Anything not listed is silently
# dropped by Secretary.update. resolved_at/closed_at are deliberately NOT here:
# the model stamps them when status transitions to resolved/closed.
EDIT_INFORMATION = [
    {'key': 'status', 'label': 'STATUS', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa: E501
    {'key': 'assigned_to', 'label': 'ASSIGNED TO', 'type': 'char', 'min_length': 1, 'text_presentation': None},  # noqa: E501
    {'key': 'internal_notes', 'label': 'INTERNAL NOTES', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa: E501
    {'key': 'resolution_summary', 'label': 'RESOLUTION SUMMARY', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa: E501
    {'key': 'category', 'label': 'CATEGORY', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa: E501
    {'key': 'impact', 'label': 'IMPACT', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa: E501
    {'key': 'title', 'label': 'TITLE', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa: E501
    {'key': 'description', 'label': 'DESCRIPTION', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa: E501
]

# Only these GET params are forwarded to define_filter_params — pre-filtering
# avoids its KeyError-on-unknown-param (a shared latent 500 vector).
ADMIN_FILTER_KEYS = ('status', 'category', 'impact', 'restaurant')

_FORBIDDEN_MESSAGE = 'You do not have permission to access support administration.'


class AdminIssuesEndpoint(APIView):
    def get(self, request):
        if not is_dinify_superuser(request.user):
            return Response({'status': 403, 'message': _FORBIDDEN_MESSAGE}, status=403)

        clean_params = {
            key: value
            for key, value in request.GET.items()
            if key in ADMIN_FILTER_KEYS
        }
        orm_filter = define_filter_params(
            get_params=clean_params,
            model='supportissues',
        )
        orm_filter['deleted'] = False

        secretary_args = {
            'request': request,
            'serializer': SupportIssueAdminReadSerializer,
            'filter': orm_filter,
            'paginate': True,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The support issues have been retrieved successfully.',  # noqa: E501
            'error_message': 'Sorry, an error occurred while retrieving the support issues. Please try again later.',  # noqa: E501
        }
        response = Secretary(secretary_args).read()
        return Response(response, status=response['status'])

    def put(self, request):
        if not is_dinify_superuser(request.user):
            return Response({'status': 403, 'message': _FORBIDDEN_MESSAGE}, status=403)

        secretary_args = {
            'serializer': SupportIssueWriteSerializer,
            'data': request.data,
            'edit_considerations': EDIT_INFORMATION,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The support issue has been updated successfully.',  # noqa: E501
            'error_message': 'Sorry, an error occurred while updating the support issue. Please try again later.',  # noqa: E501
        }
        response = Secretary(secretary_args).update()
        return Response(response, status=response['status'])
