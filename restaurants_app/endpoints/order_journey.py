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
            response = handle_table_scan(
                table_id=request.GET.get('table')
            )
        elif stage == 'show-menu':
            response = handle_show_menu(
                restaurant_id=request.GET.get('restaurant'),
                ignore_approval=request.GET.get('ignore-approval')
            )
        elif stage == 'order-details':
            response = handle_show_order_details(
                order_id=request.GET.get('order')
            )
        elif stage == 'payment-details':
            response = handle_show_transaction_details(
                transaction_id=request.GET.get('transaction')
            )
        else:
            response = {
                'status': 400,
                'message': 'Error'
            }
        return Response(response, status=200)
