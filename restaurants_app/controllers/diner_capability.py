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


# --- request helpers -------------------------------------------------------

def credential_from_request(request):
    """The QR credential from the header (preferred) or a ``?credential=`` fallback."""
    return request.headers.get(CREDENTIAL_HEADER) or request.GET.get('credential')


def session_token_from_request(request):
    """The session token from the header (preferred), else a query/body fallback."""
    token = request.headers.get(SESSION_HEADER) or request.GET.get('session')
    if token:
        return token
    try:
        return request.data.get('session')
    except Exception:  # noqa: BLE001 - request.data may be unavailable on some verbs
        return None


def require_table_session(request):
    """
    Resolve + verify the diner session from the request → the bound ``Table``, or
    raise ``DinerCapabilityError`` / ``DinerCapabilityDenied``. The anonymous-capability
    gate the diner endpoints call — it never reads ``request.user``.
    """
    return resolve_table_session(session_token_from_request(request))
