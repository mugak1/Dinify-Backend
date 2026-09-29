"""
Admin-plane middleware.

``ClientIPMiddleware`` is wired only into ``settings_admin.MIDDLEWARE``. Both names
here attach a request attribute the audit log consumes, and always call
``get_response``.
"""
from django.conf import settings

from dinify_backend.request_context import RequestContextMiddleware

#: The admin plane's historical name for the request-ID middleware, kept so every
#: existing import and dotted path keeps working. Since D15 R2 it IS the common
#: implementation both planes install (``dinify_backend.request_context``): a fresh
#: ``uuid4().hex`` per request, NEVER read from a client header, so a caller cannot
#: forge or pin the audit correlation ID, and echoed as ``X-Request-ID``. A stack that
#: lists it twice still yields one ID and one outcome record per request.
RequestIDMiddleware = RequestContextMiddleware


def client_ip_from_request(request):
    """
    The client IP from the trusted network position.

    Given the verified topology (Apache is the only hop — no load balancer or proxy
    in front), the trustworthy value is ``REMOTE_ADDR``. ``ADMIN_TRUSTED_PROXY_DEPTH``
    is 0, so raw ``X-Forwarded-For`` is NOT trusted; the depth>0 branch is only
    future-proofing for a deliberately-configured proxy chain.

    A function rather than middleware-only logic because the delegated-access gate on
    the CUSTOMER plane needs the same value, and that plane does not install
    ``ClientIPMiddleware``. One definition, two callers.
    """
    depth = getattr(settings, 'ADMIN_TRUSTED_PROXY_DEPTH', 0)
    remote_addr = request.META.get('REMOTE_ADDR')
    if depth <= 0:
        return remote_addr
    forwarded = [
        part.strip()
        for part in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')
        if part.strip()
    ]
    return forwarded[-depth] if len(forwarded) >= depth else remote_addr


class ClientIPMiddleware:
    """Set ``request.client_ip`` via :func:`client_ip_from_request`."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.client_ip = client_ip_from_request(request)
        return self.get_response(request)
