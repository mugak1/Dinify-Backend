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
    # 'restaurant_employee': ,
    'menu_section': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
        {'key': 'restaurant', 'label': 'description', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    ],
    'menu_item': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
        {'key': 'primary_price', 'label': 'price', 'type': 'float', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'section', 'label': 'section', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    ],
    'table': [
        {'key': 'number', 'label': 'number', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'restaurant', 'label': 'description', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    ],
}

RI_RESTAURANT_EMPLOYEES = [
    {'key': 'user', 'label': 'User', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'restaurant', 'label': 'Restaurant', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'roles', 'label': 'Roles', 'type': 'list', 'min_length': 5, 'text_presentation': None},  # noqa    
]

RI_SECTION_GROUP = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
    {'key': 'section', 'label': 'section', 'type': 'char', 'min_length': 3, 'text_presentation': None}  # noqa
]

RI_DINING_AREA = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 2, 'text_presentation': str.title},  # noqa
    {'key': 'restaurant', 'label': 'Restaurant', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
]
