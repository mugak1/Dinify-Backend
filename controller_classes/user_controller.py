"""
The controller class for user related logic
"""
from users_app.controllers.create_user import self_register


class UserController:
    """
    the controller class for user related logic
    """
    def __init__(self, data):
        self.data = data

    def self_register(self):
        """
        Handle user self registration
        """
        return self_register(self.data)
