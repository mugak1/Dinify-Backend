from typing import Optional
from restaurants_app.models import Restaurant, Table, DiningArea
from users_app.models import User
from django.db import transaction


def confirm_availability_of_table_numbers(restaurant_id: str, range_from: int, range_to: int):
    current_table_nos = Table.objects.values('number').filter(
        restaurant=restaurant_id,
        number__gte=range_from, number__lte=range_to,
        deleted=False
    )
    for x in current_table_nos:
        print(f" table number {x['number']} ")
    existing_table_numbers = [int(x['number']) for x in current_table_nos]
    r_start = range_from
    r_end = range_to + 1
    print(f" {restaurant_id} table numbers from {r_start} to {r_end} ")
    print(f" existing table numbers {existing_table_numbers} ")
    for number in range(r_start, r_end):
        if number in existing_table_numbers:
            return {
                'status': 400,
                'message': f"Table number {number} is already present."
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


def get_tables_by_area(restaurant_id: str):
    tables_listing = []

    # get the dining areas to consider
    dining_areas = DiningArea.objects.filter(
        restaurant=restaurant_id,
        deleted=False
    ).values('id', 'name', 'available', 'description')

    # get the tables in each area
    for area in dining_areas:
        tables = Table.objects.filter(
            deleted=False,
            dining_area=area['id']
        ).values('id', 'number', 'available', 'reserved')

        tables_listing.append({
            'dining_area': area,
            'tables': list(tables)
        })

    # include tables that are associated with any area
    tables = Table.objects.filter(
        deleted=False,
        dining_area=None,
        restaurant=restaurant_id,
    ).values('id', 'number', 'available', 'reserved')
    if tables.count() > 0:
        tables_listing.append({
            'dining_area': {
                'id': None,
                'name': 'Not Assigned'
            },
            'tables': list(tables)
        })
    return {
        'status': 200,
        'message': 'Tables by dining area',
        'data': tables_listing
    }
