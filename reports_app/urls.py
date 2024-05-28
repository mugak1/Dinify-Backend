from django.urls import path
from reports_app.endpoints.restuaurant_reports import RestaurantReportsEndpoint


urlpatterns = [
    path('restaurant/<str:report_name>/', RestaurantReportsEndpoint.as_view()),
]
