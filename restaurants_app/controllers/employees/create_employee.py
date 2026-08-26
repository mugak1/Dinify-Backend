from django.db import transaction

from restaurants_app.controllers.employee_membership_lock import (
    lock_restaurant_for_membership_mutation,
)
from restaurants_app.models import RestaurantEmployee


def create_employee_from_existing_user(
    user_id: str,
    restaurant_id: str,
    roles: list
) -> dict:
    """
    Checks if the employee records already exists but deleted

    SERIALIZATION. Reactivating a soft-deleted membership is the widest of the
    membership writes: it can produce a live ``RESTAURANT_OWNER`` membership out of a
    row that ``assert_owner_consistency`` never read — because the assertion filters
    on ``deleted=False`` — and, being an UPDATE that leaves the FK alone, PostgreSQL's
    referential integrity does not block it against a held parent lock the way it
    blocks an INSERT. So this path takes the parent ``Restaurant`` barrier
    (``Restaurant -> RestaurantEmployee``) before it reads the membership, and holds
    it through the write.

    The whole read-decide-write sequence is now ONE transaction with the membership
    re-read under its own row lock. It used to be three autocommitted statements with
    a check-then-act window between them, which meant two concurrent reactivations of
    the same membership could both pass the ``deleted`` check and the second could
    overwrite the first's roles.
    """
    with transaction.atomic():
        # THE SERIALIZATION POINT — taken before the membership is read, so the
        # decision below is authoritative for the write that follows it. An
        # unresolvable restaurant returns None; the caller's authorization gate has
        # already refused that case, and the membership lookup below then finds
        # nothing, so the 201 "no existing record" answer is unchanged.
        lock_restaurant_for_membership_mutation(restaurant_id)

        # `.order_by()` clears `Meta.ordering`, whose `user__first_name` term would
        # otherwise put a join in the locking SELECT for no benefit —
        # `unique_together (user, restaurant)` already guarantees at most one row.
        employee = (
            RestaurantEmployee.objects
            .select_for_update(of=('self',))
            .filter(user=user_id, restaurant=restaurant_id)
            .order_by()
            .first()
        )

        if employee is not None and employee.deleted:
            # Invariant guards. This path writes the model directly rather than
            # through SerializerPutRestaurantEmployee, so both halves have to be
            # restated here: refuse reactivating a soft-deleted membership for a
            # platform-staff account, and refuse client-supplied platform-only
            # roles. Lazy import avoids an import cycle. Both are no-ops for
            # ordinary restaurant_user accounts and ordinary restaurant roles.
            from platform_admin_app.services import (
                assert_no_platform_roles, guard_membership_creation,
            )
            guard_membership_creation(employee.user)
            assert_no_platform_roles(roles)
            employee.active = True
            employee.deleted = False
            employee.roles = roles
            employee.save()

            # TODO log the activity

            return {
                'status': 200,
                'message': 'The employee has been added successfully.'
            }

    return {
        'status': 201
    }
