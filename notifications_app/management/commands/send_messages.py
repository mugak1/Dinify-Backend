import logging

from django.core.management.base import BaseCommand
from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS

logger = logging.getLogger(__name__)
from notifications_app.controllers.messenger import Messenger
from restaurants_app.models import Restaurant
from restaurants_app.controllers.lifecycle_policy import grants_portal_access


class Command(BaseCommand):
    help = """
    - Send emails to the respective recipients
    """

    def handle(self, *args, **options):
        # find notifications where the sent attribute is missing.
        #
        # The iteration belongs inside the guard, not just `find()`. pymongo's
        # find() returns a lazy cursor and performs no I/O; the query runs when
        # the cursor is first iterated. With only find() guarded, a server the
        # client could name but not reach raised ServerSelectionTimeoutError one
        # statement later, out of handle(). Wherever the query fails, the
        # outcome is the same: log, send nothing, mark nothing, return. Every
        # pending notification stays pending for the next run.
        try:
            notifications = list(
                MONGO_DB[COL_NOTIFICATIONS].find({"sent": {"$exists": False}})
            )
        except Exception as e:
            logger.error("Failed to query pending notifications from MongoDB: %s", e)
            return
        print('\n=== Sending emails ===\n')

        for x in notifications:
            # if the subject is user credentials, check if the user has aany restaurant
            # if the user is attached to a restaurant, check if it is active
            if x['subject'] == 'Dinify Credentials!':
                owner = x['tos']
                user_restaurants = Restaurant.objects.filter(owner__email=owner).order_by('time_created')  # noqa
                if user_restaurants.count() > 0:
                    restaurant = user_restaurants.first()
                    # Hold credentials until the owner can actually sign in. That
                    # is the portal-access set (onboarding + live), NOT "live" —
                    # under the PR-5 lifecycle an onboarding owner is expected to
                    # log in and build their menu before going live, so gating on
                    # `live` alone would strand them without a password.
                    if not grants_portal_access(restaurant.status):
                        print('Restaurant cannot yet be signed in to')
                        continue

            # Log the truth of each send. Control flow is deliberately
            # unchanged — the record is still marked sent below regardless
            # (the sent-flag-on-failure bug is a separate, out-of-scope fix).
            email_ok = Messenger().send_email(
                to=x['tos'],
                cc=x['ccs'],
                subject=x['subject'],
                message=x['email']
            )
            if not email_ok:
                logger.error(
                    "Drain: email send FAILED (subject=%r, notification=%s)",
                    x['subject'], x.get('_id')
                )
                self.stderr.write(f"Email send failed: {x['subject']} ({x.get('_id')})")

            if x['sms'] is not None:
                if x['subject'] == 'Dinify Credentials!':
                    sms_ok = Messenger().send_sms(
                        msisdn=x['msisdn'],
                        message=x['sms']
                    )
                    if not sms_ok:
                        logger.error(
                            "Drain: credentials SMS send FAILED (notification=%s)",
                            x.get('_id')
                        )
                        self.stderr.write(f"SMS send failed: {x.get('_id')}")

            # update the sent attribute to True
            try:
                MONGO_DB[COL_NOTIFICATIONS].update_one(
                    {"_id": x['_id']},
                    {"$set": {"sent": True}}
                )
            except Exception as e:
                logger.error("Failed to mark notification %s as sent: %s", x.get('_id'), e)
                continue
