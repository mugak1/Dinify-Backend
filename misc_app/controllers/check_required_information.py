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
    """
    provided_information = dict(provided_information)
    for attribute in required_information:
        key = attribute.get('key')
        label = attribute.get('label')
        minimum_length = attribute.get('min_length')
        if key not in provided_information.keys():
            return {
                "status": False,
                "message": f"{label} is required"
            }

        if len(provided_information[key].strip()) < minimum_length:
            return {
                "status": False,
                "message": f"{label} must be at least {minimum_length} characters long"
            }

    return {
        "status": True,
    }
