"""
Reviews analytics controllers (read-only, tenant-scoped).

Two pure functions over the ``Review`` model:
- ``review_summary``   -> the lean dashboard card, a fixed last-30-day window.
- ``review_analytics`` -> the windowed Overview report (dimensions, weakest
  dimension, critical queue, and a weekly/daily trend).

Both return the standard ``{'status', 'message', 'data'}`` dict. Every aggregate
degrades gracefully on ZERO reviews (the pilot launches near-empty): averages
become '0.0' (or null for a dimension nobody rated), counts 0, the trend an empty
list — no divide-by-zero, no None-arithmetic.

This is a ground-up replacement for the legacy reviews summary in ``reports_app``
(being retired in a separate rebuild); it does not touch ``reports_app`` or the
legacy inline-review fields on ``Order``/``OrderItem``.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count
from django.db.models.functions import TruncDay, TruncWeek
from django.utils import timezone

from misc_app.controllers.clean_dates import clean_dates
from reviews_app.controllers.submit_review import RATING_FIELDS
from reviews_app.models import PUBLIC_RATING_THRESHOLD, Review
from reviews_app.serializers import ReviewRestaurantReadSerializer

# Fixed look-back for the dashboard card; default look-back for the Overview
# report when the caller sends no date range.
SUMMARY_WINDOW_DAYS = 30
DEFAULT_ANALYTICS_WINDOW_DAYS = 90
# A dimension needs at least this many ratings before it can be named the
# "weakest" — below it the average is too noisy to act on (expected at low volume).
MIN_DIMENSION_COUNT = 3

# The five optional dimensions, i.e. the rating fields minus the mandatory
# overall_rating (one source of truth: reviews_app.controllers.submit_review).
DIMENSION_FIELDS = tuple(f for f in RATING_FIELDS if f != 'overall_rating')


def _avg_str(value, default='0.0'):
    """
    Render an aggregate average as a quantized 1-decimal string ('4.2').

    The None-check is FIRST on purpose: ``Decimal(str(None))`` -> ``Decimal('None')``
    raises ``InvalidOperation`` (a 500). ``default`` is '0.0' for the overall
    average and ``None`` for a per-dimension average nobody supplied.
    """
    if value is None:
        return default
    return str(Decimal(str(value)).quantize(Decimal('0.1')))


def _distribution(reviews):
    """5..1 star histogram over overall_rating, with absent levels filled to 0."""
    counts = {
        row['overall_rating']: row['count']
        for row in reviews.values('overall_rating').annotate(count=Count('id'))
    }
    return [{'stars': s, 'count': counts.get(s, 0)} for s in range(5, 0, -1)]


def _critical_counts(reviews):
    """
    (critical, unresolved_critical) over a queryset. Critical = is_critical =
    overall_rating < PUBLIC_RATING_THRESHOLD (the single definition); unresolved
    additionally means the service-recovery row is still 'open'.
    """
    critical = reviews.filter(overall_rating__lt=PUBLIC_RATING_THRESHOLD)
    return critical.count(), critical.filter(resolution_status='open').count()


def review_summary(restaurant_id):
    """Dashboard card — fixed last-SUMMARY_WINDOW_DAYS window."""
    cutoff = timezone.now() - timedelta(days=SUMMARY_WINDOW_DAYS)
    windowed = Review.objects.filter(
        restaurant=restaurant_id, created_at__gte=cutoff,
    )

    critical_count, unresolved_critical_count = _critical_counts(windowed)

    # recent_reviews is ALL-TIME (not the 30-day window): the newest three reviews,
    # with order context joined so the card can show table/spend without an N+1.
    recent_qs = (
        Review.objects.filter(restaurant=restaurant_id)
        .select_related('order', 'order__table')
        .order_by('-created_at')[:3]
    )

    return {
        'status': 200,
        'message': 'The review summary has been retrieved successfully.',
        'data': {
            'average_rating': _avg_str(
                windowed.aggregate(v=Avg('overall_rating'))['v']),
            'total_reviews': windowed.count(),
            'distribution': _distribution(windowed),
            'critical_count': critical_count,
            'unresolved_critical_count': unresolved_critical_count,
            'recent_reviews': ReviewRestaurantReadSerializer(
                recent_qs, many=True).data,
        },
    }


def review_analytics(restaurant_id, date_from, date_to, category):
    """Overview report — windowed, with per-dimension breakdown and a trend."""
    # Any value other than 'daily' collapses to the 'weekly' default; the echo
    # reflects the resolved bucket size.
    category = 'daily' if category == 'daily' else 'weekly'

    # Default the window BEFORE clean_dates: clean_dates does not coerce None and
    # would TypeError on ``date_to < None``. A missing ``from`` looks back
    # DEFAULT_ANALYTICS_WINDOW_DAYS; a missing ``to`` is today.
    today = timezone.now().date()
    if not date_from:
        date_from = today - timedelta(days=DEFAULT_ANALYTICS_WINDOW_DAYS)
    if not date_to:
        date_to = today

    cleaned = clean_dates(date_from, date_to)
    if cleaned['status'] != 200:
        return cleaned
    date_from, date_to = cleaned['date_from'], cleaned['date_to']

    # created_at__date__range is inclusive on both ends.
    reviews = Review.objects.filter(
        restaurant=restaurant_id,
        created_at__date__range=(date_from, date_to),
    )

    # One aggregate for all five dimensions: Count(field) counts non-null rows,
    # i.e. "reviews that rated that dimension" (distinct from Count('id')).
    agg = reviews.aggregate(
        **{f'{f}_avg': Avg(f) for f in DIMENSION_FIELDS},
        **{f'{f}_count': Count(f) for f in DIMENSION_FIELDS},
    )
    dimensions = {}
    weakest_key, weakest_avg = None, None
    for field in DIMENSION_FIELDS:
        key = field.removesuffix('_rating')  # 'food_rating' -> 'food'
        avg, count = agg[f'{field}_avg'], agg[f'{field}_count']
        # null average when nobody rated this dimension (a real "no data", not 0.0).
        dimensions[key] = {'average': _avg_str(avg, default=None), 'count': count}
        if (count >= MIN_DIMENSION_COUNT and avg is not None
                and (weakest_avg is None or avg < weakest_avg)):
            # strict < => on a tie the first dimension in field order wins.
            weakest_avg, weakest_key = avg, key
    weakest_dimension = (
        {'key': weakest_key, 'average': _avg_str(weakest_avg)}
        if weakest_key is not None else None
    )

    critical_count, unresolved_critical_count = _critical_counts(reviews)

    # Trend buckets ascending by period; empty list when there are no reviews.
    trunc = TruncDay if category == 'daily' else TruncWeek
    buckets = (
        reviews.annotate(period=trunc('created_at'))
        .values('period')
        .annotate(avg=Avg('overall_rating'), count=Count('id'))
        .order_by('period')
    )
    trend = [
        {
            'period': row['period'].date().isoformat(),  # period is a datetime
            'average': _avg_str(row['avg']),
            'count': row['count'],
        }
        for row in buckets if row['period'] is not None
    ]

    return {
        'status': 200,
        'message': 'The review analytics have been retrieved successfully.',
        'data': {
            'period': {
                'from': date_from.isoformat(),
                'to': date_to.isoformat(),
                'category': category,
            },
            'total_reviews': reviews.count(),
            'average_rating': _avg_str(
                reviews.aggregate(v=Avg('overall_rating'))['v']),
            'distribution': _distribution(reviews),
            'dimensions': dimensions,
            'weakest_dimension': weakest_dimension,
            'critical_count': critical_count,
            'unresolved_critical_count': unresolved_critical_count,
            'trend': trend,
        },
    }
