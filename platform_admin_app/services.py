"""
Service-layer enforcement of the platform-staff identity invariant.

``account_type`` is the SOLE determinant of which plane an account belongs to,
and ``roles`` carries restaurant roles only. Two halves keep that single-brained,
neither expressible as a Django ``CheckConstraint`` (one spans tables, the other
is a JSON list), so both are enforced here at the service layer:

1. An active ``platform_staff`` account holds ZERO active restaurant memberships.
   * ``promote_to_platform_staff`` refuses when the user still holds an active,
     non-deleted ``RestaurantEmployee``;
   * ``guard_membership_creation`` refuses creating/reactivating a membership for
     a ``platform_staff`` user, and is wired one-call-deep into every membership
     create/reactivate site.

2. Nobody on the customer plane holds a platform-only role.
   * ``assert_no_platform_roles`` refuses the legacy ``dinify_admin`` /
     ``dinify_account_manager`` strings at every roles write path.

Pre-existing dual-role rows (a ``platform_staff`` user that already holds an active
membership at flip time) are TOLERATED — the guard only blocks NEW active
memberships; the founder account split (gating PR-2b) resolves the standing rows.
"""
from rest_framework.exceptions import ValidationError

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app.models import PlatformStaffAuth


# The legacy platform-role strings. This is a DENYLIST, not a vocabulary: these
# literals exist here solely so writes carrying them can be REFUSED. They no
# longer name a capability anywhere — nothing reads ``User.roles`` to decide
# platform authority, and ``account_type`` is the only discriminator.
#
# Defined on the ADMIN plane on purpose. It keeps the customer-plane ambient-
# authority scanner's allowlist genuinely empty
# (``dinify_backend/tenancy/ambient_authority.py``), and it puts the denylist
# beside the invariant it serves. Frozen literals rather than imported constants,
# because the constants were deleted — same reasoning as
# ``users_app/migrations/0011_flip_admin_account_type``.
PLATFORM_ONLY_ROLES = frozenset({'dinify_admin', 'dinify_account_manager'})


class PlatformStaffInvariantError(ValidationError):
    """
    A platform-staff / restaurant-membership invariant violation.

    Subclasses DRF ``ValidationError`` so that when raised inside a serializer's
    ``validate()`` (or anywhere in a DRF request) it integrates with
    ``serializer.is_valid()`` (Secretary returns a clean 400) and DRF's exception
    handler (HTTP 400) with no per-site try/except at the wired write sites.
    Raised outside a request, it is an ordinary exception the caller can catch.
    """


def has_active_membership(user):
    """
    True if ``user`` holds any active, non-deleted restaurant membership.

    The single source of truth for the "zero active memberships" half of the
    platform-staff invariant. Reused by ``promote_to_platform_staff`` (write-time
    refusal) and by the admin session authenticator (fail-closed login rejection),
    so the predicate is defined exactly once.
    """
    from restaurants_app.models import RestaurantEmployee  # lazy: avoid import cycle

    return RestaurantEmployee.objects.filter(
        user=user, active=True, deleted=False
    ).exists()


def revoke_customer_tokens(user):
    """
    Blacklist every outstanding customer refresh token belonging to ``user``.

    Returns the number of tokens newly blacklisted. Idempotent — an already
    blacklisted token is left alone — and safe on an empty set.

    Customer login and password reset both refuse a ``platform_staff`` account, and
    so does the refresh route; but those gates only bind at the moment a token is
    minted or rotated. A token issued BEFORE an account became platform staff would
    otherwise stay live for the rest of its refresh lifetime. Promotion revokes it,
    so "platform staff cannot hold a customer session" is true from the instant the
    account_type flips rather than merely going forward.
    """
    from rest_framework_simplejwt.token_blacklist.models import (  # lazy: app registry
        BlacklistedToken, OutstandingToken,
    )

    revoked = 0
    for token in OutstandingToken.objects.filter(user=user):
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        if created:
            revoked += 1
    return revoked


def promote_to_platform_staff(user):
    """
    Promote ``user`` to platform staff and provision its auth adjunct.

    Refuses (raises ``PlatformStaffInvariantError``) if the user still holds any
    active, non-deleted restaurant membership — the invariant forbids a
    ``platform_staff`` account with active memberships. On success sets
    ``account_type``, revokes any customer session the account still holds, and
    returns the created-or-existing ``PlatformStaffAuth``.
    """
    if has_active_membership(user):
        raise PlatformStaffInvariantError(
            'Cannot promote to platform staff: the user still holds an active '
            'restaurant membership. Remove the membership(s) first.'
        )

    user.account_type = ACCOUNT_TYPE_PLATFORM_STAFF
    user.save(update_fields=['account_type'])
    revoke_customer_tokens(user)
    auth, _ = PlatformStaffAuth.objects.get_or_create(user=user)
    return auth


def guard_membership_creation(user):
    """
    Refuse creating or reactivating a restaurant membership for platform staff.

    Called one-call-deep at every membership create/reactivate site. A no-op for
    ordinary ``restaurant_user`` accounts, so normal employee flows are unaffected.
    """
    if getattr(user, 'account_type', None) == ACCOUNT_TYPE_PLATFORM_STAFF:
        raise PlatformStaffInvariantError(
            'A platform-staff account cannot hold a restaurant membership.'
        )


def platform_roles_in(roles):
    """
    The platform-only role strings present in ``roles`` (a sorted list; empty when
    clean). Tolerant of ``None`` and of non-list legacy values, which pre-``0003``
    ``User.roles`` rows can still hold.
    """
    if not isinstance(roles, (list, tuple, set, frozenset)):
        return []
    return sorted({
        role for role in roles
        if isinstance(role, str) and role in PLATFORM_ONLY_ROLES
    })


def assert_no_platform_roles(roles):
    """
    Refuse a roles write that carries a platform-only role.

    Wired at every roles write path (``User.roles`` and ``RestaurantEmployee.roles``
    alike). Rejecting rather than silently dropping is deliberate: a caller asking
    for `dinify_admin` is asking for something this plane cannot grant, and quietly
    saving a narrower list would hide that. A no-op for ordinary restaurant roles,
    so normal employee flows are unaffected.

    Raises ``PlatformStaffInvariantError`` (a DRF ``ValidationError``), so inside a
    serializer's ``validate()`` it surfaces as a clean 400.
    """
    offending = platform_roles_in(roles)
    if offending:
        raise PlatformStaffInvariantError(
            'These roles cannot be granted: {}. Platform staff are identified by '
            'account_type on the admin plane, not by a role on a restaurant '
            'account.'.format(', '.join(offending))
        )
