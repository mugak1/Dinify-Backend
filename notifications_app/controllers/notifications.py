import logging

from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS
from bson import ObjectId

logger = logging.getLogger(__name__)


def get_notifications(
    email: str,
    phone: str,
    skip_read: bool = False,
    skip_archived: bool = True
):
    try:
        # find notifications where the email is incluced in the tos
        # or the phone is included in the tos
        # or the email is included in the ccs
        # or the phone is included in the ccs
        #
        filter = {
            '$or': [
                {'tos': email},
                {'tos': phone},
                {'ccs': email},
                {'ccs': phone}
            ]
        }
        if skip_read:
            filter['read'] = {'$exists': False}
        if skip_archived:
            filter['archived'] = {'$exists': False}

        notifications = MONGO_DB[COL_NOTIFICATIONS].find(filter=filter)

        notifications = list(notifications)

        # Convert ObjectId to string
        for notification in notifications:
            notification['_id'] = str(notification['_id'])

        return notifications

    except Exception as error:
        logger.error("Error while getting notifications: %s", error)
        return []


def flag_notification_as_read(notification_id: str):
    try:
        MONGO_DB[COL_NOTIFICATIONS].update_one(
            filter={'_id': ObjectId(notification_id)},
            update={'$set': {'read': True}}
        )
        return True
    except Exception as error:
        logger.error("Error while flagging notification as read: %s", error)
        return False
