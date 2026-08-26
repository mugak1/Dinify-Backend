"""
The customer-plane refresh route: gated on ``account_type`` AND customer access.

``login`` refuses a ``platform_staff`` account and so does ``reset_password``'s
resolver — but both gates bind only where a token is MINTED. The stock
``TokenRefreshView`` re-reads nothing about the account: SimpleJWT's refresh path
validates the token's signature and expiry and issues a fresh access token
without ever loading the user. An account that held a customer refresh token
before it became platform staff could therefore rotate it indefinitely and never
meet a gate again.

This subclass closes that by resolving the token's subject and refusing any account
the customer plane no longer admits, so those refusals hold on every path that
produces a customer token rather than only the first one.

TWO REASONS TO REFUSE, ONE LOOKUP, ONE RESPONSE (Step 2D.1 added the second):

* ``platform_staff`` — the account belongs to the admin plane;
* not customer-access-established — the identity was provisioned by Admin and has
  not completed its first owner claim.

They are read from a SINGLE resolved row rather than two ``exists()`` probes: the
question is "may this subject still hold a customer session", and asking it twice
would invite the two halves to drift. The refusal is unchanged — SimpleJWT's own
``InvalidToken``.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from users_app import customer_access
from users_app.models import User

logger = logging.getLogger(__name__)


class GatedTokenRefreshView(TokenRefreshView):
    """
    ``TokenRefreshView`` that refuses accounts the customer plane no longer admits.

    The refusal is SimpleJWT's own ``InvalidToken`` — HTTP 401 with the standard
    ``{'detail': 'Token is invalid or expired', 'code': 'token_not_valid'}`` body,
    and BOTH reasons produce exactly that, so the endpoint tells a prober neither
    which of the two it hit nor that it hit a gate at all rather than a generic
    rejection. It mirrors customer login answering both refusals with the generic
    wrong-password message.

    Precisely: SimpleJWT's own detail strings vary by failure reason (a malformed
    token says "Token is invalid", a blacklisted one "Token is blacklisted"), so the
    honest claim is that the two gated refusals are indistinguishable FROM EACH OTHER
    and land on the framework's generic invalid-token answer — not that they are
    byte-identical to every possible failure. The ``code`` is ``token_not_valid``
    throughout.

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
        reason = self._refusal_reason(data.get('refresh'))
        if reason is not None:
            logger.info('token refresh: refused (%s)', reason)
            raise InvalidToken('Token is invalid or expired')
        return super().post(request, *args, **kwargs)

    @staticmethod
    def _refusal_reason(raw_refresh):
        """
        Why ``raw_refresh``'s subject may not refresh, or ``None`` if it may.

        ONE lookup answers both questions. The subject is resolved to a real row
        rather than probed with two ``filter(...).exists()`` calls, because the
        question is a single one — "may this subject still hold a customer session" —
        and two probes would be two places for the answer to drift.

        Fails OPEN by design — but only into ``super()``, never into a token: an
        unreadable token, or one whose subject no longer exists, is a token
        ``super()`` is about to reject anyway, so the honest answer is "no reason of
        MINE to refuse" and the standard path produces the standard error.
        """
        if not raw_refresh:
            return None
        try:
            token = RefreshToken(raw_refresh)
            user_id = token.payload.get(api_settings.USER_ID_CLAIM)
            if not user_id:
                return None
            # USER_ID_FIELD is the UUID pk, so a claim that isn't a UUID raises
            # rather than returning empty — caught here with the rest.
            user = User.objects.filter(
                **{api_settings.USER_ID_FIELD: user_id},
            ).only('account_type', 'customer_access_state').first()
        except (TokenError, DjangoValidationError, TypeError, ValueError):
            return None

        if user is None:
            return None
        if user.account_type == ACCOUNT_TYPE_PLATFORM_STAFF:
            return 'platform staff on customer origin'
        if customer_access.is_refused(user):
            return 'customer access not established'
        return None
