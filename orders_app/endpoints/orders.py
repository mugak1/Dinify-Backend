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

            if source == 'admin':
                data['customer'] = None
                data['created_by'] = str(request.user.pk)
            else:
                user = request.user.pk
                print(user, request.user)

                data['customer'] = user
                data['created_by'] = None

            # response = initiate_order(data)
            response = {}
            return Response(response, status=200)
