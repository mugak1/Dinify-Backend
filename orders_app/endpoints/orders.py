"""
endpoints to handle order
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from orders_app.models import Order
from orders_app.controllers.initiate_order import initiate_order
from orders_app.controllers.rate import rate_and_review
from orders_app.controllers.manage_order import update_order_status, update_item_status
from orders_app.controllers.rate import block_review
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

        elif action == 'review':
            data = request.data
            try:
                response = rate_and_review(
                    order=data.get('order'),
                    order_item=data.get('order_item'),
                    rating=data.get('rating'),
                    review=data.get('review')
                )
                return Response(response, status=200)
            except Exception as error:
                print(f"Error while reviewing order: {error}")
                response = {
                    'status': 400,
                    'message': 'Sorry, an error occurred.'
                }
                return Response(response, status=200)
        elif action == 'block-review':
            # check that the token is provided
            print(request.user)
            if request.user is None or request.user.is_anonymous:
                response = {
                    'status': 400,
                    'message': 'Please log in'
                }
                return Response(response, status=400)
            data = request.data
            try:
                response = block_review(
                    user=request.user,
                    order=data.get('order'),
                    order_item=data.get('order_item'),
                    block_reason=data.get('block_reason')
                )
                return Response(response, status=200)
            except Exception as error:
                print(f"Error while blocking review: {error}")
                response = {
                    'status': 400,
                    'message': 'Sorry, an error occurred while blocking the review.'
                }
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

        elif action in ['update-item']:
            data = request.data

            user = request.user.pk
            if user is None:
                response = {
                    'status': 400,
                    'message': 'Please log in'
                }
                return Response(response, status=400)

            item_id = data.get('item_id')
            new_status = data.get('status')
            response = update_item_status(
                item_id=item_id,
                new_status=new_status,
                user=request.user
            )

            return Response(response, status=200)


