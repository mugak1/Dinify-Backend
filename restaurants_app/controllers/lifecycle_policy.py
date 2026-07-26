"""
What each restaurant lifecycle state PERMITS — one policy, consulted by every reader.

Before PR-5 the answer to "may this restaurant do X?" was scattered across three
`restaurant__status__in=['active']` queryset filters, a `status in ['blocked']`
literal in the order path, an `== 'active'` in the menu policy, an `!= 'active'` in
a management command and a `['active', 'pending']` list filter. Five readers, five
independently-maintained opinions, no shared definition — so a sixth reader had
nowhere to look and a state change had to be remembered in five places.

This module is that shared definition. ``CAPABILITY_MATRIX`` is the specification
table transcribed as data; every predicate below reads FROM it, so the table is the
single thing to change when a state's meaning changes. Readers call a named
predicate — never ``restaurant.status == '<literal>'``.

IMPORT DISCIPLINE: pure constants and pure functions. No models, no querysets, no
Django imports beyond the state constants. ``users_app.controllers.permissions_check``
(the customer-plane permission resolver) imports this, so it must stay import-light —
the same rule ``platform_admin_app.configs.delegation_scopes`` and
``restaurants_app.configs.role_defaults`` follow.

WRITES live in ``restaurants_app.controllers.lifecycle``; this module never assigns
``Restaurant.status``.
"""
from dinify_backend.configss.string_definitions import (
    RESTAURANT_LIFECYCLE_STATES,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)

# --- capability keys --------------------------------------------------------
# One key per row of the specification's per-state behaviour table.
CAP_STAFF_PORTAL = 'staff_portal'
CAP_DINER_MENU = 'diner_menu'
CAP_ORDER_CREATE = 'order_create'
CAP_KITCHEN = 'kitchen'
CAP_SUPPORT = 'support'
CAP_DELEGATED_ACCESS = 'delegated_access'
# THE LAUNCH BOUNDARY. Is this restaurant open for real business? One key, because
# that is one fact — it decides both whether the public may order and whether the
# orders that do exist are commercial. `onboarding` is the state where the answer is
# no while everything else is yes, which is precisely what makes it onboarding.
CAP_LIVE_TRADING = 'live_trading'

# --- diner-menu outcomes ----------------------------------------------------
# The diner read is the one capability with THREE outcomes rather than two: a
# suspended restaurant is temporarily unavailable (and says so), while an
# offboarded one is simply gone.
DINER_MENU_ALLOWED = 'allowed'
DINER_MENU_UNAVAILABLE = 'unavailable'
DINER_MENU_GONE = 'gone'

# --- delegated-access ceilings ---------------------------------------------
# What a delegation may do at a restaurant in this state, expressed as the highest
# scope it is allowed to exercise. ``None`` means "the grant's own scope stands".
# The values are the scope constants on ``platform_admin_app.models``; they are
# spelled literally here rather than imported, because importing the admin app from
# the customer-plane policy would invert the dependency direction (and this module
# is imported by the permission resolver).
DELEGATED_SCOPE_VIEW = 'view'


# --- the table --------------------------------------------------------------
#
# | Capability      | onboarding | live | suspended        | offboarded        |
# |-----------------|------------|------|------------------|-------------------|
# | Staff portal    | Full       | Full | Blocked          | Blocked           |
# | Diner scan/menu | Allowed    | Allow| Unavailable      | Gone (404)        |
# | Order create    | Allowed    | Allow| Blocked          | Blocked           |
# | Live trading    | **NO**     | Yes  | Blocked          | Blocked           |
# | Kitchen board   | Allowed    | Allow| Blocked          | Blocked           |
# | Support access  | Allowed    | Allow| Allowed          | Read-only (admin) |
# | Delegated admin | Allowed    | Allow| Allowed          | `view` scope only |
#
# ONBOARDING GRANTS FULL STAFF ACCESS — a deliberate widening over the pre-PR-5
# behaviour, where the three `status__in=['active']` filters denied the portal to
# every non-active restaurant. The owner must be able to build a menu and provision
# tables BEFORE going live, and the go-live readiness checklist depends on exactly
# that work having happened. `suspended` and `offboarded` continue to deny.
#
# ONBOARDING IS NOT LIVE TRADING — the correction PR-D makes. That widening also
# handed `onboarding` public diner ordering, which was never the intent: a restaurant
# could take real money from real diners before anyone had asserted it was ready,
# which is exactly what the go-live transition and its readiness gate exist to
# prevent. `CAP_ORDER_CREATE` still says yes at `onboarding` because the owner must
# be able to place ONE end-to-end rehearsal order (a hard blocker on the Phase-1
# checklist); `CAP_LIVE_TRADING` says no, which is what refuses the public and marks
# whatever IS created as a test order. The two predicates below read them together.
#
# SUPPORT stays reachable in every state. At `offboarded` the row reads "read-only
# via admin" and that is what the surrounding machinery already produces rather than
# anything enforced here: the staff portal is blocked, so the only principal that
# can still reach the tickets is a delegated administrator — capped to `view` by the
# row below. Support itself is an ungated module and is not filtered by status.
CAPABILITY_MATRIX = {
    RestaurantStatus_Onboarding: {
        CAP_STAFF_PORTAL: True,
        CAP_DINER_MENU: DINER_MENU_ALLOWED,
        CAP_ORDER_CREATE: True,
        CAP_LIVE_TRADING: False,
        CAP_KITCHEN: True,
        CAP_SUPPORT: True,
        CAP_DELEGATED_ACCESS: None,
    },
    RestaurantStatus_Live: {
        CAP_STAFF_PORTAL: True,
        CAP_DINER_MENU: DINER_MENU_ALLOWED,
        CAP_ORDER_CREATE: True,
        CAP_LIVE_TRADING: True,
        CAP_KITCHEN: True,
        CAP_SUPPORT: True,
        CAP_DELEGATED_ACCESS: None,
    },
    RestaurantStatus_Suspended: {
        CAP_STAFF_PORTAL: False,
        CAP_DINER_MENU: DINER_MENU_UNAVAILABLE,
        CAP_ORDER_CREATE: False,
        CAP_LIVE_TRADING: False,
        CAP_KITCHEN: False,
        CAP_SUPPORT: True,
        CAP_DELEGATED_ACCESS: None,
    },
    RestaurantStatus_Offboarded: {
        CAP_STAFF_PORTAL: False,
        CAP_DINER_MENU: DINER_MENU_GONE,
        CAP_ORDER_CREATE: False,
        CAP_LIVE_TRADING: False,
        CAP_KITCHEN: False,
        CAP_SUPPORT: True,
        CAP_DELEGATED_ACCESS: DELEGATED_SCOPE_VIEW,
    },
}


def _row(status):
    """
    The capability row for ``status``, or the DENY-ALL row for anything unknown.

    Fail-closed by construction: a value that is not one of the four states — a
    legacy string that somehow survived, a typo, ``None`` — grants nothing and shows
    no menu. A new state added to the vocabulary without a row here is denied rather
    than inheriting a neighbour's permissions.
    """
    return CAPABILITY_MATRIX.get(status) or {
        CAP_STAFF_PORTAL: False,
        CAP_DINER_MENU: DINER_MENU_GONE,
        CAP_ORDER_CREATE: False,
        CAP_LIVE_TRADING: False,
        CAP_KITCHEN: False,
        CAP_SUPPORT: False,
        CAP_DELEGATED_ACCESS: DELEGATED_SCOPE_VIEW,
    }


# --- queryset-level state sets ---------------------------------------------
# Derived from the table, not hand-listed, so they can never drift from it. These
# are for the places that must filter in SQL rather than test one loaded row.

PORTAL_ACCESS_STATES = frozenset(
    status for status in RESTAURANT_LIFECYCLE_STATES
    if CAPABILITY_MATRIX[status][CAP_STAFF_PORTAL]
)

ORDERING_STATES = frozenset(
    status for status in RESTAURANT_LIFECYCLE_STATES
    if CAPABILITY_MATRIX[status][CAP_ORDER_CREATE]
)

DINER_MENU_STATES = frozenset(
    status for status in RESTAURANT_LIFECYCLE_STATES
    if CAPABILITY_MATRIX[status][CAP_DINER_MENU] == DINER_MENU_ALLOWED
)

# The states in which an order is real commerce. Everything outside this set either
# cannot produce an order at all or produces a rehearsal one.
LIVE_TRADING_STATES = frozenset(
    status for status in RESTAURANT_LIFECYCLE_STATES
    if CAPABILITY_MATRIX[status][CAP_LIVE_TRADING]
)


def portal_access_states():
    """The states that grant staff-portal / module access, as a sorted list.

    A list (not the frozenset) because it feeds ``__in`` lookups, and sorted so the
    generated SQL — and therefore any query snapshot in a test — is deterministic.
    """
    return sorted(PORTAL_ACCESS_STATES)


# --- predicates -------------------------------------------------------------

def grants_portal_access(status) -> bool:
    """Whether staff may reach the restaurant portal and its modules in this state."""
    return bool(_row(status)[CAP_STAFF_PORTAL])


def allows_order_creation(status) -> bool:
    """
    Whether ANY new order may be created against a restaurant in this state.

    True at ``onboarding`` — the owner's end-to-end rehearsal order needs it. This is
    NOT the predicate that decides whether the public may order; see
    ``allows_diner_ordering``.
    """
    return bool(_row(status)[CAP_ORDER_CREATE])


def allows_diner_ordering(status) -> bool:
    """
    Whether an ANONYMOUS DINER may create an order — the launch boundary.

    Both halves must hold: the restaurant must be able to take an order at all, AND
    it must be trading for real. That second half is what `onboarding` fails, so a
    diner who scans a printed QR at a restaurant that has not gone live is refused
    while the owner can still rehearse.
    """
    row = _row(status)
    return bool(row[CAP_ORDER_CREATE] and row[CAP_LIVE_TRADING])


def orders_are_commercial(status) -> bool:
    """
    Whether orders created in this state count as real business.

    The single source for ``Order.is_test``: an order created while the answer is no
    is a rehearsal — operationally real (it occupies its table and reaches the
    kitchen) but invisible to revenue, history and diner analytics.
    """
    return bool(_row(status)[CAP_LIVE_TRADING])


def allows_kitchen(status) -> bool:
    """
    Whether the kitchen board is reachable in this state.

    Kitchen authorisation runs through ``can_user_access_module(..., MODULE_KITCHEN)``,
    which resolves against the portal-access state set — so this predicate exists to
    make the policy row explicit and testable, not because kitchen has a second gate.
    The two agree by construction (both rows are True for exactly onboarding+live);
    ``tests_lifecycle`` asserts that they cannot drift apart.
    """
    return bool(_row(status)[CAP_KITCHEN])


def allows_support(status) -> bool:
    """Whether the support module is reachable for a restaurant in this state."""
    return bool(_row(status)[CAP_SUPPORT])


def diner_menu_visibility(status) -> str:
    """
    How the anonymous diner surface should respond for a restaurant in this state.

    Returns ``DINER_MENU_ALLOWED`` (serve it), ``DINER_MENU_UNAVAILABLE`` (a
    graceful "temporarily unavailable", which does admit the restaurant exists) or
    ``DINER_MENU_GONE`` (the non-disclosing not-found posture).
    """
    return _row(status)[CAP_DINER_MENU]


def serves_diner_menu(status) -> bool:
    """Whether the diner menu is served outright in this state."""
    return diner_menu_visibility(status) == DINER_MENU_ALLOWED


def delegated_scope_ceiling(status):
    """
    The highest delegation scope exercisable at a restaurant in this state.

    ``None`` -> no ceiling, the grant's own scope stands. ``'view'`` -> the grant is
    capped at read-only however it was minted, which is what an offboarded tenant
    gets: the commercial relationship is over, so an administrator may still look at
    the record but may no longer act inside it.
    """
    return _row(status)[CAP_DELEGATED_ACCESS]


def effective_delegated_scope(granted_scope, status):
    """
    The scope a delegated session may actually exercise at a restaurant in ``status``.

    Applies ``delegated_scope_ceiling`` to the scope the grant was minted with. Only
    ever narrows: a ceiling replaces the granted scope, it never upgrades it.
    """
    ceiling = delegated_scope_ceiling(status)
    return granted_scope if ceiling is None else ceiling
