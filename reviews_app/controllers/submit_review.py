"""
Diner review submission controller (the AllowAny path).

Returns the standard ``{'status', 'message', 'data'}`` dict. The order is
pre-checked (existence / cancelled / already-reviewed) BEFORE serializer
validation, so the diner gets a precise, friendly message and the right status
code instead of a raw DRF error.
"""
import logging

from django.core.exceptions import ValidationError

from orders_app.models import Order
from dinify_backend.configss.string_definitions import OrderStatus_Cancelled
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


def submit_review(order_id, rating_fields, comment=None):
    """
    Submit a diner review for an order.

    ``order_id``      : the Order PK (a UUID) being reviewed.
    ``rating_fields`` : dict of overall_rating + optional dimension ratings.
    ``comment``       : optional free text.
    """
    # 1. Order existence (404). The Order PK is a UUID — a malformed id raises
    #    ValueError/ValidationError on the lookup; treat that as not-found so a
    #    junk ``order`` value can never surface as a 500.
    if order_id is None:
        return {'status': 404, 'message': 'We could not find that order.'}
    try:
        order = Order.objects.get(id=order_id)
    except (Order.DoesNotExist, ValidationError, ValueError):
        return {'status': 404, 'message': 'We could not find that order.'}

    # 2. Cancelled orders cannot be reviewed (400).
    if order.order_status == OrderStatus_Cancelled:
        return {
            'status': 400,
            'message': 'This order was cancelled and cannot be reviewed.',
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

    # 4. Validate + create. Build the payload explicitly; ``order`` must be the
    #    PK the OneToOne resolves (the UUID string). Drop unset ratings so a
    #    missing overall_rating surfaces the serializer's "required" 400.
    payload = {'order': str(order.id)}
    for field in RATING_FIELDS:
        value = rating_fields.get(field)
        if value is not None:
            payload[field] = value
    if comment is not None:
        payload['comment'] = comment

    serializer = ReviewWriteSerializer(data=payload)
    if not serializer.is_valid():
        logger.info('Review submission rejected: %s', serializer.errors)
        return {
            'status': 400,
            'message': _shape_errors(serializer.errors),
            'data': serializer.errors,
        }

    # save() denormalises restaurant + seeds is_public; submission_channel keeps
    # its 'in_app' model default.
    review = serializer.save()
    return {
        'status': 201,
        'message': 'Thank you! Your review has been submitted.',
        'data': ReviewRestaurantReadSerializer(review).data,
    }
