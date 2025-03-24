from typing import Optional
from django.db import transaction
from restaurants_app.models import Restaurant, DiningArea
from users_app.models import User
from restaurants_app.controllers.tables import (
    confirm_availability_of_table_numbers,
    create_tables_in_section
)


def create_dining_area(
    restaurant_id: str,
    dining_area_name: str,
    smoking_zone: bool,
    outdoor_seating: bool,
    user: User,
    create_tables: bool = False,
    consideration: Optional[str] = 'count',
    no_tables: Optional[int] = None,
    range_from: Optional[int] = None,
    range_to: Optional[int] = None
) -> dict:
    """
    creates a dining area and tables in the dining area
    """
    message = f"Dining area, {dining_area_name.title()} created successfully"

    if create_tables and consideration == 'range':
        if range_from is None:
            return {
                'status': 400,
                'message': "Please provide the starting table number"
            }
        if range_to is None:
            return {
                'status': 400,
                'message': "Please provide the ending table number"
            }
        clear_table_numbers = confirm_availability_of_table_numbers(
            restaurant_id=restaurant_id,
            range_from=range_from,
            range_to=range_to
        )
        if clear_table_numbers.get('status') == 400:
            return clear_table_numbers

    with transaction.atomic():
        restaurant = Restaurant.objects.get(id=restaurant_id)
        # check if the dining area is already present
        try:
            dining_area = DiningArea.objects.get(
                name=dining_area_name.title(),
                restaurant=restaurant
            )
            return {
                'status': 400,
                'message': f"The dining area, {dining_area_name.title()} already exists"
            }
        except DiningArea.DoesNotExist:
            pass
        dining_area = DiningArea(
            name=dining_area_name.title(),
            restaurant=restaurant,
            smoking_zone=smoking_zone,
            outdoor_seating=outdoor_seating,
            created_by=user
        )
        dining_area.save()

        if create_tables:
            tables = create_tables_in_section(
                restaurant_id=restaurant_id,
                no_tables=no_tables,
                user=user,
                consideration=consideration,
                range_from=range_from,
                range_to=range_to,
                dining_area=dining_area
            )
            if tables.get('status') == 200:
                message += f" with {tables.get('data').get('no_tables', 0)} tables"

    return {
        'status': 200,
        'message': message
    }
