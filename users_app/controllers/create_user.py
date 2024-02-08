"""
create a user on dinify
"""
from controller_classes.misc_controller import MiscController
from dinify_backend.configs import REQUIRED_INFORMATION, MESSAGES
from users_app.models import User


def self_register(data: dict) -> dict:
    """
    Handle user self registration
    - `data` is the registration data
    """
    # check if all the required information is present
    info_check = MiscController({
        'required_information': REQUIRED_INFORMATION.get('new_user'),
        'provided_information': data
    }).check_required_information()
    if not info_check.get('status'):
        return {
            'status': 400,
            'message': info_check.get('message')
        }

    # check that that the phone number is not repeated
    if User.objects.filter(phone_number=data.get('phone_number')).exists():
        return {
            'status': 400,
            'message': MESSAGES.get('PHONE_NUMBER_EXISTS')
        }

    # check that the email is not repeated
    if User.objects.filter(email=data.get('email')).exists():
        return {
            'status': 400,
            'message': MESSAGES.get('EMAIL_EXISTS')
        }

    # create the user
    User.objects.create_user(
        first_name=data.get('first_name').strip().title(),
        last_name=data.get('last_name').strip().title(),
        email=data.get('email').strip().lower(),
        phone_number=data.get('phone_number'),
        username=data.get('phone_number'),
        country=data.get('country').strip().upper(),
        password=data.get('password')
    )

    # TODO send welcome email

    return {
        'status': 200,
        'message': MESSAGES.get('OK_SELF_REGISTER')
    }
