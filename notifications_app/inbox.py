"""
Reading the in-app notifications store (SHELL-FINISH-00, PR 4).

ONE VISIBILITY RULE, used by every inbox operation (list, count, mark one, mark
all), so no operation can see a row another one hides. For a user at ONE
restaurant (owner decision: the bell covers the selected restaurant only):

  * the user must be employed there (active, non-deleted membership), or the
    restaurant is answered as though it did not exist;
  * a row is visible only if its module passes ``can_user_access_module`` for
    that user at that restaurant, asked AT READ TIME, so a Roles & Access change
    applies to every existing row immediately;
  * a row the user caused (``actor``) is never shown to them;
  * only rows whose last alert is inside ``READ_WINDOW``.

A delegated administrator never reaches here: these routes are not on
``ALLOWED_ROUTES``, so the delegated middleware refuses them before dispatch.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from notifications_app.models import (
    NOTIFICATION_MODULES, NotificationRead, RestaurantNotification,
)
from notifications_app.producers import READ_WINDOW
from users_app.controllers.permissions_check import (
    can_user_access_module, get_employed_restaurant_ids,
)

#: The list is bounded: the newest first, at most this many.
LIST_LIMIT = 50


class NotFound(Exception):
    """The restaurant, or the notification, is not one this user may see."""


def parse_uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def visible_notifications(user, restaurant_id):
    """
    The queryset of notifications ``user`` may see at ``restaurant_id``.

    Raises ``NotFound`` when the user is not employed there, so a foreign or
    unknown restaurant gets the same answer as a malformed one.
    """
    if restaurant_id is None or restaurant_id not in get_employed_restaurant_ids(user):
        raise NotFound()
    modules = [m for m in NOTIFICATION_MODULES if can_user_access_module(user, restaurant_id, m)]
    return (
        RestaurantNotification.objects
        .filter(
            restaurant_id=restaurant_id,
            module__in=modules,
            occurred_at__gte=timezone.now() - READ_WINDOW,
        )
        .filter(Q(actor__isnull=True) | ~Q(actor_id=user.pk))
    )


def _read_since(user):
    """Exists: this user read the row at or after its last alert."""
    return Exists(NotificationRead.objects.filter(
        notification=OuterRef('pk'),
        user=user,
        read_at__gte=OuterRef('occurred_at'),
    ))


def list_inbox(user, restaurant_id) -> dict:
    qs = visible_notifications(user, restaurant_id)
    rows = list(
        qs.annotate(is_read=_read_since(user))
        .select_related('actor')
        .order_by('-occurred_at', '-pk')[:LIST_LIMIT]
    )
    return {
        'results': [_project(row) for row in rows],
        'unread_count': unread_count(user, restaurant_id, qs=qs),
    }


def unread_count(user, restaurant_id, *, qs=None) -> int:
    qs = qs if qs is not None else visible_notifications(user, restaurant_id)
    return qs.filter(~_read_since(user)).count()


def mark_read(user, notification_id) -> None:
    """Mark ONE notification read. Anything the user may not see is ``NotFound``."""
    nid = parse_uuid(notification_id)
    if nid is None:
        raise NotFound()
    restaurant_id = (RestaurantNotification.objects
                     .filter(pk=nid).values_list('restaurant_id', flat=True).first())
    if restaurant_id is None:
        raise NotFound()
    restaurant_id = str(restaurant_id)
    if not visible_notifications(user, restaurant_id).filter(pk=nid).exists():
        raise NotFound()
    _upsert_reads(user, [nid])


def mark_all_read(user, restaurant_id) -> int:
    """Mark every visible, unread notification at ``restaurant_id`` read."""
    qs = visible_notifications(user, restaurant_id)
    ids = list(qs.filter(~_read_since(user)).values_list('pk', flat=True))
    _upsert_reads(user, ids)
    return len(ids)


def _upsert_reads(user, notification_ids) -> None:
    """One read record per (notification, user); a later read moves ``read_at``."""
    if not notification_ids:
        return
    now = timezone.now()
    with transaction.atomic():
        NotificationRead.objects.bulk_create(
            [NotificationRead(notification_id=nid, user=user, read_at=now)
             for nid in notification_ids],
            update_conflicts=True,
            unique_fields=['notification', 'user'],
            update_fields=['read_at'],
        )


def _project(row) -> dict:
    """The wire shape. Explicit: no model field reaches the client by accident."""
    return {
        'id': str(row.pk),
        'kind': row.kind,
        'module': row.module,
        'occurred_at': row.occurred_at.isoformat(),
        'read': bool(row.is_read),
        'payload': dict(row.payload or {}),
        'actor_name': _actor_name(row.actor),
    }


def _actor_name(actor) -> str | None:
    """A first name, or "Dinify support" for a delegated administrator."""
    if actor is None:
        return None
    if getattr(actor, 'account_type', None) == ACCOUNT_TYPE_PLATFORM_STAFF:
        return 'Dinify support'
    return (actor.first_name or '').strip() or None
