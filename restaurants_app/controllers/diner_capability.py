"""
Opaque, expiring diner table-session capability (PR 7A).

Anonymous diner operations are authorised by a server-issued, cryptographically
signed capability bound to a restaurant+table — NOT by knowledge of a raw table /
order / transaction UUID. Two tiers, both ``django.core.signing`` tokens signed with
a DEDICATED key (``settings.DINER_CAP_KEY``, secret-separated from staff JWT):

* **QR credential** — long-lived, encoded into the QR sticker (salt ``QR_SALT``).
  Verified WITHOUT a ``max_age``; revoked when the table's ``qr_version`` is bumped.
* **Table session** — short-lived, minted on a successful scan (salt ``SESSION_SALT``).
  Verified WITH ``max_age`` (``settings.DINER_SESSION_TTL_SECONDS``) AND a live
  re-check of ``qr_version`` + ``table.is_available_for_scan()``.

Tokens are SIGNED, not encrypted — the payload (restaurant id, table id, generation)
is readable, so never place a secret inside. This module is crypto-only: top-level
imports are limited to ``django.core.signing`` / ``settings``; the ``Table`` lookup is
function-local to avoid the ``restaurants_app`` <-> ``orders_app`` import cycle. It
NEVER reads ``request.user`` — staff JWT auth is a completely separate channel.
"""
from dataclasses import dataclass

from django.conf import settings
from django.core import signing

CAPABILITY_VERSION = 1
QR_SALT = 'dinify.diner.qr.v1'
SESSION_SALT = 'dinify.diner.session.v1'

# Custom headers the diner SPA presents (mirrored in settings.CORS_ALLOW_HEADERS).
CREDENTIAL_HEADER = 'X-Diner-Credential'
SESSION_HEADER = 'X-Diner-Session'


class DinerCapabilityError(Exception):
    """
    A malformed / missing / invalid / expired capability. Maps to a clean 400 —
    deliberately NOT 401/403 (which would trip the diner app's logout interceptor).
    """

    def __init__(self, message='Invalid diner capability.', status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class DinerCapabilityDenied(DinerCapabilityError):
    """
    The capability is well-formed but does not resolve to a usable table (unknown /
    regenerated / deleted / disabled / out-of-service). Maps to a NON-DISCLOSING 404
    that never confirms existence — matching the 404-for-both posture the table-scan
    handler already uses.
    """

    def __init__(self, message='Not found.'):
        super().__init__(message=message, status=404)


def _key():
    return settings.DINER_CAP_KEY


# --- issuance --------------------------------------------------------------

def issue_qr_credential(restaurant_id, table_id, qr_version):
    """The opaque credential encoded into a table's QR sticker (long-lived)."""
    return signing.dumps(
        {'v': CAPABILITY_VERSION, 'r': str(restaurant_id),
         't': str(table_id), 'g': int(qr_version)},
        key=_key(), salt=QR_SALT, compress=True,
    )


def issue_table_session(table):
    """A short-lived diner table session, minted after a successful scan."""
    return signing.dumps(
        {'v': CAPABILITY_VERSION, 'r': str(table.restaurant_id),
         't': str(table.id), 'g': int(table.qr_version)},
        key=_key(), salt=SESSION_SALT, compress=True,
    )


# --- verification ----------------------------------------------------------

def _load(token, salt, max_age):
    if not isinstance(token, str) or not token.strip():
        raise DinerCapabilityError('A diner capability is required.')
    try:
        payload = signing.loads(token, key=_key(), salt=salt, max_age=max_age)
    except signing.SignatureExpired:
        # SignatureExpired subclasses BadSignature — catch it FIRST. Expiry is not
        # sensitive, so a friendly re-scan prompt is fine.
        raise DinerCapabilityError(
            'Your table session has expired. Please rescan the QR code.'
        )
    except signing.BadSignature:
        # Wrong signature / tampered / wrong salt (a QR credential replayed on a
        # session-gated endpoint, or vice-versa) — opaque, non-disclosing.
        raise DinerCapabilityError('Invalid diner capability.')
    if not isinstance(payload, dict) or payload.get('v') != CAPABILITY_VERSION:
        raise DinerCapabilityError('Unsupported diner capability.')
    return payload


def _resolve_table(payload):
    """
    Load the bound table and enforce generation + scan-availability. Any failure
    raises ``DinerCapabilityDenied`` (404, non-disclosing) so an unknown id, a stale
    generation and an unavailable table are indistinguishable to the caller.
    """
    from restaurants_app.models import Table  # function-local: avoid import cycle
    table = (
        Table.objects
        .select_related('restaurant', 'dining_area')
        .filter(id=payload.get('t'), restaurant_id=payload.get('r'))
        .first()
    )
    if table is None:
        raise DinerCapabilityDenied()
    if table.qr_version != payload.get('g'):
        # Regenerated QR: this credential/session is a stale generation.
        raise DinerCapabilityDenied()
    if not table.is_available_for_scan():
        # deleted / disabled / inactive / out-of-service — re-checked live so a
        # table that changes state AFTER the token was minted stops working.
        raise DinerCapabilityDenied()
    return table


def resolve_qr_credential(credential):
    """Verify a QR credential → the bound ``Table``. No expiry (revoked via qr_version)."""
    payload = _load(credential, QR_SALT, max_age=None)
    return _resolve_table(payload)


def resolve_table_session(token, max_age=None):
    """Verify a table session (expiry + live generation/availability) → the ``Table``."""
    if max_age is None:
        max_age = settings.DINER_SESSION_TTL_SECONDS
    payload = _load(token, SESSION_SALT, max_age=max_age)
    return _resolve_table(payload)


# --- carrying a verified capability to a protected boundary ----------------

@dataclass(frozen=True)
class TableCapability:
    """What a capability that has ALREADY been verified asserted, in a form a
    transaction can re-check against a row it holds a lock on.

    WHY IT EXISTS. ``resolve_table_session`` verifies the signature, the expiry,
    the generation and the table's availability — in autocommit, at the endpoint,
    before any transaction opens. The write that follows then WAITS for the
    admission advisory lock, the table row and the order row, and a QR
    regeneration committing inside that wait revokes the session the caller is
    holding. Nothing downstream knew what generation had been presented, so
    nothing could notice.

    It carries THREE facts and no token: a capability is a credential and must not
    travel further into the application than the point that verifies it. These are
    conclusions drawn from one, which is why re-checking them can only ever refuse
    — it can never admit a request that was not already admitted at the door.

    ``qr_generation`` is the table's ``qr_version`` AS VERIFIED, not as stored now.
    ``_resolve_table`` has just proved the two were equal, so reading it off the
    resolved table is reading the presented value; it is stored separately here so
    that remains true if either side of that equality ever moves.
    """

    restaurant_id: str
    table_id: str
    qr_generation: int


def capability_from_table(table) -> TableCapability:
    """The context to carry, built from a table a capability has just resolved to.

    MUST be called only on the result of ``resolve_table_session`` /
    ``resolve_qr_credential``. Building one from a table nobody presented a
    capability for would manufacture an authorization fact.
    """
    return TableCapability(
        restaurant_id=str(table.restaurant_id),
        table_id=str(table.id),
        qr_generation=int(table.qr_version),
    )


def assert_capability_current(capability, table):
    """Re-verify a carried capability against a row the caller has LOCKED.

    Raises ``DinerCapabilityDenied`` — the same non-disclosing 404 the door
    answers with — so a revocation that lands mid-request is indistinguishable
    from a session that never resolved. It re-checks exactly the AUTHORIZATION
    facts and no others:

      * the table is the one the capability names (a defensive identity check;
        the caller already scoped its lookup by it),
      * the GENERATION still matches, i.e. the QR has not been regenerated.

    It deliberately does NOT re-check ``is_available_for_scan()``. A table taken
    out of service is an OPERATIONAL fact that binds every provenance, staff
    included, and ``order_eligibility`` owns it and answers it with a refusal a
    diner can read. Answering it here as well would give one fact two answers
    depending on how the caller authenticated, and would report a table the owner
    took out of service as though the diner's session were forged.

    THAT HOLDS WHERE AN ELIGIBILITY RULE RUNS, which is the create and acceptance
    boundaries. ``retire_quote_for_review`` deliberately runs none — it is not a
    lifecycle operation — so the fact reaches no rule there at all, and the
    answer its ENDPOINT gives a moment earlier is this channel's own 404, because
    ``_resolve_table`` treats a table that has stopped being scannable as a
    REVOKED SESSION. ``session_still_admissible`` below is that question asked
    again under the lock, kept as a separate named predicate rather than folded
    in here precisely so acceptance keeps answering with the sentence a diner can
    read (D06 completion, G1b).

    ``capability`` of ``None`` means no capability channel was used (a staff
    caller on the module gate), and this is a no-op: there is nothing to revoke.
    """
    if capability is None:
        return
    if table is None:
        raise DinerCapabilityDenied()
    if str(table.id) != capability.table_id:
        raise DinerCapabilityDenied()
    if str(table.restaurant_id) != capability.restaurant_id:
        raise DinerCapabilityDenied()
    if int(table.qr_version) != capability.qr_generation:
        # The QR was regenerated while this request waited on its locks. That is
        # a deliberate revocation by the owner, and it revokes retroactively:
        # a request already in flight is not grandfathered in.
        raise DinerCapabilityDenied()


def session_still_admissible(capability, table) -> bool:
    """Would this table still MINT the session the caller is holding?

    The scannability half of the door's rule, as a predicate rather than a
    refusal, so the route that asks it answers in its own established
    vocabulary. ``True`` for a caller with no capability: a staff principal holds
    no table session, so there is no session for a table going out of service to
    revoke — and refusing them would buy the diner nothing, since a quote at an
    unavailable table cannot be accepted through any channel either.

    It asks the MODEL predicate, never a re-spelling of it: ``_resolve_table``
    and ``order_eligibility`` both read the same one, and a third copy would
    disagree the first time a field is added to any of them.
    """
    if capability is None:
        return True
    if table is None:
        return False
    return bool(table.is_available_for_scan())


def denial_envelope() -> dict:
    """THE capability channel's one non-disclosing refusal, as a plain dict.

    DERIVED from ``DinerCapabilityDenied`` and never re-spelled. A service that
    answers "the capability channel's own opaque 404" has to answer with the
    channel's own BYTES: both order endpoints render a raised
    ``DinerCapabilityDenied`` as ``exc.message``, so a literal written out
    beside it is one edit away from becoming a discriminator — and it was.
    ``session_still_admissible``'s three refusal sites spelled it
    ``'Not found'`` while the door spells it ``'Not found.'``, so on ONE route a
    client could tell a revocation that landed at the door from one that landed
    inside the lock wait, and liveness revocation from generation revocation.

    That is not only an oracle in a channel built for non-disclosure. The
    deployed client matches the body EXACTLY
    (``DinerSessionService.CAPABILITY_DENIED_404``), so the periodless form was
    not recognised as a capability denial at all: the diner whose table went out
    of service mid-request was never shown the rescan panel.

    The STAFF channel is deliberately NOT this. Its door is the orders
    endpoints' own periodless ``'Not found'`` and ``StaffAuthorityError``
    already matches it; routing it through here would introduce on the staff
    side exactly the mismatch this removes on the diner side.
    """
    denied = DinerCapabilityDenied()
    return {'status': denied.status, 'message': denied.message}


# --- request helpers -------------------------------------------------------

def credential_from_request(request):
    """
    The QR credential — accepted ONLY from the ``X-Diner-Credential`` header. A
    bearer capability must never travel in a URL/query string or request body:
    those leak into access logs, ``Referer`` headers, shared caches and browser
    history, and would let a raw value grant anonymous authority.
    """
    return request.headers.get(CREDENTIAL_HEADER)


def session_token_from_request(request):
    """
    The diner session token — accepted ONLY from the ``X-Diner-Session`` header
    (never a query string or request body, for the same reasons as the QR
    credential above).
    """
    return request.headers.get(SESSION_HEADER)


def require_table_session(request):
    """
    Resolve + verify the diner session from the request → the bound ``Table``, or
    raise ``DinerCapabilityError`` / ``DinerCapabilityDenied``. The anonymous-capability
    gate the diner endpoints call — it never reads ``request.user``.
    """
    return resolve_table_session(session_token_from_request(request))
