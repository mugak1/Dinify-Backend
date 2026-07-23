"""
Admin control-plane views.

``AdminAPIView`` is the base every downstream admin endpoint inherits; it restates
the authenticator + deny-by-default permission explicitly (belt-and-braces alongside
the ``settings_admin`` defaults). ``AdminHealthView`` is the sole unauthenticated
liveness route — it proves the vhost, daemon process and urlconf are wired before any
auth exists. Everything else in this app stays authenticated.
"""
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from platform_admin_app.authentication import AdminSessionAuthentication


class AdminAPIView(APIView):
    """Base view for the admin control plane — authenticated, deny-by-default."""

    authentication_classes = [AdminSessionAuthentication]
    permission_classes = [IsAuthenticated]


class AdminHealthView(APIView):
    """Unauthenticated liveness probe. Returns no sensitive detail."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'status': 'ok'})
