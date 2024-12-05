from typing import Optional
from users_app.models import User
from users_app.serializers import SerPutUserProfile
from restaurants_app.models import RestaurantEmployee
from dinify_backend.mongo_db import COL_PROFILE_UPDATE_APPROVALS
from misc_app.controllers.save_to_mongo import save_to_mongodb
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    get_user_restaurant_roles
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN
)
from misc_app.controllers.secretary import Secretary
from users_app.controllers.otp_manager import OtpManager


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


def update_user_profile(
    actor: User,
    user_id: str,
    country: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    other_names: Optional[str] = None,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,
    otp: Optional[str] = None
) -> dict:
    # check if the actor has rights to perform the action
    has_permission = False
    if is_dinify_admin(actor):
        has_permission = True

    if not has_permission:
        # get the restaurants to which the user belongs
        res_mapping = RestaurantEmployee.objects.values('restaurant').filter(
            user=user_id,
            active=True,
        )
        restaurant_ids = [str(res['restaurant']) for res in res_mapping]
        for restaurant_id in restaurant_ids:
            roles = get_user_restaurant_roles(
                user_id=str(actor.id),
                restaurant_id=restaurant_id
            )
            restaurant_roles = [RESTAURANT_OWNER, RESTAURANT_MANAGER]
            if len(roles) > 0:
                if any(role in restaurant_roles for role in roles):
                    has_permission = True
                    break

    if not has_permission:
        return {
            'status': 401,
            'message': 'You do not have permission to perform this action.'
        }

    # check if the phone number has changed
    user_profile = User.objects.get(id=user_id)
    if phone_number is not None:
        if user_profile.phone_number != phone_number:
            # check if the otp has been provided
            if otp is None:
                return {
                    'status': 400,
                    'message': 'Please provide the OTP to update the phone number.'
                }
            # verify the otp
            verified_otp = OtpManager().verify_otp(
                user_id=str(actor.id),
                otp=otp
            )

            if not verified_otp['data']['valid']:
                return {
                    'status': 400,
                    'message': 'Invalid OTP.'
                }

    put_data = {
        'id': user_id,
        'country': country,
        'first_name': first_name,
        'last_name': last_name,
        'other_names': other_names,
        'email': email,
        'phone_number': phone_number
    }
    # remove None values
    put_data = {k: v for k, v in put_data.items() if v is not None}

    edit_information = [
        {'key': 'country', 'label': 'Country'},
        {'key': 'first_name', 'label': 'First Name'},
        {'key': 'last_name', 'label': 'Last Name'},
        {'key': 'other_names', 'label': 'Other Names'},
        {'key': 'email', 'label': 'Email'},
        {'key': 'phone_number', 'label': 'Phone Number'}
    ]

    secretary_args = {
        'serializer': SerPutUserProfile,
        'data': put_data,
        'edit_considerations': edit_information,
        'user_id': str(actor.id),
        'username': str(actor.username),
        'success_message': 'The user profile has been updated successfully.',
        'error_message': 'Sorry, an error occurred while updating the user profile.'
    }
    return Secretary(secretary_args).update()
