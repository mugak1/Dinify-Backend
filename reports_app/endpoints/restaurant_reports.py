"""
endpoints to handle order
"""
from datetime import datetime
from rest_framework.response import Response
from rest_framework.views import APIView
from reports_app.controllers.dashboard import generate_restaurant_dashboard_details
from reports_app.controllers.sales import (
    generate_restaurant_sales_summary,
    generate_restaurant_sales_listing,
    generate_restaurant_sales_trends
)
from reports_app.controllers.diners import (
    generate_restaurant_diners_summary,
    generate_restaurant_diners_listing
)


class RestaurantReportsEndpoint(APIView):
    """
    The endpoint for handling reports for a restaurant
    """

    def get(self, request, report_name):
        if report_name == 'dashboard':
            response = generate_restaurant_dashboard_details(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date()))
            )
        if report_name == 'sales-summary':
            response = generate_restaurant_sales_summary(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date()))
            )
        if report_name == 'sales-listing':
            response = generate_restaurant_sales_listing(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date()))
            )
        if report_name == 'sales-trends':
            response = generate_restaurant_sales_trends(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date())),
                trend_category=request.GET.get('category', 'daily'),
                trend_result=request.GET.get('result', 'table')
            )
        if report_name == 'diners-summary':
            response = generate_restaurant_diners_summary(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date()))
            )
        if report_name == 'diners-listing':
            response = generate_restaurant_diners_listing(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(datetime.now().date())),
                date_to=request.GET.get('to', str(datetime.now().date()))
            )

        return Response(response, status=200)
