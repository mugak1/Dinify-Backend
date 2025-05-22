from restaurants_app.controllers.employees.create_employee import create_employee_from_existing_user


class ConRestaurantEmployee:

    @staticmethod
    def create_employee_from_existing_user(
        user_id: str,
        restaurant_id: str,
        roles: list
    ) -> dict:
        """
        Checks if the employee records already exists but deleted
        """
        return create_employee_from_existing_user(
            user_id=user_id,
            restaurant_id=restaurant_id,
            roles=roles
        )
