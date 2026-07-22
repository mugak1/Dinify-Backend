"""
constructs the orm filter
"""

RESTAURANT_FILTERS = {
    'name': 'name__icontains',
    'location': 'location__icontains',
    'owner': 'owner',
    'status': 'status',
}

EMPLOYEE_FILTERS = {
    'restaurant': 'restaurant',
    'user_phone': 'user__phone_number__icontains',
    'user_email': 'user__email__icontains',
    'active': 'active',
}

MENU_SECTIION_FILTERS = {
    'restaurant': 'restaurant',
    'name': 'name__icontains',
}

MENU_ITEM_FILTERS = {
    'restaurant': 'section__restaurant',
    'section': 'section',
    'name': 'name__icontains',
    'running_discount': 'running_discount',
    'description': 'description__icontains',
    'is_extra': 'is_extra',
    'has_extras': 'has_extras',
}

TABLE_FILTERS = {
    'restaurant': 'restaurant',
    'number': 'number',
    'grouping': 'grouping',
}

GROUP_FILTERS = {
    'name': 'name__icontains',
    'description': 'description__icontains',
    'section': 'section'
}

DINING_AREA_FILTERS = {
    'name': 'name__icontains',
    'outdoor_seating': 'outdoor_seating',
    'smoking_zone': 'smoking_zone',
    'restaurant': 'restaurant'
}

SUPPORT_ISSUE_FILTERS = {
    'status': 'status',
    'category': 'category',
    'impact': 'impact',
    'restaurant': 'restaurant',
}

FILTER_DEFINITIONS = {
    'restaurants': RESTAURANT_FILTERS,
    'employees': EMPLOYEE_FILTERS,
    'menusections': MENU_SECTIION_FILTERS,
    'sectiongroups': GROUP_FILTERS,
    'menuitems': MENU_ITEM_FILTERS,
    'tables': TABLE_FILTERS,
    'diningareas': DINING_AREA_FILTERS,
    'supportissues': SUPPORT_ISSUE_FILTERS
}


def define_filter_params(get_params, model) -> dict:
    """
    defines the parameters to consider for the filter
    """
    filter_params = {}
    # resolve the model's filter map once; unknown model -> empty (no filters)
    filter_considerations = FILTER_DEFINITIONS.get(model) or {}

    # define the filter considerations
    for key, value in get_params.items():
        # skip the pagination details
        if key in ['page', 'page_size']:
            continue
        # check if the length is greater than 1
        if len(value) > 1:
            # only apply known filter keys; unknown params are skipped
            mapped = filter_considerations.get(key.lower())
            if mapped is not None:
                filter_params[mapped] = value
    return filter_params
