from users_app.models import User
from restaurants_app.models import RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN
)

dinify_roles = [DINIFY_ACCOUNT_MANAGER, DINIFY_ADMIN]


def get_user_restaurant_roles(
    user_id: str,
    restaurant_id: str
) -> list:
    try:
        return RestaurantEmployee.objects.values('roles').get(
            user=user_id,
            restaurant=restaurant_id,
            deleted=False
        )['roles']
    except RestaurantEmployee.DoesNotExist:
        return []
