from bson import ObjectId
from rest_framework.response import Response
from rest_framework.views import APIView
from notifications_app.controllers.notifications import (
    get_notifications, flag_notification_as_read
)


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

    def put(self, request):
        notification_id = request.data.get('notification_id')
        if not notification_id:
            response = {
                'status': 400,
                'message': "notification_id is required"
            }
            return Response(response, status=400)

        if not ObjectId.is_valid(notification_id):
            response = {
                'status': 400,
                'message': "Invalid notification_id"
            }
            return Response(response, status=400)

        flagged = flag_notification_as_read(
            notification_id,
            email=request.user.email,
            phone=request.user.phone_number
        )
        if not flagged:
            response = {
                'status': 404,
                'message': "notification not found"
            }
            return Response(response, status=404)

        response = {
            'status': 200,
            'message': "Successfully flagged the notification as read"
        }
        return Response(response, status=200)
