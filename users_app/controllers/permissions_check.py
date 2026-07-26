"""
Authority resolution for the CUSTOMER plane.

There is exactly one way a principal reaches a restaurant here: an active,
non-deleted ``RestaurantEmployee`` row at a restaurant whose lifecycle state
grants portal access — or a ``DelegationGrant``, which is bounded to ONE
restaurant, ONE scope, for a bounded time.

There is no third way. In particular there is no platform-administrator
short-circuit: ``User.roles`` is NOT consulted for platform authority anywhere
in this module, and a customer JWT can never carry cross-tenant reach. Platform
staff live on the admin plane (``platform_admin_app``), are identified by
``User.account_type``, and cannot obtain a customer token at all. Reaching a
tenant is what delegation is for. See ``dinify_backend/tenancy/ambient_authority.py``
for the standing gate that keeps it that way.
"""
import logging

from users_app.models import User
from restaurants_app.models import RestaurantEmployee, RestaurantRolePermission
from restaurants_app.configs.role_defaults import DEFAULT_ROLE_MODULES
from restaurants_app.controllers.lifecycle_policy import portal_access_states
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    GRID_MODULES,
    OWNER_ONLY_MODULES,
    MODULE_BILLING,
    MODULE_TEAM,
    MODULE_SUPPORT,
)

logger = logging.getLogger(__name__)

# Restaurant roles permitted to WRITE to a restaurant's data (owners + managers).
# Used by ``can_manage_restaurant`` for the manage-level elevation gates (review
# resolution, kitchen goodwill-cancel) that sit ABOVE module access and are
# intentionally NOT module-granular.
MANAGE_ROLES = (RESTAURANT_OWNER, RESTAURANT_MANAGER)

# The in-memory marker DelegatedSessionAuthentication puts on the principal of a
# delegated request. Kept as a string constant so this module does not import the
# admin app at load time.
_DELEGATION_ATTR = 'active_delegation'


def _delegation(user):
    """
    The delegation a principal is acting under, or ``None`` for everyone else.

    A platform administrator reaching into a tenant under a ``DelegationGrant`` is
    NOT an employee of that tenant: they hold one restaurant, one scope, for a
    bounded time. Every public predicate below therefore checks this FIRST and
    short-circuits, so a delegated principal is resolved from the stored grant
    rather than from employment it does not have.

    Set only by ``platform_admin_app.delegated_auth`` — an in-memory attribute on
    the user instance, never a model field, so it cannot be persisted and cannot be
    supplied by a request. For a JWT or anonymous principal this is ``None`` and
    every function behaves exactly as it did before delegation existed.
    """
    return getattr(user, _DELEGATION_ATTR, None)


def _support_module_scopes():
    """Delegation scopes that reach the ungated ``support`` module."""
    from platform_admin_app.configs.delegation_scopes import SUPPORT_MODULE_SCOPES

    return SUPPORT_MODULE_SCOPES


def _delegation_grants(delegation, restaurant_id, module) -> bool:
    """
    Whether ``delegation`` grants ``module`` at ``restaurant_id``.

    The restaurant comes from the stored grant and is only ever COMPARED against
    the id the caller resolved — it is never taken from, nor written back into, a
    request parameter. A mismatch denies; it can never widen.
    """
    from platform_admin_app.configs.delegation_scopes import scope_modules

    if not restaurant_id:
        return False
    if str(restaurant_id) != delegation.restaurant_id:
        return False
    return bool(scope_modules(delegation.scope).get(module, False))


def get_user_restaurant_roles(user_id: str, restaurant_id: str) -> list:
    try:
        return RestaurantEmployee.objects.values('roles').get(
            restaurant__status__in=portal_access_states(),
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


def is_restaurant_owner(user: User, restaurant_id: str) -> bool:
    # Delegation hands over bounded access, never the owner's identity — attribution
    # must not be launderable into "the owner did it".
    if _delegation(user) is not None:
        return False
    return any(role in [RESTAURANT_OWNER] for role in get_user_restaurant_roles(user, restaurant_id))  # noqa


def _full_access_map() -> dict:
    """Every grid module plus the owner-only keys, all True."""
    return {module: True for module in (*GRID_MODULES, *OWNER_ONLY_MODULES)}


def _resolve_from_roles(roles, overrides_by_role: dict) -> dict:
    """
    Pure in-memory module resolution for ONE restaurant — no DB access.

    ``roles`` is the caller's role list for that restaurant; ``overrides_by_role``
    is a ``{role: modules}`` map of the persisted RestaurantRolePermission rows for
    that restaurant.

    Owner -> full access (all grid + billing + team). Otherwise the union (most
    permissive) across the caller's roles, each role's persisted override OR its
    coded default; billing/team are never granted via the role merge (owner-only)
    so they resolve False.
    """
    if RESTAURANT_OWNER in (roles or []):
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
            restaurant__status__in=portal_access_states(),
            user=user,
            active=True,
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
    return [
        {
            'restaurant_id': str(emp.restaurant.id),
            'restaurant': emp.restaurant.name,
            'roles': emp.roles,
            'permissions': _resolve_from_roles(
                emp.roles,
                overrides_by_restaurant.get(str(emp.restaurant.id), {}),
            ),
        }
        for emp in employments
    ]


def resolve_module_permissions(user: User, restaurant_id) -> dict:
    """
    Resolve the ``{module: bool}`` access map for ``user`` at ``restaurant_id``.

    Owner of the restaurant -> all grid + billing + team (short-circuit);
    otherwise the union across the user's roles for THAT restaurant, each role's
    persisted RestaurantRolePermission row OR its coded default (multi-role
    resolves to the most permissive). billing/team are owner-only; ``support`` is
    ungated and never represented here (see ``can_user_access_module``).

    A delegated administrator resolves from their grant instead: the scope's coded
    module grid at the grant's ONE restaurant, and all-False everywhere else.
    """
    if (
        user is None
        or not getattr(user, 'is_authenticated', False)
        or not user.is_active
    ):
        return _resolve_from_roles([], {})
    delegation = _delegation(user)
    if delegation is not None:
        from platform_admin_app.configs.delegation_scopes import scope_modules

        if not restaurant_id or str(restaurant_id) != delegation.restaurant_id:
            # A different tenant (or none named): a delegation confers nothing here.
            return _resolve_from_roles([], {})
        grid = _resolve_from_roles([], {})
        grid.update(scope_modules(delegation.scope))
        # Never the owner-only keys, whatever a scope grid might come to say.
        grid[MODULE_BILLING] = False
        grid[MODULE_TEAM] = False
        return grid
    roles = get_user_restaurant_roles(user, restaurant_id)
    overrides = {
        row['role']: row['modules']
        for row in RestaurantRolePermission.objects.filter(
            restaurant_id=restaurant_id,
            role__in=roles,
            deleted=False,
        ).values('role', 'modules')
    } if roles else {}
    return _resolve_from_roles(roles, overrides)


def can_user_access_module(user: User, restaurant_id, module: str) -> bool:
    """
    Whether ``user`` may access ``module`` at ``restaurant_id``.

    The dominant enforcement entry point across the portal (restaurant-setup writes
    and detail reads, kitchen, reports, reviews, role-permissions and the rest).
    ``support`` is ungated for ordinary principals — its scoping is done by
    ``get_employed_restaurant_ids`` at the call sites — and every other module
    defers to ``resolve_module_permissions``.

    A delegated principal is the one exception to ``support`` being ungated: it is
    resolved from the grant like any other module, so a delegation can never reach
    a tenant it was not issued for through the ungated path.
    """
    delegation = _delegation(user)
    if delegation is not None:
        if module == MODULE_SUPPORT:
            return (
                delegation.scope in _support_module_scopes()
                and bool(restaurant_id)
                and str(restaurant_id) == delegation.restaurant_id
            )
        return _delegation_grants(delegation, restaurant_id, module)
    if module == MODULE_SUPPORT:
        return True
    return bool(resolve_module_permissions(user, restaurant_id).get(module, False))


def get_employed_restaurant_ids(user: User) -> set:
    """
    Return the set of restaurant ids where ``user`` holds ANY active,
    non-deleted employment.

    Role-agnostic on purpose: it powers the ungated ``support`` module's list
    scoping, where EVERY employee — not just owners/managers — may see their
    own restaurants' issues. Unlike the module path it does NOT filter on
    lifecycle state at all, which is what makes support reachable in every state
    (the ``Support access`` row of the ``lifecycle_policy`` matrix).

    ALWAYS a set — the caller must scope by it unconditionally. There is no
    "unrestricted" return: the ``None`` sentinel that used to mean "a dinify admin
    may see every tenant, do not scope" was removed with the role predicates it
    depended on, and must not come back. A delegated administrator holds NO
    employment, so it resolves from the grant: the granted restaurant if the scope
    reaches support, otherwise deny-all.

    Returns:
        ``set()``     -> deny-all (anonymous, inactive, or no employment).
        ``{ids...}``  -> the restaurant ids (as strings) the user is employed at.
    """
    if user is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return set()
    delegation = _delegation(user)
    if delegation is not None:
        if delegation.scope in _support_module_scopes():
            return {delegation.restaurant_id}
        return set()
    return {
        str(restaurant_id)
        for restaurant_id in RestaurantEmployee.objects.filter(
            user=user,
            active=True,
            deleted=False,
        ).values_list('restaurant_id', flat=True)
    }


def get_module_restaurant_ids(user: User, module: str) -> set:
    """
    Return the set of restaurant ids where ``user`` may access ``module``.

    The list-scoping counterpart of ``can_user_access_module`` (the single-record
    check): a GET list is authoritatively bound to exactly the restaurants where
    the caller's resolved module grid grants ``module``. ``support`` is ungated,
    so it maps to every restaurant the caller is employed at. Mirrors the
    resolver's lifecycle scope (employment at a restaurant whose state grants
    portal access — ``onboarding`` or ``live``, per ``lifecycle_policy``) and
    batches the override rows in one query (no N+1).

    ALWAYS a set, at most the ONE granted restaurant for a delegated administrator.
    There is no "unrestricted" return. The ``None`` sentinel this used to have for a
    dinify admin was load-bearing in the worst way: callers read it as "do not
    scope", which is how ``scope_list_filter`` left a list filter untouched and
    ``build_scoped_instance_queryset`` returned ``model.objects.all()``. It went with
    the role predicates — do not reintroduce it.

    Returns:
        ``set()``     -> deny-all.
        ``{ids...}``  -> the restaurant ids (as strings) whose grid grants ``module``.
    """
    if user is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return set()
    delegation = _delegation(user)
    if delegation is not None:
        if module == MODULE_SUPPORT:
            return get_employed_restaurant_ids(user)
        if _delegation_grants(delegation, delegation.restaurant_id, module):
            return {delegation.restaurant_id}
        return set()
    if module == MODULE_SUPPORT:
        return get_employed_restaurant_ids(user)
    employments = list(
        RestaurantEmployee.objects.filter(
            restaurant__status__in=portal_access_states(),
            user=user,
            active=True,
            deleted=False,
        ).values_list('restaurant_id', 'roles')
    )
    if not employments:
        return set()
    # One query for every override row across these restaurants (avoid N+1).
    overrides_by_restaurant = {}
    for row in RestaurantRolePermission.objects.filter(
        restaurant_id__in=[restaurant_id for restaurant_id, _ in employments],
        deleted=False,
    ).values('restaurant_id', 'role', 'modules'):
        overrides_by_restaurant.setdefault(
            str(row['restaurant_id']), {}
        )[row['role']] = row['modules']
    return {
        str(restaurant_id)
        for restaurant_id, roles in employments
        if _resolve_from_roles(
            roles, overrides_by_restaurant.get(str(restaurant_id), {})
        ).get(module)
    }


def can_manage_restaurant(user: User, restaurant_id) -> bool:
    """
    Whether ``user`` may WRITE to a single record owned by ``restaurant_id``.

    The manage-level elevation check that sits ABOVE module access (review
    resolution, kitchen goodwill-cancel) — intentionally NOT module-granular: the
    user must hold an active owner/manager role in that restaurant. A missing/empty
    ``restaurant_id`` is denied (fail closed).

    A delegation NEVER carries manage-level authority. Delegation is bounded,
    time-boxed help; the elevated actions behind this gate (resolving a diner's
    review, cancelling a paid-for order as goodwill) speak for the restaurant to its
    own customers, and that is the owner's to do.
    """
    if _delegation(user) is not None:
        return False
    if not restaurant_id:
        return False
    return any(
        role in MANAGE_ROLES
        for role in get_user_restaurant_roles(user, restaurant_id)
    )
