"""
The customer plane's JWT authenticator, gated on ``account_type``.

WHY THIS EXISTS. ``login`` refuses a ``platform_staff`` account and so does
``GatedTokenRefreshView`` — but both bind where a token is MINTED. Nothing looked
at ``account_type`` when a token was PRESENTED, so an access token issued moments
before an account was promoted stayed valid for the rest of its lifetime
(``ACCESS_TOKEN_LIFETIME``, 30 minutes by default). Migration ``users_app/0013``
blacklists outstanding REFRESH tokens; access tokens are stateless and cannot be
revoked that way. This class closes the remaining window, so "platform staff
cannot act through a customer session" is true on every request rather than
eventually.

WHY NOT ``USER_AUTHENTICATION_RULE``. It looks like the natural hook and it is
not one. In SimpleJWT 5.5.1 that setting is consulted in exactly two places —
``TokenObtainSerializer.validate`` (login) and ``TokenRefreshSerializer.validate``
(refresh). ``JWTAuthentication.get_user`` never calls it; it checks
``CHECK_USER_IS_ACTIVE`` and nothing else. A custom rule would therefore gate only
the two paths that are ALREADY gated and leave the presented access token exactly
as it was. Do not "simplify" this class into that setting.

WHY NOT A MIDDLEWARE OR A PERMISSION CLASS. On this plane
``request.user.account_type == ACCOUNT_TYPE_PLATFORM_STAFF`` is true if and only if
the request is DELEGATED: ``DelegatedSessionAuthentication`` binds the
administrator's real ``User`` row, and ``delegated_sessions`` re-checks that it is
platform staff on every request. A generic check on ``request.user`` would refuse
the one legitimate platform-staff principal and break delegated drill-in entirely.
The refusal belongs on the JWT resolution path specifically — which is also why the
two channels can never collide: ``DelegatedAccessMiddleware`` refuses any request
carrying both ``Authorization`` and ``X-Delegation-Session`` before either
authenticator runs.

THE REFUSAL IS NOT AN ORACLE. It reuses SimpleJWT's own ``user_inactive`` failure —
byte-identical to what a DEACTIVATED ordinary customer receives from the branch
three lines above it in ``get_user``. A prober cannot tell "this account was
promoted to platform staff" from "this account was deactivated". The real reason
goes to the log, without the username or the token, mirroring
``GatedTokenRefreshView``.

WIRED IN TWO PLACES, and the second is the one that matters:

1. ``settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']`` — the DRF chain.
2. ``misc_app.controllers.decode_auth_token`` — which instantiates an authenticator
   DIRECTLY, outside the DRF chain, on behalf of ~30 customer-plane call sites. A
   gate wired only into settings would leave every one of them ungated.

``users_app.tests_customer_jwt_gate`` pins both wirings and fails if stock
``JWTAuthentication`` is named anywhere on the customer plane outside this module.
"""
import logging

from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF

logger = logging.getLogger(__name__)


class CustomerJWTAuthentication(JWTAuthentication):
    """
    ``JWTAuthentication`` that refuses platform-staff accounts.

    Subtracts only: a token that resolves to a ``restaurant_user`` is returned by
    ``super()`` untouched, and every other failure mode (bad signature, expiry,
    blacklist, missing claim, unknown user, inactive user) is left exactly as
    SimpleJWT produces it.
    """

    def get_user(self, validated_token):
        """
        Resolve the token's subject, refusing platform staff.

        The check runs AFTER ``super()`` deliberately: the row is already loaded by
        then, so this costs no additional query, and a token that was going to fail
        for an ordinary reason still fails for that reason.
        """
        user = super().get_user(validated_token)
        if getattr(user, 'account_type', None) == ACCOUNT_TYPE_PLATFORM_STAFF:
            logger.info('jwt auth: refused (platform staff on customer origin)')
            raise AuthenticationFailed(_('User is inactive'), code='user_inactive')
        return user
