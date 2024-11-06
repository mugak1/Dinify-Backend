"""
configurations or definitions of various values on the syste
"""
ACTION_LOG_STATUSES = {
    'success': 'success',
    'failed': 'failed',
    'unauthorised': 'unauthorised',
}

# the roles that can be granted on the system
ROLES = {
    'DINIFY_ADMIN': 'dinify_admin',
    'DINIFY_ACCOUNT_MANAGER': 'dinify_account_manager',
    'RESTAURANT_OWNER': 'owner',
    'RESTAURANT_MANAGER': 'manager',
    'RESTAURANT_STAFF': 'restaurant_staff',
    'RESTAURANT_KITCHEN': 'kitchen',
    'RESTAURANT_WAITER': 'waiter',
    'RESTAURANT_FINANCE': 'finance',
    'DINER': 'diner'
}

# fields to ignore or modify when saving to the logs
IGNORE_LOG_FIELDS = ['password']
STRINGIFY_LOG_FIELDS = [
    'logo', 'cover_photo', 'section_banner_image', 'image',
]
