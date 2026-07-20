import logging

logger = logging.getLogger(__name__)

from misc_app.controllers.notifications.message_builder import build_messages
from misc_app.controllers.notifications.determine_recipients import determine_receipients
from misc_app.controllers.save_to_mongo import save_to_mongodb
from dinify_backend.mongo_db import COL_NOTIFICATIONS
from notifications_app.controllers.messenger import Messenger


class Notification:
    def __init__(self, msg_data: dict):
        self.msg_data = msg_data
    # msg_data: dict

    def create_notification(self):
        message = build_messages(self.msg_data)
        recipients = determine_receipients(
            message_type=self.msg_data.get('msg_type'),
            restaurant_id=self.msg_data.get('restaurant_id'),
            user_id=self.msg_data.get('user_id')
        )

        message_data = {
            'tos': recipients['tos'],
            'ccs': recipients['ccs'],
            'subject': message['subject'],
            'email': message['email'],
            'sms': message['sms'],
            'msisdn': recipients['msisdn'],
        }

        # Log the truth; control flow unchanged (callers stay fire-and-forget).
        # A False here means the notification was NEVER queued — without the
        # drain re-reading it from Mongo, it will not be delivered.
        stored = save_to_mongodb(collection=COL_NOTIFICATIONS, data=message_data)
        if not stored:
            logger.error(
                "Notification enqueue FAILED (Mongo unavailable): subject=%r was not queued",
                message_data['subject']
            )

        try:
            # if the sms is not None, send it inline
            if message_data['sms'] is not None:
                if message_data['subject'] != 'Dinify Credentials!':
                    sent = Messenger().send_sms(
                        msisdn=message_data['msisdn'],
                        message=message_data['sms']
                    )
                    if not sent:
                        logger.error(
                            "Inline notification SMS FAILED: subject=%r",
                            message_data['subject']
                        )
        except Exception as error:
            logger.error("Error sending sms: %s", error)
