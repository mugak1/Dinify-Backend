from dataclasses import dataclass
from misc_app.controllers.notifications.message_builder import build_messages


class Notification:
    def __init__(self, msg_data: dict):
        self.msg_data = msg_data
    # msg_data: dict

    def create_notification(self):
        message = build_messages(self.msg_data)
        print(message)
