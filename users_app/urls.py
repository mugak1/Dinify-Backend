from django.urls import path
from users_app.endpoints.auth import UsersAuthenticationEndpoint
from users_app.endpoints.token_refresh import GatedTokenRefreshView
from users_app.endpoints.user_lookup import UserLookupEndpoint, MsisdnLookupEndpoint
from users_app.endpoints.user_profile import UserProfileEndpoint

# The owner-claim challenge lives in `platform_admin_app` with every other line of
# invitation state, but is MOUNTED here on the customer plane: the owner is claiming
# their own restaurant identity and an AdminSession has no authority in it. Same
# arrangement as the customer-plane half of delegation.
from platform_admin_app.endpoints.owner_claim import (
    OwnerClaimChallengeView, OwnerClaimRedeemView,
)


urlpatterns = [
    path('auth/<str:action>/', UsersAuthenticationEndpoint.as_view()),
    # GatedTokenRefreshView, not the stock TokenRefreshView: refresh is a place a
    # customer token is produced, so it carries the same account_type refusal as
    # login. Request/response contract is unchanged.
    path('auth/token/refresh/', GatedTokenRefreshView.as_view(), name='token_refresh'),
    path('user-lookup/', UserLookupEndpoint.as_view()),
    path('msisdn-lookup/', MsisdnLookupEndpoint.as_view()),
    path('user-profile/', UserProfileEndpoint.as_view()),
    # TWO EXPLICIT ROUTES, never `owner-claim/<str:action>/`. Requesting a
    # verification code and exercising a claim credential are different decisions with
    # different consequences, and which one a request made should be readable from the
    # path rather than from a body.
    path(
        'owner-claim/challenge/',
        OwnerClaimChallengeView.as_view(),
        name='owner-claim-challenge',
    ),
    path(
        'owner-claim/redeem/',
        OwnerClaimRedeemView.as_view(),
        name='owner-claim-redeem',
    ),
]
