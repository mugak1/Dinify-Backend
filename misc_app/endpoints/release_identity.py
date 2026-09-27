"""``GET`` the loaded-process release identity (D08 B3) — one bounded, non-secret document
saying which installed Backend release the answering worker process loaded at startup.
See ``dinify_backend/release_identity.py`` for what it means and what it never discloses.

Unauthenticated, like the two health probes: it is how a promotion verifier, and later a
peer's release gate, asks a RUNNING process what it is, without holding any credential.
It reads no request data, touches no database and has no side effect. ``no-store`` so no
cache between the process and the verifier can answer for a process that has since gone.
One class serves both planes; each plane's urlconf names the plane it is, and a process
started for the other plane answers ``mismatch`` rather than lending its identity.
"""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from dinify_backend import release_identity


class ReleaseIdentityView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    plane = None

    def get(self, request):
        response = Response(release_identity.current(self.plane))
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response
