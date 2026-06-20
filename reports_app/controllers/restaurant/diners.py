"""
Restaurant Diners reports — summary and listing.

Diners is Order-based, so it shares the PR3 reporting foundations with the Sales
reports: the "sale" set is ``SALE_STATUSES`` ({served, paid}) via ``sale_orders``
and per-diner spend is ``Sum('actual_cost')`` (net revenue) — the SAME basis the
Sales report uses, so the panes agree.

NULL-customer handling is the whole point of this rebuild. Dinify is
anonymous-QR-first, so ``Order.customer`` is a nullable FK and MOST sale orders
are guests (``customer IS NULL``). Identified-diner metrics therefore operate
strictly on ``sale_orders(...).exclude(customer__isnull=True)``: the NULL bucket
is never collapsed into one phantom "diner" and never counted as a repeat diner
(the old inflation bug). Guests are surfaced honestly as a separate
``guest_orders`` count.

The ``{status, message, data}`` envelope and the two public entrypoints
(``generate_restaurant_diners_summary`` / ``_listing``) are kept so the endpoint
dispatch (``reports_app/endpoints/restaurant_reports.py``) is unchanged. The
legacy ``diners-trends`` report was dropped: on anonymous-QR data it is dominated
by guest volume and adds nothing over the Sales trend (a clean
distinct-diners-per-period series can be added later if ever wanted).
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Avg, Max

from misc_app.controllers.clean_dates import clean_dates
from reports_app.controllers.common.sale_filters import (
    sale_orders, revenue_sum, REVENUE_FIELD,
)


TWO_PLACES = Decimal('0.01')


def _money_2dp(value):
    """Quantize a Decimal money value to 2dp (HALF_UP); ``None`` -> ``0``.

    A ``Sum`` of 2dp columns is already exact, but an ``Avg`` or a division can
    carry extra places — money is 2dp on the wire.
    """
    if value is None:
        return 0
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _diner_name(first_name, last_name, phone_number):
    """Build a display name from the (both-nullable) identity fields.

    Falls back to the phone number (always present on an identified User) and
    finally the empty string, so an unnamed diner never renders as ``'None'``.
    """
    name = ' '.join(part for part in (first_name, last_name) if part)
    return name or phone_number or ''


def generate_restaurant_diners_summary(
    restaurant_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    dates = clean_dates(date_from=date_from, date_to=date_to)
    if dates.get('status') != 200:
        return dates
    date_from = dates['date_from']
    date_to = dates['date_to']

    sales = sale_orders(restaurant_id, date_from, date_to)

    # ONE grouped row per IDENTIFIED diner (NULL customers excluded up front) —
    # grouped by the customer PK (NOT first_name, which would merge distinct
    # diners and fold every guest into a single None group) and ordered by sale
    # count so row[0] is the most active diner.
    per_diner = list(
        sales
        .exclude(customer__isnull=True)
        .values(
            'customer',
            'customer__first_name',
            'customer__last_name',
            'customer__phone_number',
        )
        .annotate(
            order_count=Count('id'),
            total_spend=revenue_sum(),
        )
        .order_by('-order_count')
    )

    identified_diners = len(per_diner)
    # >1 sale in range == a repeat diner. The NULL bucket is not in per_diner, so
    # the anonymous majority can never inflate this (the whole point).
    repeat_diners = sum(1 for row in per_diner if row['order_count'] > 1)

    # Average spend PER DINER: total net spend over identified sale orders divided
    # by the number of identified diners (not per order); 0 with no diners.
    total_identified_spend = sum(
        (row['total_spend'] for row in per_diner), Decimal('0.00'),
    )
    average_spend_per_identified_diner = (
        _money_2dp(total_identified_spend / identified_diners)
        if identified_diners else 0
    )

    # Most active == most sales in range (by COUNT, never by spend); null when
    # there are no identified diners.
    most_active_diner = None
    if per_diner:
        top = per_diner[0]
        most_active_diner = {
            'name': _diner_name(
                top['customer__first_name'],
                top['customer__last_name'],
                top['customer__phone_number'],
            ),
            'order_count': top['order_count'],
            'total_spend': top['total_spend'],
        }

    # Guests (anonymous QR) are the honest majority — their own count, NOT folded
    # into the identified-diner metrics above.
    guest_orders = sales.filter(customer__isnull=True).count()

    data = {
        'identified_diners': identified_diners,
        'repeat_diners': repeat_diners,
        'guest_orders': guest_orders,
        'average_spend_per_identified_diner': average_spend_per_identified_diner,
        'most_active_diner': most_active_diner,
    }
    return {
        'status': 200,
        'message': 'Successfully retrieved the diners summary',
        'data': data,
    }


def generate_restaurant_diners_listing(
    restaurant_id: str,
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
            'message': 'Date range cannot be greater than 31 days.',
        }

    # ONE grouped+joined query — replaces the legacy per-row loop that (a) did
    # ``UUID.first_name`` (the customer value is the PK, not a User) and crashed,
    # (b) ran a per-customer N+1, and (c) aggregated all-time instead of the date
    # range. Identified diners only; the User identity fields come through the
    # ORM join and the money is ``actual_cost`` over the date-bounded sale set.
    rows = (
        sale_orders(restaurant_id, date_from, date_to)
        .exclude(customer__isnull=True)
        .values(
            'customer',
            'customer__first_name',
            'customer__last_name',
            'customer__phone_number',
        )
        .annotate(
            no_orders=Count('id'),
            total_spend=revenue_sum(),
            average_spend=Avg(REVENUE_FIELD),
            last_order_date=Max('time_created'),
        )
        .order_by('-no_orders')
    )

    diners = [
        {
            'customer_id': row['customer'],
            'name': _diner_name(
                row['customer__first_name'],
                row['customer__last_name'],
                row['customer__phone_number'],
            ),
            'phone_number': row['customer__phone_number'],
            'no_orders': row['no_orders'],
            'total_spend': row['total_spend'],
            'average_spend': _money_2dp(row['average_spend']),
            'last_order_date': row['last_order_date'],
        }
        for row in rows
    ]
    return {
        'status': 200,
        'message': 'Successfully retrieved the diners listing',
        'data': diners,
    }
