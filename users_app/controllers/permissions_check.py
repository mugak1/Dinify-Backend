import logging
from typing import Optional

from users_app.models import User
from restaurants_app.models import RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN,
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER
)

logger = logging.getLogger(__name__)

dinify_roles = [DINIFY_ACCOUNT_MANAGER, DINIFY_ADMIN]

# Restaurant roles permitted to READ a restaurant's setup data. Mirrors the
# write-path / dedicated-GET gate (check_restaurant_permission, check_permission):
# only owners and managers, plus the dinify-admin bypass handled separately.
READ_ROLES = (RESTAURANT_OWNER, RESTAURANT_MANAGER)

# Restaurant roles permitted to WRITE to a restaurant's data — mirrors
# READ_ROLES (owners + managers); the dinify-admin bypass is handled separately.
MANAGE_ROLES = (RESTAURANT_OWNER, RESTAURANT_MANAGER)


def get_user_restaurant_roles(user_id: str, restaurant_id: str) -> list:
    try:
        return RestaurantEmployee.objects.values('roles').get(
            restaurant__status__in=['active'],
            user=user_id,
            restaurant=restaurant_id,
            deleted=False,
            active=True,
        )['roles']
    except RestaurantEmployee.DoesNotExist:
        logger.debug("User does not have roles in the restaurant")
        return []
    except Exception as error:
        logger.error("Error while getting user roles in the restaurant: %s", error)
        return []


def is_dinify_admin(user: User) -> bool:
    return any(role in dinify_roles for role in user.roles)


def is_dinify_superuser(user: User) -> bool:
    return any(role in [DINIFY_ADMIN] for role in user.roles)


def is_restaurant_owner(user: User, restaurant_id: str) -> bool:
    return any(role in [RESTAURANT_OWNER] for role in get_user_restaurant_roles(user, restaurant_id))  # noqa


def get_any_restaurant_roles(user: User) -> list:
    res_roles = RestaurantEmployee.objects.select_related('restaurant').filter(
        restaurant__status__in=['active'],
        user=user,
        deleted=False
    )
    return [
        {
            'restaurant_id': str(res_role.restaurant.id),
            'restaurant': res_role.restaurant.name,
            'roles': res_role.roles
        }
        for res_role in res_roles
    ]


def get_readable_restaurant_ids(user: User) -> Optional[set]:
    """
    Return the set of restaurant ids the user may READ, or ``None`` for
    unrestricted access (a dinify admin / account manager).

    This is the reusable per-restaurant read-authorization primitive. It
    mirrors the role model of ``check_restaurant_permission`` /
    ``check_permission`` — a dinify admin reads across every restaurant;
    everyone else is bound to the restaurants where they hold an active,
    non-deleted owner/manager employment.

    Returns:
        ``None``      -> unrestricted (dinify admin); callers must NOT scope.
        ``set()``     -> deny-all (anonymous, inactive, or no qualifying role).
        ``{ids...}``  -> the restaurant ids (as strings) the user may read.
    """
    if user is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return set()
    if is_dinify_admin(user):
        return None
    rows = RestaurantEmployee.objects.filter(
        user=user,
        active=True,
        deleted=False,
    ).values_list('restaurant_id', 'roles')
    return {
        str(restaurant_id)
        for restaurant_id, roles in rows
        if any(role in READ_ROLES for role in (roles or []))
    }


def can_read_restaurant(user: User, restaurant_id) -> bool:
    """
    Whether ``user`` may read a single record owned by ``restaurant_id``.

    Built on ``get_readable_restaurant_ids``: a dinify admin (unrestricted
    set ``None``) may read anything; otherwise the restaurant must be in the
    user's readable set. A missing/unresolved ``restaurant_id`` is denied for
    non-admins (fail closed).
    """
    allowed = get_readable_restaurant_ids(user)
    if allowed is None:
        return True
    return restaurant_id is not None and str(restaurant_id) in allowed


def can_manage_restaurant(user: User, restaurant_id) -> bool:
    """
    Whether ``user`` may WRITE to a single record owned by ``restaurant_id``.

    Write counterpart of ``can_read_restaurant``: a dinify admin may write
    anywhere; otherwise the user must hold an active owner/manager role in that
    restaurant. A missing/empty ``restaurant_id`` is denied for non-admins
    (fail closed).
    """
    if is_dinify_admin(user):
        return True
    if not restaurant_id:
        return False
    return any(
        role in MANAGE_ROLES
        for role in get_user_restaurant_roles(user, restaurant_id)
    )
