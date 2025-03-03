from restaurants_app.models import Restaurant, Table
from users_app.models import User
from django.db import transaction


def create_tables_in_section(
    restuarant_id: str,
    section_name: str,
    no_tables: int,
    smoking_zone: bool,
    outdoor_seating: bool,
    user: User
) -> dict:
    tables = []
    restaurant = Restaurant.objects.get(id=restuarant_id)
    with transaction.atomic():
        # get the count of tables at the restaurant
        table_count = Table.objects.select_for_update().filter(restaurant=restaurant).count()
        for i in range(no_tables):
            table = Table(
                number=table_count+i+1,
                restaurant=restaurant,
                room_name=section_name,
                created_by=user,
                smoking_zone=smoking_zone,
                outdoor_seating=outdoor_seating
            )
            tables.append(table)

        Table.objects.bulk_create(tables)
    return {
        'status': 200,
        'message': f"{no_tables} tables created successfully in the section, {section_name}"
    }
