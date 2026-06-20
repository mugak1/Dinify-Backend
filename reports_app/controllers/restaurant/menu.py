"""
Restaurant Menu performance report — one grouped row-set per grouping.

Rebuilt on the PR3 reporting foundations so it agrees with the Sales report:
  * it counts only OrderItems whose order is a *sale* ({served, paid}) via
    ``order__in=sale_orders(...)`` — the legacy controller had no
    ``order_status`` filter, so cancelled / pending orders inflated every row,
  * revenue is ``Sum('actual_cost')`` (the canonical net revenue basis, via
    ``revenue_sum``) — NOT ``total_cost`` (pre-discount gross),
  * ``quantity_sold`` is ``Sum('quantity')`` and ``order_count`` is
    ``Count('id')`` (the number of order-item line entries in the group),
  * each grouping is ONE grouped query (``values().annotate()``) — the legacy
    per-section / per-group / per-item N+1 (and its ``peak_hours`` /
    ``most_ordered_item`` per-row sub-queries) is gone.

Names come from the *live* FK join (``item__section__name`` /
``item__section_group__name`` / ``item__name``). ``OrderItem`` only snapshots
``item_name_snapshot`` (for order display) — there is no section/group snapshot
— so grouping walks the live menu tree, which is the correct source for a
"current menu performance" report.

Dropped on purpose:
  * ``peak_hours`` / ``most_ordered_item`` — these were the per-row sub-query
    culprits, and hourly / peak-time is a *separate* report, not a menu-row
    column.
  * ``average_rating`` is included only as a ``null`` scaffold on the ITEMS
    grouping. Reviews are visit-level (``reviews_app.Review`` is one per
    ``Order``) — there is no per-item rating source — so it is NOT computed.

NOTE on ``.order_by('-quantity_sold')``: besides sorting by popularity, the
explicit ``order_by`` is *required* to override ``OrderItem.Meta.ordering``
(``['-time_created', 'item__name']``). Without it, Django appends those fields
to the GROUP BY of a ``values().annotate()`` and silently fragments the groups.
"""
from django.db.models import Count, Sum

from orders_app.models import OrderItem
from misc_app.controllers.clean_dates import clean_dates
from reports_app.controllers.common.sale_filters import sale_orders, revenue_sum


# ?grouping= value -> (group-by FK field, display-name field) on the live join.
GROUPINGS = {
    'sections': ('item__section', 'item__section__name'),
    'groups': ('item__section_group', 'item__section_group__name'),
    'items': ('item', 'item__name'),
}


def generate_restaurant_menu_summary(
    restaurant_id: str,
    grouping: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    if (date_to - date_from).days > 31:
        return {
            'status': 400,
            'message': 'Date range should not be greater than 31 days.',
        }

    if grouping not in GROUPINGS:
        return {
            'status': 400,
            'message': 'Invalid grouping',
        }
    group_field, name_field = GROUPINGS[grouping]

    # Only line items belonging to a sale order. ``sale_orders`` stays lazy and
    # is inlined as an ``order__in`` subquery, so the grouped aggregation below
    # is a single query. Restaurant scope + EAT-aligned date range come free.
    sale_items = OrderItem.objects.filter(
        order__in=sale_orders(restaurant_id, date_from, date_to),
    )
    if grouping == 'groups':
        # section_group is nullable; an item with no group belongs to none.
        sale_items = sale_items.exclude(item__section_group__isnull=True)

    grouped = (
        sale_items
        .values(group_field, name_field)
        .annotate(
            order_count=Count('id'),
            quantity_sold=Sum('quantity'),
            revenue=revenue_sum(),
        )
        .order_by('-quantity_sold')  # also overrides Meta.ordering GROUP BY leak
    )

    rows = []
    for row in grouped:
        record = {
            'name': row[name_field],
            'order_count': row['order_count'],
            'quantity_sold': row['quantity_sold'] or 0,
            'revenue': row['revenue'] if row['revenue'] is not None else 0,
        }
        if grouping == 'items':
            # Scaffold only: reviews are visit-level, so there is no per-item
            # rating source. Emit null; do NOT compute.
            record['average_rating'] = None
        rows.append(record)

    return {
        'status': 200,
        'message': 'Successfully retrieved the menu summary',
        'data': {
            'grouping': grouping,
            'rows': rows,
        },
    }
