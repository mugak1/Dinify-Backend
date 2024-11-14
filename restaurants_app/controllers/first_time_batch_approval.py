from typing import Optional
from django.db import transaction
from restaurants_app.models import (
    Restaurant,
    MenuSection,
    SectionGroup,
    MenuItem,
    RestaurantEmployee
)
from misc_app.controllers.save_action_log import save_action
from dinify_backend.configss.string_definitions import(
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER
)
from dinify_backend.mongo_db import ACTON_LOGS
from users_app.models import User
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    is_dinify_superuser,
    is_restaurant_owner
)


def check_permissions(
    restaurant_id: str,
    user_id: str,
) -> dict:
    try:
        employee_record = RestaurantEmployee.objects.get(
            user_id=user_id,
            restaurant_id=restaurant_id
        )
        accepted_roles = [RESTAURANT_OWNER, RESTAURANT_MANAGER]
        if any(role in accepted_roles for role in employee_record.roles):
            return True
        else:
            return False
    except Exception as error:
        print(f'FirstTimeBatchApprovalPermissionError: {error}')
        return False


def first_time_batch_approval(
    restaurant_id: str,
    approval_decision: str,
    auth: dict,
    user: User,
    rejection_reason: Optional[str] = None    
) -> dict:
    """
    handling the first time batch approvals
    """
    response = {
        'status': 400,
        'message': 'Sorry an occurred. Please try again later.'
    }

    # check that the user has the necessary rights
    has_permission = check_permissions(
        restaurant_id=restaurant_id,
        user_id=auth.get('user_id')
    )
    if not has_permission:
        if not is_dinify_admin(user):
            return {
                'status': 401,
                'message': 'You do not have the necessary permissions to perform this action.'
            }

    # check if the person is not the one who created the menu
    # pick a random menu section and check who created it
    first_menu_section = MenuSection.objects.filter(
        restaurant_id=restaurant_id
    ).first()
    if first_menu_section is None:
        return {
            'status': 400,
            'message': 'Sorry, the restaurant does not have any menu sections.'
        }

    # get the restaurant
    restaurant = Restaurant.objects.get(id=restaurant_id)

    if approval_decision not in ['approve', 'reject', 'submit']:
        return {
            'status': 400,
            'message': 'Invalid decision. Please try again.'
        }

    with transaction.atomic():
        message = 'The restaurant menu has been submitted.'
        if approval_decision in ['approve', 'submit']:
            # flag the restaurant detail to indicate that a first time memenu approval has been done
            restaurant.first_time_menu_approval_decision = approval_decision

            if approval_decision == 'submit':
                if not restaurant.first_time_menu_approval_decision == 'pending':
                    return {
                        'status': 400,
                        'message': 'Sorry, the restaurant menu has already been submitted.'
                    }

            if approval_decision == 'approve':
                message = 'The restaurant menu has been approved.'

                # user should not approve a menu that they created
                if first_menu_section.created_by == auth.get('user_id'):
                    # check if the user is not a restaurant owner or dinify admin
                    if not is_dinify_superuser(user) and not is_restaurant_owner(user, restaurant_id):  # noqa
                        return {
                            'status': 400,
                            'message': 'Sorry, you cannot approve a menu that you created.'
                        }

                # user who submitted menu for approval should not approve
                # except for dinify superuser and restaurant owner
                action_logs = ACTON_LOGS.find({
                    'affected_model': 'restaurant-menu-approval',
                    'affected_record': restaurant_id,
                    'action': approval_decision,
                    'result': 'success'
                })

                submitter_id = None
                for log in action_logs:
                    if log.get('action') == 'submit':
                        submitter_id = log.get('user_id')
                        break

                if submitter_id == auth.get('user_id'):
                    if not is_dinify_superuser(user) and not is_restaurant_owner(user, restaurant_id):
                        return {
                            'status': 400,
                            'message': 'Sorry, you cannot approve a menu that you submitted.'
                        }

                restaurant.first_time_menu_approval = True
            restaurant.save()

            # bulk update the menu sections
            sections = MenuSection.objects.filter(restaurant=restaurant)
            sections.update(approved=True, enabled=True)

            # bulk update the section groups
            groups = SectionGroup.objects.filter(section__restaurant=restaurant)
            groups.update(approved=True, enabled=True)

            # bulk update the menu items
            items = MenuItem.objects.filter(section__restaurant=restaurant)
            items.update(approved=True, enabled=True)

            save_action(
                affected_model='restaurant-menu-approval',
                affected_record=restaurant_id,
                action=approval_decision,
                narration=f'{approval_decision} the restaurant menu',
                result='success',
                user_id=auth.get('user_id'),
                username=auth.get('username'),
                changes=None,
                filter_information=None
            )

            message
            response = {
                'status': 200,
                'message': message
            }

        else:
            # check if one has provided a reason
            if rejection_reason is None:
                return {
                    'status': 400,
                    'message': 'Please provide a reason for rejecting the menu.'
                }

            #  log the reason for not accepting the menu
            save_action(
                affected_model='restaurant-menu-approval',
                affected_record=restaurant_id,
                action='Reviewed restaurant menu',
                narration='',
                result='success',
                user_id=auth.get('user_id'),
                username=auth.get('username'),
                submitted_data={
                    'approval_decision': approval_decision,
                    'rejection_reason': rejection_reason,
                },
                changes=None,
                filter_information=None
            )

            # TODO send a notification to the the various personnel who
            # created the menu sections

            response = {
                'status': 200,
                'message': 'Your decision has been acknowledged'
            }
    return response
