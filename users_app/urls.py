from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from users_app.endpoints.auth import UsersAuthenticationEndpoint
from users_app.endpoints.user_lookup import UserLookupEndpoint, MsisdnLookupEndpoint
from users_app.endpoints.user_profile import UserProfileEndpoint, V2UserProfileEndpoint


urlpatterns = [
    path('auth/<str:action>/', UsersAuthenticationEndpoint.as_view()),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('user-lookup/', UserLookupEndpoint.as_view()),
    path('msisdn-lookup/', MsisdnLookupEndpoint.as_view()),
    path('user-profile/', UserProfileEndpoint.as_view()),
    path('user-profile/<str:action>/', V2UserProfileEndpoint.as_view()),
]
