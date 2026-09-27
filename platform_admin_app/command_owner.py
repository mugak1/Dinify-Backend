"""
The admin command-owner precondition (D10).

WHAT IT CLOSES. A matched CSRF pair says a request came from this origin. It does not
say which ``AdminSession`` a command was issued under, and on this plane the two can
come apart. ``session/`` calls ``get_token``, which RE-EMITS whatever CSRF secret the
request carried. So a ``session/`` response sent before another sign-in and delivered
after it puts the old CSRF cookie back beside the new session cookie. A tab still
holding the old token then passes CSRF, and its command runs as whoever signed in
since. A second sign-in by the SAME administrator has the same shape, with nothing
about CSRF stale at all.

THE CONTRACT. ``verify/`` and ``session/`` publish ``describe(session)``:

    {"version": 1, "actor": "<User.pk>", "session": "<AdminSession.id>"}

Both identifiers are canonical lowercase UUID strings. They identify; they grant
nothing, and neither is the session token nor its hash. A client that names this owner
on an unsafe request sends

    X-Admin-Command-Owner: 1;<actor>;<session>

and the request is refused unless it describes the session that authenticated it:

    header cannot be read                      400  admin_command_owner_malformed
    names a different administrator            409  admin_command_actor_changed
    same administrator, a different session    409  admin_command_session_changed

THE SESSION COMPARISON IS WHAT ENFORCES. The session id is unique, so naming the
current session is the whole precondition. The actor comparison only decides which
409 the client sees: "somebody else is signed in now" and "you signed in again" call
for different screens. Without it, a command issued under any other session would
still be refused, as ``session_changed``; the one value it would then let through
names the current session itself, and a client can only know that id if it was
published to it.

PARSING IS STRICT, AND ONLY ABSENCE IS LEGACY. A header that is missing entirely is the
pre-D10 contract, and the request proceeds exactly as before. A header that is PRESENT
must be exactly one well-formed value: the grammar has one length, and an empty value,
a partial or extra field, another version, an uppercase or braced id, surrounding
whitespace, or two values joined by a proxy (``a, b``) is refused rather than read as
absent. Nothing is trimmed, lower-cased or otherwise normalised, because every such
rule is a second way to spell an owner that the client never published.

WHERE IT RUNS. ``AdminSessionAuthentication`` calls ``enforce`` on unsafe methods,
after the session is resolved and the account's eligibility is re-checked, and BEFORE
CSRF, permissions, throttles or the handler. So a missing or invalid session is still
the existing 401 and an ineligible account the existing denial, while an owner mismatch
is never reported as a CSRF failure. That matters: a client answers a CSRF failure by
re-reading ``session/`` and retrying once, and that retry would carry the new session's
token and succeed. ``logout/`` does not use the authenticator, so it applies
``read`` and ``compare`` itself; see ``AdminLogoutView``.

WHAT IT IS NOT. Not an authentication method: the header can only refuse, and a request
that passes it still needs its cookie, CSRF token and elevation. Not a credential, and
never logged or echoed: refusals carry a fixed sentence and a code, and name no id.
Not audited: like a CSRF failure it is refused inside authentication, before any
administrative decision exists. Not a protection for a client that sends no header,
which keeps the exposure described above.
"""
import re

from rest_framework import exceptions

HEADER = 'X-Admin-Command-Owner'
META_KEY = 'HTTP_X_ADMIN_COMMAND_OWNER'
VERSION = 1

_UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_GRAMMAR = re.compile(f'{VERSION};({_UUID});({_UUID})')
# The grammar has exactly one length. It is checked before the pattern, so an oversized
# value costs a len() and nothing else.
_LENGTH = len(f'{VERSION};') + 36 + len(';') + 36

CODE_MALFORMED = 'admin_command_owner_malformed'
CODE_ACTOR_CHANGED = 'admin_command_actor_changed'
CODE_SESSION_CHANGED = 'admin_command_session_changed'

# Fixed sentences. They never vary with, or quote, anything the request carried.
DETAILS = {
    CODE_MALFORMED: (
        'The command-owner precondition could not be read. The command was not run.'
    ),
    CODE_ACTOR_CHANGED: (
        'This browser is now signed in as a different administrator. '
        'The command was not run.'
    ),
    CODE_SESSION_CHANGED: (
        'This browser has started a new admin session since the command was '
        'issued. The command was not run.'
    ),
}


class CommandOwnerMalformed(exceptions.APIException):
    """The header is present and cannot be read. 400."""

    status_code = 400

    def __init__(self):
        super().__init__(detail={
            'detail': DETAILS[CODE_MALFORMED], 'code': CODE_MALFORMED,
        })


class CommandOwnerChanged(exceptions.APIException):
    """The header names an owner other than the session that authenticated. 409."""

    status_code = 409

    def __init__(self, code):
        super().__init__(detail={'detail': DETAILS[code], 'code': code})


def describe(session):
    """The owner a client may name for ``session``. Identifiers only."""
    return {
        'version': VERSION,
        'actor': str(session.user_id),
        'session': str(session.id),
    }


def read(meta):
    """
    The ``(actor, session)`` the request names, or ``None`` when the header is ABSENT.

    Raises ``CommandOwnerMalformed`` for any present value that is not exactly one
    well-formed owner. Absence is the only legacy case.
    """
    if META_KEY not in meta:
        return None
    raw = meta[META_KEY]
    if not isinstance(raw, str) or len(raw) != _LENGTH:
        raise CommandOwnerMalformed()
    match = _GRAMMAR.fullmatch(raw)
    if match is None:
        raise CommandOwnerMalformed()
    return match.group(1), match.group(2)


def compare(named, session):
    """Refuse unless ``named`` describes ``session``. Raises ``CommandOwnerChanged``."""
    actor, session_id = named
    # Classification: which of the two refusals the client is shown.
    if actor != str(session.user_id):
        raise CommandOwnerChanged(CODE_ACTOR_CHANGED)
    # Enforcement: the command was issued under this session, or it does not run.
    if session_id != str(session.id):
        raise CommandOwnerChanged(CODE_SESSION_CHANGED)


def enforce(request, session):
    """
    The authenticator's precondition for an unsafe request on a resolved session.

    An absent header proceeds as before. Anything else must name ``session``.
    """
    named = read(request.META)
    if named is not None:
        compare(named, session)
