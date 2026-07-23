"""
Admin session cookie helpers.

The admin session travels in a ``__Host-``-prefixed cookie. The prefix is a browser
contract that REQUIRES ``Secure``, host-only (no ``Domain``) and ``Path=/`` — exactly
our intent. The cookie is also ``HttpOnly`` (the SPA never reads it) and
``SameSite=Strict``. PR-2b's login / logout endpoints call these helpers; this PR
only defines them.
"""
from datetime import timedelta

from django.conf import settings

_COOKIE_NAME_DEFAULT = '__Host-dinify_admin_session'
_ABSOLUTE_LIFETIME_DEFAULT = timedelta(hours=8)


def cookie_name():
    return getattr(settings, 'ADMIN_SESSION_COOKIE_NAME', _COOKIE_NAME_DEFAULT)


def _max_age_seconds():
    lifetime = getattr(
        settings, 'ADMIN_SESSION_ABSOLUTE_LIFETIME', _ABSOLUTE_LIFETIME_DEFAULT,
    )
    return int(lifetime.total_seconds())


def set_session_cookie(response, raw_token):
    """
    Attach the admin session cookie carrying ``raw_token`` to ``response``.

    ``__Host-`` mandates Secure + host-only + Path=/, so they are passed explicitly
    (Django does not infer them from the cookie name when setting).
    """
    response.set_cookie(
        cookie_name(),
        raw_token,
        max_age=_max_age_seconds(),
        httponly=True,
        secure=True,
        samesite='Strict',
        path='/',
        domain=None,
    )
    return response


def clear_session_cookie(response):
    """Delete the admin session cookie (logout / revocation)."""
    # delete_cookie auto-sets Secure for a ``__Host-``/``__Secure-`` prefixed name.
    response.delete_cookie(cookie_name(), path='/', samesite='Strict')
    return response
