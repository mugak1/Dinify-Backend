"""
What a delegated session may reach — the coded, deny-by-default authority map.

Two independent bounds, and a request must clear BOTH:

* ``SCOPE_MODULES`` — the INNER bound. Which permission-grid modules the scope
  grants, consumed by the delegation branches in
  ``users_app.controllers.permissions_check``. This is the same vocabulary the
  role grid uses (``GRID_MODULES`` + the owner-only keys), so a delegated
  principal is evaluated by the machinery every other principal is.
* ``ALLOWED_ROUTES`` — the OUTER bound. Which (route, method) pairs a delegated
  credential may reach at all, enforced by ``DelegatedAccessMiddleware`` BEFORE
  the request is dispatched and before the credential is bound to
  ``request.user``. Anything absent is refused; there is no wildcard.

The outer bound exists because module access is necessary but not sufficient. A
module can gate several endpoints with very different consequences — ``kitchen``
covers both "is this item sold out?" and "mark this order served", and the latter
moves the tenant's revenue. It also blocks the endpoints that have no restaurant
dimension at all (notifications, user-profile, user-lookup, the OTP flow), which
key on ``request.user`` and would otherwise let a delegated principal read or
mutate *the administrator's own* records.

This module is imported by the customer-plane permission resolver, so it must stay
import-light: constants only, no querysets, and no app imports beyond
``string_definitions`` and the scope constants themselves (which stay defined on
the model, so ``scope`` has one source of truth). Mirrors
``restaurants_app.configs.role_defaults``.

Every entry here is a deliberate grant. Add one only with a reason written beside
it, and prefer refusing.
"""
from collections import namedtuple

from dinify_backend.configss.string_definitions import (
    GRID_MODULES,
    MODULE_BILLING,
    MODULE_TEAM,
)
from platform_admin_app.models import SCOPE_SUPPORT, SCOPE_VIEW

# HTTP methods that cannot change state. Everything else is a write and needs an
# explicit ALLOWED_ROUTES entry naming the scope that may perform it.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

BOTH_SCOPES = frozenset({SCOPE_VIEW, SCOPE_SUPPORT})
SUPPORT_ONLY = frozenset({SCOPE_SUPPORT})


def _scope_grid():
    """
    The module grid every delegation scope carries.

    Identical for both scopes on purpose: ``view`` and ``support`` differ in what
    they may WRITE (see ALLOWED_ROUTES), not in what they may see. Splitting the
    read surface as well would give an administrator a reason to ask for the
    higher scope just to look at something, which is exactly backwards.

    ``billing`` and ``team`` are False for both: subscription figures are Dinify's
    commercial relationship with the restaurant, and the team grid is employee PII.
    Neither is needed to help a restaurant with what it can see itself.
    """
    grid = {module: True for module in GRID_MODULES}
    grid[MODULE_BILLING] = False
    grid[MODULE_TEAM] = False
    return grid


SCOPE_MODULES = {
    SCOPE_VIEW: _scope_grid(),
    SCOPE_SUPPORT: _scope_grid(),
}

# Scopes whose principal counts as "employed at" the granted restaurant for the
# UNGATED support module (``get_employed_restaurant_ids``). ``view`` is excluded:
# reading a tenant's support tickets is not part of looking at their menu.
SUPPORT_MODULE_SCOPES = SUPPORT_ONLY

# ``config_detail`` values a delegated GET may pass to the restaurant-setup
# catch-all. One route serves many behaviours there, so the allowlist names the
# permitted values rather than trusting the route alone.
#   employees            -> excluded: employee PII, and the team module is False
#   subscription-details -> excluded: billing figures
# Anything not listed (including a verb added later) is refused by default.
SETUP_READABLE_RECORDS = frozenset({
    'restaurants',
    'menusections',
    'sectiongroups',
    'menuitems',
    'tables',
    'diningareas',
    'details',
    'menu-item-sort-mode',
})

# scopes:       which delegation scopes may use this (route, method)
# kwargs_allow: optional {view kwarg: permitted values} narrowing within a route
Rule = namedtuple('Rule', 'scopes kwargs_allow')


def _rule(scopes, kwargs_allow=None):
    return Rule(scopes=scopes, kwargs_allow=kwargs_allow or {})


# Keyed on (resolver_match.route, method) — the URL PATTERN, never the resolved
# path, so a record id can never affect whether a route is allowed.
ALLOWED_ROUTES = {
    # --- reads: both scopes -------------------------------------------------
    # The restaurant's own setup: menu tree, tables, dining areas, profile.
    ('api/v1/restaurant-setup/<str:config_detail>/', 'GET'): _rule(
        BOTH_SCOPES, {'config_detail': SETUP_READABLE_RECORDS},
    ),
    # The live kitchen board, as the restaurant sees it.
    ('api/v1/kitchen/orders/active/', 'GET'): _rule(BOTH_SCOPES),
    ('api/v1/kitchen/orders/completed/', 'GET'): _rule(BOTH_SCOPES),
    ('api/v1/kitchen/menu-items/', 'GET'): _rule(BOTH_SCOPES),
    # Their reports. Scoped by the module resolver to the granted restaurant.
    ('api/v1/reports/restaurant/<str:report_name>/', 'GET'): _rule(BOTH_SCOPES),
    # Their reviews and review analytics.
    ('api/v1/reviews/', 'GET'): _rule(BOTH_SCOPES),
    ('api/v1/reviews/summary/', 'GET'): _rule(BOTH_SCOPES),
    ('api/v1/reviews/analytics/', 'GET'): _rule(BOTH_SCOPES),
    # Their support tickets — the context for the call being handled.
    ('api/v1/support/issues/', 'GET'): _rule(BOTH_SCOPES),
    ('api/v1/support/issues/<uuid:issue_id>/', 'GET'): _rule(BOTH_SCOPES),

    # --- the delegated session's own surface --------------------------------
    ('api/v1/delegation/session/', 'GET'): _rule(BOTH_SCOPES),
    # Ending your own session must never depend on holding a write scope.
    ('api/v1/delegation/end/', 'POST'): _rule(BOTH_SCOPES),

    # --- writes: support scope only, and only these two ---------------------
    # Raise a ticket on the restaurant's behalf. Reversible, no financial axis,
    # and the reporter is honestly recorded as the administrator.
    ('api/v1/support/issues/', 'POST'): _rule(SUPPORT_ONLY),
    # 86 / un-86 one menu item. Single-record, instantly reversible, no financial
    # axis — the canonical "my item shows sold out" support call.
    ('api/v1/kitchen/menu-items/<str:pk>/stock/', 'PUT'): _rule(SUPPORT_ONLY),
    #
    # DELIBERATELY ABSENT, with reasons:
    #   kitchen/orders/<pk>/fulfilment-status/  serving sets order_status='served',
    #       which makes the order a SALE in Reports. An administrator must never
    #       move a tenant's revenue.
    #   kitchen/orders/<pk>/cancel/  writes cancelled_by / cancellation_reason onto
    #       the tenant's order history, and needs manage-level authority, which a
    #       delegation never carries.
    #   kitchen/orders/<pk>/priority/  harmless but useless alone.
    #   kitchen/orders/<pk>/state/  the per-order OBSERVATION that settles an
    #       uncertain kitchen command. Omitted BY REASONING, not by oversight:
    #       a delegated session cannot issue any of the three commands above, so
    #       it can never hold an uncertain one to reconcile. Nothing it discloses
    #       is new — the two board feeds it MAY read carry the same fields — so
    #       this is a surface with no delegated caller rather than a withheld
    #       capability. Add it only alongside a delegated writer that needs it.
    #   reviews/<id>/resolution/  manage-level gate; closed by construction.
    #   restaurant-setup writes, table-actions, reservations, waitlist, tags,
    #       upsell-config, role-permissions, manager-actions  all authenticate
    #       through decode_jwt_token, which stays JWT-only.
    #   restaurant-setup/subscription-details/ (read), finances/transactions/
    #       billing and the transaction ledger.
    #   PR-A retired four routes outright rather than leaving them off this list:
    #       restaurant-setup/admin-register-restaurant/ (no target to scope to),
    #       the subscription-details WRITE, reports/dinify/* and
    #       support/admin/issues/ (cross-tenant by design). Kept named here so a
    #       future reader does not mistake their absence for an oversight.
    #   notifications/, users/user-profile/, users/user-lookup/, users/auth/*  no
    #       restaurant dimension at all; they act on request.user, i.e. on the
    #       administrator's own records.
    #   the anonymous diner channel (orders/journey/*, v2 initiate, reviews/submit)
    #       untouched — it authenticates by diner capability, not by request.user.
    #   orders/submit/ and orders/retire-quote/ (D06)  both act on ONE diner's
    #       saved draft and both are authorised by the diner's table session or
    #       by a staff caller's tables module — neither of which a delegated
    #       administrator holds. `submit` places an order; `retire-quote` writes
    #       a durable, IRREVERSIBLE record that a diner's quote may never be
    #       accepted, which is a decision about that diner's purchase and not a
    #       support action. Named here so their absence reads as deliberate.
}


def route_rule(route, method):
    """The rule for this (route, method), or ``None`` if it is not allowed."""
    return ALLOWED_ROUTES.get((route, method))


def scope_modules(scope):
    """
    The module grid for ``scope``. An unknown scope grants NOTHING.

    Fail-closed by construction: a scope value added to the model without an entry
    here denies everything rather than inheriting someone else's grid.
    """
    return SCOPE_MODULES.get(scope, {})
