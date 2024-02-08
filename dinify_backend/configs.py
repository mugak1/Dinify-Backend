"""
configurations or definitions of various values on the syste
"""


# a dictionary of the various configurations to consider
class ConfigDictionary(object):
    def __init__(self, dictionary):
        self.__dict__ = dictionary


# the roles that can be granted on the system
ROLES = {
    'DINIFY_ADMIN': 'dinify_admin',
    'RESTAURANT_MANAGER': 'restaurant_manager',
    'RESTAURANT_STAFF': 'restaurant_staff',
    'DINER': 'diner'
}

REQUIRED_INFORMATION = {
    'new_user': [
        {'key': 'first_name', 'label': 'First Name', 'type': 'char', 'min_length': 2},
        {'key': 'last_name', 'label': 'Last Name', 'type': 'char', 'min_length': 2},
        {'key': 'email', 'label': 'Email', 'type': 'char', 'min_length': 5},
        {'key': 'phone_number', 'label': 'Phone Number', 'type': 'char', 'min_length': 5},
        {'key': 'password', 'label': 'Password', 'type': 'char', 'min_length': 4},
        {'key': 'country', 'label': 'Country', 'type': 'char', 'min_length': 2},
    ]
}

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
}
