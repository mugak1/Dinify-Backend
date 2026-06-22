"""
Seeding helper for per-restaurant role-permission override rows.
"""
from restaurants_app.models import RestaurantRolePermission
from restaurants_app.configs.role_defaults import DEFAULT_ROLE_MODULES


def ensure_role_permissions(restaurant):
    """
    Idempotently get_or_create the four default role-permission rows for a
    restaurant. ``restaurant`` may be a Restaurant instance or its id.

    Belt-and-suspenders: the resolver already falls back to
    DEFAULT_ROLE_MODULES, so the system is correct without these rows — but
    persisting them makes each role's grid visible/editable in the portal.
    """
    restaurant_id = getattr(restaurant, 'id', restaurant)
    for role, modules in DEFAULT_ROLE_MODULES.items():
        RestaurantRolePermission.objects.get_or_create(
            restaurant_id=restaurant_id,
            role=role,
            defaults={'modules': dict(modules)},
        )
