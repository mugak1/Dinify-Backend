"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users_app.models import User
from orders_app.controllers.initiate_order import initiate_order


class OrdersEndpoint(APIView):
    """
    the endpoint for handling orders
    """
    permission_classes = [AllowAny]

    def post(self, request, action):
        if action == 'initiate':
            data = request.data
            source = data.get('source')
            user = request.user.pk

            if source == 'admin':
                if user is None:
                    response = {
                        'status': 401,
                        'message': 'Please log in'
                    }
                    return Response(response, status=200)
                data['customer'] = None
                data['created_by'] = str(user)
            else:
                if user is not None:
                    user = str(user)
                data['customer'] = user
                data['created_by'] = None

            response = initiate_order(data)
            return Response(response, status=200)
