from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from reports_app.controllers.dinify.dashboard import generate_dinify_dashboard
from reports_app.controllers.dinify.restaurants import generate_dinify_restaurant_report
from reports_app.controllers.dinify.transactions import generate_dinify_transaction_report
from users_app.controllers.permissions_check import is_dinify_admin


class DinifyReportsEndpoint(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, report_name):
        # Platform-admin (dinify-mgt) surface: cross-tenant revenue, owner PII
        # and the whole transaction ledger. Deny non-admins with 404 (not 403),
        # mirroring RestaurantReportsEndpoint — so a non-admin can't confirm the
        # endpoint exists, and it fires before the invalid-name 400 so report
        # validity isn't leaked either.
        if not is_dinify_admin(request.user):
            return Response({'status': 404, 'message': 'Not found'}, status=404)
        if report_name == 'dashboard':
            response = generate_dinify_dashboard()
        elif report_name == 'restaurant-listing':
            response = generate_dinify_restaurant_report(
                date_from=request.GET.get('from', None),
                date_to=request.GET.get('to', None),
                name=request.GET.get('name', None)
            )
        elif report_name == 'transactions-listing':
            response = generate_dinify_transaction_report(
                date_from=request.GET.get('from', None),
                date_to=request.GET.get('to', None),
                restaurant_id=request.GET.get('restaurant', None),
                transaction_status=request.GET.get('status', None),
                transaction_type=request.GET.get('type', None)
            )
        else:
            response = {
                'status': 400,
                'message': 'Invalid report specification'
            }
        return Response(response, status=response.get('status', 200))
