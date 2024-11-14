from users_app.models import User
from restaurants_app.models import RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN,
    RESTAURANT_OWNER
)

dinify_roles = [DINIFY_ACCOUNT_MANAGER, DINIFY_ADMIN]


def get_user_restaurant_roles(user_id: str, restaurant_id: str) -> list:
    try:
        return RestaurantEmployee.objects.values('roles').get(
            restaurant__status__in=['active'],
            user=user_id,
            restaurant=restaurant_id,
            deleted=False
        )['roles']
    except RestaurantEmployee.DoesNotExist:
        return []


def is_dinify_admin(user: User) -> bool:
    return any(role in dinify_roles for role in user.roles)


def is_dinify_superuser(user: User) -> bool:
    return any(role in [DINIFY_ADMIN] for role in user.roles)


def is_restaurant_owner(user: User, restaurant_id: str) -> bool:
    return any(role in [RESTAURANT_OWNER] for role in get_user_restaurant_roles(user, restaurant_id))  # noqa