from django.urls import path
from finance_app.endpoints.order_payments import OrderPaymentsEndpoint


urlpatterns = [
    path('initiate-order-payment/', OrderPaymentsEndpoint.as_view()),
]
