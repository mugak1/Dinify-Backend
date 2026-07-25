"""
Delegated-session authentication — the BINDER, and nothing more.

This class exists for one reason: ~40 authorization gates across the customer plane
read ``request.user``, so a delegated request must arrive at them with a principal
bound. It does not decide anything. Every decision — is the credential real, is it
still live, may it reach this route, may it write — was already made by
``DelegatedAccessMiddleware`` before the view was dispatched.

Hence the deliberate design: **this class never reads the delegation header itself.**
It returns ``None`` unless the middleware parked a validated context on the request.
That is what keeps it safe to sit in the customer plane's global
``DEFAULT_AUTHENTICATION_CLASSES``: on any request the middleware did not clear, it
is inert, so the 39-of-40 views that inherit the global authenticator list see no
change whatsoever.

This is NOT ``AdminSessionAuthentication`` and must never be confused with it. The
admin session cookie authenticates the admin control plane and nothing else; on this
plane it is simply an unread cookie. The delegated credential is a separate,
independent credential with its own storage, its own lifetime and its own header.

The principal is the administrator's real ``User`` row — so ORM filters, foreign
keys and the audit log's ``actor`` all work — carrying the delegation as an
in-memory attribute. The row itself is never mutated and never saved.
"""
from rest_framework.authentication import BaseAuthentication

from platform_admin_app.delegated_middleware import (
    SESSION_HEADER,
    delegation_context,
)

# The attribute the authority seam looks for. Deliberately not a model field and
# deliberately not a name any serializer or request parser could produce, so it can
# only ever be set here, in memory, for the lifetime of one request.
PRINCIPAL_DELEGATION_ATTR = 'active_delegation'


class DelegatedSessionAuthentication(BaseAuthentication):
    """Bind the administrator named by an already-validated delegated session."""

    def authenticate(self, request):
        context = delegation_context(request)
        if context is None:
            # Either no delegated credential was presented, or the middleware
            # already refused it. Nothing to bind — hand back to the chain.
            return None

        administrator = context.administrator
        # In-memory only. Every predicate in
        # users_app.controllers.permissions_check checks for this FIRST and
        # short-circuits: no dinify-admin bypass, no manage-level authority, and
        # module access confined to the grant's single restaurant.
        setattr(administrator, PRINCIPAL_DELEGATION_ATTR, context)
        return (administrator, context.session)

    def authenticate_header(self, request):
        # Non-None ⇒ DRF answers 401 rather than 403 for an unauthenticated request,
        # which is the signal the portal needs to go and exchange again. Only a
        # scheme label; the real credential is the header named here.
        return SESSION_HEADER
