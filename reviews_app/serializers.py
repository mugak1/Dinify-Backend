"""
Serializers for reviews_app.

Two explicit-field serializers (never ``__all__`` / ``exclude``):
- ``ReviewWriteSerializer``           -> diner submission (create only); exposes
  ONLY the diner-supplied fields.
- ``ReviewRestaurantReadSerializer``  -> owner-facing reads, carrying the model's
  ``is_critical`` flag and null-safe joined order context.

Every order-context helper is NULL-SAFE: a saved review always has an order, but
the joined columns (``order_number``, ``served_at``, ...) are themselves nullable
and we never assume the relation resolves.
"""
import logging

from rest_framework import serializers

from reviews_app.models import Review, ReviewTag

logger = logging.getLogger(__name__)


def _table_label(order):
    """
    Human-facing table label for an order, or ``None``.

    ``Table.display_name`` and ``str_number`` both default to '' (blank), so
    prefer a non-empty ``display_name`` and otherwise fall back to the always-
    present integer ``number``. Null-safe on a missing order/table.
    """
    if order is None:
        return None
    table = getattr(order, 'table', None)
    if table is None:
        return None
    if table.display_name:
        return table.display_name
    if table.number is not None:
        return f'Table {table.number}'
    return None


class ReviewWriteSerializer(serializers.ModelSerializer):
    """
    Diner-facing write serializer (create only).

    Exposes only the diner-supplied fields. ``restaurant`` and ``is_public`` are
    set in ``Review.save()``; ``submission_channel`` keeps its 'in_app' default;
    ``resolution_status`` / ``themes`` / ``invite_sent_at`` / ``reminder_sent_at``
    are server-owned — none are writable here. DRF auto-attaches the model's 1-5
    validators on the rating fields and a uniqueness validator on the OneToOne
    ``order`` (the controller also does a friendly pre-check). ``tags`` is the one
    additive diner field beyond the ratings/comment; ``validate_tags`` filters it
    to the ``ReviewTag`` allowed set without ever blocking the submission.
    """

    class Meta:
        model = Review
        fields = [
            'order', 'overall_rating', 'food_rating', 'speed_rating',
            'service_rating', 'value_rating', 'cleanliness_rating', 'comment',
            'tags',
        ]

    def validate_tags(self, value):
        """
        Filter submitted quick-chip tags down to the ``ReviewTag`` allowed set.

        Tags must never block a submission: the stars and comment always save.
        So unknown keys are DROPPED (not rejected) and only the valid subset is
        stored — an arbitrary string can never persist. Order is preserved and
        duplicates collapsed. Empty/absent is allowed (returns ``[]``).

        The one 400 here is a malformed payload — ``tags`` that is not a list.
        Dropped keys are logged via standard application logging (NOT the
        MongoDB action-log pipeline, which is unreachable from EC2) so frontend
        bugs or stale-client version skew stay visible.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                'Tags must be a list of tag keys.'
            )
        allowed = set(ReviewTag.values)
        cleaned = []
        dropped = []
        for tag in value:
            if tag not in allowed:
                dropped.append(tag)
                continue
            if tag not in cleaned:
                cleaned.append(tag)
        if dropped:
            logger.warning(
                'Dropped %d unknown review tag(s): %s. Allowed tags: %s.',
                len(dropped), dropped, list(ReviewTag.values),
            )
        return cleaned


class ReviewRestaurantReadSerializer(serializers.ModelSerializer):
    """
    Owner-facing read serializer. Explicit allowlist (not ``exclude``) so a
    future model field is never silently exposed. Adds ``is_critical`` (the model
    property) and null-safe joined order context.
    """

    is_critical = serializers.SerializerMethodField()
    order_id = serializers.SerializerMethodField()
    order_number = serializers.SerializerMethodField()
    table_label = serializers.SerializerMethodField()
    served_at = serializers.SerializerMethodField()
    spend = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            'id', 'overall_rating', 'food_rating', 'speed_rating',
            'service_rating', 'value_rating', 'cleanliness_rating', 'comment',
            'tags',
            'is_public', 'resolution_status', 'resolution_note',
            'submission_channel',
            'created_at', 'updated_at',
            'is_critical', 'order_id', 'order_number', 'table_label',
            'served_at', 'spend',
        ]

    def get_is_critical(self, obj):
        return obj.is_critical

    def get_order_id(self, obj):
        # obj.order_id is the FK column (a UUID); stringify for JSON.
        return str(obj.order_id) if obj.order_id else None

    def get_order_number(self, obj):
        order = getattr(obj, 'order', None)
        return order.order_number if order else None

    def get_table_label(self, obj):
        return _table_label(getattr(obj, 'order', None))

    def get_served_at(self, obj):
        order = getattr(obj, 'order', None)
        return order.served_at if order else None

    def get_spend(self, obj):
        order = getattr(obj, 'order', None)
        if order is None or order.actual_cost is None:
            return None
        # actual_cost is a Decimal; render as a string to keep full precision and
        # avoid float coercion (money-as-not-float).
        return str(order.actual_cost)
