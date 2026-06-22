from datetime import datetime

from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import MODULE_TABLES
from restaurants_app.models import Reservation, Restaurant
from restaurants_app.serializers import (
    SerializerGetReservation, SerializerPutReservation
)


class ReservationsEndpoint(APIView):
    """Endpoint for managing restaurant reservations."""

    def get(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_TABLES):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        reservations = Reservation.objects.filter(
            restaurant=restaurant_id, deleted=False
        )

        # Optional filters
        status_param = request.GET.get('status')
        if status_param:
            statuses = [s.strip() for s in status_param.split(',')]
            reservations = reservations.filter(status__in=statuses)

        date_param = request.GET.get('date')
        if date_param:
            try:
                target_date = datetime.strptime(date_param, '%Y-%m-%d').date()
                reservations = reservations.filter(date_time__date=target_date)
            except ValueError:
                return Response(
                    {'status': 400, 'message': 'Invalid date format. Use YYYY-MM-DD.'},
                    status=400
                )

        table_param = request.GET.get('table')
        if table_param:
            reservations = reservations.filter(table=table_param)

        data = SerializerGetReservation(reservations, many=True).data
        return Response({
            'status': 200,
            'message': 'Reservations retrieved successfully',
            'data': data
        }, status=200)

    def post(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        post_data = request.data
        restaurant_id = post_data.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant is required'},
                status=400
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_TABLES):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # Validate required fields
        guest_name = post_data.get('guest_name')
        date_time = post_data.get('date_time')
        if not guest_name or not date_time:
            return Response(
                {'status': 400, 'message': 'guest_name and date_time are required'},
                status=400
            )

        try:
            Restaurant.objects.get(id=restaurant_id)
        except Restaurant.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404
            )

        serializer = SerializerPutReservation(data={
            **post_data,
            'created_by': str(request.user.id),
        })
        if serializer.is_valid():
            reservation = serializer.save()
            data = SerializerGetReservation(reservation).data
            return Response({
                'status': 201,
                'message': 'Reservation created successfully',
                'data': data
            }, status=201)
        return Response({
            'status': 400,
            'message': 'Validation error',
            'errors': serializer.errors
        }, status=400)

    def put(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        put_data = request.data
        reservation_id = put_data.get('id')
        if not reservation_id:
            return Response(
                {'status': 400, 'message': 'id is required'},
                status=400
            )

        try:
            reservation = Reservation.objects.get(id=reservation_id, deleted=False)
        except Reservation.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Reservation not found'}, status=404
            )

        if not can_user_access_module(
            request.user, str(reservation.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        serializer = SerializerPutReservation(
            reservation, data=put_data, partial=True
        )
        if serializer.is_valid():
            reservation = serializer.save()
            data = SerializerGetReservation(reservation).data
            return Response({
                'status': 200,
                'message': 'Reservation updated successfully',
                'data': data
            }, status=200)
        return Response({
            'status': 400,
            'message': 'Validation error',
            'errors': serializer.errors
        }, status=400)

    def delete(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        reservation_id = request.data.get('id') or request.GET.get('id')
        if not reservation_id:
            return Response(
                {'status': 400, 'message': 'id is required'},
                status=400
            )

        try:
            reservation = Reservation.objects.get(id=reservation_id, deleted=False)
        except Reservation.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Reservation not found'}, status=404
            )

        if not can_user_access_module(
            request.user, str(reservation.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        reservation.deleted = True
        reservation.deleted_by = request.user
        reservation.save()
        return Response({
            'status': 200,
            'message': 'Reservation deleted successfully'
        }, status=200)
