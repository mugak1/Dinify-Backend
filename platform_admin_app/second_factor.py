"""
The second-factor dispatcher — which mechanism the caller presented, and did it pass.

Both ``auth/verify/`` (the second step of login) and ``auth/elevate/`` (step-up on a
live session) accept EITHER a TOTP code or a one-shot recovery code. This module owns
the choice between the two, and the choice is driven by an EXPLICIT ``method`` field
on the request rather than inferred from the code.

WHY EXPLICIT. Both endpoints used to try TOTP first and fall through to recovery::

    if totp.verify(auth_row, code):          # decrypts the stored secret
        ...
    elif recovery.consume(auth_row, code):
        ...

``totp.verify`` decrypts ``totp_secret_encrypted`` BEFORE it can reject a wrong code,
and ``platform_admin_app.crypto`` fails closed — a missing or invalid
``ADMIN_SECRET_ENCRYPTION_KEY`` raises ``ImproperlyConfigured``. That exception
propagated straight out of the TOTP attempt, so the ``elif`` was never reached. Two
factors that exist precisely to be INDEPENDENT failure paths were chained through one
key: losing the key lost the recovery codes with it. At one administrator, that is the
whole platform.

So ``method='recovery'`` must not touch ``totp`` at all — no decryption happens
anywhere on that branch. That is the entire point of this module, and the reason the
recovery branch below must never grow a TOTP call "as a fallback".

INFERENCE WOULD HAVE WORKED, AND IS STILL WRONG. Recovery codes are 22
urlsafe-base64 characters (``recovery.RECOVERY_CODE_BYTES``) and TOTP codes are 6
digits, so a shape heuristic could separate them today. It would silently couple
authentication dispatch to those two constants — change either and a factor re-routes
without a single test failing. The caller states which factor it is presenting.

NO ORACLE. Every failure returns the same verdict shape, and the callers map all of
them onto one byte-identical response. A wrong code, the wrong method for the code, an
unrecognised method and an unusable key are indistinguishable to the client. The
distinct ``error_code`` exists for the audit log only — the DISCLOSURE convention
``endpoints/auth.py`` documents: generic body to the caller, true cause in the log.
"""
from collections import namedtuple

from django.core.exceptions import ImproperlyConfigured

from platform_admin_app import recovery, totp

METHOD_TOTP = 'totp'
METHOD_RECOVERY = 'recovery'
SECOND_FACTOR_METHODS = (METHOD_TOTP, METHOD_RECOVERY)

# Upper bound on a normalised method name — see ``normalise_method``.
MAX_METHOD_LENGTH = 32

# Audit-only failure reasons. The client never sees these — each maps onto the single
# generic denial at the endpoint.
FAILED_BAD_CODE = 'bad_code'
FAILED_UNKNOWN_METHOD = 'unknown_method'
FAILED_TOTP_KEY_UNAVAILABLE = 'totp_key_unavailable'
FAILED_NOT_ENROLLED = 'not_enrolled'

# ``ok`` — did the factor verify. ``used_recovery`` — was a one-shot code spent (the
# endpoints surface this and audit it distinctly). ``error_code`` — audit-only cause.
FactorVerdict = namedtuple('FactorVerdict', 'ok used_recovery error_code')

_PASSED_TOTP = FactorVerdict(True, False, '')
_PASSED_RECOVERY = FactorVerdict(True, True, '')


def _failed(error_code):
    return FactorVerdict(False, False, error_code)


def normalise_method(value):
    """
    Canonicalise a caller-supplied ``method`` for comparison and audit.

    The method name is an enum, not a credential, so leniency about case and
    surrounding whitespace costs nothing. The CODE is never treated this way.

    Truncated because the normalised value is written to the audit log's ``reason``,
    an unbounded ``TextField``: without a cap, every failed attempt would persist a
    caller-supplied string of arbitrary length. Nothing valid comes close to the
    limit, and an over-long value stays invalid after truncation.
    """
    return str(value or '').strip().lower()[:MAX_METHOD_LENGTH]


def check(auth, method, code):
    """
    Check ``code`` against the ONE mechanism ``method`` names. Returns a
    ``FactorVerdict``; never raises.

    Total by construction — a missing ``auth`` row, an unrecognised method and an
    unusable encryption key all produce a verdict rather than an exception, because
    every caller is an authentication path where an exception means a 500 and an
    outage. ``recovery.consume`` in particular dereferences ``auth`` without a
    None-guard, and ``auth/elevate/`` has no eligibility gate ahead of it.

    Side effects belong to the mechanisms, not here: a successful TOTP check advances
    the replay counter and a successful recovery check SPENDS the code, so callers
    must treat a passing verdict as already-consumed.
    """
    if auth is None:
        return _failed(FAILED_NOT_ENROLLED)

    if method == METHOD_RECOVERY:
        # Nothing on this branch decrypts, and nothing here may call into ``totp`` —
        # see the module docstring. This is the path that has to survive key loss.
        if recovery.consume(auth, code):
            return _PASSED_RECOVERY
        return _failed(FAILED_BAD_CODE)

    if method == METHOD_TOTP:
        try:
            verified = totp.verify(auth, code)
        except ImproperlyConfigured:
            # The Fernet key is missing or invalid, so the enrolled secret cannot be
            # read at all. That is an operator problem, not a caller problem: fail as
            # an ordinary bad code — a 500 here would leak configuration state and
            # take the endpoint down — and let the audit row carry the truth. The
            # account is not locked out of the platform: recovery codes still work,
            # because this module never routes them through the key.
            #
            # Deliberately caught HERE and not inside ``totp.verify``: enrolment
            # (``totp.encrypt_for_storage``, via ``_admin_bootstrap.require_encryption_key``)
            # must keep failing loudly, and swallowing it in the mechanism would make a
            # misconfigured key look like a wrong code to every present and future caller.
            return _failed(FAILED_TOTP_KEY_UNAVAILABLE)
        return _PASSED_TOTP if verified else _failed(FAILED_BAD_CODE)

    return _failed(FAILED_UNKNOWN_METHOD)
