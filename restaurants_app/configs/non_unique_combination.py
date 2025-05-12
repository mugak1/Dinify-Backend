RECORDS_NON_UNIQUE_COMBINATIONS = {
    'employees': {
        'unique_combination': ['user', 'restaurant'],
        'error_message': 'This user is already associated with this restaurant.',
        'fks': ['user', 'restaurant']
    },
    'menusections': {
        'unique_combination': ['restaurant', 'name'],
        'error_message': 'A menu section with this name already exists for this restaurant.',
        'fks': ['restaurant']
    },
    'sectiongroups': {
        'unique_combination': ['section', 'name'],
        'error_message': 'A section group with this name already exists for this section.',
        'fks': ['section']
    },
    'menuitems': {
        'unique_combination': ['section', 'name'],
        'error_message': 'A menu item with this name already exists for this section.',
        'fks': ['section']
    },
    'diningareas': {
        'unique_combination': ['restaurant', 'name'],
        'error_message': 'A dining area with this name already exists for this restaurant.',
        'fks': ['restaurant']
    }
}
