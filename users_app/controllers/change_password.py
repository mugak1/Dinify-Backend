"""
implementation to change user password
"""
from users_app.models import User
from dinify_backend.configs import MESSAGES


def change_password(user_id, old_password, new_password):
    """
    change a user's password
    """
    # check if the user exists
    if not User.objects.filter(id=user_id).exists():
        return {
            'status': 400,
            'message': MESSAGES.get('NO_USER_FOUND'),
        }
    user = User.objects.get(id=user_id)
    if not user.check_password(old_password):
        # TODO save the action

        return {
            'status': 400,
            'message': MESSAGES.get('WRONG_PASSWORD'),
        }
    user.set_password(new_password)
    user.prompt_password_change = False
    user.save()

    # TODO save the action

    # TODO send an email

    return {
        'status': 200,
        'message': MESSAGES.get('OK_PASSWORD_CHANGE'),
    }
