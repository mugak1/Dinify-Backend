"""
Coded default module-access grids per restaurant role.

These are the fallback the permission resolver
(users_app.controllers.permissions_check.resolve_module_permissions) uses when
no RestaurantRolePermission override row exists for a (restaurant, role). Only
GRID modules appear here — the off-grid owner-only keys (billing, team) are
granted exclusively by the resolver's owner short-circuit, never from a role's
default/override grid.
"""
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    RESTAURANT_KITCHEN,
    RESTAURANT_STAFF,
    GRID_MODULES,
    MODULE_KITCHEN,
    MODULE_TABLES,
)


def _all_grid(value):
    """A full grid map with every module set to ``value``."""
    return {module: value for module in GRID_MODULES}


def _grid_only(*enabled):
    """A grid map with only ``enabled`` modules True, the rest False."""
    grid = _all_grid(False)
    for module in enabled:
        grid[module] = True
    return grid


DEFAULT_ROLE_MODULES = {
    RESTAURANT_OWNER:   _all_grid(True),
    RESTAURANT_MANAGER: _all_grid(True),
    RESTAURANT_KITCHEN: _grid_only(MODULE_KITCHEN),
    RESTAURANT_STAFF:   _grid_only(MODULE_TABLES),
}
