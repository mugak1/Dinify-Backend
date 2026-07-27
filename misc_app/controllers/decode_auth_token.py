"""
implementation to decode the auth token
"""
from users_app.authentication import CustomerJWTAuthentication


def decode_jwt_token(request):
    """
    Decode the JWT token

    Uses ``CustomerJWTAuthentication``, NOT the stock ``JWTAuthentication``. This
    helper runs OUTSIDE the DRF authenticator chain — it is called directly by ~30
    customer-plane endpoints — so the plane's account_type gate does not reach it
    from ``DEFAULT_AUTHENTICATION_CLASSES``. It has to be named here too, or every
    one of those call sites would still honour a promoted account's access token.
    """
    auth = CustomerJWTAuthentication().authenticate(request)
    if auth is None:
        raise Exception("Invalid Token")
    user = auth[0]
    return {
        'id': str(user.id),
        'user_id': str(user.id),
        'username': str(user.username),
    }
