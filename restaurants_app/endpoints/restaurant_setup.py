"""
endpoints for restaurant configurations
"""
from calendar import c
from logging import config
from rest_framework.response import Response
from rest_framework.views import APIView
from restaurants_app.controllers.create_restaurant import create_restaurant
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import (
    SerializerPutRestaurant, SerializerPublicGetRestaurant,
    SerializerPutRestaurantEmployee, SerializerGetRestaurantEmployee,

    SerializerPutMenuSection, SerializerPublicGetMenuSection,
    SerializerPutMenuItem, SerializerPublicGetMenuItem,
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
            return Response(
                response,
                status=response['status']
            )

        serializers = {
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'menuitems': SerializerPutMenuItem,
        }

        required_information = {
            'employees': REQUIRED_INFORMATION.get('restaurant_employee'),
            'menusections': REQUIRED_INFORMATION.get('menu_section'),
            'menuitems': REQUIRED_INFORMATION.get('menu_item'),
        }

        success_messages = {
            'employees': 'The employee has been added successfully.',
            'menusections': 'The menu section has been added successfully.',
            'menuitems': 'The menu item has been added successfully.',
        }

        error_messages = {
            'employees': 'An error occurred while adding the employee.',
            'menusections': 'An error occurred while adding the menu section.',
            'menuitems': 'An error occurred while adding the menu item.',
        }

        post_data = request.data

        try:
            post_data = post_data.dict()
        except Exception as error:
            print(f"Error: {error}")

        serializer = serializers.get(config_detail)
        required_information = required_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        secretary_args = {
            'serializer': serializer,
            'data': post_data,
            'required_information': required_information,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message
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

        serializers = {
            'restaurants': SerializerPublicGetRestaurant,
            'employees': SerializerGetRestaurantEmployee,
            'menusections': SerializerPublicGetMenuSection,
            'menuitems': SerializerPublicGetMenuItem,
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'employees': 'Successfully retrieved the employees',
            'menusections': 'Successfully retrieved the menu sections',
            'menuitems': 'Successfully retrieved the menu items'
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
            'employees': 'Error while retrieving employees',
            'menusections': 'Error while retrieving menu sections',
            'menuitems': 'Error while retrieving menu items'
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
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message
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

        serializers = {
            'restaurants': SerializerPutRestaurant,
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'menuitems': SerializerPutMenuItem,
        }

        edit_information = {
            'restaurants': EDIT_INFORMATION.get('restaurant'),
            'employees': EDIT_INFORMATION.get('restaurant_employee'),
            'menusections': EDIT_INFORMATION.get('menu_section'),
            'menuitems': EDIT_INFORMATION.get('menu_item'),                                 
        }

        success_messages = {
            'restaurants': 'The details of the restaurant have been updated successfully.',
            'employees': 'The details of the employee have been updated successfully',
            'menusections': 'The details of the menu section have been updated successfully.',
            'menuitems': 'The details of the menu item have been updated successfully.',
        }

        error_messages = {
            'restaurants': 'An error occurred while updating the details of the restaurant.',
            'employees': 'An error occurred while updating the details of the employee.',
            'menusections': 'An error occurred while updating the details of the menu section.',
            'menuitems': 'An error occurred while updating the details of the menu item.',
        }

        # TODO check if the user has permissions to edit the details

        serializer = serializers.get(config_detail)
        edit_information = edit_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        secretary_args = {
            'serializer': serializer,
            'data': request.data,
            'edit_considerations': edit_information,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message
        }
        response = Secretary(secretary_args).update()

        return Response(
            response,
            status=response['status']
        )

    def delete(self, request, config_detail):
        """
        handle the DELETE method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        serializer = {
            'restaurant': SerializerPutRestaurant,
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'menuitems': SerializerPutMenuItem,
        }

        secretary_args = {
            'serializer': serializer[config_detail],
            'data': request.data,
            'user_id': auth['id'],
            'username': auth['username'],
        }
        response = Secretary(secretary_args).delete()

        return Response(
            response,
            status=response['status']
        )
