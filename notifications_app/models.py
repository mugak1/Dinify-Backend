"""
The in-app notifications store (SHELL-FINISH-00, PR 4).

TWO STORES, AND THEY MUST NOT BE CONFUSED. The MongoDB ``notifications``
collection is the outbound email/SMS queue (``send_messages`` reads it) and the
source of the legacy ``api/v1/notifications/`` endpoint; it is unreachable from
the live host, so nothing written there ever reaches a screen. It is left exactly
as it was. THIS store is PostgreSQL, holds operational events for a restaurant's
staff (a low review, a dish sold out from the kitchen board, ordering paused or
resumed), and is read by the portal's bell through ``api/v1/notifications/inbox/``.

A ROW IS ADDRESSED TO A RESTAURANT AND A MODULE, NEVER TO A PERSON. Who may see it
is decided at READ time by the same module resolver every other portal surface
uses (``can_user_access_module``), so a Roles & Access change applies to every
existing row at once and no row is ever rewritten to follow a permission change.
The actor of an event is excluded from seeing it.

READ STATE IS PER USER AND IS A TIMESTAMP, NOT A FLAG. A notification is UNREAD
for a user when they hold no ``NotificationRead`` with ``read_at`` at or after the
notification's ``occurred_at``. A RE-ALERT moves ``occurred_at`` forward, which
makes an earlier read stale without deleting it; a bump that does not re-alert
changes the payload only and leaves ``occurred_at`` (and therefore the read state)
alone. Nothing ever deletes a read record except the cascade from its notification.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from dinify_backend.configss.string_definitions import MODULE_REVIEWS, MODULE_TABLES


#: The modules a notification may be addressed to. Owner decision (29 October):
#: a low review is a `reviews` event; a dish sold out from the kitchen board and
#: ordering paused or resumed are `tables` events, because the floor needs both
#: mid-service and front-of-house staff hold `tables` by default.
NOTIFICATION_MODULES = (MODULE_REVIEWS, MODULE_TABLES)


class NotificationKind(models.TextChoices):
    REVIEW_LOW = 'review_low', 'Low review'
    ITEM_SOLD_OUT = 'item_sold_out', 'Item sold out'
    ORDERING_PAUSED = 'ordering_paused', 'Ordering paused'
    ORDERING_RESUMED = 'ordering_resumed', 'Ordering resumed'


class RestaurantNotification(models.Model):
    """One operational event for one restaurant, visible by module."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(
        'restaurants_app.Restaurant',
        on_delete=models.CASCADE,
        related_name='+',
    )
    module = models.CharField(max_length=32)
    kind = models.CharField(max_length=40, choices=NotificationKind.choices)
    # Server-built and bounded per kind (see `producers.py`). Never credentials,
    # OTPs, free text a diner wrote, or diner contact details.
    payload = models.JSONField(default=dict, blank=True)
    # Who caused it, so they are not told about their own action. NULL for an
    # event no staff member caused (a diner's review). SET_NULL: the row outlives
    # the account.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    # The repeat rule's key: one row per (restaurant, dedupe_key). A repeat of the
    # same event updates that row instead of adding another.
    dedupe_key = models.CharField(max_length=120)
    # When this notification last ALERTED. Moved forward only by a re-alert.
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'restaurant_notifications'
        constraints = [
            models.UniqueConstraint(
                fields=['restaurant', 'dedupe_key'],
                name='restaurant_notification_dedupe_unique',
            ),
            models.CheckConstraint(
                condition=Q(module__in=NOTIFICATION_MODULES),
                name='restaurant_notification_module_vocabulary',
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in NotificationKind]),
                name='restaurant_notification_kind_vocabulary',
            ),
        ]
        indexes = [
            models.Index(
                fields=['restaurant', '-occurred_at'],
                name='rest_notif_rest_occurred_idx',
            ),
        ]

    def __str__(self):
        return f'{self.kind} @ {self.restaurant_id} ({self.occurred_at:%Y-%m-%d %H:%M})'


class NotificationRead(models.Model):
    """When ``user`` last read ``notification``. Compared with ``occurred_at``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        RestaurantNotification,
        on_delete=models.CASCADE,
        related_name='reads',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='+',
    )
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'restaurant_notification_reads'
        constraints = [
            models.UniqueConstraint(
                fields=['notification', 'user'],
                name='restaurant_notification_read_unique',
            ),
        ]
