from django.urls import path
from orders_app.endpoints.orders import OrdersEndpoint

urlpatterns = [
    path('<str:action>/', OrdersEndpoint.as_view()),
]
