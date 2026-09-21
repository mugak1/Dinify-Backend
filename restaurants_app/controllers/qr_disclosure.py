"""
WHO MAY RECEIVE A TABLE'S QR BEARER CREDENTIAL — ONE DECISION, DENIED BY DEFAULT.

A table's QR credential is not an identifier. It is the SOLE anonymous authority
for that table: presenting it to ``orders/journey/table-scan/`` mints a diner
table session, and a table session is what places orders. It is verified WITHOUT
expiry and is revoked only by bumping ``Table.qr_version`` — which means
reprinting the physical sticker. So a response that carries one has handed over
live ordering authority that outlives the request, the session and the grant.

``SETUP_READABLE_RECORDS`` admits ``tables`` to the delegated restaurant-setup
GET, and that read minted a credential per row. A delegated administrator holding
even the READ-ONLY ``view`` scope therefore received working ordering authority
for every table in the restaurant as an ordinary consequence of opening a list.
This module is the containment: **field-level authority containment, not removal
of the table view.** Every other table field a delegate is authorised to see —
number, area, capacity, status, geometry, ``has_qr``, ``qr_mode`` — is untouched.

THE INVARIANT, and it is one sentence:

    QR material is emitted only after ORDINARY, NON-DELEGATED authority for the
    relevant table scope has been positively established; otherwise it is
    withheld BEFORE SIGNING.

FOUR THINGS ABOUT THAT SENTENCE ARE LOAD-BEARING.

**"POSITIVELY ESTABLISHED" — absence is never entitlement.** A missing request, an
anonymous principal, a serializer or helper constructed with no context, and a
policy nobody supplied all resolve to :data:`WITHHOLD_ALL`. That direction is
deliberate and it is the opposite of this repository's ``menu_policy`` precedent
(``restaurants_app.serializers``: ``policy = self.context.get('menu_policy'); if
policy is not None: <restrict>``), where an ABSENT context correctly means the
ordinary operator path. Copying that polarity here would produce a containment
that reads as applied and discloses anyway — measured, before this module existed,
against the real delegated read.

**"NON-DELEGATED" is a separate question from "may read tables".** It is not a
refinement of the module check, it is a veto over it.
``can_user_access_module`` / ``get_module_restaurant_ids`` INTENTIONALLY resolve a
delegated principal from the stored grant, so a delegate reading the tables list
is a permitted caller and the module resolver says so. Reading a table is not the
same authority as minting the credential that orders from it, and the resolver
cannot tell those apart because it was never asked to.

**"THE RELEVANT TABLE SCOPE" is a set, not a boolean.** The policy carries the
restaurant ids the caller holds ordinary ``tables`` authority over, so a row is
checked against what its own restaurant authorises. A non-delegated principal is
not thereby entitled to a foreign table. The set is resolved ONCE per response
and reused for every row — never a permission query per row.

**"BEFORE SIGNING" is not decoration.** Where the credential is withheld, the
signer is not called at all. Signing and then stripping at an outer layer leaves a
live bearer capability in memory for a logger, an exception repr, or the next
person who adds a ``to_representation`` override above the strip — and the strip is
the only thing standing between it and the wire.

WHAT THIS MODULE IS NOT. It is not a new authorization framework, it does not
widen or narrow any route, scope or module grid, and it resolves nothing itself:
it READS the two server-derived delegation signals the platform already
establishes (``delegated_middleware.delegation_context`` on the request, and the
principal marker ``delegated_auth.PRINCIPAL_DELEGATION_ATTR`` the delegated
authenticator sets on the user) and the existing module scope resolver. There is
deliberately no caller-supplied input of any kind — no query parameter, no body
field, no ``include_qr`` flag, no role name from a browser and no grouping value.
Entitlement is derived only AFTER the existing server authorization has run, and
is then passed explicitly.

Paired policy record: ``DELEGATED_QR_TRIAGE.md``.
"""
from dataclasses import dataclass
from typing import FrozenSet

from dinify_backend.configss.string_definitions import MODULE_TABLES


@dataclass(frozen=True)
class QrDisclosurePolicy:
    """
    The restaurants whose table QR credentials THIS response may carry.

    Immutable and explicit: a builder is handed one, or it is handed nothing and
    withholds. ``restaurant_ids`` holds ids as STRINGS, matching what
    ``get_module_restaurant_ids`` returns for an ordinary principal.
    """

    restaurant_ids: FrozenSet[str] = frozenset()

    def allows(self, restaurant_id) -> bool:
        """
        Whether a table owned by ``restaurant_id`` may have its credential emitted.

        Fail-closed on every degenerate input: an empty scope, a missing id, and
        an id outside the resolved scope all answer False. Comparison is on the
        string form because that is the shape the resolver returns and the shape a
        ``UUIDField`` attribute is not.
        """
        if not self.restaurant_ids or restaurant_id is None:
            return False
        return str(restaurant_id) in self.restaurant_ids

    @property
    def withholds_everything(self) -> bool:
        """
        True when this policy can never permit a credential.

        The cheap, row-independent question a serializer asks at construction so
        it can drop the field before any row is evaluated.
        """
        return not self.restaurant_ids


#: The default, and the answer to every question this module cannot positively
#: answer. Shared because it is immutable and carries no state.
WITHHOLD_ALL = QrDisclosurePolicy()


class _Withheld:
    """A placeholder that is obviously not a credential if it ever escapes."""

    __slots__ = ()

    def __repr__(self):  # pragma: no cover - diagnostic only
        return '<qr_credential withheld>'


#: Returned by a per-row guard INSTEAD of calling the signer, and removed from the
#: representation by the builder. It exists so a row-level refusal can omit the key
#: rather than emit ``null`` — while still never minting. Identity-compared (``is``),
#: never truth-tested, so it cannot be confused with a real value.
QR_CREDENTIAL_WITHHELD = _Withheld()


def request_is_delegated(request) -> bool:
    """
    Whether this request is acting under a delegation grant.

    Reads BOTH server-derived signals the platform already establishes, and
    neither is re-derived here:

    * ``delegated_middleware.delegation_context(request)`` — the validated context
      the middleware attaches in ``process_view``, and only AFTER the route, scope
      and kwargs gates have passed.
    * ``delegated_auth.PRINCIPAL_DELEGATION_ATTR`` on ``request.user`` — the
      in-memory marker the delegated authenticator sets, which is what
      ``users_app.controllers.permissions_check`` itself consults.

    Both, because they are established at different points and this is the one
    place a disagreement between them must not become a disclosure. Imports are
    function-local to keep this module import-light for ``serializers.py`` and to
    avoid an app-level import cycle, matching ``diner_capability._resolve_table``.

    Any failure to read a signal is treated as DELEGATED, because the only safe
    answer to "I could not tell" is the one that withholds.
    """
    if request is None:
        return True
    try:
        from platform_admin_app.delegated_middleware import delegation_context

        if delegation_context(request) is not None:
            return True
    except Exception:
        return True
    try:
        from platform_admin_app.delegated_auth import PRINCIPAL_DELEGATION_ATTR

        user = getattr(request, 'user', None)
        if getattr(user, PRINCIPAL_DELEGATION_ATTR, None) is not None:
            return True
    except Exception:
        return True
    return False


def qr_disclosure_policy(request) -> QrDisclosurePolicy:
    """
    Resolve, once per response, which restaurants' credentials this request may see.

    The whole decision, in order:

    1. **No request at all** -> withhold. A direct serializer or helper call, a
       management command and a unit test all land here. Absence of a request is
       not proof of ordinary authority; it is proof of nothing.
    2. **Delegated** -> withhold, even though the module resolver would happily
       return the granted restaurant. This is the veto described at module level.
    3. **Not an authenticated, active principal** -> withhold.
    4. Otherwise the ordinary ``tables`` module scope, resolved by the existing
       ``get_module_restaurant_ids`` — ONE query, reused for every row of the
       response.

    Never raises: any unexpected failure resolving the scope withholds rather than
    propagating, so a containment fault can cost a QR preview but can never take
    out a tables read that is otherwise fine.
    """
    if request is None or request_is_delegated(request):
        return WITHHOLD_ALL

    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return WITHHOLD_ALL
    if not getattr(user, 'is_active', False):
        return WITHHOLD_ALL

    try:
        from users_app.controllers.permissions_check import (
            get_module_restaurant_ids,
        )

        scope = get_module_restaurant_ids(user, MODULE_TABLES)
    except Exception:
        return WITHHOLD_ALL

    if not scope:
        return WITHHOLD_ALL
    return QrDisclosurePolicy(restaurant_ids=frozenset(str(x) for x in scope))


def policy_from_context(context) -> QrDisclosurePolicy:
    """
    The policy a serializer should apply, given its DRF ``context``.

    Prefers an explicitly supplied ``qr_policy`` (how the grouped builder and the
    table-action responses pass an already-resolved decision without re-resolving
    it), then falls back to resolving from ``context['request']``. A context that
    carries neither withholds.
    """
    if not context:
        return WITHHOLD_ALL
    explicit = context.get('qr_policy')
    if isinstance(explicit, QrDisclosurePolicy):
        return explicit
    return qr_disclosure_policy(context.get('request'))
