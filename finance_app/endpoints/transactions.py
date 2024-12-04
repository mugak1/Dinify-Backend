"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from finance_app.controllers.initiate_transaction import initiate_transaction


class TransactionsEndpoint(APIView):
    """
    The endpoint for handling payments
    """

    def post(self, request):
        data = request.data
        response = initiate_transaction(
            transaction_type=data.get('transaction_type'),
            transaction_platform=data.get('transaction_platform'),
            actor=request.user,
            amount=data.get('amount'),
            otp=data.get('otp')
        )
        return Response(response, status=200)
