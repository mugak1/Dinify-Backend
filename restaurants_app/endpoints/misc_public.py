"""
endpoints for restaurant configurations
"""
import ast
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import (
    SerializerMiscPublicRestaurant,
    SerializerMiscPublicTable,
)


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

        # Only these two public listings exist. Any other config_detail —
        # including the removed 'details' value (whose handler never existed and
        # had no frontend caller) — returns a clean 404 rather than an
        # AttributeError 500 or a fall-through into Secretary with a None
        # serializer.
        if config_detail not in ('restaurants', 'tables'):
            return Response(
                {'status': 404, 'message': 'Not found'},
                status=404,
            )

        orm_filter = define_filter_params(request.GET, config_detail)

        # update the filter based on the config_detail
        if config_detail == 'restaurants':
            orm_filter['status'] = 'active'
        elif config_detail == 'tables':
            # Require an explicit restaurant scope. The endpoint is AllowAny, so
            # without this an anonymous caller with no filter would receive a
            # paginated cross-tenant dump of every restaurant's tables. Set the
            # scope explicitly rather than trusting define_filter_params, which
            # silently drops single-character values (its `len(value) > 1`
            # guard) — this guarantees the query is always tenant-scoped.
            restaurant_id = request.GET.get('restaurant')
            if not restaurant_id:
                return Response(
                    {'status': 400, 'message': 'restaurant is required'},
                    status=400,
                )
            orm_filter['restaurant'] = restaurant_id

        serializers = {
            'restaurants': SerializerMiscPublicRestaurant,
            'tables': SerializerMiscPublicTable,
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'tables': 'Successfully retrieved the tables',
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
            'tables': 'Error while retrieving the tables',
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
