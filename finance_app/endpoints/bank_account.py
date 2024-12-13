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
