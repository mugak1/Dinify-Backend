"""
endpoints to handle order
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction
from users_app.controllers.permissions_check import can_manage_restaurant

class TransactionsEndpoint(APIView):
    """
    The endpoint for handling payments
    """
    permission_classes = [IsAuthenticated]

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
