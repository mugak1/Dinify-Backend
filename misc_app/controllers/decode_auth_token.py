"""
implementation to decode the auth token
"""
from rest_framework_simplejwt.authentication import JWTAuthentication


def decode_jwt_token(request):
    """
    Decode the JWT token
    """
    auth = JWTAuthentication().authenticate(request)
    if auth is None:
        raise Exception("Invalid Token")
    user = auth[0]
    return {
        'id': str(user.id),
        'user_id': str(user.id),
        'username': str(user.username),
    }
