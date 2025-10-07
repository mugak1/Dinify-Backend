EDIT_INFORMATION = {
    'restaurants': [
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
        {'key': 'order_surcharge_min_amount', 'label': 'min order surcharge amount', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'order_surcharge_cap_amount', 'label': 'max order surcharge amount', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'flat_fee', 'label': 'flat fee', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'branding_configuration', 'label': 'branding configuration', 'type': 'dict', 'min_length': 5, 'text_presentation': None},  # noqa
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
        {'key': 'listing_position', 'label': 'listing position', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
    ],
    'menu_item': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'primary_price', 'label': 'price', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'discounted_price', 'label': 'discounted price', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'running_discount', 'label': 'running discount', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'consider_discount_object', 'label': 'consider discount object', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'image', 'label': 'image', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'section', 'label': 'section', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'section_group', 'label': 'group', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'discount_description', 'label': 'discount description', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'discount_details', 'label': 'discount details', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'options', 'label': 'options', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'extras_applicable', 'label': 'extras_applicable', 'type': 'list', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'is_extra', 'label': 'is extra', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'is_special', 'label': 'is special', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'has_extras', 'label': 'has extras', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'allergens', 'label': 'allergens', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
    ],
    'table': [
        {'key': 'dining_area', 'label': 'dining area', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'number', 'label': 'number', 'type': 'int', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'room_name', 'label': 'room name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'prepayment_required', 'label': 'prepayment required', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'smoking_zone', 'label': 'smoking zone', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'outdoor_seating', 'label': 'outdoor seating', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'reserved', 'label': 'reserved', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'enabled', 'label': 'enabled', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    ]
}


EI_SECTION_GROUP = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 3, 'text_presentation': str.title},  # noqa
    {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa
    {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
]


EI_DINING_AREA = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 3, 'text_presentation': str.title},  # noqa
    {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa
    {'key': 'smoking_zone', 'label': 'smoking zone', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'outdoor_seating', 'label': 'outdoor seating', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa  
    {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa  
]
