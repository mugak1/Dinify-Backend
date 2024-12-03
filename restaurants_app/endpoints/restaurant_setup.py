"""
endpoints for restaurant configurations
"""
import ast
from logging import config
from rest_framework.response import Response
from rest_framework.views import APIView
from restaurants_app.controllers.create_restaurant import (
    create_restaurant,
    admin_register_restaurant
)
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import (
    SerializerPutRestaurant, SerializerPublicGetRestaurant,
    SerializerPutRestaurantEmployee, SerializerGetRestaurantEmployee,

    SerializerPutMenuSection, SerializerPublicGetMenuSection,
    SerializerPutMenuItem, SerializerPublicGetMenuItem,
    SerializerPutTable, SerializerPublicGetTable,

    SerializerPutSectionGroup, SerializerPublicGetSectionGroup,
    SerializerAdminGetOrderReview, SerializerAdminGetOrderItemReview
)
from orders_app.serializers import SerializerListGetOrder
from restaurants_app.models import Restaurant, MenuSection, SectionGroup, MenuItem
from dinify_backend.configss.required_information import (
    REQUIRED_INFORMATION,
    RI_RESTAURANT_EMPLOYEES,
    RI_SECTION_GROUP
)
from dinify_backend.configss.edit_information import EDIT_INFORMATION, EI_SECTION_GROUP
from dinify_backend.configss.messages import (
    OK_GET_RECORD_DETAIL, ERR_GENERAL,
    ERR_UNSPECIFIED_RECORD_DETAILS,
    OK_ADDED_SECTION_GROUP, ERR_ADDED_SECTION_GROUP, OK_RETRIEVED_SECTION_GROUP, ERR_RETRIEVED_SECTION_GROUP, OK_UPDATED_SECTION_GROUP, ERR_UPDATED_SECTION_GROUP  # noqa
)
from restaurants_app.controllers.create_employee import create_employee
from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    OrderStatus_Cancelled,
    OrderStatus_Refunded,
    PaymentStatus_Paid,
    PaymentStatus_Pending
)


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
            post_data = request.data
            try:
                post_data = post_data.dict()
            except Exception as error:
                print(f"Error: {error}")

            data = post_data.copy()
            data['owner'] = auth['user_id']
            response = create_restaurant(
                data,
                # auth,
                {
                    'id': str(request.user.id),
                    'user_id': str(request.user.id),
                    'username': request.user.username,
                    'first_name': request.user.first_name,
                    'email': request.user.email
                }
            )
            return Response(
                response,
                status=response['status']
            )
        if config_detail == 'admin-register-restaurant':
            post_data = request.data
            try:
                post_data = post_data.dict()
            except Exception as error:
                print(f"Error: {error}")

            data = post_data.copy()
            response = admin_register_restaurant(
                data=data,
                auth_info={
                    'id': str(request.user.id),
                    'user_id': str(request.user.id),
                    'username': request.user.username,
                    'first_name': request.user.first_name,
                    'email': request.user.email
                }
            )
            return Response(response, status=response['status'])

        if config_detail == 'create-employee':
            # TODO if the user is not a Dinify admin,.
            # then set the owner value from the auth details
            data = request.data
            try:
                data = data.dict()
            except Exception as error:
                print(f"Error: {error}")

            try:
                response = create_employee(
                    first_name=data.get('first_name'),
                    last_name=data.get('last_name'),
                    email=data.get('email'),
                    phone_number=data.get('phone_number'),
                    restaurant=Restaurant.objects.get(id=data.get('restaurant')),
                    roles=data.get('roles'),
                    creator=request.user,
                    otp=data.get('otp')
                )
            except Exception as error:
                print(f"Error while creating employee: {error}")
                response = {
                    'status': 500,
                    'message': "An error occurred while creating the employee. Please check that you have provided all the details."
                }
            return Response(
                response,
                status=response['status']
            )

        serializers = {
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable
        }

        required_information = {
            'employees': RI_RESTAURANT_EMPLOYEES,
            'menusections': REQUIRED_INFORMATION.get('menu_section'),
            'sectiongroups': RI_SECTION_GROUP,
            'menuitems': REQUIRED_INFORMATION.get('menu_item'),
            'tables': REQUIRED_INFORMATION.get('table')
        }

        success_messages = {
            'employees': 'The employee has been added successfully.',
            'menusections': 'The menu section has been added successfully.',
            'sectiongroups': OK_ADDED_SECTION_GROUP,
            'menuitems': 'The menu item has been added successfully.',
            'tables': 'The table has been added successfully'
        }

        error_messages = {
            'employees': 'An error occurred while adding the employee.',
            'menusections': 'An error occurred while adding the menu section.',
            'sectiongroups': ERR_ADDED_SECTION_GROUP,
            'menuitems': 'An error occurred while adding the menu item.',
            'tables': 'An error occurred while adding the table'
        }

        post_data = request.data

        # attempt to auto approve menu items if a first time approval has already been done
        if config_detail in ['menusections', 'sectiongroups', 'menuitems']:
            restaurant_id = None
            if config_detail == 'menusections':
                restaurant_id = post_data.get('restaurant')
            if config_detail in ['sectiongroups', 'menuitems']:
                restaurant_id = MenuSection.objects.get(
                    id=post_data['section']
                ).restaurant.pk
                restaurant_id = str(restaurant_id)

            if restaurant_id is not None:
                restaurant = Restaurant.objects.get(id=restaurant_id)

                post_data['approved'] = restaurant.first_time_menu_approval
                post_data['enabled'] = restaurant.first_time_menu_approval

        if config_detail == 'tables':
            try:
                post_data['number'] = str(post_data.get('number'))
            except Exception as error:
                print(f"Error converting table number to string: {error}")

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

        # if the config_detail is menusections,
        # check if the groups were posted so as to create them
        if config_detail == 'menusections':
            if response['status'] != 200:
                return Response(
                    response,
                    status=response['status']
                )

            try:
                # check if the groups were posted
                section_groups = post_data.get('groups')
                if section_groups is None:
                    return Response(
                        response,
                        status=response['status']
                    )
                section = MenuSection.objects.get(id=response['data']['id'])
                section_groups = ast.literal_eval(section_groups)
                group_records = []
                for record in section_groups:
                    group_records.append(
                        SectionGroup(
                            name=record,
                            section=section
                        )
                    )
                SectionGroup.objects.bulk_create(group_records)
            except Exception as error:
                print(f"BulkCreateSectionGroupsError: {error}")
                response['message'] = f"{response['message']}. However, an error while defining the section groups." # noqa

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
        # auth = decode_jwt_token(request)

        if config_detail == 'details':
            return self.get_detail(request)

        filter_params = request.GET.copy()
        if config_detail == 'orders':
            if 'status' in request.GET:
                filter_params.pop('status')

        orm_filter = define_filter_params(filter_params, config_detail)

        if config_detail == 'orders':
            if 'status' in request.GET:
                try:
                    orm_filter.pop('order_status')
                except KeyError:
                    pass
                try:
                    orm_filter.pop('payment_status')
                except KeyError:
                    pass

                if request.GET.get('status') == 'active':
                    orm_filter['order_status__in'] = [
                        OrderStatus_Pending,
                        OrderStatus_Preparing,
                        OrderStatus_Served
                    ]
                    orm_filter['payment_status'] = PaymentStatus_Pending

                if request.GET.get('status') == 'closed':
                    orm_filter['order_status'] = OrderStatus_Served
                    orm_filter['payment_status'] = PaymentStatus_Paid
                
                if request.GET.get('status') == 'all':
                    orm_filter['order_status__in'] = [
                        # OrderStatus_Pending,
                        OrderStatus_Preparing,
                        # OrderStatus_Served,
                        OrderStatus_Cancelled,
                        OrderStatus_Refunded
                    ]
                    # orm_filter['payment_status'] = PaymentStatus_Pending

        if config_detail == 'rest=urants':
            orm_filter['status__in'] = [
                'active',
                'pending'
            ]

        serializers = {
            'restaurants': SerializerPublicGetRestaurant,
            'employees': SerializerGetRestaurantEmployee,
            'menusections': SerializerPublicGetMenuSection,
            'sectiongroups': SerializerPublicGetSectionGroup,
            'menuitems': SerializerPublicGetMenuItem,
            'tables': SerializerPublicGetTable,
            'orders': SerializerListGetOrder,
            'orderreviews': SerializerAdminGetOrderReview,
            'orderitemreviews': SerializerAdminGetOrderItemReview
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'employees': 'Successfully retrieved the employees',
            'menusections': 'Successfully retrieved the menu sections',
            'sectiongroups': OK_RETRIEVED_SECTION_GROUP,
            'menuitems': 'Successfully retrieved the menu items',
            'tables': 'Successfully retrieved the tables',
            'orders': 'Successfully retrived the orders',
            'orderreviews': 'Successfully retrieved the order reviews',
            'orderitemreviews': 'Successfully retrieved the order item reviews'
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
            'employees': 'Error while retrieving employees',
            'menusections': 'Error while retrieving menu sections',
            'sectiongroups': ERR_RETRIEVED_SECTION_GROUP,
            'menuitems': 'Error while retrieving menu items',
            'tables': 'Error while retrieving the tables',
            'orders': 'Error while retrieving the orders',
            'orderreviews': 'Error while retrieving the order reviews',
            'orderitemreviews': 'Error while retrieving the order item reviews'
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
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable
        }

        edit_information = {
            'restaurants': EDIT_INFORMATION.get('restaurants'),
            'employees': EDIT_INFORMATION.get('restaurant_employee'),
            'menusections': EDIT_INFORMATION.get('menu_section'),
            'sectiongroups': EI_SECTION_GROUP,
            'menuitems': EDIT_INFORMATION.get('menu_item'),
            'tables': EDIT_INFORMATION.get('table')
        }

        success_messages = {
            'restaurants': 'The details of the restaurant have been updated successfully.',
            'employees': 'The details of the employee have been updated successfully',
            'menusections': 'The details of the menu section have been updated successfully.',
            'sectiongroups': OK_UPDATED_SECTION_GROUP,
            'menuitems': 'The details of the menu item have been updated successfully.',
            'tables': 'The details of the table have been updated successfully.',
        }

        error_messages = {
            'restaurants': 'An error occurred while updating the details of the restaurant.',
            'employees': 'An error occurred while updating the details of the employee.',
            'menusections': 'An error occurred while updating the details of the menu section.',
            'sectiongroups': ERR_UPDATED_SECTION_GROUP,
            'menuitems': 'An error occurred while updating the details of the menu item.',
            'tables': 'An error occurred while updating the details of the table.',
        }

        # TODO check if the user has permissions to edit the details

        serializer = serializers.get(config_detail)
        edit_information = edit_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        put_data = request.data

        # if editing a menu item,
        # convert the options and extras_applicable to a list
        if config_detail == 'menuitems':
            try:
                if type(put_data) is not dict:
                    put_data = put_data.dict()
            except Exception as error:
                print(f"Error parsing data to dict: {error}")

            options = put_data.get('options')
            if options is not None:
                # convert the options to dict
                if type(put_data) is not dict:
                    put_data['options'] = ast.literal_eval(options)

            extras_applicable = put_data.get('extras_applicable')
            if extras_applicable is not None:
                if type(put_data) is not dict:
                    put_data['extras_applicable'] = ast.literal_eval(extras_applicable)

        secretary_args = {
            'serializer': serializer,
            'data': put_data,
            'edit_considerations': edit_information,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message
        }

        print(f'\n\nthe submitted data for {config_detail} is:\n {put_data}\n\n')
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
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable
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

    def get_detail(self, request):
        try:
            serializers = {
                'restaurants': SerializerPutRestaurant,
                'employees': SerializerGetRestaurantEmployee,
                'menusections': SerializerPutMenuSection,
                'menuitems': SerializerPutMenuItem,
                'tables': SerializerPutTable,
            }

            record = request.GET.get('record')
            id = request.GET.get('id')

            if record is None or id is None:
                response = {
                    'status': 400,
                    'message': ERR_UNSPECIFIED_RECORD_DETAILS
                }
                return Response(response, status=200)

            serializer = serializers.get(record)
            db_record = serializer.Meta.model.objects.get(
                id=id
            )
            response = {
                'status': 200,
                'message': OK_GET_RECORD_DETAIL,
                'data': serializer(db_record, many=False).data
            }
            return Response(response, status=200)
        except Exception as error:
            print(f"Error while getting record detail: {error}")
            response = {
                'status': 400,
                'message': ERR_GENERAL
            }
            return Response(response, status=200)
