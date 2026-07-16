"""
the endpoint hit when one scans the QR code at the table
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from restaurants_app.models import Restaurant, Table
from restaurants_app.controllers.handle_diner_journey import (
    handle_table_scan, handle_show_menu,
    handle_show_order_details, handle_show_transaction_details
)
from orders_app.serializers import SerializerPublicOrderDetails


class OrderJourneyEndpoint(APIView):
    permission_classes = [AllowAny]

    def get(self, request, stage):

        if stage == 'table-scan':
            response = handle_table_scan(request)
        elif stage == 'show-menu':
            # Public AllowAny path: the diner menu must ALWAYS reflect published
            # (approved + enabled) state. The retired `ignore-approval` query
            # param is intentionally NOT read here — a caller-controlled preview
            # flag must never bypass publication state. An authorised menu
            # preview, if needed, belongs on a dedicated authenticated endpoint.
            response = handle_show_menu(
                restaurant_id=request.GET.get('restaurant')
            )
        elif stage == 'order-details':
            response = handle_show_order_details(request)
        elif stage == 'payment-details':
            response = handle_show_transaction_details(request)
        else:
            response = {
                'status': 400,
                'message': 'Error'
            }
        return Response(response, status=response.get('status', 200))
