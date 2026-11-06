"""
Producers of in-app notifications (SHELL-FINISH-00, PR 4).

Four events have a source today:

  * a review below ``PUBLIC_RATING_THRESHOLD``
        reviews_app/controllers/submit_review.py, after the review is saved;
  * a dish sold out from the kitchen board (``in_stock`` True -> False)
        orders_app/endpoints_kitchen.py, ``KitchenMenuItemStockView``;
  * ordering paused or resumed (``accepting_orders`` changed)
        restaurants_app/endpoints/restaurant_setup.py, the restaurants PUT;
  * rounds waiting for staff (Step 2, PR BE-G), ``notify_round_waiting``, scheduled
    from THREE places: a held acceptance
        orders_app/controllers/services/round_placement.py, ``place_first_round``,
        only when the round is held;
    and the floor's Confirm and Decline
        orders_app/controllers/services/round_release.py, only when they changed
        something.
    All three schedule the same producer with only the restaurant id; the producer
    reads what stands and decides whether that is an alert.

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
    this module (the call site compares before and after);
  * round waiting: one row per restaurant (``round_waiting``), module ``tables``,
    no actor. It RE-ALERTS when no row exists yet or when the number of held rounds
    is now HIGHER than the ``waiting`` the row last recorded: a new order is
    waiting. Otherwise (a Confirm or Decline brought it down, or it is unchanged) it
    updates the payload only, so a user who read it keeps it read, and at 0 the
    payload says 0. With no row and nothing waiting it writes nothing: there is
    nothing to announce. In pay-after every first order waits, so one row per round
    would bury the bell.

A STALE EVENT IS DROPPED. Callbacks of two overlapping changes can run in the
opposite order to their commits, so the ordering and sold-out producers re-read
the state they announce under the notification row lock and skip the write when
it no longer holds; the later change's own callback announces what stands. The
round-waiting producer carries NO state from its call site at all: its whole
payload is read from the database under the row lock (``payload`` given as a
callable), so whichever callback runs last writes what stands, in any order.

PAYLOADS are built here, server-side, and carry no credentials, OTPs, diner
contact details or diner free text: a review's comment is NOT copied, because it
is written by a diner and may hold a phone number. The round-waiting payload is
exactly ``{waiting, oldest_held_since, latest_table}``: a count, an instant and a
table's display name, and no order id.

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


# --- the producers ------------------------------------------------------------

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
        realert=lambda row, now, payload: False,  # one alert per review, ever
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
        realert=lambda row, now, payload: now - row.occurred_at >= SOLD_OUT_REALERT_AFTER,
        still_true=lambda: _item_in_stock(menu_item_id) is False,
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
        realert=lambda row, now, payload: True,
        still_true=lambda: _accepting_orders(restaurant_id) is bool(accepting_orders),
    )


def notify_round_waiting(restaurant_id) -> None:
    """Rounds are waiting for staff at the restaurant (Step 2, PR BE-G).

    Called after a held acceptance, a Confirm or a Decline commits, with nothing but
    the restaurant id. The payload is read under the row lock from the database as it
    stands; it re-alerts only when more rounds wait than the row last said, or when
    the row is new. See THE REPEAT RULES above.
    """
    from orders_app.controllers.services.round_placement import held_rounds_summary

    def snapshot(row):
        summary = held_rounds_summary(restaurant_id)
        if row is None and summary['waiting'] == 0:
            return None  # nothing waits and nothing was announced: no write
        return {
            'waiting': summary['waiting'],
            'oldest_held_since': _iso(summary['oldest_held_since']),
            'latest_table': summary['latest_table'],
        }

    _upsert(
        restaurant_id=restaurant_id,
        module=MODULE_TABLES,
        kind=NotificationKind.ROUND_WAITING,
        dedupe_key='round_waiting',
        payload=snapshot,
        actor_id=None,  # a diner's order: no member of staff is excluded
        realert=lambda row, now, payload: payload['waiting'] > _last_waiting(row),
    )


def _last_waiting(row) -> int:
    """The ``waiting`` the row last recorded; anything unreadable counts as 0, so a
    damaged payload re-alerts rather than staying silent."""
    value = (row.payload or {}).get('waiting') if isinstance(row.payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _iso(value):
    """An instant as ISO 8601 with its offset, in the platform zone, to the
    millisecond: the floor contract's rendering of ``held_since``."""
    if value is None:
        return None
    return timezone.localtime(value, timezone.get_default_timezone()).isoformat(
        timespec='milliseconds')


# --- the one write path -------------------------------------------------------

def _upsert(*, restaurant_id, module, kind, dedupe_key, payload, actor_id, realert,
            still_true=None) -> None:
    """
    Create the (restaurant, dedupe_key) row, or apply the repeat rule to it.

    ``payload`` is a dict, or a callable ``payload(row)`` evaluated UNDER the row
    lock (``row`` is ``None`` when no row exists yet) that may return ``None`` to
    skip the write; it is evaluated again on the row a concurrent producer created.
    ``realert(row, now, payload)`` decides whether this occurrence alerts.

    ``still_true`` (optional) re-reads the state this event announces, and the
    write is skipped when it no longer holds. It is asked AFTER the row lock (or
    just before the create that the unique key arbitrates), because callbacks of
    two overlapping changes can run in the opposite order to their commits: a
    pause's callback landing after a resume committed would otherwise overwrite
    "resumed" with "paused". The later change's own callback announces the state
    that actually stands (Codex P2 on #379).

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
            if still_true is not None and not still_true():
                return
            current = payload(None) if callable(payload) else payload
            if current is None:
                return
            try:
                with transaction.atomic():
                    RestaurantNotification.objects.create(
                        restaurant_id=restaurant_id, module=module, kind=kind,
                        dedupe_key=dedupe_key, payload=current, actor_id=actor_id,
                        occurred_at=now,
                    )
                    _prune(restaurant_id, now)
                    return
            except IntegrityError:
                # A concurrent producer created it first: fall through and apply
                # the repeat rule to that row instead.
                row = (RestaurantNotification.objects.select_for_update()
                       .get(restaurant_id=restaurant_id, dedupe_key=dedupe_key))
        if still_true is not None and not still_true():
            return
        current = payload(row) if callable(payload) else payload
        if current is None:
            return
        if realert(row, now, current):
            row.kind = kind
            row.module = module
            row.payload = current
            row.actor_id = actor_id
            row.occurred_at = now
            row.save(update_fields=['kind', 'module', 'payload', 'actor', 'occurred_at', 'updated_at'])
        else:
            row.payload = current
            row.save(update_fields=['payload', 'updated_at'])
        _prune(restaurant_id, now)


def _accepting_orders(restaurant_id):
    from restaurants_app.models import Restaurant
    return (Restaurant.objects.filter(pk=restaurant_id)
            .values_list('accepting_orders', flat=True).first())


def _item_in_stock(menu_item_id):
    from restaurants_app.models import MenuItem
    return (MenuItem.objects.filter(pk=menu_item_id)
            .values_list('in_stock', flat=True).first())


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
