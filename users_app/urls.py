from django.urls import path
from users_app.endpoints.auth import UsersAuthenticationEndpoint
from users_app.endpoints.token_refresh import GatedTokenRefreshView
from users_app.endpoints.user_lookup import UserLookupEndpoint, MsisdnLookupEndpoint
from users_app.endpoints.user_profile import UserProfileEndpoint


urlpatterns = [
    path('auth/<str:action>/', UsersAuthenticationEndpoint.as_view()),
    # GatedTokenRefreshView, not the stock TokenRefreshView: refresh is a place a
    # customer token is produced, so it carries the same account_type refusal as
    # login. Request/response contract is unchanged.
    path('auth/token/refresh/', GatedTokenRefreshView.as_view(), name='token_refresh'),
    path('user-lookup/', UserLookupEndpoint.as_view()),
    path('msisdn-lookup/', MsisdnLookupEndpoint.as_view()),
    path('user-profile/', UserProfileEndpoint.as_view()),
]
