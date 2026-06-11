from django.urls import path

from orders_app.endpoints_kitchen import (
    ActiveKitchenOrdersView,
    KitchenOrderFulfilmentStatusView,
    KitchenOrderPriorityView,
    KitchenMenuItemsView,
    KitchenMenuItemStockView,
)

urlpatterns = [
    path('orders/active/', ActiveKitchenOrdersView.as_view()),
    path('orders/<str:pk>/fulfilment-status/', KitchenOrderFulfilmentStatusView.as_view()),
    path('orders/<str:pk>/priority/', KitchenOrderPriorityView.as_view()),
    path('menu-items/', KitchenMenuItemsView.as_view()),
    path('menu-items/<str:pk>/stock/', KitchenMenuItemStockView.as_view()),
]
