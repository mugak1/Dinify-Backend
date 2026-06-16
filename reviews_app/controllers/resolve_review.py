"""
Owner-facing review resolution controller.

Marks a review handled by toggling ``Review.resolution_status`` between 'open'
and 'resolved'. A controlled state transition (like the order-status writes), so
a direct update — NOT a Secretary generic-edit — which keeps it off the
edit_information gate. Returns the standard ``{'status', 'message', 'data'}`` dict.
"""
import logging

from django.core.exceptions import ValidationError

from reviews_app.models import Review
from reviews_app.serializers import ReviewRestaurantReadSerializer
from users_app.controllers.permissions_check import can_manage_restaurant

logger = logging.getLogger(__name__)

# The only resolution states a caller may set. Mirrors the model's choices on
# ``Review.resolution_status``.
VALID_RESOLUTION_STATUSES = ('open', 'resolved')


def resolve_review(user, review_id, target_status):
    """
    Toggle a review's resolution_status to ``target_status``.

    ``user``          : the requesting user (owner/manager or dinify admin).
    ``review_id``     : the Review PK (an integer).
    ``target_status`` : 'open' or 'resolved'.
    """
    # 1. Validate the target first — pure input validation, no DB hit, and it
    #    leaks nothing about whether the review exists.
    if target_status not in VALID_RESOLUTION_STATUSES:
        return {
            'status': 400,
            'message': "resolution_status must be 'open' or 'resolved'.",
        }

    # 2. Review existence (404). The PK is an integer; a malformed id raises
    #    ValueError/ValidationError on the lookup — treat that as not-found so a
    #    junk id can never surface as a 500.
    if review_id is None:
        return {'status': 404, 'message': 'We could not find that review.'}
    try:
        review = Review.objects.get(pk=review_id)
    except (Review.DoesNotExist, ValidationError, ValueError):
        return {'status': 404, 'message': 'We could not find that review.'}

    # 3. Tenant write-authorization (403). Uses the denormalised restaurant_id —
    #    no extra FK fetch.
    if not can_manage_restaurant(user, review.restaurant_id):
        return {
            'status': 403,
            'message': 'You do not have permission to update this review.',
        }

    # 4. Apply the transition. updated_at is auto_now, so it must be listed in
    #    update_fields for Django to refresh it.
    review.resolution_status = target_status
    review.save(update_fields=['resolution_status', 'updated_at'])
    return {
        'status': 200,
        'message': f'The review has been marked {target_status}.',
        'data': ReviewRestaurantReadSerializer(review).data,
    }
