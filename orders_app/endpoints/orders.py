"""
endpoints to handle order
"""
import logging

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from orders_app.models import Order
from orders_app.controllers.initiate_order import initiate_order
from orders_app.controllers.v2_initiate_order import (
    handle_add_order_items,
    v2_initiate_order,
    handle_delete_items
)
from orders_app.serializers import SerializerListGetOrder
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
from orders_app.controllers.con_orders import ConOrder

logger = logging.getLogger(__name__)


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
                    return Response(response, status=401)
                data['customer'] = None
                data['created_by'] = str(user)
            else:
                if user is not None:
                    user = str(user)
                data['customer'] = user
                data['created_by'] = None

            response = initiate_order(data)
            return Response(response, status=response.get('status', 200))

        elif action == 'review':
            data = request.data
            try:
                response = rate_and_review(
                    order=data.get('order'),
                    order_item=data.get('order_item'),
                    rating=data.get('rating'),
                    review=data.get('review')
                )
                return Response(response, status=response.get('status', 200))
            except Exception as error:
                logger.error("Error while reviewing order: %s", error)
                response = {
                    'status': 400,
                    'message': 'Sorry, an error occurred.'
                }
                return Response(response, status=400)
        elif action == 'block-review':
            # check that the token is provided
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
                return Response(response, status=response.get('status', 200))
            except Exception as error:
                logger.error("Error while blocking review: %s", error)
                response = {
                    'status': 400,
                    'message': 'Sorry, an error occurred while blocking the review.'
                }
                return Response(response, status=400)

    def put(self, request, action):
        if action in ['submit', 'prepare', 'cancel']:
            data = request.data
            # source = data.get('source')

            user = request.user
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

            return Response(response, status=response.get('status', 200))

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

            return Response(response, status=response.get('status', 200))


class V2OrdersEndpoint(APIView):
    """
    The V2 endpoint for handling orders
    """
    permission_classes = [AllowAny]

    def post(self, request, action):
        if action == 'initiate':
            data = request.data
            source = data.get('source')
            user = request.user
            try:
                user = request.user.pk
            except Exception:
                user = None

            customer = None
            created_by = None

            if source == 'admin':
                if user is None:
                    response = {
                        'status': 401,
                        'message': 'Please log in'
                    }
                    return Response(response, status=401)
                created_by = request.user
            else:
                if user is not None:
                    user = str(user)
                    customer = request.user

            restaurant_id = data.get('restaurant')
            table_id = data.get('table')
            items = data.get('items')
            order_remarks = data.get('order_remarks')
            if restaurant_id is None or table_id is None:
                response = {
                    'status': 400,
                    'message': 'Please provide the restaurant and table ID'
                }
                return Response(response, status=400)
            # response = v2_initiate_order(
            #     restaurant_id=restaurant_id,
            #     table_id=table_id,
            #     items=items,
            #     order_remarks=order_remarks,
            #     customer=customer,
            #     created_by=created_by,
            # )
            response = ConOrder.initiate_order(
                restaurant_id=restaurant_id,
                table_id=table_id,
                items=items,
                order_remarks=order_remarks,
                customer=customer,
                created_by=created_by,
            )
            return Response(response, status=response.get('status', 200))

        elif action == 'add-items':
            data = request.data
            order_id = data.get('order')
            items = data.get('items')
            response = handle_add_order_items(
                order_id=order_id,
                items=items
            )
            return Response(response, status=response.get('status', 200))

    def delete(self, request, action):
        if action == 'add-items':
            data = request.data
            item_id = data.get('item')
            response = handle_delete_items(
                order_item=item_id,
                reason=data.get('reason'),
                user=request.user
            )
            return Response(response, status=response.get('status', 200))

    def get(self, request, action):
        """
        This endpoint is used to get the order items for a given order
        """
        order_id = request.GET.get('order')

        if action == 'details':
            if order_id is None:
                response = {
                    'status': 400,
                    'message': 'No order reference found'
                }
                return Response(response, status=400)

            try:
                order = Order.objects.get(id=order_id)
                order_data = SerializerListGetOrder(order, many=False).data
                response = {
                    'status': 200,
                    'message': 'Order retrieved successfully',
                    'data': order_data
                }
            except Exception as error:
                logger.error("Error retrieving order: %s", error)
                response = {
                    'status': 404,
                    'message': 'Order not found'
                }
            return Response(response, status=response['status'])
