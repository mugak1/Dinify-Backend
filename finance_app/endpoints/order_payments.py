"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from orders_app.models import Order
from finance_app.controllers.initiate_order_payment import initiate_order_payment
from dinify_backend.configss.string_definitions import TransactionPlatform_Web


class OrderPaymentsEndpoint(APIView):
    """
    The endpoint for handling order payments
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        user = request.user
        if user.pk is None:
            user = None

        manual_payment = data.get('manual_payment', None)
        if manual_payment in ['t', 'true', True]:
            manual_payment = True
        else:
            manual_payment = False

        response = initiate_order_payment(
            order=Order.objects.get(id=data.get('order')),
            payment_mode=data.get('payment_mode'),
            transaction_platform=data.get('platform', TransactionPlatform_Web),
            payment_form=data.get('payment_form'),
            msisdn=data.get('msisdn'),
            amount=data.get('amount'),
            user=user,
            manual_payment=manual_payment,
            manual_payment_details=data.get('manual_payment_details')
        )
        return Response(response, status=200)
