from typing import Optional
from users_app.models import User
from restaurants_app.models import RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN
)
from dinify_backend.mongo_db import COL_PROFILE_UPDATE_APPROVALS
from misc_app.controllers.save_to_mongo import save_to_mongodb


def self_update_user_profile(
    user_id: str,
    country: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    other_names: Optional[str] = None,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> dict:
    """
    Update the user profile
    """
    require_approval = False
    # check if the user has dinify or restaurant roles
    user = User.objects.get(id=user_id)
    res_roles = RestaurantEmployee.objects.filter(
        user=user_id,
        deleted=False
    )

    dinify_roles = [DINIFY_ACCOUNT_MANAGER, DINIFY_ADMIN]
    if any(role in dinify_roles for role in user.roles):
        require_approval = True

    if res_roles.count() > 0:
        require_approval = True

    if require_approval:
        return {
            'status': 200,
            'message': 'Kindly refer to your manager to update the profile.'
        }

        profile_changes = []
        if country is not None:
            if user.country != country:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'country',
                    'old': user.country,
                    'new': country
                })
        if first_name is not None:
            if user.first_name != first_name:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'first_name',
                    'old': user.first_name,
                    'new': first_name
                })
        if last_name is not None:
            if user.last_name != last_name:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'last_name',
                    'old': user.last_name,
                    'new': last_name
                })
        if other_names is not None:
            if user.other_names != other_names:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'other_names',
                    'old': user.other_names,
                    'new': other_names
                })
        if email is not None:
            if user.email != email:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'email',
                    'old': user.email,
                    'new': email
                })
        if phone_number is not None:
            if user.phone_number != phone_number:
                profile_changes.append({
                    'user_id': str(user_id),
                    'detail': 'phone_number',
                    'old': user.phone_number,
                    'new': phone_number
                })

        # TODO replace with batch insert
        # which should still consider the inclusion of record creation time
        if len(profile_changes) > 0:
            for profile_change in profile_changes:
                save_to_mongodb(
                    collection=COL_PROFILE_UPDATE_APPROVALS,
                    data=profile_change
                )
        return {
            'status': 200,
            'message': 'Your changes have been saved. However, your profile update requires approval.'
        }

    if country is not None:
        user.country = country
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if other_names is not None:
        user.other_names = other_names
    if email is not None:
        user.email = email
    if phone_number is not None:
        user.phone_number = phone_number
    user.save()

    return {
        'status': 200,
        'message': 'Your profile has been updated successfully.'
    }


# def update_user_profile(
    
# )
