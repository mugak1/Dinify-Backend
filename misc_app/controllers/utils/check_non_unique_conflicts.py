from typing import Optional
from django.db.models import Model


def check_non_unique_conflicts(
    model: Model,
    unique_combination: list,
    fks: list,
    values: dict,
    error_message: str,
    existing_record_id: Optional[str] = None,
) -> dict:
    # make the filters
    filters = {}
    for key in unique_combination:
        if values.get(key) is not None:
            if key in fks:
                filters[key] = values.get(key)
            else:
                filters[f"{key}__iexact"] = values.get(key)

    if len(filters) < len(unique_combination):
        return {
            'status': 200,
            'message': 'The combination is not full.'
        }

    filters['deleted'] = False
    if existing_record_id is not None:
        filters['id__ne'] = existing_record_id

    # check if the record exists
    existing_record = model.objects.filter(**filters)
    if existing_record.exists():
        return {
            'status': 400,
            'message': error_message
        }

    return {
        'status': 200,
        'message': 'No conflicts found'
    }
