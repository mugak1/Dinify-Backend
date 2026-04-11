from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from restaurants_app.endpoints.upsell_config import check_restaurant_permission
from restaurants_app.models import Table, Reservation
from restaurants_app.serializers import SerializerPublicGetTable


TABLE_STATUS_CHOICES = {
    'available', 'seated', 'bill_requested', 'dirty', 'out_of_service'
}


class TableActionsEndpoint(APIView):
    """Endpoint for table lifecycle actions (seat, clear, transfer, etc.)."""

    def post(self, request, action):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        dispatch = {
            'seat': self._seat,
            'clear': self._clear,
            'transfer': self._transfer,
            'update-status': self._update_status,
            'update-floor-plan': self._update_floor_plan,
        }

        handler = dispatch.get(action)
        if not handler:
            return Response(
                {'status': 400, 'message': f'Unknown action: {action}'},
                status=400
            )

        return handler(request)

    def _get_table_or_error(self, table_id):
        """Look up a non-deleted table by ID. Returns (table, None) or (None, Response)."""
        try:
            table = Table.objects.get(id=table_id, deleted=False)
            return table, None
        except Table.DoesNotExist:
            return None, Response(
                {'status': 404, 'message': 'Table not found'}, status=404
            )

    # ------------------------------------------------------------------
    # action = "seat"
    # ------------------------------------------------------------------
    def _seat(self, request):
        data = request.data
        table_id = data.get('table_id')
        if not table_id:
            return Response(
                {'status': 400, 'message': 'table_id is required'}, status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not check_restaurant_permission(request.user, str(table.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        table.status = 'seated'
        table.save()

        # If a reservation is provided, mark it as seated
        reservation_id = data.get('reservation_id')
        if reservation_id:
            try:
                reservation = Reservation.objects.get(
                    id=reservation_id, deleted=False
                )
                reservation.status = 'seated'
                reservation.seated_at = timezone.now()
                reservation.table = table
                reservation.save()
            except Reservation.DoesNotExist:
                pass  # Non-fatal: table is already seated

        return Response({
            'status': 200,
            'message': 'Table seated successfully',
            'data': SerializerPublicGetTable(table).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "clear"
    # ------------------------------------------------------------------
    def _clear(self, request):
        data = request.data
        table_id = data.get('table_id')
        if not table_id:
            return Response(
                {'status': 400, 'message': 'table_id is required'}, status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not check_restaurant_permission(request.user, str(table.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        mark_as = data.get('mark_as', 'dirty')
        if mark_as not in ('dirty', 'available'):
            return Response(
                {'status': 400, 'message': 'mark_as must be "dirty" or "available"'},
                status=400
            )

        table.status = mark_as
        table.save()

        return Response({
            'status': 200,
            'message': 'Table cleared successfully',
            'data': SerializerPublicGetTable(table).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "transfer"
    # ------------------------------------------------------------------
    def _transfer(self, request):
        data = request.data
        source_id = data.get('source_table_id')
        dest_id = data.get('destination_table_id')
        if not source_id or not dest_id:
            return Response(
                {'status': 400, 'message': 'source_table_id and destination_table_id are required'},
                status=400
            )

        source, err = self._get_table_or_error(source_id)
        if err:
            return err
        dest, err = self._get_table_or_error(dest_id)
        if err:
            return err

        if source.restaurant_id != dest.restaurant_id:
            return Response(
                {'status': 400, 'message': 'Both tables must belong to the same restaurant'},
                status=400
            )

        if not check_restaurant_permission(request.user, str(source.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        with transaction.atomic():
            source.status = 'dirty'
            source.save()

            dest.status = 'seated'
            dest.save()

            # Move any active reservation from source to destination
            Reservation.objects.filter(
                table=source, status='seated', deleted=False
            ).update(table=dest)

        return Response({
            'status': 200,
            'message': 'Party transferred successfully',
            'data': {
                'source': SerializerPublicGetTable(source).data,
                'destination': SerializerPublicGetTable(dest).data,
            }
        }, status=200)

    # ------------------------------------------------------------------
    # action = "update-status"
    # ------------------------------------------------------------------
    def _update_status(self, request):
        data = request.data
        table_id = data.get('table_id')
        new_status = data.get('status')
        if not table_id or not new_status:
            return Response(
                {'status': 400, 'message': 'table_id and status are required'},
                status=400
            )

        if new_status not in TABLE_STATUS_CHOICES:
            return Response(
                {'status': 400, 'message': f'Invalid status. Must be one of: {", ".join(sorted(TABLE_STATUS_CHOICES))}'},
                status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not check_restaurant_permission(request.user, str(table.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        old_status = table.status
        table.status = new_status

        if new_status == 'out_of_service':
            table.is_active = False
        elif old_status == 'out_of_service':
            table.is_active = True

        table.save()

        return Response({
            'status': 200,
            'message': 'Table status updated successfully',
            'data': SerializerPublicGetTable(table).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "update-floor-plan"
    # ------------------------------------------------------------------
    def _update_floor_plan(self, request):
        data = request.data
        restaurant_id = data.get('restaurant')
        tables_data = data.get('tables')

        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant is required'}, status=400
            )
        if not tables_data or not isinstance(tables_data, list):
            return Response(
                {'status': 400, 'message': 'tables array is required'}, status=400
            )

        if not check_restaurant_permission(request.user, restaurant_id):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        updated = 0
        with transaction.atomic():
            for entry in tables_data:
                table_id = entry.get('id')
                if not table_id:
                    continue
                try:
                    table = Table.objects.get(
                        id=table_id, restaurant=restaurant_id, deleted=False
                    )
                except Table.DoesNotExist:
                    continue

                if 'floor_x' in entry:
                    table.floor_x = float(entry['floor_x'])
                if 'floor_y' in entry:
                    table.floor_y = float(entry['floor_y'])
                if 'floor_width' in entry:
                    table.floor_width = float(entry['floor_width'])
                if 'floor_height' in entry:
                    table.floor_height = float(entry['floor_height'])

                table.save()
                updated += 1

        return Response({
            'status': 200,
            'message': f'{updated} table(s) updated successfully',
            'data': {'updated_count': updated}
        }, status=200)
