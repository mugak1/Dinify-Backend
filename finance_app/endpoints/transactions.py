"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from finance_app.controllers.initiate_transaction import initiate_transaction
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction
from finance_app.controllers.tx_disbursement import DisbursementTransaction

class TransactionsEndpoint(APIView):
    """
    The endpoint for handling payments
    """

    def post(self, request):
        data = request.data
        # response = initiate_transaction(
        #     transaction_type=data.get('transaction_type'),
        #     transaction_platform=data.get('transaction_platform'),
        #     actor=request.user,
        #     amount=data.get('amount'),
        #     otp=data.get('otp'),
        #     bank_account=data.get('bank_account'),
        # )
        # return Response(response, status=200)

        transaction_type = data.get('transaction_type')

        if transaction_type not in ['subscription', 'disbursement', 'order_refund']:
            return Response({
                'status': 400,
                'message': 'Invalid transaction type'
            }, status=400)

        if transaction_type == 'subscription':
            response = SubscriptionPaymentTransaction().initiate(
                restaurant_id=data.get('restaurant_id'),
                transaction_platform=data.get('transaction_platform'),
                payment_mode=data.get('payment_mode'),
                user=request.user,
                msisdn=data.get('msisdn'),
                otp=data.get('otp'),
            )
            return Response(response, status=response.get('status', 200))

        if transaction_type == 'disbursement':
            response = DisbursementTransaction().initiate(
                user=request.user,
                amount=data.get('amount'),
                payment_mode=data.get('payment_mode'),
                restaurant_id=data.get('restaurant_id'),
                msisdn=data.get('msisdn'),
                bank_account_id=data.get('bank_account_id'),
                otp=data.get('otp'),
            )
            return Response(response, status=response.get('status', 200))
