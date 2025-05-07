"""
a user creating a restaurant on their own
"""
import random
from django.db import transaction
from restaurants_app.models import Restaurant
from dinify_backend.configs import ROLES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.required_information import REQUIRED_INFORMATION
from restaurants_app.serializers import SerializerPutRestaurant, SerializerPutRestaurantEmployee
from misc_app.controllers.check_required_information import check_required_information
from dinify_backend.configss.string_definitions import AccountType_Restaurant
from finance_app.serializers import SerializerPutAccount
from users_app.controllers.self_register import self_register
from users_app.models import User
from misc_app.controllers.notifications.notification import Notification


def create_restaurant(data: dict, auth_info: dict) -> dict:
    """
    When a user is creating a restaurant by themselves
    """

    # check that the required information is provided
    info_check = check_required_information(
        REQUIRED_INFORMATION.get('restaurant_registration'),
        data
    )
    if not info_check.get('status'):
        return {
            'status': 400,
            'message': info_check.get('message')
        }

    # check if the user has a restaurant with the same name
    duplicate_name = Restaurant.objects.filter(
        owner=data['owner'],
        name=data['name'].strip().lower()
    )
    if duplicate_name:
        return {
            'status': 400,
            'message': MESSAGES.get('DUPLICATE_RESTAURANT_NAME')
        }

    # construct the info to submit to the database
    record_data = data.copy()
    record_data['name'] = data['name'].strip().title()
    record_data['created_by'] = auth_info['user_id']

    record = SerializerPutRestaurant(data=record_data)
    if record.is_valid():
        with transaction.atomic():
            record.save()
            # create the restaurant account
            account_data = {
                'account_type': AccountType_Restaurant,
                'restaurant': record.data['id']
            }
            account_record = SerializerPutAccount(data=account_data)
            if account_record.is_valid():
                account_record.save()

                # save the restaurant-employee mapping
                employee = {
                    'user': record.data['owner'],
                    'restaurant': record.data['id'],
                    'roles': [ROLES.get('RESTAURANT_OWNER')],
                    'created_by': auth_info['user_id']
                }

                employee_record = SerializerPutRestaurantEmployee(data=employee)
                if employee_record.is_valid():
                    employee_record.save()

                    # create the notification for the restaurant
                    Notification(msg_data={
                        'msg_type': 'new-restaurant',
                        'first_name': auth_info['first_name'],
                        'restaurant_name': record.data['name'],
                        'recipient_email': auth_info['email'],
                        'restaurant_id': record.data['id']
                    }).create_notification()

                    return {
                        'status': 200,
                        'message': MESSAGES.get('OK_CREATE_RESTAURANT'),
                        'data': record.data
                    }
                raise Exception('Failed to create restaurant employee')
            raise Exception('Failed to create restaurant account')
    else:
        print(f"RestaurantError-Create:{record.errors}")
        error_message = ""
        for _, value in record.errors.items():
            error_message += f"{', '.join(value)}\n"
        return {
            'status': 400,
            'message': error_message
        }


def admin_register_restaurant(data: dict, auth_info: dict) -> dict:
    """
    When an admin is creating a restaurant
    """
    # TODO check that the user is an admin

    # add a random password
    data['password'] = ''.join(random.choices(
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        k=8
    ))
    # data['password'] = '1234'
    data['owner'] = 'owner'

    # check that the required restaurant information is provided
    info_check = check_required_information(
        REQUIRED_INFORMATION.get('restaurant_registration'),
        data
    )
    if not info_check.get('status'):
        return {
            'status': 400,
            'message': info_check.get('message')
        }

    # check if the user exists
    if not User.objects.filter(phone_number=data['phone_number']).exists():
        # check if all the required information is present
        info_check = check_required_information(
            REQUIRED_INFORMATION.get('new_user'),
            data
        )
        if not info_check.get('status'):
            return {
                'status': 400,
                'message': info_check.get('message')
            }

    # check if the user has a restaurant with the same name
    duplicate_name = Restaurant.objects.filter(
        name=data['name'].strip().lower()
    )
    if duplicate_name:
        return {
            'status': 400,
            'message': MESSAGES.get('DUPLICATE_RESTAURANT_NAME')
        }

    # create the user
    # create the restaurant
    # create the user-restaurant mapping
    with transaction.atomic():
        user_creation_result = self_register(
            data=data,
            return_user_id=True,
            send_credentials=True,
            skip_otp=True
        )

        if not user_creation_result.get('status') == 200:
            if not user_creation_result.get('message') == MESSAGES.get('PHONE_NUMBER_EXISTS'):
                return user_creation_result

        # construct the info to submit to the database
        record_data = data.copy()
        record_data['name'] = data['name'].strip().title()
        record_data['owner'] = user_creation_result['user_id']
        record_data['created_by'] = auth_info['user_id']

        print(f"RestaurantData:{record_data}")

        record = SerializerPutRestaurant(data=record_data)
        user_profile = user_creation_result['user_profile']

    if record.is_valid():
        with transaction.atomic():
            record.save()

            # create the restaurant account
            account_data = {
                'account_type': AccountType_Restaurant,
                'restaurant': record.data['id']
            }

            account_record = SerializerPutAccount(data=account_data)
            if account_record.is_valid():
                account_record.save()

                # save the restaurant-employee mapping
                employee = {
                    'user': user_creation_result['user_id'],
                    'restaurant': record.data['id'],
                    'roles': [ROLES.get('RESTAURANT_OWNER')],
                    'created_by': auth_info['user_id']
                }

                employee_record = SerializerPutRestaurantEmployee(data=employee)
                if employee_record.is_valid():
                    employee_record.save()

                    # create the notification for the restaurant
                    Notification(msg_data={
                        'msg_type': 'admin-new-restaurant',
                        'first_name': user_profile.first_name,
                        'restaurant_name': data['name'],
                        'restaurant_id': record.data['id']
                    }).create_notification()

                    return {
                        'status': 200,
                        'message': MESSAGES.get('OK_CREATE_RESTAURANT'),
                        'data': record.data
                    }
                raise Exception('Failed to create restaurant employee')
            raise Exception('Failed to create restaurant account')
    else:
        print(f"RestaurantError-Create:{record.errors}")
        error_message = ""
        for _, value in record.errors.items():
            error_message += f"{', '.join(value)}\n"
        return {
            'status': 400,
            'message': error_message
        }
