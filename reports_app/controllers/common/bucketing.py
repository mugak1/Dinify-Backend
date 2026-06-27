"""
DB-side period bucketing for Order-based reports.

Replaces the legacy "trends" N+1 — the pattern in ``sales.py`` / ``diners.py``
that re-runs the full per-period summary once per day / month / quarter / year
inside a Python loop (a 31-day trend ran ~250 queries) — with a SINGLE grouped
aggregation: truncate ``time_created`` to the period boundary in EAT, group by
it, and let the database sum each bucket.

The revenue / discount basis is imported from :mod:`sale_filters` so it stays
defined in exactly one place.
"""

from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import (
    ExtractHour,
    TruncDay,
    TruncWeek,
    TruncMonth,
    TruncQuarter,
    TruncYear,
)

from reports_app.controllers.common.sale_filters import (
    revenue_sum,
    discount_sum,
)


# Bucket on the local (EAT) calendar boundary, not UTC, so an order at
# 23:30 UTC (02:30 EAT the next day) lands in the correct local day/period.
LOCAL_TZ = ZoneInfo(settings.TIME_ZONE)

PERIOD_TRUNC = {
    'day': TruncDay,
    'week': TruncWeek,
    'month': TruncMonth,
    'quarter': TruncQuarter,
    'year': TruncYear,
}


def bucket_sales(order_qs, period):
    """Group a (sale-filtered) ``Order`` queryset into period buckets.

    Runs as ONE grouped query — never the legacy loop of re-running the
    summary once per period.

    :param order_qs: an ``Order`` queryset (typically from
        :func:`reports_app.controllers.common.sale_filters.sale_orders`).
    :param period: one of ``'day'``, ``'week'``, ``'month'``, ``'quarter'``,
        ``'year'``.
    :returns: a list of dicts ordered ascending by period, each shaped::

            {
                'period': <aware datetime, start of the EAT period>,
                'count': <int>,
                'revenue': <Decimal | None>,
                'discount': <Decimal | None>,
            }

    Truncation runs in EAT (``tzinfo=LOCAL_TZ``) so buckets align to the local
    calendar; with ``tzinfo`` set, each returned ``period`` is an aware
    datetime in EAT.
    :raises ValueError: if ``period`` is not a supported granularity.
    """
    try:
        trunc = PERIOD_TRUNC[period]
    except KeyError:
        raise ValueError(
            f"Unsupported period {period!r}; expected one of "
            f"{sorted(PERIOD_TRUNC)}"
        )

    return list(
        order_qs
        .annotate(period=trunc('time_created', tzinfo=LOCAL_TZ))
        .values('period')
        .annotate(
            count=Count('id'),
            revenue=revenue_sum(),
            discount=discount_sum(),
        )
        .order_by('period')
    )


def bucket_sales_by_hour(order_qs):
    """Group a (sale-filtered) ``Order`` queryset by hour-of-day (0–23) in EAT.

    A sibling to :func:`bucket_sales`, but keyed by an *integer hour* rather
    than a ``Trunc`` datetime — so it is deliberately NOT a ``PERIOD_TRUNC``
    entry. Runs as ONE grouped query, reusing the same revenue / discount
    basis so the figures agree with the other Sales panes.

    :param order_qs: an ``Order`` queryset (typically from
        :func:`reports_app.controllers.common.sale_filters.sale_orders`).
    :returns: a list of dicts ordered ascending by hour, with one entry per
        hour that had orders, each shaped::

            {
                'hour': <int 0–23>,
                'count': <int>,
                'revenue': <Decimal | None>,
                'discount': <Decimal | None>,
            }

        Hours with no orders are absent — the caller zero-fills to a
        continuous 0–23 axis.

    The hour is extracted in EAT (``tzinfo=LOCAL_TZ``), which is the
    correctness must: an order at 23:30 UTC buckets into hour 2 (02:30 EAT),
    not hour 23.
    """
    return list(
        order_qs
        .annotate(hour=ExtractHour('time_created', tzinfo=LOCAL_TZ))
        .values('hour')
        .annotate(
            count=Count('id'),
            revenue=revenue_sum(),
            discount=discount_sum(),
        )
        .order_by('hour')
    )
