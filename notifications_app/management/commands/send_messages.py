
from django.core.management.base import BaseCommand
from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS
from notifications_app.controllers.messenger import Messenger


class Command(BaseCommand):
    help = """
    - Send emails to the respective recipients
    """

    def handle(self, *args, **options):
        # find notifications where the sent attribute is missing
        notifications = MONGO_DB[COL_NOTIFICATIONS].find({"sent": {"$exists": False}})
        print('\n=== Sending emails ===\n')
        # print(list(notifications))

        notifications = list(notifications)
        for x in notifications:
            Messenger().send_email(
                to=x['tos'],
                cc=x['ccs'],
                subject=x['subject'],
                message=x['email']
            )

            # if the x['sms'] is not None, send the sms
            if x['sms']:
                Messenger().send_sms(
                    msisdn=x['msisdn'],
                    message=x['sms']
                )

            # update the sent attribute to True
            MONGO_DB[COL_NOTIFICATIONS].update_one(
                {"_id": x['_id']},
                {"$set": {"sent": True}}
            )
