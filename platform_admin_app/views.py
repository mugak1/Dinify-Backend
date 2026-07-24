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

from platform_admin_app.audit import record_from_request
from platform_admin_app.authentication import AdminSessionAuthentication


class AdminAPIView(APIView):
    """
    Base view for the admin control plane — authenticated, deny-by-default.

    AUDIT CONVENTION: every unsafe-method admin endpoint (POST / PUT / PATCH /
    DELETE) must produce EXACTLY ONE ``AdminAuditLog`` entry — on success, on
    failure, and on denial alike — via ``self.audit(...)``. One entry per request
    keeps the log countable; a denied or failed attempt is exactly the event an
    audit log exists to capture, so an early return is not an excuse to skip it.

    Capture is deliberately explicit rather than automatic: middleware-style
    interception cannot know which resource an endpoint touched or what the before
    and after state were, so it would produce confidently mislabelled rows. The
    ratchet is ``platform_admin_app.testing.AuditAssertionsMixin`` — endpoint tests
    assert the entry, so a missing call fails the suite rather than passing quietly.
    """

    authentication_classes = [AdminSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def audit(self, request, action, *, result, **kwargs):
        """
        Record one audit entry for this request. Raises if the write fails.

        Thin delegate to ``audit.record_from_request`` — actor, session, request id,
        source IP and user agent come from the request automatically.
        """
        return record_from_request(request, action, result=result, **kwargs)


class AdminHealthView(APIView):
    """Unauthenticated liveness probe. Returns no sensitive detail."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'status': 'ok'})
