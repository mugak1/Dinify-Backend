
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
                subject=x['message']['subject'],
                message=x['message']['email']
            )
