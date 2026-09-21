"""
The finance transactions endpoint.

Its one live action is the Dinify SUBSCRIPTION collection, which D07 disabled —
see ``finance_app.controllers.tx_subscription`` for why and for the contract.
This view is a thin adapter over that service and holds no business rule: the
refusal lives in the service precisely so an in-process caller cannot route
around it.

WHAT THIS FILE DELIBERATELY STILL DOES, unchanged by D07:
  * the transaction-type dispatch, so an unknown type keeps its existing 400;
  * ``can_manage_restaurant``, resolved against the BODY's restaurant id and
    answering 404 rather than 403, so a non-member cannot learn whether a
    restaurant exists (and a delegated principal is refused outright inside that
    predicate, independently of the delegated route allowlist);
  * passing every submitted field through to the service, so no caller has to be
    rewritten in order to be refused.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction
from misc_app.controllers.http import private_no_store
from users_app.controllers.permissions_check import can_manage_restaurant


class TransactionsEndpoint(APIView):
    """
    The endpoint for handling payments
    """
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        # Every answer here is scoped to one principal and one restaurant, and
        # the capability answer in particular must not be cached by a proxy and
        # replayed after the capability changes. Stamped in finalize_response so
        # no branch — success, refusal or error — can forget it.
        response = super().finalize_response(request, response, *args, **kwargs)
        return private_no_store(response)

    def post(self, request):
        data = request.data

        transaction_type = data.get('transaction_type')

        if transaction_type not in ['subscription']:
            return Response({
                'status': 400,
                'message': 'Invalid transaction type'
            }, status=400)

        restaurant_id = data.get('restaurant_id')
        # Authorize the caller against the target restaurant at the HTTP edge
        # (where request.user lives), mirroring the sibling report/kitchen
        # write paths. 404 (not 403) so a non-member can't learn whether the
        # restaurant exists; can_manage_restaurant fails closed on a
        # missing/empty restaurant_id, so a null id also 404s here.
        if not can_manage_restaurant(request.user, restaurant_id):
            return Response({'status': 404, 'message': 'Not found'}, status=404)

        if transaction_type == 'subscription':
            response = SubscriptionPaymentTransaction().initiate(
                restaurant_id=restaurant_id,
                transaction_platform=data.get('transaction_platform'),
                payment_mode=data.get('payment_mode'),
                user=request.user,
                msisdn=data.get('msisdn'),
                otp=data.get('otp'),
            )
            return Response(response, status=response.get('status', 200))
