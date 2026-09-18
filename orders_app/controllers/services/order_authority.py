"""
D06 completion, G1b — THE MINIMAL RECORD OF AN AUTHORIZATION, CARRIED TO THE
BOUNDARY SO IT CAN BE ASKED AGAIN.

WHY IT EXISTS. Both protected order boundaries resolve their caller in
AUTOCOMMIT and then WAIT — for the admission advisory lock, the table row and
the order row. D06 carried the diner's capability across that wait and
re-verified the QR generation under the lock (``diner_capability
.assert_capability_current``). The STAFF side had nothing, and the endpoint said
why in terms: "No capability channel was used, so there is nothing to
re-verify. A staff caller's authority is the module gate above, which is not
revoked by a QR regeneration." True, and beside the point. It IS revoked by a
membership being deactivated, a role being removed, or the restaurant leaving
the states that grant portal access — and any of those can commit inside the
wait, after which an order reaches a kitchen on authority nobody holds.

THIS IS THE SAME SHAPE AS THE CAPABILITY, DELIBERATELY. Three facts and NO
CREDENTIAL: a credential must not travel past the point that verifies it. There
is no token, no client-selectable actor field and no trusted-caller switch —
the boundary re-runs the SAME resolver call the endpoint ran, against the same
server-resolved restaurant, so it can only ever REFUSE. It cannot widen anything
and it cannot be used to grant access that was not already granted.

WHAT IT DOES NOT PROMISE. The decision LINEARIZES at the re-check, not at commit
— the same honest claim D05 records for the kitchen boundary. A revocation
committed before that point is respected; one committing after it can still
overlap with the transition. Closing that would mean holding a lock on the
membership rows for the whole transaction, which is a far larger lock-domain
change than the exposure warrants.

THE REFUSAL IS THE ENDPOINT'S OWN NON-DISCLOSING 404, so a revocation landing
mid-request is indistinguishable from a principal that never had access. It must
not become an oracle, and it must not become a second vocabulary for
"you may not do this".
"""
from dataclasses import dataclass


class StaffAuthorityError(Exception):
    """Raised when carried authority no longer holds under the lock."""

    def __init__(self, status=404, message='Not found'):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class StaffAuthority:
    """What an authorized staff caller asserted, in facts rather than tokens.

    ``user`` is the PRINCIPAL the endpoint authorized, carried as the object so
    the re-check asks exactly the question the endpoint asked. Re-fetching it by
    id here would be a second, different question: a delegated principal carries
    context the middleware established for this request, and a bare row lookup
    would silently lose it and refuse somebody the endpoint correctly admitted.

    ``restaurant_id`` is SERVER-RESOLVED (from the order), never taken from a
    request body, and ``module`` names the grid module the endpoint gated on.
    """

    user: object
    restaurant_id: str
    module: str


def assert_authority_current(authority) -> None:
    """Ask the module gate again, on the state as it is now.

    ``authority`` of ``None`` means no staff channel was used — an anonymous
    diner on the capability channel, or an in-process caller that never had a
    module gate to re-assert — and this is a no-op, exactly as
    ``assert_capability_current`` is for a caller with no capability. It is a
    RE-assertion, not a new requirement: a path that carried nothing before
    keeps precisely the guarantees it had.
    """
    if authority is None:
        return

    # Imported here rather than at module scope: the permission resolver pulls
    # in models, and this module is imported by the order transition, which is
    # already in the middle of the import graph.
    from users_app.controllers.permissions_check import can_user_access_module

    if not can_user_access_module(
        authority.user, str(authority.restaurant_id), authority.module,
    ):
        raise StaffAuthorityError()
