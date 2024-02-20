from django.urls import path
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint

urlpatterns = [
    path('<str:config_detail>/', RestaurantSetupEndpoint.as_view()),
]
