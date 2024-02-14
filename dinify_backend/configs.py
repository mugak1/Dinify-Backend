"""
configurations or definitions of various values on the syste
"""

# the roles that can be granted on the system
ROLES = {
    'DINIFY_ADMIN': 'dinify_admin',
    'RESTAURANT_OWNER': 'restaurant_owner',
    'RESTAURANT_MANAGER': 'restaurant_manager',
    'RESTAURANT_STAFF': 'restaurant_staff',
    'DINER': 'diner'
}


REQUIRED_INFORMATION = {
    'new_user': [
        {'key': 'first_name', 'label': 'First Name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},
        {'key': 'last_name', 'label': 'Last Name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},
        {'key': 'email', 'label': 'Email', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},
        {'key': 'phone_number', 'label': 'Phone Number', 'type': 'char', 'min_length': 5, 'text_presentation': None},
        {'key': 'password', 'label': 'Password', 'type': 'char', 'min_length': 4, 'text_presentation': None},
        {'key': 'country', 'label': 'Country', 'type': 'char', 'min_length': 2, 'text_presentation': None},
    ],
    'restaurant_registration': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},
        {'key': 'location', 'label': 'location', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},
    ]
}


EDIT_INFORMATION = {
    'restaurant_registration': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},
        {'key': 'location', 'label': 'location', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},
        {'key': 'logo', 'label': 'logo', 'type': 'file', 'min_length': 5, 'text_presentation': None},
        {'key': 'cover_photo', 'label': 'cover photo', 'type': 'file', 'min_length': 5, 'text_presentation': None},
        {'key': 'status', 'label': 'status', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},
        {'key': 'require_order_prepayments', 'label': 'require order prepayments', 'type': 'bool', 'min_length': 5, 'text_presentation': None},
        {'key': 'expose_order_ratings', 'label': 'expose order ratings', 'type': 'bool', 'min_length': 5, 'text_presentation': None},
        {'key': 'allow_deliveries', 'label': 'allow deliveries', 'type': 'bool', 'min_length': 5, 'text_presentation': None},
        {'key': 'allow_pickups', 'label': 'allow pickups', 'type': 'bool', 'min_length': 5, 'text_presentation': None},
        {'key': 'preferred_subscription_method', 'label': 'preferred subscription method', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},
        {'key': 'order_surcharge_percentage', 'label': 'order surcharge percentage', 'type': 'float', 'min_length': 5, 'text_presentation': None},
        {'key': 'flat_fee', 'label': 'flat fee', 'type': 'float', 'min_length': 5, 'text_presentation': None},
    ]
}

# fields to ignore or modify when saving to the logs
IGNORE_LOG_FIELDS = ['password']
STRINGIFY_LOG_FIELDS = [
    'logo', 'cover_photo'
]

# the various messages sent out
MESSAGES = {
    # user registration messages
    'PHONE_NUMBER_EXISTS': 'Sorry, a user with this phone number already exists!',
    'EMAIL_EXISTS': 'Sorry, a user with this email address already exists!',
    'OK_SELF_REGISTER': 'Your account has been successfully created on Dinify!',

    # user login
    'NO_USERNAME': 'No matching phone number or email.',
    'WRONG_PASSWORD': 'The password is incorrect.',
    'ACCOUNT_NOT_ACTIVE': 'Your account is not active.',
    'OK_LOGIN': 'Login successful.',

    # password change
    'NO_USER_FOUND': 'No user found.',
    'OK_PASSWORD_CHANGE': 'Password changed successfully.',

    # reset password
    'NO_PHONE_NUMBER': 'No matching user profile found.',
    'OK_PASSWORD_RESET': 'Password reset successfully. Please check your SMS or email for the new password',

    # create restaurant
    'DUPLICATE_RESTAURANT_NAME': 'You already have a restaurant with this name',
    'OK_CREATE_RESTAURANT': 'The restaurant has been created succesfully.',

    # deletion
    'NO_DELETION_REASON': 'Please provide a reason for the deleting the record',
    'ALREADY_DELETED': 'The record has already been deleted',
    'OK_DELETION': 'The record has been deleted successfully',
}
