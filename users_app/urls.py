from django.urls import path
from users_app.endpoints.auth import UsersAuthenticationEndpoint
from users_app.endpoints.user_lookup import UserLookupEndpoint
from users_app.endpoints.user_profile import UserProfileEndpoint

urlpatterns = [
    path('auth/<str:action>/', UsersAuthenticationEndpoint.as_view()),
    path('user-lookup/', UserLookupEndpoint.as_view()),
    path('user-profile/', UserProfileEndpoint.as_view()),
]
