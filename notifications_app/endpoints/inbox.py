"""
The in-app notifications inbox (SHELL-FINISH-00, PR 4), under
``api/v1/notifications/inbox/``. The legacy ``api/v1/notifications/`` endpoint
(the MongoDB store) is untouched beside it.

  GET  inbox/?restaurant=<uuid>               newest first, at most 50, plus the unread count
  GET  inbox/unread-count/?restaurant=<uuid>  the count alone; cheap enough to poll
  POST inbox/<uuid>/read/                     mark one read
  POST inbox/read-all/?restaurant=<uuid>      mark every visible one read

``restaurant`` is REQUIRED (the bell covers the selected restaurant only). A
missing, malformed, foreign or unknown restaurant, and a notification the caller
may not see, all get the same 404, so the inbox confirms nothing about another
tenant. Every response is ``no-store``. The thin views hold no rule; the rule is
``notifications_app.inbox``.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications_app import inbox

NOT_FOUND = {'status': 404, 'message': 'Not found'}


def _no_store(response):
    response['Cache-Control'] = 'no-store, private'
    return response


def _restaurant(request):
    return inbox.parse_uuid(request.query_params.get('restaurant'))


class InboxEndpoint(APIView):
    def get(self, request):
        try:
            data = inbox.list_inbox(request.user, _restaurant(request))
        except inbox.NotFound:
            return _no_store(Response(NOT_FOUND, status=404))
        return _no_store(Response(
            {'status': 200, 'message': 'Notifications retrieved', 'data': data},
            status=200,
        ))


class InboxUnreadCountEndpoint(APIView):
    def get(self, request):
        try:
            count = inbox.unread_count(request.user, _restaurant(request))
        except inbox.NotFound:
            return _no_store(Response(NOT_FOUND, status=404))
        return _no_store(Response(
            {'status': 200, 'message': 'Unread count retrieved',
             'data': {'unread_count': count}},
            status=200,
        ))


class InboxMarkReadEndpoint(APIView):
    def post(self, request, notification_id):
        try:
            inbox.mark_read(request.user, notification_id)
        except inbox.NotFound:
            return _no_store(Response(NOT_FOUND, status=404))
        return _no_store(Response({'status': 200, 'message': 'Marked as read'}, status=200))


class InboxMarkAllReadEndpoint(APIView):
    def post(self, request):
        try:
            marked = inbox.mark_all_read(request.user, _restaurant(request))
        except inbox.NotFound:
            return _no_store(Response(NOT_FOUND, status=404))
        return _no_store(Response(
            {'status': 200, 'message': 'Marked all as read', 'data': {'marked': marked}},
            status=200,
        ))
