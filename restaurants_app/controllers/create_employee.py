import random
from typing import Optional
from restaurants_app.models import Restaurant
from users_app.controllers.self_register import self_register
from users_app.models import User
from restaurants_app.serializers import SerializerPutRestaurantEmployee
from misc_app.controllers.secretary import Secretary
from django.db import transaction
from users_app.controllers.otp_manager import OtpManager
from misc_app.controllers.notifications.notification import Notification


def create_employee(
    first_name: str,
    last_name: str,
    email: str,
    phone_number: str,
    restaurant: Restaurant,
    roles: list,
    creator: User,
    otp: Optional[str] = None,
    skip_otp: Optional[bool] = False
) -> dict:
    with transaction.atomic():
        # password = User.objects.make_random_password()
        # password = 'password'
        password = ''.join(random.choices(
            'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
            k=8
        ))

        # attempt to verify the OTP
        if not skip_otp:
            if otp is None:
                return {
                    'status': 400,
                    'message': 'Please provide the OTP.'
                }

            otp_verification = OtpManager().verify_otp(
                user_id=str(creator.id),
                otp=otp
            )
            if not otp_verification['data']['valid']:
                return {
                    'status': 400,
                    'message': 'Invalid OTP.'
                }
        create_user = self_register(
            data={
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'phone_number': phone_number,
                'country': restaurant.country,
                'password': password
            },
            return_user_id=True,
            send_credentials=True,
            skip_otp=True
        )

        if create_user['status'] != 200:
            return create_user

        employee_data = {
            'user': create_user['user_id'],
            'restaurant': str(restaurant.id),
            'roles': roles
        }
        secretary_args = {
            'serializer': SerializerPutRestaurantEmployee,
            'data': employee_data,
            'required_information': [],
            'user_id': str(creator.id),
            'username': creator.username,
            'success_message': 'The employee has been created successfully. Access credentials have been sent to the user email.',  # noqa
            'error_message': 'The employee could not be created. Please try again later.'
        }
        response = Secretary(secretary_args).create()
        if response['status'] != 200:
            # delete the user account that was created
            User.objects.get(id=create_user['user_id']).delete()

        Notification(msg_data={
            'msg_type': 'new-restaurant-employee',
            'first_name': first_name,
            'restaurant_name': restaurant.name,
            'user_id': str(create_user['user_id'])
        }).create_notification()
        return response
