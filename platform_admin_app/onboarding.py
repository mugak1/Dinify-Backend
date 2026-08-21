"""
The owner-consistency invariant for the onboarding domain.

Dinify carries TWO related representations of "who owns this restaurant", and they
are not the same thing:

  1. ``Restaurant.owner`` — the owner OF RECORD, a plain FK on the tenant row.
  2. An active, non-deleted ``RestaurantEmployee`` carrying the ``RESTAURANT_OWNER``
     role — the owner's AUTHORITY, which is what the customer plane actually
     resolves permissions from (``users_app.controllers.permissions_check``).

Nothing today makes the two agree. They are written by different code paths at
different times, so they can drift: an owner FK pointing at a user with no live
membership has a name on the tenant and no power in it, while a membership with no
matching FK is the reverse. Either way an operator reading the Admin portal and a
diner-facing request disagree about who the owner is.

Step 2 is the first thing that will WRITE owners — adopting a legacy restaurant,
creating a new one, inviting someone to claim it. Before any of that, this module
states the agreement as one reusable, named check, so every future writer asks the
same question rather than each re-deriving it slightly differently.

VALIDATION ONLY. ``assert_owner_consistency`` reads, compares and raises. It NEVER
repairs: it does not reassign ``Restaurant.owner``, deactivate a surplus membership,
create a missing one, or touch ``RestaurantRolePermission``. Repair is a decision
with an actor and a reason behind it — an audited service's job, not a validator's
side effect.

NO TRANSACTION, NO LOCK — DELIBERATELY. This function opens no
``transaction.atomic()``, takes no ``select_for_update`` and reaches for no advisory
lock. Burying lock acquisition inside something that reads like a harmless assertion
is how lock-ordering cycles get introduced by accident, and this repository has a
documented global order to protect (see the Restaurant Lifecycle section of
CLAUDE.md). The consequence is the honest one: the answer is a snapshot, valid for
the instant it was read. A MUTATING caller must therefore establish its own
transaction and locking discipline FIRST and call this INSIDE it — the check is only
as authoritative as the transaction surrounding it.
"""
from dinify_backend.configss.string_definitions import RESTAURANT_OWNER
from restaurants_app.models import RestaurantEmployee

# Error codes. Distinct because the three failures need different remedies: one
# needs a membership created, one needs the duplicates resolved, one needs somebody
# to decide WHICH of two answers is right.
MISSING_OWNER_MEMBERSHIP = 'missing_owner_membership'
MULTIPLE_OWNER_MEMBERSHIPS = 'multiple_owner_memberships'
OWNER_MEMBERSHIP_MISMATCH = 'owner_membership_mismatch'

OWNER_CONSISTENCY_CODES = frozenset({
    MISSING_OWNER_MEMBERSHIP,
    MULTIPLE_OWNER_MEMBERSHIPS,
    OWNER_MEMBERSHIP_MISMATCH,
})


class OwnerConsistencyError(Exception):
    """
    The owner of record and the owner authority do not agree.

    A plain ``Exception``, NOT a DRF ``APIException``: this is an invariant a
    future writer must handle explicitly (refuse the adoption, surface the conflict
    to the operator), never something an exception handler should quietly render as
    a 4xx on a path that had no business continuing.

    ``code`` is the short machine-readable reason, so a caller branches on a
    constant rather than parsing prose. ``details`` carries UUIDs only — never an
    owner's email, phone or name. This exception is going to end up in logs and,
    eventually, in operator-facing error surfaces; identifiers are enough to
    investigate with, and personal contact details are not.
    """

    code = 'owner_inconsistent'

    def __init__(self, code, details=None):
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})

    def __str__(self):
        return f'{self.code}: {self.details}'


def _carries_owner_role(roles):
    """
    Whether a ``RestaurantEmployee.roles`` value grants the owner role.

    ``roles`` is an untyped ``JSONField``, so it is defensive about shape: a value
    that is not a list grants nothing rather than raising. Compared against the
    canonical ``RESTAURANT_OWNER`` constant — never a hand-written 'owner' literal.
    """
    if not isinstance(roles, (list, tuple)):
        return False
    return RESTAURANT_OWNER in roles


def assert_owner_consistency(restaurant):
    """
    Assert that ``restaurant``'s owner of record IS its owner authority.

    CONSISTENT means both of:

      1. exactly ONE active (``active=True``), non-deleted (``deleted=False``)
         ``RestaurantEmployee`` at this exact restaurant carries ``RESTAURANT_OWNER``;
      2. that membership's user is ``restaurant.owner``.

    Returns that membership so a caller can act on the row it just validated,
    rather than re-querying for it and re-deriving which one was canonical.

    Raises ``OwnerConsistencyError`` with one of ``OWNER_CONSISTENCY_CODES``.
    Multiplicity is checked BEFORE the identity match on purpose: when two people
    both hold live owner authority, "one of them happens to match the FK" is not a
    consistent state — it is an ambiguous one that a human has to resolve.

    WHAT DOES NOT COUNT, and why each exclusion matters:

      * an INACTIVE or SOFT-DELETED membership. Those are exactly the rows the
        permission resolver already refuses to read, so counting them here would
        let this validator pass for a restaurant whose "owner" cannot sign in.
      * the ``manager`` role. Managers hold the same default module grid as owners,
        which makes them easy to mistake for owners; they are not, and an
        onboarding writer must never treat one as the tenant's owner.
      * ``User.roles``. Global role metadata is not restaurant authority — that is
        precisely the ambient-authority mechanism Phase 0.5 removed, and it must
        not creep back as an accepted proof of ownership.

    ACCOUNT ELIGIBILITY IS A SEPARATE QUESTION and is deliberately NOT asked here.
    An owner whose ``User`` is deactivated may well be a blocker for adoption or
    go-live, but the FK and the membership can still be structurally in agreement.
    Conflating the two would mean this check answered "is this restaurant ready?"
    when the only thing it can actually answer is "does the owner of record agree
    with the owner authority?". Later services own eligibility.
    """
    # Roles live in a JSONField and are filtered in Python rather than with a
    # `roles__contains` lookup: that lookup is unsupported on SQLite (which the
    # fast local checks run on) and a restaurant's live employee list is small.
    memberships = list(
        RestaurantEmployee.objects.filter(
            restaurant=restaurant,
            active=True,
            deleted=False,
        ).only('id', 'user_id', 'roles')
    )
    owner_memberships = [m for m in memberships if _carries_owner_role(m.roles)]

    restaurant_id = str(restaurant.pk)
    owner_id = str(restaurant.owner_id) if restaurant.owner_id else None

    if not owner_memberships:
        raise OwnerConsistencyError(
            MISSING_OWNER_MEMBERSHIP,
            {'restaurant_id': restaurant_id, 'owner_id': owner_id},
        )

    if len(owner_memberships) > 1:
        raise OwnerConsistencyError(
            MULTIPLE_OWNER_MEMBERSHIPS,
            {
                'restaurant_id': restaurant_id,
                'owner_id': owner_id,
                'membership_user_ids': sorted(
                    str(m.user_id) for m in owner_memberships
                ),
            },
        )

    membership = owner_memberships[0]
    if str(membership.user_id) != owner_id:
        raise OwnerConsistencyError(
            OWNER_MEMBERSHIP_MISMATCH,
            {
                'restaurant_id': restaurant_id,
                'owner_id': owner_id,
                'membership_user_id': str(membership.user_id),
            },
        )

    return membership
