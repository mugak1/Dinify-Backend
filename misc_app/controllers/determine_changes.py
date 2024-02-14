"""
implementation to determine differences in the data
"""
from dinify_backend.configs import IGNORE_LOG_FIELDS, STRINGIFY_LOG_FIELDS


def determine_changes(args):
    """
    - Define the changes that have been made
    - `{old_info: dict, new_info: dict, consider: list }`
    """
    # define the changes made
    changes = []

    # loop through the new data
    # and check against the old data for what has changed
    for key, value in args.get('new_info').items():
        if key not in IGNORE_LOG_FIELDS and key not in STRINGIFY_LOG_FIELDS:
            if key in args.get('consider'):
                if value != args.get('old_info').get(key):
                    changes.append({
                        'field': key,
                        'old_value': args.get('old_info').get(key),
                        'new_value': value
                    })
    return changes
