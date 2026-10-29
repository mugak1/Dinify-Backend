from django.urls import path
from notifications_app.endpoints.notifications import NotificationsEndpoint
from notifications_app.endpoints.inbox import (
    InboxEndpoint,
    InboxMarkAllReadEndpoint,
    InboxMarkReadEndpoint,
    InboxUnreadCountEndpoint,
)

urlpatterns = [
    # The legacy MongoDB-backed endpoint, unchanged.
    path('', NotificationsEndpoint.as_view()),
    # The PostgreSQL in-app inbox (SHELL-FINISH-00, PR 4).
    path('inbox/', InboxEndpoint.as_view()),
    path('inbox/unread-count/', InboxUnreadCountEndpoint.as_view()),
    path('inbox/read-all/', InboxMarkAllReadEndpoint.as_view()),
    path('inbox/<str:notification_id>/read/', InboxMarkReadEndpoint.as_view()),
]
