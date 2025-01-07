from rest_framework.views import APIView
from rest_framework.response import Response
from crm_app.serializers import SerializerPutServiceTicket, SerializerGetServiceTicket
from misc_app.controllers.secretary import Secretary
from misc_app.controllers.define_filter_params import define_filter_params


REQUIRED_INFORMATION = [
    {'key': 'ticket_type', 'label': 'TYPE OF TICKET', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_title', 'label': 'TITLE', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_description', 'label': 'DESCRIPTION', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
]


EDIT_INFORMATION = [
    {'key': 'ticket_type', 'label': 'TYPE OF TICKET', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_title', 'label': 'TITLE', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_description', 'label': 'DESCRIPTION', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    {'key': 'ticket_status', 'label': 'STATUS', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_priority', 'label': 'PRIORITY', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'restaurant', 'label': 'RESTAURANT', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'assigned_to', 'label': 'ASSIGNED TO', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'resolution_notes', 'label': 'RESOLUTION NOTES', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
]


class ServiceTicketsEndpoint(APIView):
    def post(self, request):
        data = request.data

        secretary_args = {
            'serializer': SerializerPutServiceTicket,
            'data': data,
            'required_information': REQUIRED_INFORMATION,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The ticket has been raised successfully',
            'error_message': 'Sorry, an error occurred while raising the ticket. Please try again later.',
            'user': request.user,
            'msg_type': 'service-ticket',
        }
        response = Secretary(secretary_args).create()
        return Response(response, status=201)

    def get(self, request):

        filter_params = request.GET.copy()
        orm_filter = define_filter_params(
            get_params=filter_params,
            model='servicetickets'
        )
        secretary_args = {
            'request': request,
            'serializer': SerializerGetServiceTicket,
            'filter': orm_filter,
            'paginate': True,
            'user_id': request.user.id,
            'username': request.user.username,
            'success_message': 'The service tickets have been retrieved successfully.',
            'error_message': 'Sorry, an error occurred while retrieving the service tickets. Please try again later.',
        }

        response = Secretary(secretary_args).read()

        return Response(
            response,
            status=response['status']
        )

    def put(self, request):
        data = request.data

        secretary_args = {
            'serializer': SerializerPutServiceTicket,
            'data': data,
            'edit_considerations': EDIT_INFORMATION,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The ticket has been updated successfully',
            'error_message': 'Sorry, an error occurred while updating the ticket. Please try again later.',  # noqa
            'user': request.user,
            'msg_type': 'service-ticket',
        }
        response = Secretary(secretary_args).update()
        return Response(response, status=response['status'])
