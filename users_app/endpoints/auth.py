from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users_app.controllers.self_register import self_register
from users_app.controllers.login import login
from users_app.controllers.reset_password import reset_password, initiate_password_reset
from users_app.controllers.change_password import change_password
from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.otp_manager import OtpManager
from users_app.throttles import LoginThrottle, OtpThrottle, PasswordResetThrottle


# Map action names to their throttle classes.
# Actions not listed here get no extra throttling.
_ACTION_THROTTLES = {
    'login': [LoginThrottle],
    'verify-otp': [OtpThrottle],
    'resend-otp': [OtpThrottle],
    'initiate-reset-password': [PasswordResetThrottle],
    'reset-password': [PasswordResetThrottle],
}


class UsersAuthenticationEndpoint(APIView):
    """
    endpoint for authenticating users
    """
    permission_classes = (AllowAny,)

    def get_throttles(self):
        """Return throttle instances based on the requested action."""
        action = self.kwargs.get('action', '')
        throttle_classes = _ACTION_THROTTLES.get(action, [])
        return [t() for t in throttle_classes]

    def post(self, request, action):
        """
        handle the POST method
        """

        response = {'status': 500, 'message': "Invalid request"}

        if action == 'register':
            response = self_register(data=request.data)
        elif action == "login":
            response = login(
                username=request.data.get('username'),
                password=request.data.get('password'),
                source=request.data.get('source', 'restaurant')
            )
        elif action == "initiate-reset-password":
            response = initiate_password_reset(
                username=request.data.get('phone_number')
            )
        elif action == "reset-password":
            response = reset_password(
                username=request.data.get('phone_number'),
                otp=request.data.get('otp')
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
            purpose = request.data.get('purpose', None)
            skip_auth = request.data.get('skip_auth', 'no')

            if skip_auth == 'no' and request.user is not None and request.user.is_authenticated:
                identification = 'id'
                identifier = request.user.id
            response = OtpManager().resend_otp(
                identification=identification,
                identifier=identifier,
                purpose=purpose
            )
        elif action == 'verify-otp':
            user = request.data.get('user', None)
            if request.user is not None and request.user.is_authenticated:
                user = str(request.user.id)

            response = OtpManager().verify_otp(
                user_id=user,
                otp=request.data.get('otp')
            )

        return Response(
            response,
            status=response['status']
        )
