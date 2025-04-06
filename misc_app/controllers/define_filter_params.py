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
    'section': 'section',
    'name': 'name__icontains',
    'running_discount': 'running_discount',
    'description': 'description__icontains',
    'is_extra': 'is_extra'
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

ORDER_FILTERS = {
    'restaurant': 'restaurant',
    'table': 'table',
    'customer_phone': 'customer_phone__icontains',
    'customer_email': 'customer_email__icontains',
    'min_actual_cost': 'actual_cost__gte',
    'max_actual_cost': 'actual_cost__lte',
    'order_status': 'order_status',
    'payment_status': 'payment_status'
}

ORDERREVIEWS_FILTERS = {
    'restaurant': 'restaurant',
    'from': 'time_created__gte',
    'to': 'time_created__lte',
}

ORDERITEMREVIEWS_FILTERS = {
    'restaurant': 'order__restaurant',
    'order': 'order',
    'item': 'item',
    'from': 'time_created__gte',
    'to': 'time_created__lte',
}


SERVICE_TICKET_FILTERS = {
    'ticket_type': 'ticket_type',
    'ticket_title': 'ticket_title__icontains',
    'ticket_description': 'ticket_description__icontains',
    'ticket_status': 'ticket_status',
    'ticket_priority': 'ticket_priority',
    'restaurant': 'restaurant',
    'assigned_to': 'assigned_to',
    'assigned_by': 'assigned_by',
    'resolution_notes': 'resolution_notes__icontains',
}

DINING_AREA_FILTERS = {
    'name': 'name__icontains',
    'outdoor_seating': 'outdoor_seating',
    'smoking_zone': 'smoking_zone',
    'restaurant': 'restaurant'
}

FILTER_DEFINITIONS = {
    'restaurants': RESTAURANT_FILTERS,
    'employees': EMPLOYEE_FILTERS,
    'menusections': MENU_SECTIION_FILTERS,
    'sectiongroups': GROUP_FILTERS,
    'menuitems': MENU_ITEM_FILTERS,
    'tables': TABLE_FILTERS,
    'orders': ORDER_FILTERS,
    'orderreviews': ORDERREVIEWS_FILTERS,
    'orderitemreviews': ORDERITEMREVIEWS_FILTERS,
    'servicetickets': SERVICE_TICKET_FILTERS,
    'diningareas': DINING_AREA_FILTERS
}


def define_filter_params(get_params, model) -> dict:
    """
    defines the parameters to consider for the filter
    """
    filter_params = {}

    # define the filter considerations
    for key, value in get_params.items():
        # skip the pagination details
        if key in ['page', 'page_size']:
            continue
        # check if the length is greater than 1
        if len(value) > 1:
            filter_considerations = FILTER_DEFINITIONS.get(model)
            filter_params[
                filter_considerations[key.lower()]
            ] = value
    # for reviews, exclude where the details are not available
    if model in ['orderreviews', 'orderitemreviews']:
        filter_params['review__isnull'] = False
    return filter_params
