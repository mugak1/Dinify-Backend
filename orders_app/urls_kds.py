from django.urls import path
from orders_app.endpoints_kds import KitchenTicketListView, KitchenTicketDetailView

urlpatterns = [
    path('tickets/', KitchenTicketListView.as_view()),
    path('tickets/<str:pk>/', KitchenTicketDetailView.as_view()),
]
