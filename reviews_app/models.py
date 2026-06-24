"""
Models for the reviews_app.

`Review` is the visit-level review — one per `Order` — that becomes the
order-join behind the reviews analytics built in later phases. It is the
system of record for order reviews, having fully replaced the legacy inline
review fields that previously lived on `Order`/`OrderItem`; those columns were
dropped in the teardown PR.
"""
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


# Rating band that splits public-eligible from private-only reviews. A
# module-level constant so later phases (public banding, the low-rating
# service-recovery queue) share one source of truth: high -> public-eligible,
# low -> private-only.
PUBLIC_RATING_THRESHOLD = 4


def _rating_validators():
    """Shared 1-5 bound for every star field."""
    return [MinValueValidator(1), MaxValueValidator(5)]


class ReviewTag(models.TextChoices):
    """
    The fixed set of diner review quick-chip tags.

    Stored as STABLE KEYS (never display labels) so the frontend owns
    presentation and Reports can aggregate on a stable axis. This enum is the
    single source of truth for the allowed set — ``Review.tags`` holds a JSON
    list of these ``.value`` strings, and the write serializer validates every
    submitted tag against ``ReviewTag.values``. Add a new chip ONLY by adding a
    member here.
    """
    GREAT_FLAVOUR = 'great_flavour', 'Great flavour'
    QUICK_SERVICE = 'quick_service', 'Quick service'
    FRIENDLY_STAFF = 'friendly_staff', 'Friendly staff'
    GOOD_VALUE = 'good_value', 'Good value'
    SPOTLESS = 'spotless', 'Spotless'


class Review(models.Model):
    """
    A single diner's review of one order/visit.
    """

    # === relationships ===
    # One review per order. related_name='review_record' (NOT 'review') so it
    # does not collide with the legacy inline ``Order.review`` TextField that
    # still exists during the build-new-then-retire window.
    order = models.OneToOneField(
        'orders_app.Order',
        on_delete=models.CASCADE,
        related_name='review_record',
    )
    # Denormalised from order.restaurant (set in save() on creation) so
    # dashboard/list queries are tenant-scoped without joining through Order.
    restaurant = models.ForeignKey(
        'restaurants_app.Restaurant',
        on_delete=models.CASCADE,
        related_name='reviews',
    )

    # === ratings (1-5) ===
    # overall_rating is the one mandatory star; the five dimensions are optional.
    overall_rating = models.IntegerField(validators=_rating_validators())
    food_rating = models.IntegerField(
        null=True, blank=True, validators=_rating_validators())
    speed_rating = models.IntegerField(
        null=True, blank=True, validators=_rating_validators())
    service_rating = models.IntegerField(
        null=True, blank=True, validators=_rating_validators())
    value_rating = models.IntegerField(
        null=True, blank=True, validators=_rating_validators())
    cleanliness_rating = models.IntegerField(
        null=True, blank=True, validators=_rating_validators())

    # === content ===
    comment = models.TextField(null=True, blank=True)
    # Diner quick-chip tags: a JSON list of stable ``ReviewTag`` keys (never
    # display labels). JSONField (not ArrayField) to match the codebase's small-
    # string-list convention. Empty by default; the write serializer filters
    # every element against ``ReviewTag.values`` (unknown keys are dropped, not
    # stored) so an arbitrary string can never persist.
    tags = models.JSONField(default=list, blank=True)

    # === public visibility (per-review, overridable) ===
    # Seeded from the rating band on creation (see save()); stays overridable so
    # an owner can later hide or surface an individual review.
    is_public = models.BooleanField(default=True)

    # === service-recovery ===
    # CharField-with-choices (not a bool) so richer states can be added later
    # without a schema change.
    resolution_status = models.CharField(
        max_length=20,
        choices=[('open', 'Open'), ('resolved', 'Resolved')],
        default='open',
    )
    # Optional free-text record of the corrective action taken when an owner
    # marks a review resolved. Independent of resolution_status — it persists
    # across reopen/re-resolve (a reopen never wipes it; re-resolving with a new
    # note updates it).
    resolution_note = models.TextField(null=True, blank=True)

    # === forward-looking (dormant; defined now to avoid a re-migration when
    # later phases land — no behaviour is attached to them yet) ===
    themes = models.JSONField(null=True, blank=True, default=None)
    submission_channel = models.CharField(
        max_length=20,
        choices=[('in_app', 'In-app'), ('sms', 'SMS'), ('email', 'Email')],
        default='in_app',
    )
    invite_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    # === timestamps ===
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'reviews'
        ordering = ['-created_at']
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'
        indexes = [
            models.Index(
                fields=['restaurant', 'created_at'],
                name='reviews_rest_created_idx',
            ),
            models.Index(
                fields=['restaurant', 'overall_rating'],
                name='reviews_rest_overall_idx',
            ),
        ]

    def __str__(self):
        return f'{self.overall_rating}-star review (order {self.order_id})'

    @property
    def is_critical(self):
        """
        Low-rating flag for the service-recovery queue (filter added later).
        """
        return (
            self.overall_rating is not None
            and self.overall_rating < PUBLIC_RATING_THRESHOLD
        )

    def save(self, *args, **kwargs):
        # On creation only: (a) denormalise restaurant from the order, and
        # (b) seed public visibility from the rating band. is_public stays
        # overridable on later updates (an owner can hide/surface a review), so
        # it is never re-derived once the row exists. Using the *_id attributes
        # avoids needless FK fetches.
        if self._state.adding:
            if self.restaurant_id is None and self.order_id is not None:
                self.restaurant = self.order.restaurant
            if self.overall_rating is not None:
                self.is_public = self.overall_rating >= PUBLIC_RATING_THRESHOLD
        super().save(*args, **kwargs)
