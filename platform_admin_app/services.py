"""
Service-layer enforcement of the platform-staff identity invariant.

Invariant: an active ``platform_staff`` account holds ZERO active restaurant
memberships. A Django ``CheckConstraint`` cannot span tables, so this is enforced
at the service layer on both write directions:

* ``promote_to_platform_staff`` refuses when the user still holds an active,
  non-deleted ``RestaurantEmployee``;
* ``guard_membership_creation`` refuses creating/reactivating a membership for a
  ``platform_staff`` user, and is wired one-call-deep into every membership
  create/reactivate site.

Pre-existing dual-role rows (a ``platform_staff`` user that already holds an active
membership at flip time) are TOLERATED — the guard only blocks NEW active
memberships; the founder account split (gating PR-2b) resolves the standing rows.
"""
from rest_framework.exceptions import ValidationError

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app.models import PlatformStaffAuth


class PlatformStaffInvariantError(ValidationError):
    """
    A platform-staff / restaurant-membership invariant violation.

    Subclasses DRF ``ValidationError`` so that when raised inside a serializer's
    ``validate()`` (or anywhere in a DRF request) it integrates with
    ``serializer.is_valid()`` (Secretary returns a clean 400) and DRF's exception
    handler (HTTP 400) with no per-site try/except at the wired write sites.
    Raised outside a request, it is an ordinary exception the caller can catch.
    """


def promote_to_platform_staff(user):
    """
    Promote ``user`` to platform staff and provision its auth adjunct.

    Refuses (raises ``PlatformStaffInvariantError``) if the user still holds any
    active, non-deleted restaurant membership — the invariant forbids a
    ``platform_staff`` account with active memberships. On success sets
    ``account_type`` and returns the created-or-existing ``PlatformStaffAuth``.
    """
    from restaurants_app.models import RestaurantEmployee  # lazy: avoid import cycle

    if RestaurantEmployee.objects.filter(
        user=user, active=True, deleted=False
    ).exists():
        raise PlatformStaffInvariantError(
            'Cannot promote to platform staff: the user still holds an active '
            'restaurant membership. Remove the membership(s) first.'
        )

    user.account_type = ACCOUNT_TYPE_PLATFORM_STAFF
    user.save(update_fields=['account_type'])
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
