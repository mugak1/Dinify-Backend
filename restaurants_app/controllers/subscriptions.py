"""
Read-side controller for restaurant subscription details.

The WRITE verb (``update``) was REMOVED with ambient administrator authority:
it was reachable on the strength of a ``dinify_admin`` string in the caller's
``User.roles`` and could grant any restaurant an indefinite free subscription.
Phase 1: setting subscription validity/expiry is admin-plane functionality,
built natively on /api/admin/v1, where it gets elevation and an audit row. Do
not re-add a write path here.

━━ WHAT THIS READ NOW CARRIES, AND WHY (D07) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The restaurant's own billing screen had no truthful source for two facts it was
displaying, so it invented both: it rendered a hard-coded price catalogue that no
server ever sent, beside a Pay control for a collector that does not exist. This
read now states both facts instead.

``in_app_collection_supported`` — whether Dinify can collect a subscription payment
in-app AT ALL. Read from ``finance_app.subscription_capability``, the SAME constant
the collection boundary itself is pinned against, so the portal and the server
cannot come to disagree about whether a Pay button has anything behind it.

``subscription_terms`` — the CANONICAL terms
(``commercial_app.RestaurantSubscriptionTerms``), or an explicit
``recorded: false``. Projected by ``commercial_app.reads``, which also owns the
selection rule the Step 3C writers use, so "which terms are in force" has one
answer on both planes.

━━ THE TWO LEGACY KEYS ARE KEPT, AND THEY ARE NOT CANONICAL ━━━━━━━━━━━━━━━━━━━━

``subscription_validity`` and ``subscription_expiry_date`` are returned unchanged,
byte for byte, because a deployed client reads them and this change is additive
under the expand-then-contract rule. They are NOT evidence of anything:

  * no supported writer maintains either column — the only one that did was the
    ambient-admin ``update`` removed above;
  * ``subscription_validity`` defaults to ``True``, so it cannot distinguish a
    decision from a column nobody has ever touched;
  * the Admin plane already labels them ``source: 'legacy_restaurant_fields'`` for
    exactly this reason (``restaurant_reads.subscription_summary``).

**WHERE THEY DISAGREE WITH ``subscription_terms``, THE TERMS WIN.** Nothing here
derives one from the other, in either direction, and no consumer should read the
legacy pair. They are contracted once the portal has migrated.

━━ NOTHING IS INFERRED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No terms are reconstructed from ``flat_fee``, ``preferred_subscription_method``,
``subscription_validity``, a transaction count or a UI price list. No billing
verdict is derived from an open terms row: there is no invoice model, no receivable
and no collection record in this repository, so "paid", "due", "active" and "in good
standing" have nothing behind them and are not published. **The read creates no
rows** — no ``get_or_create``, no default configuration, no backfill.

━━ AUTHORIZATION IS UPSTREAM AND UNCHANGED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``RestaurantSetupEndpoint.get`` gates this on ``can_user_access_module(...,
MODULE_SETTINGS)`` and answers 404 on denial, so a foreign or unknown
``?restaurant=`` never reaches here. A DELEGATED session cannot reach it either:
``subscription-details`` is deliberately absent from
``delegation_scopes.SETUP_READABLE_RECORDS``, so the middleware refuses it before
dispatch. **Read permission is deliberately NOT the collector's manage-level
permission** (``can_manage_restaurant``) — reading what your restaurant pays and
attempting to pay it are different decisions — so do not "tighten" this to match
the retired write path. This controller adds no gate of its own and only hardens
input so a missing or malformed restaurant returns 400/404 rather than a 500.
"""
from django.core.exceptions import ValidationError
from rest_framework.response import Response

from commercial_app.reads import subscription_terms_summary
from finance_app.subscription_capability import IN_APP_COLLECTION_SUPPORTED
from restaurants_app.models import Restaurant


class RestaurantSubscription:
    def __init__(self, ):
        pass

    def get_details(self, request):
        # Read-authorization is enforced upstream in the endpoint's GET handler
        # (settings-module gate, 404 on denial); here we only harden input so a
        # missing/unknown restaurant returns 400/404 instead of a 500.
        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant is required.'},
                status=400,
            )

        try:
            # `id` joins the two legacy columns so the terms read below can be
            # scoped by the pk the DATABASE returned rather than by the raw query
            # parameter — the value is known-good by then, and a malformed one has
            # already been refused.
            restaurant = Restaurant.objects.values(
                'id',
                'subscription_validity',
                'subscription_expiry_date',
            ).get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError, ValidationError):
            # DoesNotExist, or a malformed (non-UUID) id. Treat both as not found
            # — defensive, so a bad value can never surface as a 500.
            return Response(
                {'status': 404, 'message': 'Restaurant not found.'},
                status=404,
            )

        data = {
            # --- legacy, retained for the deployed client; see the docstring ---
            'subscription_validity': restaurant['subscription_validity'],
            'subscription_expiry_date': restaurant['subscription_expiry_date'],

            # --- canonical -----------------------------------------------------
            # A CAPABILITY, not a permission and not an account state: it says
            # whether this build can collect in-app, never whether this restaurant
            # has paid, owes anything, or has settled by some external means.
            'in_app_collection_supported': IN_APP_COLLECTION_SUPPORTED,
            # `recorded` names the same fact the Admin projection calls
            # `configured` — an OPEN terms row exists. The word differs
            # deliberately: this surface is read by the restaurant itself, where
            # "configured" invites "so I am set up / active / paid", and the model's
            # own vocabulary (`recorded_at` / `recorded_by`) is the honest one. The
            # SHAPES differ deliberately too — see `commercial_app.reads` for which
            # Admin-only fields are withheld and why.
            'subscription_terms': subscription_terms_summary(restaurant['id']),
        }

        response = {
            'status': 200,
            'message': 'Successfully retrieved the restaurant subscription information',
            'data': data
        }
        return Response(response, status=200)
