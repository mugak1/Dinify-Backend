from typing import Optional
from django.db import transaction

from restaurants_app.models import (
    Restaurant,
    MenuSection,
    SectionGroup,
    MenuItem,
)
from misc_app.controllers.save_action_log import save_action
from dinify_backend.configss.string_definitions import MODULE_MENU
from users_app.models import User
from users_app.controllers.permissions_check import (
    can_user_access_module,
    is_restaurant_owner
)

SUBMITTED_IT_YOURSELF = 'Sorry, you cannot approve a menu that you submitted.'
SUBMITTER_NOT_ON_RECORD = (
    'Sorry, only the restaurant owner can approve this menu, '
    'because the person who submitted it is not on record.'
)


def _submitter_refusal(
    restaurant: Restaurant,
    user: User,
    restaurant_id: str,
) -> Optional[str]:
    """
    Whoever submitted the menu for approval may not approve it, unless they are
    the restaurant owner. Returns the refusal message, or None to proceed.

    The submitter is read from `Restaurant.first_time_menu_submitted_by`, which
    the submit decision writes. It used to be looked up in the MongoDB action
    log, and that lookup never found anything: it asked for `affected_model`,
    `affected_record` and `user_id` where `save_action` stores `model`, `record`
    and `user.id`, and it pinned `action` to the approval's own decision while
    looking for `'submit'`. The log is also the wrong record to decide on.
    `save_action` writes it from a daemon thread and swallows a failure, so an
    entry can be missing for good. And a check that reads it depends on MongoDB
    at decision time: the old one proceeded with no submitter when the client
    could not be built, and raised out of the approval when the server could not
    be reached.

    A menu awaiting approval with no recorded submitter fails CLOSED: only the
    owner may approve it. That is every submission made before the column
    existed, and any whose submitter's account was since deleted. With no record
    of who submitted it, no other member can be shown not to be the submitter.

    A menu that was never submitted is not governed here. Approval has never
    required a submission, and this does not add that requirement.
    """
    submitter_id = restaurant.first_time_menu_submitted_by_id
    if submitter_id is not None:
        refusal = SUBMITTED_IT_YOURSELF if submitter_id == user.pk else None
    elif restaurant.first_time_menu_approval_decision == 'submit':
        refusal = SUBMITTER_NOT_ON_RECORD
    else:
        refusal = None

    # The owner is the one principal allowed to self-approve: it is their
    # restaurant, and separation of duties within a tenant cannot bind the
    # tenant's own principal.
    if refusal is not None and is_restaurant_owner(user, restaurant_id):
        return None
    return refusal


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

    # check that the user has the necessary rights — the `menu` module at this
    # restaurant. There is no platform short-circuit: the resolver grants only on
    # ownership or the role grid.
    if not can_user_access_module(user, restaurant_id, MODULE_MENU):
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

    # The exact set of columns this decision is entitled to write; see the
    # `save(update_fields=...)` call below for why it is built rather than
    # assumed.
    approval_columns = []

    with transaction.atomic():
        message = 'The restaurant menu has been submitted.'
        if approval_decision in ['approve', 'submit']:
            # flag the restaurant detail to indicate that a first time memenu approval has been done
            if approval_decision == 'submit':
                # print(f"the current approval decision is {restaurant.first_time_menu_approval_decision}")
                if not restaurant.first_time_menu_approval_decision == 'pending':
                    return {
                        'status': 400,
                        'message': 'Sorry, the restaurant menu has already been submitted.'
                    }
                # Record who submitted, in the same narrow save as the decision.
                # The approval reads it back from this row (_submitter_refusal).
                restaurant.first_time_menu_submitted_by = user
                approval_columns.append('first_time_menu_submitted_by')

            if approval_decision == 'approve':
                message = 'The restaurant menu has been approved.'

                # user should not approve a menu that they created
                if first_menu_section.created_by == auth.get('user_id'):
                    # The owner is the one principal allowed to self-approve: it is
                    # their restaurant, and separation of duties within a tenant
                    # cannot bind the tenant's own principal. The Dinify-superuser
                    # exemption that used to sit alongside it is gone — a role
                    # string in User.roles is no longer an authority anywhere on
                    # this plane.
                    if not is_restaurant_owner(user, restaurant_id):
                        return {
                            'status': 400,
                            'message': 'Sorry, you cannot approve a menu that you created.'
                        }

                # user who submitted menu for approval should not approve,
                # except for the restaurant owner
                refusal = _submitter_refusal(restaurant, user, restaurant_id)
                if refusal is not None:
                    return {
                        'status': 400,
                        'message': refusal
                    }

                restaurant.first_time_menu_approval = True
                approval_columns.append('first_time_menu_approval')
                # bulk update the menu sections
                sections = MenuSection.objects.filter(restaurant=restaurant)
                sections.update(approved=True, enabled=True)

                # bulk update the section groups
                groups = SectionGroup.objects.filter(section__restaurant=restaurant)
                groups.update(approved=True, enabled=True)

                # bulk update the menu items
                items = MenuItem.objects.filter(section__restaurant=restaurant)
                items.update(approved=True, enabled=True)

            restaurant.first_time_menu_approval_decision = approval_decision
            approval_columns.append('first_time_menu_approval_decision')
            # ONLY THE COLUMNS THIS DECISION OWNS (D06). It used to be a bare
            # `restaurant.save()`, which writes EVERY field from an instance
            # loaded before the transaction opened — so a menu approval silently
            # reverted whatever else had been committed to that restaurant in the
            # meantime. Two of those reverts are serious:
            #
            #   * `accepting_orders`. An owner pausing ordering mid-service had
            #     the pause undone by a manager approving the menu, with nothing
            #     reported to either of them. Since D06 both order boundaries
            #     enforce that pause, so reverting it silently resumes trading.
            #   * `status`. The commercial lifecycle has exactly ONE writer
            #     (`transition_restaurant`), enforced by keeping the field out of
            #     EDIT_INFORMATION and read-only on the serializer — and a
            #     full-row save walked straight past both walls, restoring a
            #     state an administrator had deliberately left.
            #
            # NARROWING THE WRITE IS THE FIX HERE, NOT A LOCK. A writer that only
            # writes what it decided cannot revert anything, whether or not it is
            # serialized against the writer it used to trample. (A lock was first
            # ruled out because this block held its transaction across a MongoDB
            # query, the submitter lookup, which would have stalled every order at
            # the restaurant behind a remote call: the PR #306 lesson. That query
            # is gone. The submitter is read off this row, which is loaded before
            # the transaction and without a lock, so two decisions on one
            # restaurant are still not serialized against each other.)
            restaurant.save(update_fields=approval_columns)

            save_action(
                affected_model='restaurant-menu-approval',
                affected_record=restaurant_id,
                action=approval_decision,
                narration=f'{approval_decision} the restaurant menu',
                result='success',
                user_id=auth.get('user_id'),
                username=auth.get('username'),
                submitted_data={'decision': approval_decision, 'reason': rejection_reason},
                changes=None,
                filter_information=None
            )

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
