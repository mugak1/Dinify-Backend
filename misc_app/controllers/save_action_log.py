"""
saves an action that a user has performed
"""
import logging
import threading
from django.utils import timezone
from dinify_backend.mongo_db import MONGO_DB, ACTION_LOGS

logger = logging.getLogger(__name__)

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
    affected_model: str,
    affected_record: str,
    action: str,
    narration: str,
    result: str,
    user_id: str,
    username: str,
    submitted_data: dict,
    changes=None,
    filter_information=None
 ) -> bool:
    """
    - Saves an action that a user has performed
    """
    # if the action is edit and result is success,
    # then ensure that the changes are provided

    # make the action data to consider
    action_data = {
        'model': affected_model,
        'record': affected_record,
        'action': action,
        'narration': narration,
        'result': result,
        'user': {
            'id': user_id,
            'username': username
        },
        'submitted_data': submitted_data,
        'changes': changes,
        'filter_information': filter_information
    }

    # create an object to save to mongodb
    # include the time details
    time_detail = make_detailed_time()
    action_details = action_data

    action_details['timestamp'] = time_detail

    # save to mongodb in background — action logs are fire-and-forget
    def _write_action_log():
        try:
            MONGO_DB[ACTION_LOGS].insert_one(action_details)
        except Exception as e:
            logger.error("Failed to write action log to MongoDB: %s", e)

    threading.Thread(target=_write_action_log, daemon=True).start()
