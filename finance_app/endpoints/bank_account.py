from rest_framework.views import APIView
from rest_framework.response import Response
from finance_app.serializers import SerializerPutBankAccountRecord
from misc_app.controllers.secretary import Secretary


REQUIRED_INFORMATION = [
    {'key': 'bank_name', 'label': 'BANK NAME', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'account_name', 'label': 'ACCOUNT NAME', 'type': 'char', 'min_length': 10, 'text_presentation': None},  # noqa
    {'key': 'account_number', 'label': 'ACCOUNT NUMBER', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'address_line1', 'label': 'ADDRESS LINE 1', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'address_line2', 'label': 'ADDRESS LINE 2', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'city', 'label': 'CITY', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
    {'key': 'country', 'label': 'COUNTRY', 'type': 'char', 'min_length': 5, 'text_presentation': None},  # noqa
]


class BankAccountRecordsEndpoint(APIView):
    def post(self, request):
        # TODO only dinify admins should be able to add bank account records

        data = request.data
        data = {k: (v.upper() if k not in ['restaurant', 'user'] else v) for k, v in data.items()}

        secretary_args = {
            'serializer': SerializerPutBankAccountRecord,
            'data': data,
            'required_information': REQUIRED_INFORMATION,
            'user_id': str(request.user.pk),
            'username': str(request.user.username),
            'success_message': 'The bank account record has been added successfully',
            'error_message': 'Sorry, an error occurred while adding the bank account record. Please try again later.',
            'user': request.user,
            'msg_type': 'new-bank-account',
        }
        response = Secretary(secretary_args).create()
        return Response(response, status=201)

    def get(self, request):

        filter = {}

        if 'restaurant' in request.query_params:
            filter['restaurant'] = request.query_params['restaurant']

        secretary_args = {
            'request': request,
            'serializer': SerializerPutBankAccountRecord,
            'filter': filter,
            'paginate': True,
            'user_id': request.user.id,
            'username': request.user.username,
            'success_message': 'The bank accounts have been retrieved successfully.',
            'error_message': 'Sorry, an error occurred while retrieving the bank accounts. Please try again later.',
        }

        response = Secretary(secretary_args).read()

        return Response(
            response,
            status=response['status']
        )
