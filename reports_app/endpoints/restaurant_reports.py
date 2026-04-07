"""
endpoints to handle order
"""
from datetime import datetime
from rest_framework.response import Response
from rest_framework.views import APIView
from reports_app.controllers.restaurant.dashboard import (
    generate_restaurant_dashboard_details,
    get_restaurant_dashboard_1,
    generate_restaurant_dashboard_v2,
    generate_restaurant_reviews_summary
)
from reports_app.controllers.restaurant.sales import (
    generate_restaurant_sales_summary,
    generate_restaurant_sales_listing,
    generate_restaurant_sales_trends
)
from reports_app.controllers.restaurant.diners import (
    generate_restaurant_diners_summary,
    generate_restaurant_diners_listing,
    generate_restaurant_diners_trends
)
from reports_app.controllers.restaurant.menu import generate_restaurant_menu_summary
from reports_app.controllers.restaurant.transactions import (
    generate_restaurant_transaction_summary,
    generate_restaurant_transaction_listing
)


class RestaurantReportsEndpoint(APIView):
    """
    The endpoint for handling reports for a restaurant
    """

    def get(self, request, report_name):
        date_today = datetime.now().date()
        if report_name == 'dashboard':
            response = generate_restaurant_dashboard_details(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'dashboard1':
            response = get_restaurant_dashboard_1(
                restaurant_id=request.GET.get('restaurant', None),
            )
        elif report_name == 'sales-summary':
            response = generate_restaurant_sales_summary(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'sales-listing':
            response = generate_restaurant_sales_listing(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'sales-trends':
            response = generate_restaurant_sales_trends(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today)),
                trend_category=request.GET.get('category', 'daily'),
                trend_result=request.GET.get('result', 'table')
            )
        elif report_name == 'diners-summary':
            response = generate_restaurant_diners_summary(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'diners-listing':
            response = generate_restaurant_diners_listing(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'diners-trends':
            response = generate_restaurant_diners_trends(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today)),
                trend_category=request.GET.get('category', 'daily'),
                trend_result=request.GET.get('result', 'table')
            )
        elif report_name == 'menu-summary':
            response = generate_restaurant_menu_summary(
                restaurant_id=request.GET.get('restaurant', None),
                grouping=request.GET.get('grouping', 'sections'),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'transactions-summary':
            response = generate_restaurant_transaction_summary(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
            )
        elif report_name == 'transactions-listing':
            response = generate_restaurant_transaction_listing(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today)),
                transaction_type=request.GET.get('type', None),
                transaction_status=request.GET.get('status', None)
            )
        elif report_name == 'dashboard-v2':
            response = generate_restaurant_dashboard_v2(
                restaurant_id=request.GET.get('restaurant'),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today)),
                period=request.GET.get('period', 'day')
            )
        elif report_name == 'dashboard-reviews':
            response = generate_restaurant_reviews_summary(
                restaurant_id=request.GET.get('restaurant')
            )
        else:
            response = {
                'status': 400,
                'message': 'Invalid report name'
            }

        return Response(response, status=response.get('status', 200))
