"""
endpoints to handle order
"""
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import MODULE_REPORTS
from reports_app.controllers.restaurant.dashboard import (
    generate_restaurant_dashboard_details,
    generate_restaurant_dashboard_v2
)
from reports_app.controllers.restaurant.sales import (
    generate_restaurant_sales_listing,
    generate_restaurant_sales_trends,
    generate_restaurant_sales_hourly
)
from reports_app.controllers.restaurant.diners import (
    generate_restaurant_diners_summary,
    generate_restaurant_diners_listing
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
        # Tenant isolation: every restaurant report is single-target, scoped by
        # the client-supplied ?restaurant=. Gate that one restaurant on the
        # `reports` module before dispatching — a dinify admin reads any; a role
        # without `reports` (or a cross-tenant / missing id) is denied. 404, not
        # 403, so we don't confirm another tenant's restaurant exists, and the
        # guard runs before the invalid-name 400 so report validity isn't leaked.
        if not can_user_access_module(
            request.user, request.GET.get('restaurant'), MODULE_REPORTS,
        ):
            return Response({'status': 404, 'message': 'Not found'}, status=404)

        # Default the from/to window to *today in EAT*. A naive datetime.now()
        # returns the server's UTC wall clock, so between 00:00-03:00 EAT it
        # would report yesterday; timezone.localdate() resolves in EAT.
        date_today = timezone.localdate()
        if report_name == 'dashboard':
            response = generate_restaurant_dashboard_details(
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
        elif report_name == 'sales-hourly':
            response = generate_restaurant_sales_hourly(
                restaurant_id=request.GET.get('restaurant', None),
                date_from=request.GET.get('from', str(date_today)),
                date_to=request.GET.get('to', str(date_today))
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
                period=request.GET.get('period', 'day'),
                # No default: absence must stay distinguishable from an empty
                # string, so the controller can treat `&bucket=` as "not supplied"
                # (legacy `period` path) rather than as an unknown granularity.
                bucket=request.GET.get('bucket')
            )
        else:
            response = {
                'status': 400,
                'message': 'Invalid report name'
            }

        return Response(response, status=response.get('status', 200))
