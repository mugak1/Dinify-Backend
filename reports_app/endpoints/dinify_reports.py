from datetime import datetime
from rest_framework.response import Response
from rest_framework.views import APIView
from reports_app.controllers.dinify.dashboard import generate_dinify_dashboard
from reports_app.controllers.dinify.restaurants import generate_dinify_restaurant_report


class DinifyReportsEndpoint(APIView):
    def get(self, request, report_name):
        date_today = datetime.now().date()
        if report_name == 'dashboard':
            response = generate_dinify_dashboard()
        elif report_name == 'restaurant-listing':
            response = generate_dinify_restaurant_report(
                date_from=request.GET.get('from', None),
                date_to=request.GET.get('to', None),
                name=request.GET.get('name', None)
            )
        return Response(response)

