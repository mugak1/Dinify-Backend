"""
Admin-plane middleware (wired only into ``settings_admin.MIDDLEWARE``).

No custom middleware existed in the repo before this, so these follow the standard
new-style callable convention. Both attach a request attribute the PR-3 audit log
will consume, and always call ``get_response``.
"""
import uuid

from django.conf import settings


class RequestIDMiddleware:
    """
    Attach a server-generated ``request.request_id`` to every request.

    Always a fresh ``uuid4`` — NEVER read from a client header, so a caller cannot
    forge or pin the audit correlation id. Echoed on the response as ``X-Request-ID``
    for log correlation.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid.uuid4().hex
        response = self.get_response(request)
        response['X-Request-ID'] = request.request_id
        return response


class ClientIPMiddleware:
    """
    Set ``request.client_ip`` from the trusted network position.

    Given the verified topology (Apache is the only hop — no load balancer or proxy
    in front), the trustworthy value is ``REMOTE_ADDR``. ``ADMIN_TRUSTED_PROXY_DEPTH``
    is 0, so raw ``X-Forwarded-For`` is NOT trusted; the depth>0 branch is only
    future-proofing for a deliberately-configured proxy chain.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        depth = getattr(settings, 'ADMIN_TRUSTED_PROXY_DEPTH', 0)
        remote_addr = request.META.get('REMOTE_ADDR')
        if depth <= 0:
            request.client_ip = remote_addr
        else:
            forwarded = [
                part.strip()
                for part in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')
                if part.strip()
            ]
            request.client_ip = (
                forwarded[-depth] if len(forwarded) >= depth else remote_addr
            )
        return self.get_response(request)
