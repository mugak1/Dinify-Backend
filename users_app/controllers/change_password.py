"""
implementation to change user password
"""
from users_app.models import User
from dinify_backend.configs import ACTION_LOG_STATUSES
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.save_action_log import save_action


def change_password(user_id, old_password, new_password):
    """
    change a user's password
    """
    # check if the user exists
    if not User.objects.filter(id=user_id).exists():
        save_action(
            affected_model='User',
            affected_record=None,
            action='change-password',
            narration=MESSAGES.get('NO_USER_FOUND'),
            result=ACTION_LOG_STATUSES.get('failed'),
            user_id=None,
            username=None,
            submitted_data={'user_id': user_id},
            changes=None,
            filter_information=None
        )
        return {
            'status': 400,
            'message': MESSAGES.get('NO_USER_FOUND'),
        }
    user = User.objects.get(id=user_id)
    if not user.check_password(old_password):
        # save the action
        save_action(
            affected_model='User',
            affected_record=user_id,
            action='change-password',
            narration=MESSAGES.get('WRONG_PASSWORD'),
            result=ACTION_LOG_STATUSES.get('failed'),
            user_id=None,
            username=None,
            submitted_data={'user_id': user_id},
            changes=None,
            filter_information=None
        )
        return {
            'status': 400,
            'message': MESSAGES.get('WRONG_PASSWORD'),
        }
    user.set_password(new_password)
    user.prompt_password_change = False
    user.save()

    # TODO save the action
    save_action(
        affected_model='User',
        affected_record=user_id,
        action='change-password',
        narration=MESSAGES.get('OK_PASSWORD_CHANGE'),
        result=ACTION_LOG_STATUSES.get('success'),
        user_id=None,
        username=None,
        submitted_data={'user_id': user_id},
        changes=None,
        filter_information=None
    )

    # TODO send an email

    return {
        'status': 200,
        'message': MESSAGES.get('OK_PASSWORD_CHANGE'),
    }
