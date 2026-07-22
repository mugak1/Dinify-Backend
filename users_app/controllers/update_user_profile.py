from typing import Optional
from users_app.models import User
from users_app.serializers import SerGetUserProfile
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError


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
    # any real change is rejected. There is no self-service path to change a
    # phone number (the manager-OTP path was retired).
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
