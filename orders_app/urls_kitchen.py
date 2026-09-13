from django.urls import path

from orders_app.endpoints_kitchen import (
    ActiveKitchenOrdersView,
    CompletedKitchenOrdersView,
    KitchenOrderFulfilmentStatusView,
    KitchenOrderPriorityView,
    KitchenOrderCancelView,
    KitchenOrderStateView,
    KitchenMenuItemsView,
    KitchenMenuItemStockView,
)

urlpatterns = [
    path('orders/active/', ActiveKitchenOrdersView.as_view()),
    path('orders/completed/', CompletedKitchenOrdersView.as_view()),
    path('orders/<str:pk>/fulfilment-status/', KitchenOrderFulfilmentStatusView.as_view()),
    path('orders/<str:pk>/priority/', KitchenOrderPriorityView.as_view()),
    path('orders/<str:pk>/cancel/', KitchenOrderCancelView.as_view()),
    # The per-order OBSERVATION that settles an uncertain command. Declared
    # beside the commands it reconciles, not with the feeds: it answers for an
    # order that has left both of them, which is the case that needs it.
    path('orders/<str:pk>/state/', KitchenOrderStateView.as_view()),
    path('menu-items/', KitchenMenuItemsView.as_view()),
    path('menu-items/<str:pk>/stock/', KitchenMenuItemStockView.as_view()),
]
