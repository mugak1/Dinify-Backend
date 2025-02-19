
from django.core.management.base import BaseCommand
from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS
from notifications_app.controllers.messenger import Messenger
from restaurants_app.models import Restaurant


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
            # if the subject is user credentials, check if the user has aany restaurant
            # if the user is attached to a restaurant, check if it is active
            if x['subject'] == 'Dinify Credentials!':
                owner = x['tos'][0]
                print(owner)
                user_restaurants = Restaurant.objects.filter(owner__email=owner).order_by('time_created')  # noqa
                print(user_restaurants)
                if user_restaurants.count() > 0:
                    restaurant = user_restaurants.first()
                    if restaurant.status != 'active':
                        continue

            Messenger().send_email(
                to=x['tos'],
                cc=x['ccs'],
                subject=x['subject'],
                message=x['email']
            )

            if x['sms'] is not None:
                if x['subject'] == 'Dinify Credentials!':
                    Messenger().send_sms(
                        msisdn=x['msisdn'],
                        message=x['sms']
                    )

            # update the sent attribute to True
            MONGO_DB[COL_NOTIFICATIONS].update_one(
                {"_id": x['_id']},
                {"$set": {"sent": True}}
            )
