from django.urls import path
from finance_app.endpoints.transactions import TransactionsEndpoint


urlpatterns = [
    path('transactions/', TransactionsEndpoint.as_view()),
]
