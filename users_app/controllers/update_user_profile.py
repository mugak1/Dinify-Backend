from typing import Optional
from users_app.models import User


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
