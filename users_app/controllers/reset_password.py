"""
implementation to reset a user's password
"""
from users_app.models import User
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action


def reset_password(username):
    """
    reset a user's password
    """
    # check if the user exists
    if not User.objects.filter(username=username).exists():
        save_action(
            affected_model='User',
            affected_record=None,
            action='reset-password',
            narration=MESSAGES.get('NO_PHONE_NUMBER'),
            result=ACTION_LOG_STATUSES.get('failed'),
            user_id=None,
            username=username,
            submitted_data={'username': username},
            changes=None,
            filter_information=None
        )
        return {
            'status': 400,
            'message': MESSAGES.get('NO_PHONE_NUMBER')
        }

    user = User.objects.get(username=username)
    password = User.objects.make_random_password()
    password = 'password'  # for testing purposes since emails are not sent out
    user.set_password(password)
    user.prompt_password_change = True
    user.save()

    # save the action performed
    save_action(
        affected_model='User',
        affected_record=str(user.id),
        action='reset-password',
        narration=MESSAGES.get('OK_PASSWORD_RESET'),
        result=ACTION_LOG_STATUSES.get('success'),
        user_id=None,
        username=username,
        submitted_data={'username': username},
        changes=None,
        filter_information=None
    )

    #  TODO send out the email with the new password

    return {
        'status': 200,
        'message': MESSAGES.get('OK_PASSWORD_RESET'),
    }
