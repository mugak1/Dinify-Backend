"""
The one user-profile resource: ``GET`` reads it, ``PUT`` self-updates it.

WHY ``GET`` EXISTS (Step 2F.3). Owner-claim redemption hands back
``token + refresh + restaurant_id`` and deliberately no profile — a claim
transaction is not a profile endpoint. But the restaurant portal persists a
principal containing ``profile``, and its route guard reads
``profile.restaurant_roles``, so a session alone cannot bootstrap it. The client
must not close that gap itself: inventing ``restaurant_roles`` from the single
``restaurant_id`` the claim returned would be a guess about tenant authority
(an owner may hold several memberships, and the one just claimed carries a
resolved permissions map the client cannot compute), and fabricating a partial
profile would put a second, wrong source of truth in front of the real one.

Nor can the claimant simply log in again: an owner membership sets
``require_otp`` in ``users_app.controllers.login``, so ordinary login would
demand a SECOND verification code moments after the claim transaction consumed
its own — asking a user to prove themselves twice for one act.

So the read side of this resource is the bootstrap. It is an ordinary
authenticated customer read that happens to be the missing half of that handoff,
not a claim-specific surface: nothing here knows about invitations, and a claim
token has no authority in it.
"""
import logging

from django.utils.cache import patch_vary_headers
from rest_framework.response import Response
from rest_framework.views import APIView

from users_app.controllers.update_user_profile import (
    self_update_user_profile,
)
from users_app.serializers import SerGetUserProfile

logger = logging.getLogger(__name__)


class UserProfileEndpoint(APIView):
    """
    ``users/user-profile/`` — the authenticated customer's own profile.

    AUTHORITY IS THE DEFAULT CUSTOMER STACK, and deliberately nothing more:
    ``CustomerJWTAuthentication`` + ``IsAuthenticated`` from
    ``settings.REST_FRAMEWORK``. That is what makes the three refusals this
    endpoint needs true without restating any of them here — platform staff on a
    customer token, a ``pending_initial_claim`` identity and a deactivated user
    are all refused inside ``get_user`` before a handler runs. Re-checking them
    in the view would create a second copy of a gate that already exists, which
    is how the two come to disagree.

    A delegated administrator cannot reach either verb: this route is
    deliberately absent from ``platform_admin_app.configs.delegation_scopes``
    ``ALLOWED_ROUTES`` (it acts on ``request.user``, i.e. on the administrator's
    OWN records, and has no restaurant dimension to scope), so
    ``DelegatedAccessMiddleware`` refuses it before dispatch. Adding ``GET`` did
    not widen that — the allowlist is keyed on ``(route, method)`` and neither
    pair is in it.
    """

    def finalize_response(self, request, response, *args, **kwargs):
        """
        Stamp every response non-cacheable, on the VIEW rather than per return.

        Both verbs answer with the canonical profile — identity fields plus every
        restaurant membership and its resolved module permissions — which has no
        business in a shared cache, a proxy, or a browser's back button. Setting
        it here rather than at one ``return`` is the same reasoning the owner-claim
        views use: a branch added later cannot forget it.

        ``Vary: Authorization`` because the body genuinely varies by the bearer
        token, mirroring how ``misc_app.controllers.http.no_store`` varies the
        diner responses on the header that determines them. No cookie is set.
        """
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        patch_vary_headers(response, ('Authorization',))
        return response

    def get(self, request):
        """
        Return the canonical profile for the authenticated caller.

        GENUINELY READ-ONLY. No ``last_login`` touch, no token minted or rotated,
        no OTP consumed, no invitation or ``customer_access_state`` written, no
        action log, no audit row, no field repaired. A GET that fixes state is not
        a read, and this one is the bootstrap for a session that was just
        established by a transaction which already recorded everything durable.

        THE SERIALIZER IS THE CONTRACT, and it is the same one login emits.
        ``SerGetUserProfile`` with no ``restaurant_roles`` context delegates to
        ``get_any_restaurant_roles`` — the same call ``login`` makes and feeds in
        through context — so the two paths cannot disagree about which tenants a
        principal holds or what it may do there. That equivalence is the whole
        architectural point: owner claim and ordinary login must bootstrap the
        same frontend principal.

        The membership resolver is authoritative. Nothing here reads the
        ``restaurant_id`` a redemption returned: that is CONTEXT for the UI, not
        authority, and a principal's memberships come from the database.
        """
        return Response(
            {
                'status': 200,
                'message': 'Profile retrieved.',
                'data': {
                    'profile': SerGetUserProfile(request.user).data,
                },
            },
            status=200,
        )

    def put(self, request):
        try:
            response = self_update_user_profile(
                user_id=request.user.id,
                country=request.data.get('country'),
                first_name=request.data.get('first_name'),
                last_name=request.data.get('last_name'),
                other_names=request.data.get('other_names'),
                email=request.data.get('email'),
                phone_number=request.data.get('phone_number')
            )
            return Response(response, status=response.get('status', 200))
        except Exception as error:
            logger.error("Error while updating profile: %s", error)
            response = {
                'status': 400,
                'message': 'Sorry, an error occurred.'
            }
            return Response(response, status=400)
