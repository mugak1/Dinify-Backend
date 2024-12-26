from django.urls import path
from crm_app.endpoints.tickets import ServiceTicketsEndpoint

urlpatterns = [
    path('service-tickets/', ServiceTicketsEndpoint.as_view()),
]
