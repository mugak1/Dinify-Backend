import logging

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users_app.models import User

logger = logging.getLogger(__name__)


class UserLookupEndpoint(APIView):
    def get(self, request):
        try:
            # check if the identity includes @
            contact = request.GET.get('contact')

            if '@' in contact:
                user = User.objects.values(
                    'id', 'first_name', 'last_name',
                    'phone_number', 'email'
                ).get(email=contact)
            else:
                # TODO internationalise the phone number
                user = User.objects.values(
                    'id', 'first_name', 'last_name',
                    'phone_number', 'email'
                ).get(phone_number=contact)
            response = {
                'status': 200,
                'message': 'User found',
                'data': {
                    'id': str(user.get('id')),
                    'first_name': user.get('first_name'),
                    'last_name': user.get('last_name'),
                    'phone_number': user.get('phone_number'),
                    'email': user.get('email')
                }
            }
            return Response(response, status=200)
        except Exception as error:
            logger.error("Error while looking up user: %s", error)
            response = {
                'status': 404,
                'message': 'User not found'
            }
            return Response(response, status=404)


class MsisdnLookupEndpoint(APIView):
    permission_classes = (AllowAny,)

    def get(self, request):
        try:
            User.objects.values('id').get(
                phone_number=request.GET.get('msisdn')
            )
            response = {
                'status': 200,
                'message': 'User found',
                'data': {
                    'found': True
                }
            }
        except Exception as error:
            logger.error("Error while looking up user: %s", error)
            response = {
                'status': 404,
                'message': 'User not found',
                'data': {
                    'found': False
                }
            }

        return Response(response, status=response['status'])
