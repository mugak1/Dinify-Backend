"""
a user creating a restaurant on their own
"""
import logging
import random
from django.db import transaction

logger = logging.getLogger(__name__)

from misc_app.controllers.save_action_log import save_action
from restaurants_app.models import Restaurant
from dinify_backend.configs import ROLES, ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.required_information import REQUIRED_INFORMATION
from restaurants_app.serializers import SerializerPutRestaurant, SerializerPutRestaurantEmployee
from restaurants_app.controllers.role_permissions import ensure_role_permissions
from misc_app.controllers.check_required_information import check_required_information
from users_app.controllers.self_register import self_register
from users_app.models import User
from misc_app.controllers.notifications.notification import Notification


def admin_register_restaurant(data: dict, auth_info: dict) -> dict:
    """
    When an admin is creating a restaurant
    """
    # Authorization is enforced at the endpoint (admin-only): the
    # admin-register-restaurant POST branch gates on is_dinify_admin(request.user)
    # before calling this. This function assumes an admin caller.

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
        name__iexact=data['name'].strip(),
        location__iexact=data['location'].strip().lower()
    )
    if duplicate_name:
        return {
            'status': 400,
            'message': "A restaurant record with the same name and location already exists."
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
        try:
            record_data.pop('status', None)
        except Exception as error:
            logger.error("Error while dropping status: %s", error)

        record = SerializerPutRestaurant(data=record_data)
        user_profile = user_creation_result['user_profile']

    if record.is_valid():
        with transaction.atomic():
            # owner + created_by are server-derived (read_only) — set via save().
            record.save(
                owner_id=user_creation_result['user_id'],
                created_by_id=auth_info['user_id'],
            )

            # save the restaurant-employee mapping
            employee = {
                'user': user_creation_result['user_id'],
                'roles': [ROLES.get('RESTAURANT_OWNER')],
            }

            employee_record = SerializerPutRestaurantEmployee(data=employee)
            if employee_record.is_valid():
                # restaurant + created_by are server-derived (read_only).
                employee_record.save(
                    restaurant_id=record.data['id'],
                    created_by_id=auth_info['user_id'],
                )

                # seed the default role-permission grid for the new restaurant
                ensure_role_permissions(record.data['id'])

                # create the notification for the restaurant
                Notification(msg_data={
                    'msg_type': 'admin-new-restaurant',
                    'first_name': user_profile.first_name,
                    'restaurant_name': data['name'],
                    'restaurant_id': record.data['id']
                }).create_notification()

                # save action for creating a restaurant
                save_action(
                    affected_model='Restaurant',
                    affected_record=str(record.data['id']),
                    action='created-restaurant',
                    narration='Created a new restaurant record',
                    result=ACTION_LOG_STATUSES.get('success'),
                    user_id=auth_info['user_id'],
                    username=auth_info['email'],
                    submitted_data=data,
                    changes=None,
                    filter_information=None
                )

                # save action for creating employee mapping
                save_action(
                    affected_model='RestaurantEmployee',
                    affected_record=str(record.data['id']),
                    action='added-employee-to-restaurant',
                    narration='Added restaurant owner as an employee to the restaurant',
                    result=ACTION_LOG_STATUSES.get('success'),
                    user_id=auth_info['user_id'],
                    username=auth_info['email'],
                    submitted_data=data,
                    changes=None,
                    filter_information=None
                )

                return {
                    'status': 200,
                    'message': MESSAGES.get('OK_CREATE_RESTAURANT'),
                    'data': record.data
                }
            raise Exception('Failed to create restaurant employee')
    else:
        logger.error("RestaurantError-Create: %s", record.errors)
        error_message = ""
        for _, value in record.errors.items():
            error_message += f"{', '.join(value)}\n"
        return {
            'status': 400,
            'message': error_message
        }
