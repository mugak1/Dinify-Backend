"""
endpoints for restaurant configurations
"""
from calendar import c
from rest_framework.response import Response
from rest_framework.views import APIView
from restaurants_app.controllers.create_restaurant import create_restaurant
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import (
    SerializerPutRestaurant, SerializerPublicGetRestaurant, SerializerEmployeeGetRestaurant, SerializerPutRestaurantEmployee,
)
from dinify_backend.configs import EDIT_INFORMATION, REQUIRED_INFORMATION


class RestaurantSetupEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    def post(self, request, config_detail):
        """
        handle the POST method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        if config_detail == 'restaurants':
            # TODO if the user is not a Dinify admin,.
            # then set the owner value from the auth details
            data = request.data.copy()
            data['owner'] = auth['user_id']
            response = create_restaurant(
                data,
                auth
            )

        if config_detail == 'employees':
            secretary_args = {
                'serializer': SerializerPutRestaurantEmployee,
                'data': request.data,
                'required_information': REQUIRED_INFORMATION.get('restaurant_employee'),
                'user_id': auth['id'],
                'username': auth['username'],
                'success_message': 'The employee has been added successfully.',
                'error_message': 'An error occurred while adding the employee.'
            }
            response = Secretary(secretary_args).create()

        return Response(
            response,
            status=response['status']
        )

    def get(self, request, config_detail):
        """
        handle the GET method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        orm_filter = define_filter_params(request.GET, config_detail)

        if config_detail == 'restaurants':
            # TODO determine the correct serializer to use
            secretary_args = {
                'request': request,
                'serializer': SerializerPublicGetRestaurant,
                'filter': orm_filter,
                'paginate': True,
                'user_id': auth['id'],
                'username': auth['username'],
                'success_message': "Successfully retrieved the restaurants",
                'error_message': "Error while retrieving restaurants"
            }

            response = Secretary(secretary_args).read()

        return Response(
            response,
            status=response['status']
        )

    def put(self, request, config_detail):
        """
        handle the PUT method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        if config_detail == 'restaurants':
            # TODO check if the user has permissions to edit the details
            secretary_args = {
                'serializer': SerializerPutRestaurant,
                'data': request.data,
                'edit_considerations': EDIT_INFORMATION.get('restaurant_registration'),
                'user_id': auth['id'],
                'username': auth['username'],
                'success_message': 'The details of the restaurant have been updated successfully.',
                'error_message': 'An error occurred while updating the details of the restaurant.'
            }
            response = Secretary(secretary_args).update()

        return Response(
            response,
            status=response['status']
        )
