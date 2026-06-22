from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import MODULE_TABLES
from restaurants_app.models import WaitlistEntry, Restaurant
from restaurants_app.serializers import (
    SerializerGetWaitlistEntry, SerializerPutWaitlistEntry
)


class WaitlistEndpoint(APIView):
    """Endpoint for managing the restaurant walk-in waitlist."""

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

        entries = WaitlistEntry.objects.filter(
            restaurant=restaurant_id, deleted=False
        )

        # Default to 'waiting' if no status filter provided
        status_param = request.GET.get('status', 'waiting')
        if status_param:
            statuses = [s.strip() for s in status_param.split(',')]
            entries = entries.filter(status__in=statuses)

        data = SerializerGetWaitlistEntry(entries, many=True).data
        return Response({
            'status': 200,
            'message': 'Waitlist entries retrieved successfully',
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

        guest_name = post_data.get('guest_name')
        if not guest_name:
            return Response(
                {'status': 400, 'message': 'guest_name is required'},
                status=400
            )

        try:
            Restaurant.objects.get(id=restaurant_id)
        except Restaurant.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404
            )

        serializer = SerializerPutWaitlistEntry(data={
            **post_data,
            'created_by': str(request.user.id),
        })
        if serializer.is_valid():
            entry = serializer.save()
            data = SerializerGetWaitlistEntry(entry).data
            return Response({
                'status': 201,
                'message': 'Waitlist entry created successfully',
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
        entry_id = put_data.get('id')
        if not entry_id:
            return Response(
                {'status': 400, 'message': 'id is required'},
                status=400
            )

        try:
            entry = WaitlistEntry.objects.get(id=entry_id, deleted=False)
        except WaitlistEntry.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Waitlist entry not found'}, status=404
            )

        if not can_user_access_module(
            request.user, str(entry.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        serializer = SerializerPutWaitlistEntry(
            entry, data=put_data, partial=True
        )
        if serializer.is_valid():
            entry = serializer.save()
            data = SerializerGetWaitlistEntry(entry).data
            return Response({
                'status': 200,
                'message': 'Waitlist entry updated successfully',
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

        entry_id = request.data.get('id') or request.GET.get('id')
        if not entry_id:
            return Response(
                {'status': 400, 'message': 'id is required'},
                status=400
            )

        try:
            entry = WaitlistEntry.objects.get(id=entry_id, deleted=False)
        except WaitlistEntry.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Waitlist entry not found'}, status=404
            )

        if not can_user_access_module(
            request.user, str(entry.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        entry.deleted = True
        entry.deleted_by = request.user
        entry.save()
        return Response({
            'status': 200,
            'message': 'Waitlist entry deleted successfully'
        }, status=200)
