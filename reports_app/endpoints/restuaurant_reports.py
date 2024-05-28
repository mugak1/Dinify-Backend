"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from reports_app.controllers.dashboard import generate_restaurant_dashboard_details


class RestaurantReportsEndpoint(APIView):
    """
    The endpoint for handling reports for a restaurant
    """
    permission_classes = [AllowAny]

    def get(self, request, report_name):
        if report_name == 'dashboard':
            response = generate_restaurant_dashboard_details(
                restaurant_id=request.GET.get('restaurant', None)
            )
        return Response(response, status=200)
