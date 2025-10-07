"""
endpoints for restaurant configurations.
Refactoring needed to make it more maintainable.
"""
import ast
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
    SerializerPutRestaurant, SerializerPublicGetRestaurant, SerializerGetRestaurantDetail,
    SerializerPutRestaurantEmployee, SerializerGetRestaurantEmployee,

    SerializerPutMenuSection, SerializerPublicGetMenuSection,
    SerializerPutMenuItem, SerializerPublicGetMenuItem,
    SerializerPutTable, SerializerPublicGetTable,

    SerializerPutSectionGroup, SerializerPublicGetSectionGroup,
    SerializerAdminGetOrderReview, SerializerAdminGetOrderItemReview,

    SerializerPutDiningArea, SerializerGetDiningArea
)
from orders_app.serializers import SerializerListGetOrder
from restaurants_app.models import Restaurant, MenuSection, SectionGroup, MenuItem
from restaurants_app.controllers.tables import (
    create_tables_in_section,
    get_tables_by_area
)
from restaurants_app.controllers.dining_areas import create_dining_area
from restaurants_app.controllers.menu_sections import ConMenuSection
from dinify_backend.configss.required_information import (
    REQUIRED_INFORMATION,
    RI_RESTAURANT_EMPLOYEES,
    RI_SECTION_GROUP,
    RI_DINING_AREA
)
from dinify_backend.configss.edit_information import EDIT_INFORMATION, EI_DINING_AREA, EI_SECTION_GROUP
from dinify_backend.configss.messages import (
    OK_GET_RECORD_DETAIL, ERR_GENERAL,
    ERR_UNSPECIFIED_RECORD_DETAILS,
    OK_ADDED_SECTION_GROUP, ERR_ADDED_SECTION_GROUP,
    OK_RETRIEVED_SECTION_GROUP, ERR_RETRIEVED_SECTION_GROUP,
    OK_UPDATED_SECTION_GROUP, ERR_UPDATED_SECTION_GROUP  # noqa
)
from restaurants_app.controllers.create_employee import create_employee
from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Served,
    OrderStatus_Paid,
    OrderStatus_Cancelled,
    OrderStatus_Refunded,
    PaymentStatus_Paid,
    PaymentStatus_Pending,
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER, OrderStatus_Initiated
)

from users_app.controllers.permissions_check import (
    is_dinify_admin,
    get_user_restaurant_roles
)

from restaurants_app.models import RestaurantEmployee, DiningArea, Table
from users_app.models import User
from restaurants_app.controllers.subscriptions import RestaurantSubscription
from restaurants_app.configs.non_unique_combination import RECORDS_NON_UNIQUE_COMBINATIONS
from restaurants_app.controllers.con_cla_employees import ConRestaurantEmployee



def check_permission(user: User, record: str, id: str):
    # return False
    has_permission = False
    if is_dinify_admin(user):
        has_permission = True

    if not has_permission:
        # get the restaurants to which the user belongs
        # TODO #60 only get roles at the restaurant in question
        res_mapping = RestaurantEmployee.objects.values('restaurant').filter(
            user=user,
            active=True,
        )
        # for x in res_mapping:
        #     print(f"res mapping: {x['restaurant']}")
        restaurant_ids = [str(res['restaurant']) for res in res_mapping]
        for restaurant_id in restaurant_ids:
            roles = get_user_restaurant_roles(
                user_id=str(user.id),
                restaurant_id=restaurant_id
            )
            print(restaurant_id, user.id, roles)
            restaurant_roles = [RESTAURANT_OWNER, RESTAURANT_MANAGER]
            if len(roles) > 0:
                if any(role in restaurant_roles for role in roles):
                    has_permission = True
                    break
    return has_permission


class RestaurantSetupEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    def handle_create_employee(self, request):
        # TODO if the user is not a Dinify admin,.
        # then set the owner value from the auth details
        data = request.data
        try:
            data = data.dict()
        except Exception as error:
            print(f"Error: {error}")

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record='employee',
            id=data.get('restaurant')
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=400)

        # response = {
        #     'status': 200,
        #     'message': 'Ok'
        # }
        # return Response(response, status=200)

        try:
            response = create_employee(
                first_name=data.get('first_name'),
                last_name=data.get('last_name'),
                email=data.get('email'),
                phone_number=data.get('phone_number'),
                restaurant=Restaurant.objects.get(id=data.get('restaurant')),
                roles=data.get('roles'),
                creator=request.user,
                otp=data.get('otp'),
                skip_otp=True
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

    def post(self, request, config_detail):
        """
        handle the POST method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)
        non_unique_combination = None

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
            return self.handle_create_employee(request)

        if config_detail == 'tables':
            tables_count = Table.objects.filter(
                restaurant=request.data.get('restaurant'),
                number=request.data.get('number'),
                deleted=False
            ).count()
            if tables_count > 0:
                response = {
                    'status': 400,
                    'message': f"Table number {request.data.get('number')} is already in use."
                }
                return Response(response, status=400)

        if config_detail == 'employees':
            shortcut_employee_creation = ConRestaurantEmployee.create_employee_from_existing_user(
                user_id=request.data.get('user'),
                restaurant_id=request.data.get('restaurant'),
                roles=request.data.get('roles')
            )

            if shortcut_employee_creation['status'] == 200:
                return Response(
                    shortcut_employee_creation,
                    status=shortcut_employee_creation['status']
                )

        serializers = {
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        required_information = {
            'employees': RI_RESTAURANT_EMPLOYEES,
            'menusections': REQUIRED_INFORMATION.get('menu_section'),
            'sectiongroups': RI_SECTION_GROUP,
            'menuitems': REQUIRED_INFORMATION.get('menu_item'),
            'tables': REQUIRED_INFORMATION.get('table'),
            'diningareas': RI_DINING_AREA
        }

        success_messages = {
            'employees': 'The employee has been added successfully.',
            'menusections': 'The menu section has been added successfully.',
            'sectiongroups': OK_ADDED_SECTION_GROUP,
            'menuitems': 'The menu item has been added successfully.',
            'tables': 'The table has been added successfully',
            'diningareas': 'The dining area has been added successfully'
        }

        error_messages = {
            'employees': 'An error occurred while adding the employee.',
            'menusections': 'An error occurred while adding the menu section.',
            'sectiongroups': ERR_ADDED_SECTION_GROUP,
            'menuitems': 'An error occurred while adding the menu item.',
            'tables': 'An error occurred while adding the table',
            'diningareas': 'An error occurred while adding the dining area'
        }

        msg_types = {
            'employees': 'new-restaurant-employee',
            'menusections': 'new-menu-section',
            'sectiongroups': 'new-menu-group',
            'menuitems': 'new-menu-item',
            'tables': 'new-table',
            'diningareas': 'new-dining-area'
        }

        post_data = request.data

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            id=post_data.get('restaurant')
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=400)

        try:
            post_data = post_data.dict()
        except Exception as error:
            print(f"Error: {error}")

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
            post_data['restaurant_id'] = restaurant_id

        if config_detail == 'tables':
            try:
                post_data['number'] = str(post_data.get('number'))
                post_data['str_number'] = str(post_data.get('number'))
            except Exception as error:
                print(f"Error converting table number to string: {error}")

        if config_detail == 'section-tables':
            dining_area = None
            if post_data.get('dining_area') is not None:
                dining_area = DiningArea.objects.get(id=post_data.get('dining_area'))

            response = create_tables_in_section(
                restaurant_id=post_data.get('restaurant'),
                section_name=post_data.get('room_name'),
                no_tables=int(post_data.get('number', 0)),
                user=request.user,
                smoking_zone=post_data.get('smoking_zone', False),
                outdoor_seating=post_data.get('outdoor_seating'),
                consideration=post_data.get('consideration', 'count'),
                range_from=int(post_data.get('start', 0)),
                range_to=int(post_data.get('end', 0)),
                dining_area=dining_area
            )
            return Response(response, status=response['status'])

        if config_detail == 'diningareas':
            response = create_dining_area(
                restaurant_id=post_data.get('restaurant'),
                dining_area_name=post_data.get('name'),
                smoking_zone=post_data.get('smoking_zone'),
                outdoor_seating=post_data.get('outdoor_seating'),
                user=request.user,
                create_tables=post_data.get('create_tables', False),
                consideration=post_data.get('consideration', 'count'),
                description=post_data.get('description', None),
                no_tables=post_data.get('no_tables', 0),
                range_from=int(post_data.get('start', 0)),
                range_to=int(post_data.get('end', 0))
            )
            return Response(response, status=response['status'])

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
            'error_message': error_message,
            'user': request.user,
            'msg_type': msg_types.get(config_detail),
            'non_unique_handling': RECORDS_NON_UNIQUE_COMBINATIONS.get(config_detail),
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

        if config_detail == 'subscription-details':
            return RestaurantSubscription().get_details(request)

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
                        # OrderStatus_Initiated,
                        OrderStatus_Pending,
                        OrderStatus_Preparing,
                        OrderStatus_Served
                    ]
                    orm_filter['payment_status'] = PaymentStatus_Pending

                if request.GET.get('status') == 'closed':
                    orm_filter['order_status__in'] = [OrderStatus_Served, OrderStatus_Paid]
                    orm_filter['payment_status'] = PaymentStatus_Paid

                if request.GET.get('status') == 'all':
                    orm_filter['order_status__in'] = [
                        # OrderStatus_Pending,
                        # OrderStatus_Initiated,
                        # OrderStatus_Preparing,
                        # OrderStatus_Served,
                        # OrderStatus_Cancelled,
                        # OrderStatus_Refunded,

                        OrderStatus_Paid,
                        OrderStatus_Cancelled,
                        OrderStatus_Refunded
                    ]
                    # orm_filter['payment_status'] = PaymentStatus_Pending

        if config_detail == 'restaurants':
            if 'status' not in request.GET:
                orm_filter['status__in'] = ['active', 'pending']

        if 'deleted' not in request.GET:
            orm_filter['deleted'] = False
        print(f"The ORM filter is: {orm_filter}")
        print(f"The GET params are: {request.GET}")

        if config_detail == 'menuitems':
            # orm_filter['section_group__deleted'] = False
            # orm_filter['section_group__available'] = True
            # if 'available' not in request.GET:
            #     orm_filter['available'] = True
            if 'is_extra' in request.GET:
                orm_filter['is_extra'] = True if request.GET.get('is_extra') == 'true' else False

        if config_detail == 'sectiongroups':
            if 'available' not in request.GET:
                orm_filter['available'] = True

        if config_detail == 'tables':
            if request.GET.get('grouping') is not None:
                response = get_tables_by_area(
                    restaurant_id=request.GET.get('restaurant')
                )
                return Response(response, status=200)

        serializers = {
            'restaurants': SerializerPublicGetRestaurant,
            'employees': SerializerGetRestaurantEmployee,
            'menusections': SerializerPublicGetMenuSection,
            'sectiongroups': SerializerPublicGetSectionGroup,
            'menuitems': SerializerPublicGetMenuItem,
            'tables': SerializerPublicGetTable,
            'orders': SerializerListGetOrder,
            'orderreviews': SerializerAdminGetOrderReview,
            'orderitemreviews': SerializerAdminGetOrderItemReview,
            'diningareas': SerializerGetDiningArea
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'employees': 'Successfully retrieved the employees',
            'menusections': 'Successfully retrieved the menu sections',
            'sectiongroups': OK_RETRIEVED_SECTION_GROUP,
            'menuitems': 'Successfully retrieved the menu items',
            'tables': 'Successfully retrieved the tables',
            'orders': 'Successfully retrieved the orders',
            'orderreviews': 'Successfully retrieved the order reviews',
            'orderitemreviews': 'Successfully retrieved the order item reviews',
            'diningareas': 'Successfully retrieved the dining areas'
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
            'orderitemreviews': 'Error while retrieving the order item reviews',
            'diningareas': 'Error while retrieving the dining areas'
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
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        edit_information = {
            'restaurants': EDIT_INFORMATION.get('restaurants'),
            'employees': EDIT_INFORMATION.get('restaurant_employee'),
            'menusections': EDIT_INFORMATION.get('menu_section'),
            'sectiongroups': EI_SECTION_GROUP,
            'menuitems': EDIT_INFORMATION.get('menu_item'),
            'tables': EDIT_INFORMATION.get('table'),
            'diningareas': EI_DINING_AREA
        }

        success_messages = {
            'restaurants': 'The details of the restaurant have been updated successfully.',
            'employees': 'The details of the employee have been updated successfully',
            'menusections': 'The details of the menu section have been updated successfully.',
            'sectiongroups': OK_UPDATED_SECTION_GROUP,
            'menuitems': 'The details of the menu item have been updated successfully.',
            'tables': 'The details of the table have been updated successfully.',
            'diningareas': 'The details of the dining area have been updated successfully.'
        }

        error_messages = {
            'restaurants': 'An error occurred while updating the details of the restaurant.',
            'employees': 'An error occurred while updating the details of the employee.',
            'menusections': 'An error occurred while updating the details of the menu section.',
            'sectiongroups': ERR_UPDATED_SECTION_GROUP,
            'menuitems': 'An error occurred while updating the details of the menu item.',
            'tables': 'An error occurred while updating the details of the table.',
            'diningareas': 'An error occurred while updating the details of the dining area.'
        }

        # TODO check if the user has permissions to edit the details

        serializer = serializers.get(config_detail)
        edit_information = edit_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        put_data = request.data

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            id=put_data.get('restaurant')
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=400)

        if config_detail == 'subscription-details':
            return RestaurantSubscription().update(request)

        if config_detail == 'reorder-menu-items':
            response = ConMenuSection().reorder_listing(
                ordering=put_data.get('ordering'),
                user=request.user
            )
            return Response(response, status=response['status'])
            # reorder the menu items

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

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            id=request.data['id']
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=400)

        data = request.data
        if config_detail == 'employees':
            data['active'] = False

            # check if the employee is an owner
            roles = RestaurantEmployee.objects.values('roles', 'restaurant').get(id=data['id'])
            if RESTAURANT_OWNER in roles['roles']:
                more_owners = RestaurantEmployee.objects.filter(
                    restaurant_id=roles['restaurant'],
                    roles__contains=[RESTAURANT_OWNER],
                    active=True,
                    deleted=False
                ).exclude(id=data['id']).count() > 0
                if not more_owners:
                    response = {
                        'status': 400,
                        'message': 'You need to assign another restaurant owner before you can delete this one.'
                    }
                    return Response(response, status=400)

        serializer = {
            'restaurant': SerializerPutRestaurant,
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        secretary_args = {
            'serializer': serializer[config_detail],
            'data': data,
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
                'restaurants': SerializerGetRestaurantDetail,
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
