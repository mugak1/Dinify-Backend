EDIT_INFORMATION = {
    'restaurants': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'location', 'label': 'location', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'logo', 'label': 'logo', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'cover_photo', 'label': 'cover photo', 'type': 'file', 'min_length': 5, 'text_presentation': None},  # noqa
        # `status` (the commercial lifecycle) is DELIBERATELY ABSENT (PR-5). It is
        # written only by restaurants_app.controllers.lifecycle, through the
        # elevation-gated admin transition endpoint, and is read_only on
        # SerializerPutRestaurant. Secretary builds its update payload solely from
        # the keys listed here, so omitting it is the enforcement, not a comment.
        # Re-adding it would restore a generic edit path around the matrix, the
        # reason requirement and the audit entry — and would resurrect the
        # `min_length: 5` trap that made the four-character state `live` unwritable.
        {'key': 'require_order_prepayments', 'label': 'require order prepayments', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'expose_order_ratings', 'label': 'expose order ratings', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'allow_deliveries', 'label': 'allow deliveries', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'allow_pickups', 'label': 'allow pickups', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'preferred_subscription_method', 'label': 'preferred subscription method', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        {'key': 'order_surcharge_percentage', 'label': 'order surcharge percentage', 'type': 'decimal', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'order_surcharge_min_amount', 'label': 'min order surcharge amount', 'type': 'decimal', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'order_surcharge_cap_amount', 'label': 'max order surcharge amount', 'type': 'decimal', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'flat_fee', 'label': 'flat fee', 'type': 'decimal', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'branding_configuration', 'label': 'branding configuration', 'type': 'dict', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'preset_tags', 'label': 'preset tags', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'country', 'label': 'country', 'type': 'char', 'min_length': 2, 'text_presentation': None},  # noqa
        {'key': 'contact_phone', 'label': 'contact phone', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'contact_email', 'label': 'contact email', 'type': 'char', 'min_length': 0, 'text_presentation': str.lower},  # noqa
        {'key': 'landmark', 'label': 'landmark', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'tagline', 'label': 'tagline', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'cuisine_types', 'label': 'cuisine types', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'socials', 'label': 'socials', 'type': 'dict', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'accepting_orders', 'label': 'accepting orders', 'type': 'bool', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'opening_hours', 'label': 'opening hours', 'type': 'dict', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'vat_registered', 'label': 'vat registered', 'type': 'bool', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'vat_rate', 'label': 'vat rate', 'type': 'decimal', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'tin', 'label': 'tin', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'receipt_footer', 'label': 'receipt footer', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
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
        {'key': 'availability', 'label': 'availability', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        {'key': 'schedules', 'label': 'schedules', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
    ],
    'menu_item': [
        {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 5, 'text_presentation': str.title},  # noqa
        {'key': 'primary_price', 'label': 'price', 'type': 'float', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'calories', 'label': 'calories', 'type': 'int', 'min_length': 0, 'text_presentation': None},  # noqa
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
        {'key': 'is_featured', 'label': 'is featured', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'is_popular', 'label': 'is popular', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'is_new', 'label': 'is new', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'tag_ids', 'label': 'tag ids', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'in_stock', 'label': 'in stock', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'listing_position', 'label': 'listing position', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'age_restricted', 'label': 'age restricted', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'extras_min_selections', 'label': 'extras min selections', 'type': 'int', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'extras_max_selections', 'label': 'extras max selections', 'type': 'int', 'min_length': 0, 'text_presentation': None},  # noqa
    ],
    'table': [
        {'key': 'dining_area', 'label': 'dining area', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'number', 'label': 'number', 'type': 'int', 'text_presentation': None},  # noqa
        {'key': 'prepayment_required', 'label': 'prepayment required', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'reserved', 'label': 'reserved', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'enabled', 'label': 'enabled', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'display_name', 'label': 'display name', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'min_capacity', 'label': 'min capacity', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'max_capacity', 'label': 'max capacity', 'type': 'int', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'shape', 'label': 'shape', 'type': 'char', 'min_length': 3, 'text_presentation': str.lower},  # noqa
        # Table.status (available/seated/dirty/out_of_service) is a DIFFERENT field
        # from the restaurant lifecycle above — it is the floor-service axis. It is
        # removed here for its own reason: `table-actions/update-status/` is the
        # dedicated writer, and it validates against TABLE_STATUS_CHOICES and keeps
        # `is_active` in step with `out_of_service`. This generic path did neither,
        # so writing status through it silently desynchronised the two.
        {'key': 'tags', 'label': 'tags', 'type': 'list', 'min_length': 0, 'text_presentation': None},  # noqa
        {'key': 'has_qr', 'label': 'has QR', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
        {'key': 'qr_mode', 'label': 'QR mode', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
        # qr_regenerated_at is server-owned (regenerate-qr endpoint) — deliberately
        # NOT editable via the generic table PUT, so an ordinary edit cannot forge a
        # rotation timestamp. It is also read_only on SerializerPutTable.
        {'key': 'floor_x', 'label': 'floor X', 'type': 'float', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'floor_y', 'label': 'floor Y', 'type': 'float', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'floor_width', 'label': 'floor width', 'type': 'float', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'floor_height', 'label': 'floor height', 'type': 'float', 'min_length': 1, 'text_presentation': None},  # noqa
        {'key': 'is_active', 'label': 'is active', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    ]
}


EI_SECTION_GROUP = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 3, 'text_presentation': str.title},  # noqa
    {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa
    {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
]


EI_RESTAURANT_TAG = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 1, 'text_presentation': None},  # noqa
    {'key': 'category', 'label': 'category', 'type': 'char', 'min_length': 5, 'text_presentation': str.lower},  # noqa
    {'key': 'colour', 'label': 'colour', 'type': 'char', 'min_length': 3, 'text_presentation': str.lower},  # noqa
    {'key': 'icon', 'label': 'icon', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
    {'key': 'filterable', 'label': 'filterable', 'type': 'bool', 'min_length': 0, 'text_presentation': None},  # noqa
    {'key': 'display_order', 'label': 'display order', 'type': 'int', 'min_length': 0, 'text_presentation': None},  # noqa
]


EI_DINING_AREA = [
    {'key': 'name', 'label': 'name', 'type': 'char', 'min_length': 3, 'text_presentation': str.title},  # noqa
    {'key': 'description', 'label': 'description', 'type': 'char', 'min_length': 3, 'text_presentation': None},  # noqa
    {'key': 'smoking_zone', 'label': 'smoking zone', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'outdoor_seating', 'label': 'outdoor seating', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa  
    {'key': 'available', 'label': 'available', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'is_indoor', 'label': 'is indoor', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'accessible', 'label': 'accessible', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'default_server_section', 'label': 'default server section', 'type': 'char', 'min_length': 0, 'text_presentation': None},  # noqa
    {'key': 'is_active', 'label': 'is active', 'type': 'bool', 'min_length': 5, 'text_presentation': None},  # noqa
]
