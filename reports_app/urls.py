from django.urls import path
from reports_app.endpoints.restaurant_reports import RestaurantReportsEndpoint


urlpatterns = [
    path('restaurant/<str:report_name>/', RestaurantReportsEndpoint.as_view()),
]
