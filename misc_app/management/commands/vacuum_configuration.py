from restaurants_app.models import (
    Restaurant, MenuSection, SectionGroup,
    MenuItem, DiningArea, Table
)

VACUUM_MODELS = [
    {
        'model': Restaurant,
        'unique_fields': [{
            'name': 'name__icontains',
            'location': 'location',
            'owner': 'owner'
        }],
        'rename_field': 'name'
    },
    {
        'model': MenuSection,
        'unique_fields': [{
            'name': 'name__icontains',
            'restaurant': 'restaurant'
        }],
        'rename_field': 'name'
    },
    {
        'model': SectionGroup,
        'unique_fields': [{
            'name': 'name__icontains',
            'section': 'section'
        }],
        'rename_field': 'name'
    },
    {
        'model': MenuItem,
        'unique_fields': [{
            'name': 'name__icontains',
            'section': 'section'
        }],
        'rename_field': 'name'
    },
    {
        'model': DiningArea,
        'unique_fields': [{
            'name': 'name__icontains',
            'restaurant': 'restaurant'
        }],
        'rename_field': 'name'
    },
    {
        'model': Table,
        'unique_fields': [{
            'number': 'number',
            'str_number': 'str_number',
            'restaurant': 'restaurant'
        }],
        'rename_field': 'str_number'
    }
]
