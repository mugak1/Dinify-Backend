"""
Step-up authorization for the admin control plane.

A valid session proves the admin authenticated at some point in the last eight
hours. For the actions whose blast radius is largest — delegation minting (PR-4),
lifecycle transitions (PR-5), mark-paid, staff-account changes — that is not
enough: they require a SECOND factor re-verified within the last few minutes, so a
walked-away-from browser cannot be used to do the irreversible things.

This module ships the mechanism and is attached to NOTHING in this PR, because no
sensitive endpoint exists yet. PR-4 and PR-5 add ``IsRecentlyElevated`` to their
views' ``permission_classes``.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import BasePermission

from platform_admin_app.models import AdminSession

_ELEVATION_MAX_AGE_DEFAULT = timedelta(minutes=5)


def elevation_max_age():
    return getattr(
        settings, 'ADMIN_ELEVATION_MAX_AGE', _ELEVATION_MAX_AGE_DEFAULT,
    )


def require_recent_elevation(session, max_age=None):
    """
    True when ``session`` cleared a second factor within ``max_age``.

    Fail-closed: a missing session, a never-elevated session, or a stale stamp all
    return False. A future-dated stamp is treated as valid only up to the window,
    since ``timezone.now()`` is the only clock we trust.
    """
    if session is None or getattr(session, 'elevated_at', None) is None:
        return False
    window = max_age if max_age is not None else elevation_max_age()
    return (timezone.now() - session.elevated_at) <= window


class IsRecentlyElevated(BasePermission):
    """
    Require a recently re-authenticated admin session.

    Layered ON TOP of ``IsAuthenticated`` (never instead of it) — it asserts
    freshness, not identity. ``request.auth`` is the ``AdminSession`` placed there
    by ``AdminSessionAuthentication``; anything else denies.
    """

    message = 'This action requires recent re-authentication.'

    def has_permission(self, request, view):
        session = getattr(request, 'auth', None)
        if not isinstance(session, AdminSession):
            return False
        return require_recent_elevation(session)
