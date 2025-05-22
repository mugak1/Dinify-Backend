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
