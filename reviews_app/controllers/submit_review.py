"""
Diner review submission controller (the AllowAny path).

Returns the standard ``{'status', 'message', 'data'}`` dict. The order is
pre-checked (existence / cancelled / already-reviewed) BEFORE serializer
validation, so the diner gets a precise, friendly message and the right status
code instead of a raw DRF error.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from orders_app.models import Order
from reports_app.controllers.common.sale_filters import SALE_STATUSES
from reviews_app.serializers import (
    ReviewWriteSerializer,
    ReviewRestaurantReadSerializer,
)

logger = logging.getLogger(__name__)

# The rating fields a diner may supply: overall_rating is mandatory, the rest are
# optional dimensions. Defined here so the endpoint and controller agree on the
# accepted keys.
RATING_FIELDS = (
    'overall_rating', 'food_rating', 'speed_rating',
    'service_rating', 'value_rating', 'cleanliness_rating',
)


def _shape_errors(errors):
    """Flatten DRF ``serializer.errors`` into one friendly sentence."""
    parts = []
    for field, messages in errors.items():
        if isinstance(messages, (list, tuple)):
            text = ' '.join(str(message) for message in messages)
        else:
            text = str(messages)
        label = 'review' if field == 'non_field_errors' else field
        parts.append(f'{label}: {text}')
    return ' '.join(parts) or 'The review could not be validated.'


def submit_review(order_id, rating_fields, comment=None, tags=None,
                  session_restaurant_id=None, session_table_id=None):
    """
    Submit a diner review for an order.

    ``order_id``      : the Order PK (a UUID) being reviewed.
    ``rating_fields`` : dict of overall_rating + optional dimension ratings.
    ``comment``       : optional free text.
    ``tags``          : optional list of quick-chip tag keys (validated against
                        the ``ReviewTag`` allowed set by the write serializer).
    ``session_restaurant_id`` / ``session_table_id`` : the diner table session the
                        review is authorised by. The order MUST belong to that
                        restaurant+table — order-UUID knowledge alone is not
                        authority (PR 7A). The endpoint resolves the session and
                        passes these; a call without them fails closed (matches no
                        order → 404).
    """
    # 1. Order existence, SCOPED to the caller's table session (404). A foreign /
    #    unknown / malformed id all collapse to ONE non-disclosing not-found so a
    #    junk ``order`` value can never surface as a 500 and cross-table/tenant
    #    order UUIDs cannot be reviewed.
    if order_id is None:
        return {'status': 404, 'message': 'We could not find that order.'}
    try:
        order = Order.objects.get(
            id=order_id,
            restaurant_id=session_restaurant_id,
            table_id=session_table_id,
        )
    except (Order.DoesNotExist, ValidationError, ValueError):
        return {'status': 404, 'message': 'We could not find that order.'}

    # 2. Only a completed service may be reviewed (400). SALE_STATUSES
    #    ({served, paid}) is the canonical "completed service" predicate
    #    (reports_app.controllers.common.sale_filters). Every other state —
    #    initiated / pending / preparing (in-flight and reversible) and
    #    cancelled / refunded (reversed) — is rejected with ONE restrained
    #    message that deliberately does not disclose which lifecycle state the
    #    order is in to an unauthorised caller.
    if order.order_status not in SALE_STATUSES:
        return {
            'status': 400,
            'message': 'This order is not eligible for review.',
        }

    # 3. Already reviewed (409). The reverse OneToOne raises
    #    RelatedObjectDoesNotExist when absent, so probe with hasattr. This
    #    friendly pre-check intentionally precedes the write serializer's
    #    uniqueness validator (kept as the race-condition backstop) so the common
    #    duplicate case returns a clean 409 rather than the validator's 400.
    if hasattr(order, 'review_record'):
        return {
            'status': 409,
            'message': 'This order has already been reviewed.',
        }

    # 4. Validate + create. ``order`` is SERVER-DERIVED: it was resolved above
    #    scoped to the diner's table session (knowing the order UUID is not
    #    authority) and is passed via save(order=...); it is read_only on the
    #    serializer so it can never be spoofed from the request body. Drop unset
    #    ratings so a missing overall_rating surfaces the serializer's 400.
    payload = {}
    for field in RATING_FIELDS:
        value = rating_fields.get(field)
        if value is not None:
            payload[field] = value
    if comment is not None:
        payload['comment'] = comment
    # Forward tags only when supplied; absent leaves the model's [] default.
    # The serializer's validate_tags constrains them to the allowed set.
    if tags is not None:
        payload['tags'] = tags

    serializer = ReviewWriteSerializer(data=payload)
    if not serializer.is_valid():
        logger.info('Review submission rejected: %s', serializer.errors)
        return {
            'status': 400,
            'message': _shape_errors(serializer.errors),
            'data': serializer.errors,
        }

    # save() denormalises restaurant + seeds is_public; submission_channel keeps
    # its 'in_app' model default. The Review.order OneToOne DB constraint is the
    # atomic one-per-order backstop: if a concurrent same-order submission races
    # past the hasattr / UniqueValidator pre-checks, the second INSERT raises
    # IntegrityError, which we translate into the same clean 409 as the common
    # duplicate path (never a 500). The atomic() block keeps that failed INSERT
    # from poisoning any surrounding transaction.
    try:
        with transaction.atomic():
            review = serializer.save(order=order)
    except IntegrityError:
        return {
            'status': 409,
            'message': 'This order has already been reviewed.',
        }
    return {
        'status': 201,
        'message': 'Thank you! Your review has been submitted.',
        'data': ReviewRestaurantReadSerializer(review).data,
    }
