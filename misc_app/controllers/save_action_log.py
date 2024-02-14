"""
saves an action that a user has performed
"""
from django.utils import timezone
from dinify_backend.mongo_db import MONGO_DB, ACTON_LOGS

day_names = [
    'Mon', 'Tue', 'Wed',
    'Thu', 'Fri', 'Sat',
    'Sun'
]


def make_detailed_time() -> dict:
    """
    - Return the detailed time to consider
    """
    right_now = timezone.now()
    time_detail = {}
    time_detail['date'] = right_now.day
    time_detail['month'] = right_now.month
    time_detail['year'] = right_now.year
    time_detail['hour'] = right_now.hour
    time_detail['minute'] = right_now.minute
    day = right_now.weekday()
    time_detail['day'] = day_names[day]
    time_detail['timestamp'] = right_now
    time_detail['epoch'] = right_now.timestamp()
    return time_detail


def save_action(
        action_data: dict
 ) -> bool:
    """
    - Saves an action that a user has performed
    """
    # create an object to save to mongodb
    # include the time details
    time_detail = make_detailed_time()
    action_details = action_data

    action_details['timestamp'] = time_detail
    # save to mongodb
    MONGO_DB[ACTON_LOGS].insert_one(action_details)
