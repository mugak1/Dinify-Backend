"""
endpoints for restaurant configurations
"""
import ast
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import SerializerMiscPublicRestaurant


class MiscPublicEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    permission_classes = (AllowAny, )

    def get(self, request, config_detail):
        """
        handle the GET method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        # auth = decode_jwt_token(request)

        # Only the anonymous restaurants directory remains public. The 'tables'
        # listing was RETIRED for tenant isolation (PR2): it handed out table
        # UUIDs in bulk, and a table UUID is itself the order-journey table-scan
        # token — so the public listing let an anonymous caller enumerate every
        # restaurant's tables and chain table-scan -> active order UUID ->
        # order-details / review submission without ever holding a QR. It had no
        # in-repo caller. Any other config_detail — including the retired
        # 'tables' and the long-removed 'details' — returns a clean 404 rather
        # than an AttributeError 500 or a fall-through into Secretary with a
        # None serializer.
        if config_detail != 'restaurants':
            return Response(
                {'status': 404, 'message': 'Not found'},
                status=404,
            )

        orm_filter = define_filter_params(request.GET, config_detail)

        # The sole public listing is the active-restaurant directory.
        orm_filter['status'] = 'active'

        # This ANONYMOUS listing ALWAYS excludes soft-deleted rows. Unlike the
        # authenticated catch-all (restaurant_setup.py), there is NO ?deleted
        # opt-in here: a caller-supplied `deleted` param is ignored, never
        # honoured, so an anonymous client can never surface soft-deleted records.
        # The endpoint owns this filter — define_filter_params does not map
        # `deleted` (a pure param-mapper pinned by tests_define_filter_params) —
        # so setting it unconditionally is the sole and complete guard.
        orm_filter['deleted'] = False

        serializers = {
            'restaurants': SerializerMiscPublicRestaurant,
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
        }

        # This endpoint is AllowAny, so every caller is anonymous; the narrowed
        # public serializers above are the single safe serializer for all callers.
        serializer = serializers.get(config_detail)

        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        secretary_args = {
            'request': request,
            'serializer': serializer,
            'filter': orm_filter,
            'paginate': True,
            'user_id': request.user.id,
            'username': request.user.username,
            'success_message': success_message,
            'error_message': error_message
        }

        response = Secretary(secretary_args).read()

        return Response(
            response,
            status=response['status']
        )
