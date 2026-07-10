"""
Models for the support_app.

`SupportIssue` is the restaurant-facing support/ticketing record. The reporter
is `created_by` (inherited from `BaseModel`) — there is no separate reporter
field.
"""
from django.db import models, transaction, IntegrityError
from django.utils import timezone

from users_app.models import BaseModel, User
from restaurants_app.models import Restaurant


# Human-readable reference: zero-padded sequential, e.g. "SUP-000123".
REFERENCE_PREFIX = 'SUP-'
REFERENCE_PADDING = 6
MAX_REFERENCE_ATTEMPTS = 5


class SupportIssue(BaseModel):
    """
    A support issue raised by a restaurant and triaged by Dinify staff.
    """

    class Category(models.TextChoices):
        ORDERS_KDS = 'orders_kds', 'Orders / KDS'
        MENU = 'menu', 'Menu'
        TABLES_QR = 'tables_qr', 'Tables / QR'
        PAYMENTS = 'payments', 'Payments'
        REPORTS = 'reports', 'Reports'
        ACCOUNT = 'account', 'Account'
        BUG = 'bug', 'Bug'
        OTHER = 'other', 'Other'

    class Impact(models.TextChoices):
        BLOCKING_SERVICE = 'blocking_service', 'Blocking service'
        AFFECTING_SERVICE = 'affecting_service', 'Affecting service'
        NON_URGENT = 'non_urgent', 'Non-urgent'
        QUESTION = 'question', 'Question'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In progress'
        RESOLVED = 'resolved', 'Resolved'
        CLOSED = 'closed', 'Closed'

    # Human-readable, collision-safe sequential reference generated in save().
    reference = models.CharField(max_length=20, unique=True, db_index=True)

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name='support_issues',
    )

    category = models.CharField(max_length=50, choices=Category.choices)
    impact = models.CharField(max_length=50, choices=Impact.choices)
    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.OPEN,
    )

    title = models.CharField(max_length=255)
    description = models.TextField()

    # Reporter contact details (frontend-supplied).
    contact_phone = models.CharField(max_length=50, blank=True)
    contact_email = models.EmailField(blank=True)
    preferred_contact_method = models.CharField(max_length=30, blank=True)

    # Context auto-captured by the frontend.
    page_url = models.TextField(blank=True)
    user_agent = models.TextField(blank=True)

    # Triage / resolution (Dinify-staff controlled).
    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_support_issues',
    )
    # Dinify-staff only — NEVER serialized to a restaurant.
    internal_notes = models.TextField(blank=True)
    # Restaurant-visible summary of how the issue was resolved.
    resolution_summary = models.TextField(blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'support_issues'
        ordering = ['-time_created']

    def __str__(self):
        return f'{self.reference} ({self.status})'

    def _next_reference(self) -> str:
        """
        Compute the next sequential reference from the current maximum.

        Lexicographic max equals numeric max because references are
        zero-padded to a fixed width, so an indexed ``ORDER BY reference DESC``
        is both correct and cheap.
        """
        latest = (
            type(self).objects
            .filter(reference__startswith=REFERENCE_PREFIX)
            .order_by('-reference')
            .values_list('reference', flat=True)
            .first()
        )
        next_num = (int(latest[len(REFERENCE_PREFIX):]) + 1) if latest else 1
        return f'{REFERENCE_PREFIX}{next_num:0{REFERENCE_PADDING}d}'

    def save(self, *args, **kwargs):
        # Status -> timestamp. Idempotent (the ``is None`` guards stop
        # re-stamping) and runs on every path before the row is written, so it
        # fires on the admin PUT (status is in EDIT_INFORMATION) regardless of
        # the persistence substrate.
        if self.status == self.Status.RESOLVED and self.resolved_at is None:
            self.resolved_at = timezone.now()
        if self.status == self.Status.CLOSED and self.closed_at is None:
            self.closed_at = timezone.now()

        # Update path (admin PUT, soft-delete, ...) or reference already set:
        # plain save, never loop, never force_insert.
        if self.reference or not self._state.adding:
            return super().save(*args, **kwargs)

        # Insert path: generate a collision-safe reference with retry. Each
        # attempt is its own savepoint so an IntegrityError can be recovered
        # WITHOUT poisoning the outer transaction Secretary.create() opens.
        # force_insert is required because the UUID pk is assigned at instance
        # construction — a bare retry would attempt an UPDATE, not an INSERT.
        kwargs.pop('force_insert', None)
        last_error = None
        for _ in range(MAX_REFERENCE_ATTEMPTS):
            self.reference = self._next_reference()
            try:
                with transaction.atomic():
                    return super().save(force_insert=True, **kwargs)
            except IntegrityError as error:
                last_error = error
                self.reference = ''
        raise last_error
