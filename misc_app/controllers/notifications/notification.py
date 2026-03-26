import logging

logger = logging.getLogger(__name__)

from misc_app.controllers.notifications.message_builder import build_messages
from misc_app.controllers.notifications.determine_recipients import determine_receipients
from misc_app.controllers.save_to_mongo import save_to_mongodb
from dinify_backend.mongo_db import COL_NOTIFICATIONS
from notifications_app.controllers.messenger import Messenger
# from payment_integrations_app.controllers.yo_integrations import YoIntegration


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

        save_to_mongodb(collection=COL_NOTIFICATIONS, data=message_data)

        try:
            # if the sms is not None, send it inline
            if message_data['sms'] is not None:
                if message_data['subject'] != 'Dinify Credentials!':
                    Messenger().send_sms(
                        msisdn=message_data['msisdn'],
                        message=message_data['sms']
                    )
                    # YoIntegration().send_sms(
                    #     to=message_data['msisdn'],
                    #     message=message_data['sms']
                    # )
        except Exception as error:
            logger.error("Error sending sms: %s", error)
