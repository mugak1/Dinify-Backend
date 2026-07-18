from typing import Optional
from users_app.models import User
from users_app.serializers import SerPutUserProfile, SerGetUserProfile
from restaurants_app.models import RestaurantEmployee
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    get_user_restaurant_roles
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
)
from misc_app.controllers.secretary import Secretary
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError
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
    user = User.objects.get(id=user_id)

    # Phone number is NOT self-editable here. Canonicalise the submission
    # (256XXXXXXXXX, no '+') and accept it only when it echoes the stored value;
    # any real change is rejected. Phone changes go through the manager path
    # (update_user_profile) with an OTP.
    if phone_number is not None:
        try:
            phone_number = normalise_msisdn(
                phone_number, country=(country or user.country or 'UG')
            )
        except MsisdnError:
            return {
                'status': 400,
                'message': 'Please provide a valid Ugandan phone number.',
            }
        if phone_number != user.phone_number:
            return {
                'status': 400,
                'message': 'Phone number cannot be changed here.',
            }

    # Email is not unique on the model, yet reset_password._resolve_user looks it
    # up with User.objects.get(email=...). Reject a change that would duplicate
    # another user's email, otherwise that user's password reset would 500 with
    # MultipleObjectsReturned.
    if email is not None and email != user.email:
        if User.objects.filter(email=email).exclude(id=user.id).exists():
            return {
                'status': 400,
                'message': 'This email is already in use.',
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
    user.save()

    response = {
        'status': 200,
        'message': 'Your profile has been updated successfully.',
        'data': {
            'profile': SerGetUserProfile(user, many=False).data
        }
    }
    return response


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
        # Canonicalise before comparing to the stored value and before it flows
        # into put_data (phone_number + username) for Secretary.update().
        try:
            phone_number = normalise_msisdn(
                phone_number, country=(country or user_profile.country or 'UG')
            )
        except MsisdnError:
            return {
                'status': 400,
                'message': 'Please provide a valid Ugandan phone number.',
            }
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
        'phone_number': phone_number,
        'username': phone_number
    }
    # remove None values
    put_data = {k: v for k, v in put_data.items() if v is not None}

    edit_information = [
        {'key': 'country', 'label': 'Country'},
        {'key': 'first_name', 'label': 'First Name'},
        {'key': 'last_name', 'label': 'Last Name'},
        {'key': 'other_names', 'label': 'Other Names'},
        {'key': 'email', 'label': 'Email'},
        {'key': 'phone_number', 'label': 'Phone Number'},
        {'key': 'username', 'label': 'Username'}
    ]

    secretary_args = {
        'serializer': SerPutUserProfile,
        'data': put_data,
        'edit_considerations': edit_information,
        'user_id': str(actor.id),
        'username': str(actor.username),
        'success_message': 'The user profile has been updated successfully.',
        'error_message': 'Sorry, an error occurred while updating the user profile.',
        'user': actor,
        # User is platform-global, so an unrestricted lookup must still be an
        # EXPLICIT caller decision: scope Secretary to exactly the target user
        # resolved after the manager/admin permission check above.
        'instance_queryset': User.objects.filter(pk=put_data['id']),
    }

    secretary_response = Secretary(secretary_args).update()
    secretary_response['data'] = {}

    user_object = User.objects.get(id=put_data['id'])
    secretary_response['data']['profile'] = SerGetUserProfile(user_object, many=False).data
    return secretary_response
