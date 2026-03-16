"""
endpoints for restaurant manager actions
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from restaurants_app.controllers.first_time_batch_approval import (
    first_time_batch_approval,
)
from misc_app.controllers.decode_auth_token import decode_jwt_token


class RestaurantManagerActionsEndpoint(APIView):
    """
    the endpoint for restaurant management actions
    """
    def post(self, request, action):
        """
        handle the POST method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        post_data = request.data

        if action == 'first-time-menu-review':
            response = first_time_batch_approval(
                restaurant_id=post_data.get('restaurant'),
                approval_decision=post_data.get('decision'),
                rejection_reason=post_data.get('reason'),
                auth=auth,
                user=request.user
            )
        return Response(response, status=response.get('status', 200))
