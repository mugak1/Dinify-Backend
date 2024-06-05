from rest_framework.response import Response
from rest_framework.views import APIView
from reports_app.controllers.dinify.dashboard import generate_dinify_dashboard
from reports_app.controllers.dinify.restaurants import generate_dinify_restaurant_report
from reports_app.controllers.dinify.transactions import generate_dinify_transaction_report


class DinifyReportsEndpoint(APIView):
    def get(self, request, report_name):
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
        return Response(response)
