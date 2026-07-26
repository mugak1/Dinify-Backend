"""
The customer-plane refresh route, gated on ``account_type``.

``login`` refuses a ``platform_staff`` account and so does ``reset_password``'s
resolver — but both gates bind only where a token is MINTED. The stock
``TokenRefreshView`` re-reads nothing about the account: SimpleJWT's refresh path
validates the token's signature and expiry and issues a fresh access token
without ever loading the user. An account that held a customer refresh token
before it became platform staff could therefore rotate it indefinitely and never
meet a gate again.

This subclass closes that by resolving the token's subject and refusing platform
staff, so "platform staff cannot hold a customer session" holds on every path
that produces a customer token, not just the first one.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from users_app.models import User

logger = logging.getLogger(__name__)


class GatedTokenRefreshView(TokenRefreshView):
    """
    ``TokenRefreshView`` that refuses platform-staff accounts.

    The refusal is SimpleJWT's own ``InvalidToken`` — HTTP 401 with the standard
    ``{'detail': ..., 'code': 'token_not_valid'}`` body. That is deliberate: it is
    byte-identical to what a forged, expired or blacklisted refresh token returns,
    so the endpoint is not an account-type oracle. It mirrors customer login
    answering a platform-staff attempt with the generic wrong-password message.

    A token that cannot be parsed, or whose subject cannot be resolved, is NOT
    rejected here — it falls through to ``super()``, which owns malformed input
    and produces exactly the errors it always did. The success response is
    likewise untouched: this view only ever subtracts.
    """

    def post(self, request, *args, **kwargs):
        # A non-dict body (a bare JSON list, say) has no `.get`; that is a
        # malformed request `super()` already answers with a clean 400, so read
        # the field defensively rather than turning it into a 500 here.
        data = request.data if isinstance(request.data, dict) else {}
        if self._is_platform_staff(data.get('refresh')):
            logger.info('token refresh: refused (platform staff on customer origin)')
            raise InvalidToken('Token is invalid or expired')
        return super().post(request, *args, **kwargs)

    @staticmethod
    def _is_platform_staff(raw_refresh):
        """
        Whether ``raw_refresh`` belongs to a platform-staff account.

        Fails OPEN by design — but only into ``super()``, never into a token: an
        unreadable token is a token ``super()`` is about to reject anyway, so the
        honest answer is "not a platform-staff refresh" and let the standard path
        produce the standard error.
        """
        if not raw_refresh:
            return False
        try:
            token = RefreshToken(raw_refresh)
            user_id = token.payload.get(api_settings.USER_ID_CLAIM)
            if not user_id:
                return False
            # USER_ID_FIELD is the UUID pk, so a claim that isn't a UUID raises
            # rather than returning empty — caught here with the rest.
            return User.objects.filter(
                **{api_settings.USER_ID_FIELD: user_id},
                account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
            ).exists()
        except (TokenError, DjangoValidationError, TypeError, ValueError):
            return False
