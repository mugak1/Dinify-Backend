from rest_framework.response import Response
from rest_framework.views import APIView
from notifications_app.controllers.notifications import get_notifications


class NotificationsEndpoint(APIView):
    def get(self, request):
        notifications = get_notifications(
            email=request.user.email,
            phone=request.user.phone_number,
            skip_read=request.query_params.get('skip_read', False),
            skip_archived=request.query_params.get('skip_archived', True)
        )
        response = {
            'status': 200,
            'message': "Successfully retrieved the notifications",
            'data': notifications
        }
        return Response(response, status=200)
