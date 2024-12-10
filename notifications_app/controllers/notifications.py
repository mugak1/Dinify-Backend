from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS


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
        print(f"Error while getting notifications: {error}")
        return []


# {
#   "_id": {
#     "$oid": "6757b7b626db88147975bf1f"
#   },
#   "tos": [
#     "me@example.org"
#   ],
#   "ccs": [],
#   "message": {
#     "subject": "New Menu Section",
#     "email": "\n        <p><span style=\"font-weight:400;\">Hello The Name Of The Restaurant,</span></p>\n        <p>&nbsp;</p>\n        <p><span style=\"font-weight:400;\">new last has added a new menu section, Breakfast9.&nbsp;</span></p>\n        <p><span style=\"font-weight:400;\">You can now add items to the section and customers will be able to order for the items accordingly.&nbsp;</span></p>\n    <p>&nbsp;</p>\n    <p><span style=\"font-weight:400;\">Sincerely,&nbsp;</span></p>\n    <p><span style=\"font-weight:400;\">Dinify</span></p>\n    <p><span style=\"font-weight:400;\">Various Links to Dinify Social Media Accounts</span></p>\n\n        ",
#     "sms": null
#   },
#   "subject": "New Menu Section",
#   "email": "\n        <p><span style=\"font-weight:400;\">Hello The Name Of The Restaurant,</span></p>\n        <p>&nbsp;</p>\n        <p><span style=\"font-weight:400;\">new last has added a new menu section, Breakfast9.&nbsp;</span></p>\n        <p><span style=\"font-weight:400;\">You can now add items to the section and customers will be able to order for the items accordingly.&nbsp;</span></p>\n    <p>&nbsp;</p>\n    <p><span style=\"font-weight:400;\">Sincerely,&nbsp;</span></p>\n    <p><span style=\"font-weight:400;\">Dinify</span></p>\n    <p><span style=\"font-weight:400;\">Various Links to Dinify Social Media Accounts</span></p>\n\n        ",
#   "sms": null,
#   "creation_timestamp": {
#     "date": 10,
#     "month": 12,
#     "year": 2024,
#     "hour": 3,
#     "minute": 38,
#     "day": "Tue",
#     "timestamp": {
#       "$date": "2024-12-10T03:38:30.493Z"
#     },
#     "epoch": 1733801910.493494
#   }
# }