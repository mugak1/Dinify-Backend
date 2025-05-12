RECORDS_NON_UNIQUE_COMBINATIONS = {
    'employees': {
        'unique_combination': ['user', 'restaurant'],
        'error_message': 'This user is already associated with this restaurant.'
    },
    'menusections': {
        'unique_combination': ['restaurant', 'name'],
        'error_message': 'A menu section with this name already exists for this restaurant.'
    },
    'sectiongroups': {
        'unique_combination': ['section', 'name'],
        'error_message': 'A section group with this name already exists for this section.'
    },
    'menuitems': {
        'unique_combination': ['section', 'name'],
        'error_message': 'A menu item with this name already exists for this section.'
    },
    'diningareas': {
        'unique_combination': ['restaurant', 'name'],
        'error_message': 'A dining area with this name already exists for this restaurant.'
    }
}
