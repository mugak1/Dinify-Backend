from django.urls import path
from orders_app.endpoints.orders import V2OrdersEndpoint

urlpatterns = [
    path('<str:action>/', V2OrdersEndpoint.as_view()),
]
