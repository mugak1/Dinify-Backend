"""
configurations or definitions of various values on the syste
"""

# string definitions in the application

# account types
AccountType_Restaurant = 'restaurant'
AccountType_DinifyRevenue = 'dinify_revenue'

# payment modes
PaymentMode_Cash = 'cash'
PaymentMode_MobileMoney = 'momo'
PaymentMode_Card = 'card'

# account status
AccountStatus_Active = 'active'
AccountStatus_Inactive = 'inactive'
AccountStatus_Blocked = 'blocked'

# transaction types
TransactionType_OrderPayment = 'order_payment'
TransactionType_OrderRefund = 'order_refund'
TransactionType_OrderCharge = 'order_charge'
TransactionType_Disbursement = 'disbursement'
TransactionType_Subscription = 'subscription'

# transaction statuses
TransactionStatus_Success = 'success'
TransactionStatus_Failed = 'failed'
TransactionStatus_Pending = 'pending'

# transaction platforms
TransactionPlatform_Web = 'web'

# telecoms
AirtelUg = 'airtelug'
MtnUg = 'mtnug'


ACTION_LOG_STATUSES = {
    'success': 'success',
    'failed': 'failed',
    'unauthorised': 'unauthorised',
}

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
        {'key': 'first_name', 'label': 'First Name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
        {'key': 'last_name', 'label': 'Last Name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
        # {'key': 'email', 'label': 'Email', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        {'key': 'phone_number', 'label': 'Phone Number', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'password', 'label': 'Password', 'type': 'char', 'min_length': 4, 'text_presentation': None},  # noqa
        {'key': 'country', 'label': 'Country', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa
    ],
    'restaurant_registration': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'location', 'label': 'location', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
    ],
    'restaurant_employee': [
        {'key': 'user', 'label': 'User', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'restaurant', 'label': 'Restaurant', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'roles', 'label': 'Roles', 'type': 'list', 'min_length': 5, 'text_presentation': None},  # noqa    
    ],
    'menu_section': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'restaurant', 'label': 'description', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    ],
    'menu_item': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'primary_price', 'label': 'price', 'type': 'float', 'min_length': 3, 'text_presentation': None},  # noqa
        {'key': 'section', 'label': 'section', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    ],
    'table': [
        {'key': 'number', 'label': 'number', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'restaurant', 'label': 'description', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    ],

}


EDIT_INFORMATION = {
    'restaurant_registration': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'location', 'label': 'location', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'logo', 'label': 'logo', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'cover_photo', 'label': 'cover photo', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'status', 'label': 'status', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        {'key': 'require_order_prepayments', 'label': 'require order prepayments', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'expose_order_ratings', 'label': 'expose order ratings', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'allow_deliveries', 'label': 'allow deliveries', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'allow_pickups', 'label': 'allow pickups', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'preferred_subscription_method', 'label': 'preferred subscription method', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        {'key': 'order_surcharge_percentage', 'label': 'order surcharge percentage', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'flat_fee', 'label': 'flat fee', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
    ],
    'restaurant_employee': [
        {'key': 'roles', 'label': 'Roles', 'type': 'list', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'active', 'label': 'active', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa    
    ],
    'menu_section': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'description', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'section_banner_image', 'label': 'section banner', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    ],
    'menu_item': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'primary_price', 'label': 'price', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'discounted_price', 'label': 'discounted price', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'running_discount', 'label': 'running discount', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'image', 'label': 'image', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    ],
    'table': [
        {'key': 'number', 'label': 'number', 'type': 'int', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'room_name', 'label': 'room name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'prepayment_required', 'label': 'prepayment required', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'smoking_zone', 'label': 'smoking zone', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    ]
}

# fields to ignore or modify when saving to the logs
IGNORE_LOG_FIELDS = ['password']
STRINGIFY_LOG_FIELDS = [
    'logo', 'cover_photo', 'section_banner_image', 'image'
]

# the various messages sent out
MESSAGES = {
    # GENERAL ERROR
    'GENERAL_ERROR': 'An error occurred. Please try again later.',

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
    'OK_PASSWORD_RESET': 'Password reset successfully. Please check your SMS or email for the new password',  # noqa

    # restaurant
    'DUPLICATE_RESTAURANT_NAME': 'You already have a restaurant with this name',
    'OK_CREATE_RESTAURANT': 'The restaurant has been created succesfully.',
    'BLOCKED_RESTAURANT': 'Sorry, the restaurant cannot accept orders at this time',  # noqa
    'RESTAURANT_NOT_FOUND': 'Sorry, the provided restaurant is not supported',

    # deletion
    'NO_DELETION_REASON': 'Please provide a reason for the deleting the record',
    'ALREADY_DELETED': 'The record has already been deleted',
    'OK_DELETION': 'The record has been deleted successfully',

    # order
    'NO_ORDER_ITEMS': 'Please add items to the order',
    'ORDER_INITIATED': 'The order has been initiated successfully',
    
}