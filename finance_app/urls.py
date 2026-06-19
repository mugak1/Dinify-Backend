from django.urls import path
from finance_app.endpoints.order_payments import OrderPaymentsEndpoint
from finance_app.endpoints.transactions import TransactionsEndpoint


urlpatterns = [
    path('initiate-order-payment/', OrderPaymentsEndpoint.as_view()),
    path('transactions/', TransactionsEndpoint.as_view()),
]
