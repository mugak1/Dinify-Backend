"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from orders_app.models import Order
from orders_app.controllers.initiate_order import initiate_order
from orders_app.controllers.manage_order import update_order_status
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated,
    OrderStatus_Pending,
    OrderItemStatus_Preparing, OrderItemStatus_Served,
    OrderStatus_Cancelled,
    OrderStatus_Served
)


class OrdersEndpoint(APIView):
    """
    The endpoint for handling orders
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

    def put(self, request, action):
        if action in ['submit', 'prepare', 'cancel']:
            data = request.data
            # source = data.get('source')

            user = request.user.pk
            if user is None:
                if action not in ['submit']:
                    response = {
                        'status': 400,
                        'message': 'Please log in'
                    }
                    return Response(response, status=400)
                else:
                    user = None

            order_statuses = {
                'submit': OrderStatus_Pending,
                'prepare': OrderItemStatus_Preparing,
                'cancel': OrderStatus_Cancelled
            }
            new_status = order_statuses.get(action)
            order = Order.objects.get(id=data.get('order'))

            response = update_order_status(
                order=order,
                new_status=new_status,
                user=user
            )

            return Response(response, status=200)

    # def get(self, request, action):


