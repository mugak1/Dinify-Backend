"""
check that all required details are provided
"""


def check_required_information(
    required_information: list,
    provided_information: dict
) -> dict:
    """
    - Compare the provided information to the required information
    - If any key is missing or not meeting the minimum length, return a message
    - Type-aware: lists and bools skip the length check; numeric types are
      stringified before the length check; strings get stripped first.
    """
    provided_information = dict(provided_information)
    for attribute in required_information:
        key = attribute.get('key')
        attribute_type = attribute.get('type')
        label = attribute.get('label')
        minimum_length = attribute.get('min_length', 0)

        if key not in provided_information:
            return {
                "status": False,
                "message": f"{label} is required"
            }

        value = provided_information[key]

        # Skip validation for list types — they don't have a meaningful "length" check
        if attribute_type == 'list':
            continue

        # Skip validation for bool types — presence is sufficient
        if attribute_type == 'bool':
            continue

        # For numeric types, convert to string for length check
        if attribute_type in ('float', 'int', 'decimal'):
            str_value = str(value).strip() if value is not None else ''
        elif isinstance(value, str):
            str_value = value.strip()
        else:
            str_value = str(value).strip() if value is not None else ''

        if len(str_value) < minimum_length:
            return {
                "status": False,
                "message": f"{label} must be at least {minimum_length} characters long"
            }

    return {
        "status": True,
    }
