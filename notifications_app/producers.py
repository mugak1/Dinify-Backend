"""
Producers of in-app notifications (SHELL-FINISH-00, PR 4).

Three events have a source today, and each producer is called from exactly one
place:

  * a review below ``PUBLIC_RATING_THRESHOLD``
        reviews_app/controllers/submit_review.py, after the review is saved;
  * a dish sold out from the kitchen board (``in_stock`` True -> False)
        orders_app/endpoints_kitchen.py, ``KitchenMenuItemStockView``;
  * ordering paused or resumed (``accepting_orders`` changed)
        restaurants_app/endpoints/restaurant_setup.py, the restaurants PUT.

A support-issue status change was planned and DROPPED: nothing writes an issue's
status after it is created. The menu-setup PUT also writes ``in_stock`` and is
deliberately NOT a source (owner decision): it still sends the value captured when
the editor opened (audit item 7), so a transition seen there can be that bug.

EVERY PRODUCER RUNS AFTER COMMIT AND IS BEST-EFFORT. The call site registers it
with ``transaction.on_commit(..., robust=True)`` from inside its own transaction,
so a notification can never describe a change that rolled back, and the write is
attempted only once the triggering change is durable. Any failure here is logged
and swallowed: it never changes the triggering request's response. This is the
first ``on_commit`` in the repository; ``schedule`` is the one way to register.

THE REPEAT RULES:
  * review: one row per review (``review:<id>``), written once;
  * sold out: one row per dish (``sold_out:<item id>``). A repeat RE-ALERTS
    (moves ``occurred_at``, records the new actor) only when the last alert is
    older than ``SOLD_OUT_REALERT_AFTER``; otherwise it updates the payload only,
    so a dish toggled back and forth does not keep ringing the bell;
  * ordering: one row per restaurant (``ordering``). EVERY actual change of
    ``accepting_orders`` re-alerts. A save that leaves it unchanged never reaches
    this module (the call site compares before and after).

PAYLOADS are built here, server-side, and carry no credentials, OTPs, diner
contact details or diner free text: a review's comment is NOT copied, because it
is written by a diner and may hold a phone number.

RETENTION. The inbox shows the last ``READ_WINDOW`` (30 days). Each write prunes,
for that restaurant only and at most ``PRUNE_BATCH`` rows, notifications whose
last alert is older than ``RETAIN_FOR`` (60 days). Nothing is scheduled.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import MODULE_REVIEWS, MODULE_TABLES
from notifications_app.models import NotificationKind, RestaurantNotification

logger = logging.getLogger(__name__)

SOLD_OUT_REALERT_AFTER = timedelta(minutes=10)
READ_WINDOW = timedelta(days=30)
RETAIN_FOR = timedelta(days=60)
PRUNE_BATCH = 200


def schedule(producer, *args) -> None:
    """
    Run ``producer(*args)`` after the current transaction commits, best-effort.

    Registration itself is guarded too: nothing about a notification may change
    the result of the request that caused it.
    """
    def run():
        try:
            producer(*args)
        except Exception:  # noqa: BLE001 — best-effort by contract
            logger.exception('in-app notification producer %s failed', getattr(producer, '__name__', producer))

    try:
        transaction.on_commit(run, robust=True)
    except Exception:  # noqa: BLE001
        logger.exception('could not schedule in-app notification producer')


# --- the three producers ------------------------------------------------------

def notify_low_review(review) -> None:
    """A review below the public threshold (``Review.is_critical``)."""
    if not review.is_critical:
        return
    _upsert(
        restaurant_id=review.restaurant_id,
        module=MODULE_REVIEWS,
        kind=NotificationKind.REVIEW_LOW,
        dedupe_key=f'review:{review.pk}',
        payload={'review_id': review.pk, 'rating': review.overall_rating},
        actor_id=None,
        realert=lambda row, now: False,  # one alert per review, ever
    )


def notify_item_sold_out(restaurant_id, menu_item_id, item_name, actor_id) -> None:
    """A dish marked sold out from the kitchen board (in_stock True -> False)."""
    _upsert(
        restaurant_id=restaurant_id,
        module=MODULE_TABLES,
        kind=NotificationKind.ITEM_SOLD_OUT,
        dedupe_key=f'sold_out:{menu_item_id}',
        payload={'menu_item_id': str(menu_item_id), 'item_name': item_name or ''},
        actor_id=actor_id,
        realert=lambda row, now: now - row.occurred_at >= SOLD_OUT_REALERT_AFTER,
    )


def notify_ordering_changed(restaurant_id, accepting_orders: bool, actor_id) -> None:
    """``accepting_orders`` changed. Every actual change re-alerts."""
    _upsert(
        restaurant_id=restaurant_id,
        module=MODULE_TABLES,
        kind=(NotificationKind.ORDERING_RESUMED if accepting_orders
              else NotificationKind.ORDERING_PAUSED),
        dedupe_key='ordering',
        payload={'accepting_orders': bool(accepting_orders)},
        actor_id=actor_id,
        realert=lambda row, now: True,
    )


# --- the one write path -------------------------------------------------------

def _upsert(*, restaurant_id, module, kind, dedupe_key, payload, actor_id, realert) -> None:
    """
    Create the (restaurant, dedupe_key) row, or apply the repeat rule to it.

    A RE-ALERT moves ``occurred_at`` forward and records the kind, payload and
    actor of this occurrence. A bump that does not re-alert updates the payload
    only. Neither deletes a read record: read state is a comparison with
    ``occurred_at``, so a re-alert is unread again on its own.
    """
    now = timezone.now()
    with transaction.atomic():
        row = (RestaurantNotification.objects.select_for_update()
               .filter(restaurant_id=restaurant_id, dedupe_key=dedupe_key).first())
        if row is None:
            try:
                with transaction.atomic():
                    RestaurantNotification.objects.create(
                        restaurant_id=restaurant_id, module=module, kind=kind,
                        dedupe_key=dedupe_key, payload=payload, actor_id=actor_id,
                        occurred_at=now,
                    )
                    _prune(restaurant_id, now)
                    return
            except IntegrityError:
                # A concurrent producer created it first: fall through and apply
                # the repeat rule to that row instead.
                row = (RestaurantNotification.objects.select_for_update()
                       .get(restaurant_id=restaurant_id, dedupe_key=dedupe_key))
        if realert(row, now):
            row.kind = kind
            row.module = module
            row.payload = payload
            row.actor_id = actor_id
            row.occurred_at = now
            row.save(update_fields=['kind', 'module', 'payload', 'actor', 'occurred_at', 'updated_at'])
        else:
            row.payload = payload
            row.save(update_fields=['payload', 'updated_at'])
        _prune(restaurant_id, now)


def _prune(restaurant_id, now) -> None:
    """Bounded per-restaurant retention: at most ``PRUNE_BATCH`` rows per write."""
    stale = list(
        RestaurantNotification.objects
        .filter(restaurant_id=restaurant_id, occurred_at__lt=now - RETAIN_FOR)
        .order_by('occurred_at')
        .values_list('pk', flat=True)[:PRUNE_BATCH]
    )
    if stale:
        RestaurantNotification.objects.filter(pk__in=stale).delete()
