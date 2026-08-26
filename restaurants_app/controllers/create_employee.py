import random
from typing import Optional
from restaurants_app.controllers.employee_membership_lock import (
    lock_restaurant_for_membership_mutation,
)
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
            'roles': roles
        }
        secretary_args = {
            'serializer': SerializerPutRestaurantEmployee,
            'data': employee_data,
            'required_information': [],
            'user_id': str(creator.id),
            'username': creator.username,
            'user': creator,
            'msg_type': 'new-restaurant-employee',
            'success_message': 'The employee has been created successfully. Access credentials have been sent to the user email.',  # noqa
            'error_message': 'The employee could not be created. Please try again later.',
            # restaurant is server-derived (read_only): pass the resolved,
            # already-gated Restaurant object via the trusted server_values channel
            # so an employee can never be created against a spoofed restaurant.
            'server_values': {'restaurant': restaurant},
        }
        # THE SERIALIZATION POINT for membership writes (Restaurant ->
        # RestaurantEmployee), taken AS LATE AS THE INVARIANT ALLOWS: immediately
        # before the write it guards, and held from there through commit.
        #
        # The lateness is the point. `self_register` above issues TWO
        # `Notification.create_notification()` calls, and those go to MongoDB
        # SYNCHRONOUSLY (`save_to_mongodb` without `async_=True`, unlike
        # `save_action`, which is threaded). MongoDB is unreachable from the live
        # box, so each one burns the client's 2s `serverSelectionTimeoutMS`. Holding
        # the parent row across them would have parked a `Restaurant` lock for
        # several seconds — and a lifecycle transition waiting on that row holds the
        # EXCLUSIVE admission advisory lock while it waits, which would stall every
        # diner order at the restaurant for the duration. Acquiring the barrier here
        # keeps that I/O outside the lock entirely.
        #
        # A membership INSERT is in fact already blocked against a held parent lock
        # by PostgreSQL's referential integrity (the FK takes FOR KEY SHARE on the
        # restaurants row). Taking the lock explicitly makes the barrier a property
        # of this code rather than of one database's RI triggers, and keeps every
        # membership writer on one visible contract.
        #
        # ONE synchronous notification remains inside the lock: `Secretary.create()`
        # fires `make_notification_for_new_entry` for `msg_type` before it returns.
        # Moving that out means either changing generic Secretary (which serves every
        # resource) or dropping this path's `msg_type` — a notification-semantics
        # decision, not a concurrency one. Left as it is, and stated rather than
        # quietly tolerated.
        lock_restaurant_for_membership_mutation(getattr(restaurant, 'pk', None))

        response = Secretary(secretary_args).create()
        if response['status'] != 200:
            # delete the user account that was created
            User.objects.get(id=create_user['user_id']).delete()
        else:
            # surface the one-time credential to the (owner-only, HTTPS) caller;
            # it is also emailed to the new user via self_register(send_credentials=True)
            response['data']['temp_password'] = password

    # OUTSIDE the transaction, so it runs after the commit has released the parent
    # row. This is synchronous MongoDB I/O on a host where MongoDB is unreachable;
    # under the lock it was a multi-second hold on a row the lifecycle transition
    # and every onboarding writer need. Nothing reads its result, and it was already
    # unconditional (it fires whether or not the Secretary create succeeded), so
    # moving it past the commit changes when it runs and nothing else.
    Notification(msg_data={
        'msg_type': 'new-restaurant-employee',
        'first_name': first_name,
        'restaurant_name': restaurant.name,
        'user_id': str(create_user['user_id'])
    }).create_notification()
    return response
