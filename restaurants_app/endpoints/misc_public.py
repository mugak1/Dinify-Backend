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
    SerializerPublicGetRestaurant,
    SerializerPublicGetTable,
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

        if config_detail == 'details':
            return self.get_detail(request)

        orm_filter = define_filter_params(request.GET, config_detail)

        # update the filter based on the config_detail
        if config_detail in ['restaurants']:
            orm_filter['status'] = 'active'

        serializers = {
            'restaurants': SerializerPublicGetRestaurant,
            'tables': SerializerPublicGetTable,
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'tables': 'Successfully retrieved the tables',
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
            'tables': 'Error while retrieving the tables',
        }

        serializer = serializers.get(config_detail)
        # TODO determine the correct serializer to use depending on the role

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
