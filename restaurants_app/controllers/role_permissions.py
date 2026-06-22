"""
Per-restaurant role-permission helpers.

``ensure_role_permissions`` (from A) idempotently seeds the four default override
rows for a restaurant.

``get_role_permissions`` / ``update_role_permission`` are the owner-facing
MANAGEMENT surface (read + write of the per-(restaurant, role) module-access
grid). They are the counterpart to A's per-USER resolver
(users_app.controllers.permissions_check.resolve_module_permissions): the
resolver answers "can THIS user reach module X here?", whereas these answer "what
is each ROLE's grid here?" for the team-settings screen. They therefore reuse A's
CONSTANTS (DEFAULT_ROLE_MODULES, GRID_MODULES, the role names) but deliberately
NOT the resolver function. The owner row is read-only (all-True, advisory
``editable=False``); only the manager / kitchen / staff rows are writable.
Persistence is the RestaurantRolePermission OVERRIDE row (A's model, migration
0052) — a partial PUT merges over the row's current effective grid, never
silently resetting unspecified toggles back to the coded default.
"""
from django.db import transaction

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    RESTAURANT_KITCHEN,
    RESTAURANT_STAFF,
    GRID_MODULES,
)
from restaurants_app.configs.role_defaults import DEFAULT_ROLE_MODULES
from restaurants_app.models import RestaurantRolePermission


# Deterministic grid-row order: the GET list iterates THIS explicit sequence —
# never dict-insertion or queryset order — so rows never reshuffle between
# requests and the frontend can rely on a stable order.
ROLE_ORDER = [
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    RESTAURANT_KITCHEN,
    RESTAURANT_STAFF,
]


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


def _effective_grid(role, persisted_modules):
    """
    The role's effective grid: its persisted override (if any) merged OVER the
    coded default, normalised to exactly the 7 GRID_MODULES keys in canonical
    order and coerced to plain booleans.

    Dropping non-grid keys and coercing to ``bool`` keeps the read (and the merge
    base for a write) faithful to the grid contract regardless of any stale or
    mistyped values that may sit in a persisted row.
    """
    merged = dict(DEFAULT_ROLE_MODULES.get(role, {}))
    merged.update(persisted_modules or {})
    return {module: bool(merged.get(module, False)) for module in GRID_MODULES}


def get_role_permissions(restaurant):
    """
    Build the four-role grid for ``restaurant``.

    Returns the ``{status, message, data}`` envelope; ``data`` is a list of
    ``{role, modules, editable}`` in ROLE_ORDER. ``editable`` is sourced off the
    SAME RESTAURANT_OWNER constant the write path rejects on, so the advisory flag
    and the real gate cannot drift. One query for every override row (no N+1).
    """
    overrides = {
        row['role']: row['modules']
        for row in RestaurantRolePermission.objects.filter(
            restaurant=restaurant, deleted=False,
        ).values('role', 'modules')
    }
    data = [
        {
            'role': role,
            'modules': _effective_grid(role, overrides.get(role)),
            'editable': role != RESTAURANT_OWNER,
        }
        for role in ROLE_ORDER
    ]
    return {
        'status': 200,
        'message': 'Role permissions retrieved successfully',
        'data': data,
    }


def update_role_permission(restaurant, role, modules, user_id=None):
    """
    Validate and persist a (partial) module-grid update for a non-owner role.

    Validation is explicit (NOT the Secretary / EDIT_INFORMATION path): the owner
    row is immutable, unknown roles and non-grid module keys (billing / team /
    support / unknown) are rejected by name, and only plain JSON booleans are
    accepted.

    The merge is partial over the role's CURRENT EFFECTIVE grid — the persisted
    override row (if one exists) merged over the coded default — so a partial PUT
    arriving after a prior full PUT preserves the earlier customization rather than
    resetting unspecified toggles to defaults. The read-of-existing-row plus
    update_or_create run inside a single transaction (with the existing row
    locked) so two concurrent PUTs cannot interleave a stale merge.
    """
    if role == RESTAURANT_OWNER:
        return {
            'status': 400,
            'message': 'Owner permissions are not editable',
        }
    if not role or role not in DEFAULT_ROLE_MODULES:
        return {
            'status': 400,
            'message': f"Unknown role '{role}'",
        }
    if not isinstance(modules, dict):
        return {
            'status': 400,
            'message': 'modules must be an object of {module: boolean}',
        }
    for key, value in modules.items():
        if key not in GRID_MODULES:
            return {
                'status': 400,
                'message': f"Unknown module '{key}'",
            }
        # isinstance(_, bool) intentionally rejects "true" / 1 / 0 — the stored
        # value (and the GET) must be a plain JSON boolean.
        if not isinstance(value, bool):
            return {
                'status': 400,
                'message': f"Module '{key}' must be a boolean",
            }

    with transaction.atomic():
        existing = (
            RestaurantRolePermission.objects
            .select_for_update()
            .filter(restaurant=restaurant, role=role)
            .first()
        )
        effective = _effective_grid(role, existing.modules if existing else None)
        effective.update(modules)
        RestaurantRolePermission.objects.update_or_create(
            restaurant=restaurant,
            role=role,
            defaults={'modules': effective},
            create_defaults={'modules': effective, 'created_by_id': user_id},
        )
    return {
        'status': 200,
        'message': 'Role permissions updated successfully',
        'data': {'role': role, 'modules': effective, 'editable': True},
    }
