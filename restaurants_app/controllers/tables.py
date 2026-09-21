import logging
from typing import Optional

logger = logging.getLogger(__name__)

from restaurants_app.models import Restaurant, Table, DiningArea
from users_app.models import User
from django.db import transaction
from orders_app.controllers.initiate_order import any_present_ongoing_order
from restaurants_app.controllers.diner_capability import issue_qr_credential


def confirm_availability_of_table_numbers(restaurant_id: str, range_from: int, range_to: int):
    current_table_nos = Table.objects.values('number').filter(
        restaurant=restaurant_id,
        number__gte=range_from, number__lte=range_to,
        deleted=False
    )
    for x in current_table_nos:
        logger.debug("table number %s", x['number'])
    existing_table_numbers = [int(x['number']) for x in current_table_nos]
    r_start = range_from
    r_end = range_to + 1
    logger.debug("%s table numbers from %s to %s", restaurant_id, r_start, r_end)
    logger.debug("existing table numbers %s", existing_table_numbers)
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
    with transaction.atomic():
        # The restaurant row is fetched HERE, inside the transaction, and LOCKED —
        # it is what actually serialises table-number allocation.
        #
        # It used to be an unlocked fetch above the atomic block, and the count
        # below carried `select_for_update()` instead. That lock was never taken:
        # Django's `.count()` routes through `Query.get_aggregation()`, which sets
        # `outer_query.select_for_update = False` before compiling (django/db/
        # models/sql/query.py:625, Django 5.2), so `FOR UPDATE` was never emitted
        # and the statement ran as a plain unlocked `SELECT COUNT(*)`. PostgreSQL
        # would have rejected `SELECT COUNT(*) ... FOR UPDATE` outright — it never
        # saw one. Two concurrent creations therefore both read the same count and
        # both tried to write tables numbered count+1..count+N. Table's
        # `unique_together = ['number', 'str_number', 'restaurant']` caught the
        # collision, so this was never duplicated data — it was an uncaught
        # IntegrityError propagating out as a 500 for whichever creation lost.
        #
        # Locking the PARENT row is the fix, not locking the counted rows: a row
        # lock on existing tables would not stop a concurrent INSERT, which is the
        # race that matters. Same shape as `allocate_daily_order_number`
        # (orders_app/controllers/services/create_order.py) — lock the parent, read,
        # allocate.
        #
        # Lock order: this transaction takes `Restaurant` and then INSERTs `Table`
        # rows, and never reaches for the admission advisory lock. Order creation
        # takes `advisory(SHARED) -> Table(row) -> ...` and never row-locks
        # `Restaurant`; the lifecycle transition takes
        # `advisory(EXCLUSIVE) -> Restaurant -> AdminAuditLog`. No cycle either way.
        restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)

        if consideration == 'count':
            # get the count of tables at the restaurant
            table_count = Table.objects.filter(
                restaurant=restaurant
            ).count()
            for i in range(no_tables):
                table_number = table_count + i + 1
                table = Table(
                    number=table_number,
                    str_number=str(table_number),
                    restaurant=restaurant,
                    created_by=user,
                    dining_area=dining_area
                )
                tables.append(table)
        else:
            for i in range(range_from, range_to+1):
                table = Table(
                    number=i,
                    str_number=str(i),
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


def _grouped_table_row(table, qr_policy):
    """
    ONE row of the grouped tables read — the shape both branches emit.

    The assigned and unassigned branches built this dict independently and
    identically, which is exactly how a change lands on one of them and not the
    other: the delegated-credential triage had to pin the second branch
    separately because a fix to the first would have looked complete. They share
    one builder now, so a field cannot be added, removed or gated on one side
    only.

    ``qr_credential`` IS ADDED, NEVER COMPUTED-THEN-DROPPED. When the policy does
    not permit it the key is simply absent and ``issue_qr_credential`` is not
    called, so no bearer credential is minted for a caller who may not have one.
    Every other key, and their order, is unchanged.
    """
    row = {
        'id': table.id,
        'number': table.number,
        'enabled': table.enabled,
        'reserved': table.reserved,
        'available': get_table_availability(table_id=str(table.id)),
        'display_name': table.display_name,
        'min_capacity': table.min_capacity,
        'max_capacity': table.max_capacity,
        'shape': table.shape,
        'status': table.status,
        'tags': table.tags,
        'has_qr': table.has_qr,
        'qr_mode': table.qr_mode,
    }
    if qr_policy is not None and qr_policy.allows(table.restaurant_id):
        row['qr_credential'] = issue_qr_credential(
            table.restaurant_id, table.id, table.qr_version,
        )
    row.update({
        'floor_x': table.floor_x,
        'floor_y': table.floor_y,
        'is_active': table.is_active,
    })
    return row


def get_tables_by_area(restaurant_id: str, qr_policy=None):
    """
    The grouped (``?grouping=``) tables read.

    ``qr_policy`` is the response-entitlement decision resolved by the CALLER,
    from the request, after its own authorization has run — see
    ``restaurants_app.controllers.qr_disclosure``. It is keyword-only in practice
    and DEFAULTS TO NONE, which WITHHOLDS: a caller that forgets to pass one (a
    direct call, a management command, a test) gets a perfectly good tables
    listing with no QR credentials in it, rather than a listing that hands out
    bearer authority because nobody said not to.
    """
    tables_listing = []

    # get the dining areas to consider
    dining_areas = DiningArea.objects.filter(
        restaurant=restaurant_id,
        deleted=False
    ).values('id', 'name', 'available', 'description',
             'is_indoor', 'accessible', 'default_server_section', 'is_active')

    # get the tables in each area
    for area in dining_areas:
        # tables = Table.objects.filter(
        #     deleted=False,
        #     dining_area=area['id']
        # ).values('id', 'number', 'enabled', 'reserved')
        area_tables = Table.objects.filter(
            deleted=False,
            dining_area=area['id']
        )

        area_table_listing = [
            _grouped_table_row(table, qr_policy) for table in area_tables
        ]

        tables_listing.append({
            'dining_area': area,
            'tables': area_table_listing
        })

    # include tables that are associated with any area
    # tables = Table.objects.filter(
    #     deleted=False,
    #     dining_area=None,
    #     restaurant=restaurant_id,
    # ).values('id', 'number', 'enabled', 'reserved')

    # if tables.count() > 0:
    #     tables_listing.append({
    #         'dining_area': {
    #             'id': None,
    #             'name': 'Not Assigned'
    #         },
    #         'tables': list(tables)
    #     })

    unassigned_tables = Table.objects.filter(
        deleted=False,
        dining_area=None,
        restaurant=restaurant_id,
    )
    if unassigned_tables.count() > 0:
        tables_listing.append({
            'dining_area': {
                'id': None,
                'name': 'Not Assigned'
            },
            'tables': [
                _grouped_table_row(table, qr_policy)
                for table in unassigned_tables
            ]
        })
    return {
        'status': 200,
        'message': 'Tables by dining area',
        'data': tables_listing
    }


def get_table_availability(table_id: str = None, table: Table = None) -> dict:
    # Callers that already hold the Table instance can pass it directly to
    # skip a redundant re-fetch; otherwise it is resolved from table_id.
    table_record = table if table is not None else Table.objects.get(id=table_id)
    if not table_record.enabled:
        return {
            'available': False,
            'message': 'Disabled'
        }
    if table_record.reserved:
        return {
            'available': False,
            'message': 'Reserved'
        }
    # check for ongoing orders
    present_order = any_present_ongoing_order(table=table_record)
    if present_order['present']:
        return {
            'available': False,
            'message': 'Ongoing order',
            'order_id': present_order['order_id']
        }
    return {
        'available': True,
        'message': 'Available'
    }
