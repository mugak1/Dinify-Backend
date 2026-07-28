"""
DB-side bucketing for Order-based reports, and the axis a caller fills it onto.

Replaces the legacy "trends" N+1 — the pattern in ``sales.py`` / ``diners.py``
that re-runs the full per-period summary once per day / month / quarter / year
inside a Python loop (a 31-day trend ran ~250 queries) — with a SINGLE grouped
aggregation: truncate ``time_created`` to the period boundary in EAT, group by
it, and let the database sum each bucket.

ONE DENSITY POLICY, both paths. A grouped query can only return groups that
have rows, so :func:`bucket_sales` and :func:`bucket_sales_by_hour` BOTH return
only the buckets that had orders. Emitting the empty ones is the caller's job,
and this module supplies the axis to emit them onto:

* hour-of-day is a fixed finite domain, so its caller fills onto ``range(24)``
  (``sales.py``);
* a period axis depends on the requested window, so this module derives it —
  :func:`period_boundaries` — and the caller fills onto that.

Filling server-side is deliberate. A period with no orders is a real, reportable
zero: omitting it makes a chart join the surrounding periods into a straight
line and imply trading that did not happen. Every consumer of a series from this
module therefore gets one row per bucket in the requested window.

The revenue / discount basis is imported from :mod:`sale_filters` so it stays
defined in exactly one place.
"""

from datetime import datetime, time, timedelta
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

# The granularities :func:`period_boundaries` can enumerate — the UNION of the
# two bucketing vocabularies it serves, because it is the shared axis for both
# `sales-trends` (`PERIOD_TRUNC` here: no 'hour') and `dashboard-v2`
# (`dashboard.BUCKET_TRUNC`: no 'quarter').
#
# It is deliberately NOT derived from `PERIOD_TRUNC`, and this is NOT the
# unification of the two maps that `dashboard.py` warns against. Those map a
# granularity to a DB truncation function and each omits what its endpoint has
# no caller for; this is pure calendar arithmetic with no endpoint opinion, so
# covering both costs nothing and commits nobody. Adding 'hour' HERE does not
# add it to `PERIOD_TRUNC` — `sales-trends` still rejects it — which is what
# keeps that documented asymmetry intact.
BOUNDARY_PERIODS = ('hour', 'day', 'week', 'month', 'quarter', 'year')


def _period_start(moment, period):
    """The naive local datetime at which ``moment``'s bucket begins."""
    if period == 'hour':
        return moment.replace(minute=0, second=0, microsecond=0)

    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        return day
    if period == 'week':
        # Monday-anchored, matching TruncWeek.
        return day - timedelta(days=day.weekday())
    if period == 'month':
        return day.replace(day=1)
    if period == 'quarter':
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    return day.replace(month=1, day=1)


def _next_period_start(start, period):
    """The naive local datetime at which the bucket AFTER ``start`` begins."""
    if period == 'hour':
        return start + timedelta(hours=1)
    if period == 'day':
        return start + timedelta(days=1)
    if period == 'week':
        return start + timedelta(days=7)

    # month / quarter / year advance by whole months. ``start`` is already the
    # 1st for all three, so there is no short-month day to clamp.
    months = start.month - 1 + {'month': 1, 'quarter': 3, 'year': 12}[period]
    return start.replace(year=start.year + months // 12, month=months % 12 + 1)


def period_boundaries(date_from, date_to, period):
    """The complete, ordered bucket axis covering an inclusive local-day window.

    The counterpart to :func:`bucket_sales` / :func:`bucket_sales_by_hour`: they
    return only the buckets that HAVE orders, this returns every bucket that
    SHOULD appear, and the caller joins the two so empty periods emit as zeros
    rather than vanishing. Pure calendar arithmetic — no database access.

    :param date_from: inclusive window start (a ``date``; a ``datetime`` is
        accepted and its time component ignored).
    :param date_to: inclusive window end, same types. An inverted window
        (``date_to < date_from``) yields an empty list.
    :param period: one of :data:`BOUNDARY_PERIODS`.
    :returns: a list of aware EAT datetimes, ascending, each the start of a
        bucket — the same instants :data:`PERIOD_TRUNC` / ``BUCKET_TRUNC``
        truncation produces, so they join to a grouped row by equality.
    :raises ValueError: if ``period`` is not a supported granularity.

    The first boundary is that of the bucket CONTAINING ``date_from``, which for
    a coarse period can fall before it — a window opening mid-week starts at the
    preceding Monday. That is the same partial-edge bucket the grouped query
    already produces when it has orders (see ``sales._period_label``), so the
    axis and the data agree; clipping it to the window instead would strand
    every order in the window's first days.

    Both callers filter on the local (EAT) calendar day, so the axis runs to the
    END of ``date_to``'s day rather than to its midnight.
    """
    if period not in BOUNDARY_PERIODS:
        raise ValueError(
            f"Unsupported period {period!r}; expected one of "
            f"{sorted(BOUNDARY_PERIODS)}"
        )

    start = _period_start(datetime.combine(date_from, time.min), period)
    window_end = datetime.combine(date_to, time.max)

    boundaries = []
    while start <= window_end:
        boundaries.append(start.replace(tzinfo=LOCAL_TZ))
        start = _next_period_start(start, period)
    return boundaries


def bucket_sales(order_qs, period):
    """Group a (sale-filtered) ``Order`` queryset into period buckets.

    Runs as ONE grouped query — never the legacy loop of re-running the
    summary once per period.

    :param order_qs: an ``Order`` queryset (typically from
        :func:`reports_app.controllers.common.sale_filters.sale_orders`).
    :param period: one of ``'day'``, ``'week'``, ``'month'``, ``'quarter'``,
        ``'year'``. Note there is no ``'hour'`` — see :data:`BOUNDARY_PERIODS`.
    :returns: a list of dicts ordered ascending by period, with one entry per
        period that had orders, each shaped::

            {
                'period': <aware datetime, start of the EAT period>,
                'count': <int>,
                'revenue': <Decimal | None>,
                'discount': <Decimal | None>,
            }

        Periods with no orders are absent — the caller zero-fills onto
        :func:`period_boundaries`, exactly as the hourly path fills onto
        ``range(24)``.

    Truncation runs in EAT (``tzinfo=LOCAL_TZ``) so buckets align to the local
    calendar; with ``tzinfo`` set, each returned ``period`` is an aware
    datetime in EAT — equal to the matching :func:`period_boundaries` entry, so
    the two join by equality.
    :raises ValueError: if ``period`` is not a supported granularity.

    This function takes NO window, deliberately: it groups whatever queryset it
    is handed, and a caller may hand it an unbounded one. The window belongs to
    the fill, not to the grouping.
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
        continuous 0–23 axis. Same policy as :func:`bucket_sales`; only the
        axis differs, this one being a fixed domain rather than a window.

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
