"""
Small HTTP response helpers shared across the diner-facing endpoints.

Capability-scoped diner responses — a table scan that returns a session token,
or order / payment / review data bound to a diner session — must never be stored
by a browser, shared proxy or intermediary cache. ``no_store`` stamps the
standard non-cacheable headers and varies on the diner capability headers so a
cache can never key one diner's response for another (or an absent) credential.
"""
from django.utils.cache import patch_vary_headers

# The custom headers that carry diner authority; responses that depend on them
# Vary on them (defensive — the payloads are already no-store).
DINER_CAPABILITY_HEADERS = ('X-Diner-Session', 'X-Diner-Credential')


def no_store(response):
    """
    Mark a capability-scoped diner response as non-cacheable and vary it on the
    diner capability headers. Returns the same response for chaining.
    """
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    patch_vary_headers(response, DINER_CAPABILITY_HEADERS)
    return response


def private_no_store(response):
    """
    Mark an AUTHENTICATED, principal-scoped response as non-cacheable.

    The sibling ``no_store`` above is for the DINER capability channel and varies
    on the diner headers. A JWT-authenticated operator response has no diner
    credential to vary on — the thing that distinguishes one principal's response
    from another's there is ``Authorization`` — so it gets its own two-line
    helper rather than borrowing a Vary that does not apply to it.

    Same cache headers, different Vary. Returns the same response for chaining.
    """
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    patch_vary_headers(response, ('Authorization',))
    return response


class NoStoreResponseMixin:
    """
    APIView mixin that stamps every response from the view as non-cacheable via
    ``no_store`` (success and error paths alike). Use on anonymous diner
    endpoints whose responses carry capability-scoped data. List it BEFORE
    ``APIView`` in the bases so this ``finalize_response`` takes precedence.
    """

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return no_store(response)
