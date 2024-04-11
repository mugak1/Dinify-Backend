from rest_framework.response import Response
from rest_framework.views import APIView
from users_app.models import User


class UserLookupEndpoint(APIView):
    def get(self, request):
        try:
            # check if the identity includes @
            contact = request.GET.get('contact')

            if '@' in contact:
                user = User.objects.values('id', 'phone_number', 'email').get(
                    email=contact
                )
            else:
                # TODO internationalise the phone number
                user = User.objects.values('id', 'phone_number', 'email').get(
                    phone_number=contact
                )
            response = {
                'status': 200,
                'message': 'User found',
                'data': {
                    'id': str(user.get('id'))
                }
            }
            return Response(response, status=200)
        except Exception as error:
            print(f"Error while looking up user: {error}")
            response = {
                'status': 400,
                'message': 'User not found'
            }
            return Response(response, status=200)
