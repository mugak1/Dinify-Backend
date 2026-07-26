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
    'OK_LOGOUT': 'Logout successful.',

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
    # A restaurant that exists and is being set up, but has not gone live. Said in
    # plain language because a diner standing at a table reads it — they have done
    # nothing wrong and there is nothing for them to fix.
    'NOT_OPEN_YET': 'This restaurant is not open for orders yet. Please check back soon.',  # noqa
    'RESTAURANT_NOT_FOUND': 'Sorry, the provided restaurant is not supported',

    # deletion
    'NO_DELETION_REASON': 'Please provide a reason for the deleting the record',
    'ALREADY_DELETED': 'The record has already been deleted',
    'OK_DELETION': 'The record has been deleted successfully',

    # order
    'NO_ORDER_ITEMS': 'Please add items to the order',
    'ORDER_INITIATED': 'The order has been initiated successfully',
}

ERR_GENERAL = 'Sorry, an error occurred'


OK_GET_RECORD_DETAIL = 'The details of the record have been retrieved successfully.'
ERR_UNSPECIFIED_RECORD_DETAILS = 'Please specify the record type and the record id'


OK_ORDER_UPDATED = 'The order has been updated.'
ERR_ORDER_UPDATED = 'Sorry, an error occurred while updating the order.'


OK_SCANNED_TABLE = 'The table details have been retrieved successfully.'
ERR_TABLE_REFERENCE_REQUIRED = 'A table reference is required to view the menu.'

OK_ADDED_SECTION_GROUP = 'The section group has been added successfully.'
ERR_ADDED_SECTION_GROUP = 'An error occurred while adding the section group.'
OK_RETRIEVED_SECTION_GROUP = 'Successfully retrieved the section groups.'
ERR_RETRIEVED_SECTION_GROUP = 'Erro while retrieving section groups.'
OK_UPDATED_SECTION_GROUP = 'he details of the section group have been updated successfully.'
ERR_UPDATED_SECTION_GROUP = 'An error occurred while updating the details of the section group.'

OK_RETRIEVED_FULL_MENU = 'Successfully retrieved the restaurant menu.'
ERR_RESTAURANT_REFERENCE_REQUIRED = (
    'A restaurant reference is required to view the menu.'
)

# The graceful "temporarily unavailable" a diner sees at a SUSPENDED restaurant —
# the one lifecycle state the public surface answers with something other than the
# generic not-found (see menu_publication.resolve_public_restaurant). Deliberately
# says nothing about WHY: suspension is usually a billing matter between Dinify and
# the restaurant, and that is not a diner's business.
ERR_RESTAURANT_UNAVAILABLE = (
    'This restaurant is temporarily unavailable. Please check back later.'
)


EOD_IN_PROGRESS = 'The restaurant is currently processing the End of Day tasks. Please wait for 5 minutes.'  # noqa
