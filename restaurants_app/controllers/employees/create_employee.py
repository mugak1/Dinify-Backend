from restaurants_app.models import RestaurantEmployee


def create_employee_from_existing_user(
    user_id: str,
    restaurant_id: str,
    roles: list
) -> dict:
    """
    Checks if the employee records already exists but deleted
    """
    present_employee_record = RestaurantEmployee.objects.filter(
        user=user_id,
        restaurant=restaurant_id
    )

    if present_employee_record.exists():
        employee = present_employee_record.first()
        if employee.deleted:
            # Invariant guard: refuse reactivating a soft-deleted membership for a
            # platform-staff account. Lazy import avoids an import cycle. A no-op
            # for ordinary restaurant_user accounts.
            from platform_admin_app.services import guard_membership_creation
            guard_membership_creation(employee.user)
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
