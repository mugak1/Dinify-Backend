from rest_framework.views import APIView
from rest_framework.response import Response
from crm_app.serializers import SerializerPutServiceTicket
from misc_app.controllers.secretary import Secretary


REQUIRED_INFORMATION = [
    {'key': 'ticket_type', 'label': 'TYPE OF TICKET', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_title', 'label': 'TITLE', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'ticket_description', 'label': 'DESCRIPTION', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
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

        filter = {}

        if 'restaurant' in request.query_params:
            filter['restaurant'] = request.query_params['restaurant']

        secretary_args = {
            'request': request,
            'serializer': SerializerPutServiceTicket,
            'filter': filter,
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
