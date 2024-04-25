"""
the endpoint hit when one scans the QR code at the table
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from restaurants_app.models import Restaurant, Table
from restaurants_app.controllers.handle_diner_journey import handle_table_scan, handle_show_menu


class OrderJourneyEndpoint(APIView):
    permission_classes = [AllowAny]

    def get(self, request, stage):

        if stage == 'table-scan':
            response = handle_table_scan(
                table_id=request.GET.get('table')
            )
        elif stage == 'show-menu':
            response = handle_show_menu(
                restaurant_id=request.GET.get('restaurant')
            )
        
        else:
            response = {
                'status': 400,
                'message': 'Error'
            }
        return Response(response, status=200)
