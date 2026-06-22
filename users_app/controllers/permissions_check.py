import logging
from typing import Optional

from users_app.models import User
from restaurants_app.models import RestaurantEmployee, RestaurantRolePermission
from restaurants_app.configs.role_defaults import DEFAULT_ROLE_MODULES
from dinify_backend.configss.string_definitions import (
    DINIFY_ACCOUNT_MANAGER,
    DINIFY_ADMIN,
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    GRID_MODULES,
    OWNER_ONLY_MODULES,
    MODULE_BILLING,
    MODULE_TEAM,
    MODULE_SUPPORT,
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


def _full_access_map() -> dict:
    """Every grid module plus the owner/admin-only keys, all True."""
    return {module: True for module in (*GRID_MODULES, *OWNER_ONLY_MODULES)}


def _resolve_from_roles(roles, is_admin: bool, overrides_by_role: dict) -> dict:
    """
    Pure in-memory module resolution for ONE restaurant — no DB access.

    ``roles`` is the caller's role list for that restaurant; ``is_admin`` is the
    dinify-admin short-circuit; ``overrides_by_role`` is a ``{role: modules}``
    map of the persisted RestaurantRolePermission rows for that restaurant.

    Admin or owner -> full access (all grid + billing + team). Otherwise the
    union (most permissive) across the caller's roles, each role's persisted
    override OR its coded default; billing/team are never granted via the role
    merge (owner/admin-only) so they resolve False.
    """
    if is_admin or RESTAURANT_OWNER in (roles or []):
        return _full_access_map()
    resolved = {module: False for module in GRID_MODULES}
    for role in (roles or []):
        grid = overrides_by_role.get(role) or DEFAULT_ROLE_MODULES.get(role, {})
        for module in GRID_MODULES:
            if grid.get(module):
                resolved[module] = True
    resolved[MODULE_BILLING] = False
    resolved[MODULE_TEAM] = False
    return resolved


def get_any_restaurant_roles(user: User) -> list:
    employments = list(
        RestaurantEmployee.objects.select_related('restaurant').filter(
            restaurant__status__in=['active'],
            user=user,
            deleted=False
        )
    )
    # One query for every override row across these restaurants (avoid N+1).
    overrides_by_restaurant = {}
    for row in RestaurantRolePermission.objects.filter(
        restaurant_id__in=[emp.restaurant_id for emp in employments],
        deleted=False,
    ).values('restaurant_id', 'role', 'modules'):
        overrides_by_restaurant.setdefault(
            str(row['restaurant_id']), {}
        )[row['role']] = row['modules']
    is_admin = is_dinify_admin(user)
    return [
        {
            'restaurant_id': str(emp.restaurant.id),
            'restaurant': emp.restaurant.name,
            'roles': emp.roles,
            'permissions': _resolve_from_roles(
                emp.roles,
                is_admin,
                overrides_by_restaurant.get(str(emp.restaurant.id), {}),
            ),
        }
        for emp in employments
    ]


def resolve_module_permissions(user: User, restaurant_id) -> dict:
    """
    Resolve the ``{module: bool}`` access map for ``user`` at ``restaurant_id``.

    Dinify admin -> all True; owner of the restaurant -> all grid + billing +
    team (short-circuit); otherwise the union across the user's roles for THAT
    restaurant, each role's persisted RestaurantRolePermission row OR its coded
    default (multi-role resolves to the most permissive). billing/team are
    owner/admin-only; ``support`` is ungated and never represented here (see
    ``can_user_access_module``).
    """
    if (
        user is None
        or not getattr(user, 'is_authenticated', False)
        or not user.is_active
    ):
        return _resolve_from_roles([], False, {})
    if is_dinify_admin(user):
        return _full_access_map()
    roles = get_user_restaurant_roles(user, restaurant_id)
    overrides = {
        row['role']: row['modules']
        for row in RestaurantRolePermission.objects.filter(
            restaurant_id=restaurant_id,
            role__in=roles,
            deleted=False,
        ).values('role', 'modules')
    } if roles else {}
    return _resolve_from_roles(roles, False, overrides)


def can_user_access_module(user: User, restaurant_id, module: str) -> bool:
    """
    Whether ``user`` may access ``module`` at ``restaurant_id``.

    The single entry point future enforcement will call — it is intentionally
    NOT wired into any endpoint in this layer. ``support`` is ungated (always
    True); every other module defers to ``resolve_module_permissions``.
    """
    if module == MODULE_SUPPORT:
        return True
    return bool(resolve_module_permissions(user, restaurant_id).get(module, False))


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
