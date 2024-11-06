from restaurants_app.models import Restaurant
from users_app.controllers.self_register import self_register
from users_app.models import User
from restaurants_app.serializers import SerializerPutRestaurantEmployee
from misc_app.controllers.secretary import Secretary


def create_employee(
    first_name: str,
    last_name: str,
    email: str,
    phone_number: str,
    restaurant: Restaurant,
    roles: list,
    creator: User
) -> dict:
    password = User.objects.make_random_password()
    password = 'password'

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
        send_credential_email=True
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
    return response
