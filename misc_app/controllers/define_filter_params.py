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
}

FILTER_DEFINITIONS = {
    'restaurants': RESTAURANT_FILTERS,
    'employees': EMPLOYEE_FILTERS,

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

    return filter_params
