"""
Admin cookie helpers — the session cookie and the short-lived login challenge.

Both travel in ``__Host-``-prefixed cookies. The prefix is a browser contract that
REQUIRES ``Secure``, host-only (no ``Domain``) and ``Path=/`` — exactly our intent.
Both are also ``HttpOnly`` (the SPA never reads either) and ``SameSite=Strict``,
which is what keeps the unauthenticated login/verify pair safe from cross-site
submission: a cross-origin POST carries neither cookie.

They are deliberately SEPARATE cookies. The challenge proves only the first factor
and lives for minutes; the session means fully authenticated and lives for hours.
Reusing one cookie for both states would make "has a cookie" stop meaning anything.
"""
from datetime import timedelta

from django.conf import settings

_COOKIE_NAME_DEFAULT = '__Host-dinify_admin_session'
_ABSOLUTE_LIFETIME_DEFAULT = timedelta(hours=8)
_CHALLENGE_COOKIE_NAME_DEFAULT = '__Host-dinify_admin_challenge'
_CHALLENGE_TTL_DEFAULT = timedelta(minutes=5)


def cookie_name():
    return getattr(settings, 'ADMIN_SESSION_COOKIE_NAME', _COOKIE_NAME_DEFAULT)


def challenge_cookie_name():
    return getattr(
        settings, 'ADMIN_CHALLENGE_COOKIE_NAME', _CHALLENGE_COOKIE_NAME_DEFAULT,
    )


def _max_age_seconds():
    lifetime = getattr(
        settings, 'ADMIN_SESSION_ABSOLUTE_LIFETIME', _ABSOLUTE_LIFETIME_DEFAULT,
    )
    return int(lifetime.total_seconds())


def _challenge_max_age_seconds():
    ttl = getattr(settings, 'ADMIN_CHALLENGE_TTL', _CHALLENGE_TTL_DEFAULT)
    return int(ttl.total_seconds())


def _set(response, name, value, max_age):
    """
    Attach one ``__Host-`` cookie.

    Secure / host-only / Path=/ are passed explicitly because Django does not infer
    them from the cookie name when SETTING (it does when deleting).
    """
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite='Strict',
        path='/',
        domain=None,
    )
    return response


def _clear(response, name):
    # delete_cookie auto-sets Secure for a ``__Host-``/``__Secure-`` prefixed name.
    response.delete_cookie(name, path='/', samesite='Strict')
    return response


def set_session_cookie(response, raw_token):
    """Attach the admin session cookie carrying ``raw_token`` to ``response``."""
    return _set(response, cookie_name(), raw_token, _max_age_seconds())


def clear_session_cookie(response):
    """Delete the admin session cookie (logout / revocation)."""
    return _clear(response, cookie_name())


def set_challenge_cookie(response, raw_token):
    """Attach the first-factor challenge cookie (minutes, not hours)."""
    return _set(
        response, challenge_cookie_name(), raw_token, _challenge_max_age_seconds(),
    )


def clear_challenge_cookie(response):
    """Delete the challenge cookie — on consumption, failure, or logout."""
    return _clear(response, challenge_cookie_name())
