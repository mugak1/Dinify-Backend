from typing import Optional
from restaurants_app.models import Restaurant, Table, DiningArea
from users_app.models import User
from django.db import transaction


def confirm_availability_of_table_numbers(restaurant_id: str, range_from: int, range_to: int):
    existing_table_numbers = set(
        Table.objects.filter(
            restaurant=restaurant_id,
            number__gte=range_from, number__lte=range_to
        ).count()
    )
    for number in range(range_from, range_to + 1):
        if number in existing_table_numbers:
            return {
                'status': 400,
                'message': f"Table number {number} is already in use."
            }
    return {'status': 200}


def create_tables_in_section(
    restaurant_id: str,
    no_tables: int,
    user: User,
    consideration: Optional[str] = 'count',
    range_from: Optional[int] = None,
    range_to: Optional[int] = None,
    dining_area: Optional[DiningArea] = None
) -> dict:
    tables = []
    restaurant = Restaurant.objects.get(id=restaurant_id)
    with transaction.atomic():
        if consideration == 'count':
            # get the count of tables at the restaurant
            table_count = Table.objects.select_for_update().filter(
                restaurant=restaurant
            ).count()
            for i in range(no_tables):
                table = Table(
                    number=table_count+i+1,
                    restaurant=restaurant,
                    created_by=user,
                    dining_area=dining_area
                )
                tables.append(table)
        else:
            for i in range(range_from, range_to+1):
                table = Table(
                    number=i,
                    restaurant=restaurant,
                    created_by=user,
                    dining_area=dining_area
                )
                tables.append(table)

        Table.objects.bulk_create(tables)

    return {
        'status': 200,
        'message': f"{len(tables)} tables created successfully",
        "data": {
            "no_tables": len(tables)
        }
    }
