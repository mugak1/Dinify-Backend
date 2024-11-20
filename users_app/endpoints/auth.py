from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users_app.controllers.self_register import self_register
from users_app.controllers.login import login
from users_app.controllers.reset_password import reset_password
from users_app.controllers.change_password import change_password
from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.otp_manager import OtpManager


class UsersAuthenticationEndpoint(APIView):
    """
    endpoint for authenticating users
    """
    permission_classes = (AllowAny,)

    def post(self, request, action):
        """
        handle the POST method
        """

        response = {'status': 500, 'message': "Invalid request"}

        if action == 'register':
            response = self_register(request.data)
        elif action == "login":
            response = login(
                request.data.get('username'),
                request.data.get('password')
            )
        elif action == "reset-password":
            response = reset_password(
                request.data.get('phone_number')
            )
        elif action == "change-password":
            # decode the token to get the user id
            auth = decode_jwt_token(request)
            response = change_password(
                user_id=auth['id'],
                old_password=request.data.get('old_password'),
                new_password=request.data['new_password'],  # not using get to avoid None
            )
        elif action == "resend-otp":
            identification = request.data.get('identification', 'msisdn')
            identifier = request.data.get('identifier', None)
            if request.user is not None and request.user.is_authenticated:
                identification = 'id'
                identifier = request.user.id
            response = OtpManager().resend_otp(
                identification=identification,
                identifier=identifier
            )
        return Response(
            response,
            status=response['status']
        )
