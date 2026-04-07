import logging

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

from orders_app.models import KitchenTicket
from orders_app.serializers_kds import KitchenTicketSerializer
from dinify_backend.configss.string_definitions import (
    KdsStatus_New, KdsStatus_InPrep, KdsStatus_Ready,
    KdsStatus_Fulfilled, KdsStatus_Cancelled
)

logger = logging.getLogger(__name__)

VALID_TRANSITIONS = {
    KdsStatus_New: [KdsStatus_InPrep, KdsStatus_Cancelled],
    KdsStatus_InPrep: [KdsStatus_Ready, KdsStatus_Cancelled],
    KdsStatus_Ready: [KdsStatus_Fulfilled, KdsStatus_Cancelled],
    KdsStatus_Fulfilled: [],
    KdsStatus_Cancelled: [],
}

STATUS_TIMESTAMP_MAP = {
    KdsStatus_InPrep: 'prep_started_at',
    KdsStatus_Ready: 'ready_at',
    KdsStatus_Fulfilled: 'fulfilled_at',
    KdsStatus_Cancelled: 'cancelled_at',
}


class KitchenTicketListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        restaurant_id = request.query_params.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query parameter is required'},
                status=400
            )

        status_param = request.query_params.get('status', 'new,in_prep,ready')
        statuses = [s.strip() for s in status_param.split(',')]

        tickets = KitchenTicket.objects.filter(
            restaurant_id=restaurant_id,
            status__in=statuses,
            deleted=False
        ).order_by('placed_at')

        serializer = KitchenTicketSerializer(tickets, many=True)
        return Response(
            {'status': 200, 'message': 'Tickets retrieved successfully', 'data': serializer.data},
            status=200
        )


class KitchenTicketDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk):
        try:
            ticket = KitchenTicket.objects.get(id=pk, deleted=False)
        except KitchenTicket.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Ticket not found'},
                status=404
            )

        serializer = KitchenTicketSerializer(ticket)
        return Response(
            {'status': 200, 'message': 'Ticket retrieved successfully', 'data': serializer.data},
            status=200
        )

    def patch(self, request, pk):
        try:
            ticket = KitchenTicket.objects.get(id=pk, deleted=False)
        except KitchenTicket.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Ticket not found'},
                status=404
            )

        new_status = request.data.get('status')
        if not new_status:
            return Response(
                {'status': 400, 'message': 'status field is required'},
                status=400
            )

        allowed = VALID_TRANSITIONS.get(ticket.status, [])
        if new_status not in allowed:
            return Response(
                {'status': 400, 'message': f'Invalid transition from {ticket.status} to {new_status}'},
                status=400
            )

        ticket.status = new_status

        timestamp_field = STATUS_TIMESTAMP_MAP.get(new_status)
        if timestamp_field:
            setattr(ticket, timestamp_field, timezone.now())

        ticket.save()

        serializer = KitchenTicketSerializer(ticket)
        return Response(
            {'status': 200, 'message': 'Ticket updated successfully', 'data': serializer.data},
            status=200
        )
