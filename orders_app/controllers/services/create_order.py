"""
Order-creation service.

Centralises the single internal ``_create_order(...)`` entrypoint that
``ConOrder.initiate_order`` routes through. Inside one transaction it handles,
in order:

  1. idempotency (client_order_id) — return the existing order without gating
     or creating,
  2. ADMISSION — the shared advisory lock on the restaurant, then the lifecycle
     decision taken from status re-read under it (and the `is_test`
     classification, which follows from that same locked read: the lifecycle
     status AND the tenant-level `Restaurant.is_test` flag, both fetched in one
     query at that one protected moment),
  3. table-gating (only for genuinely new submissions),
  4. daily order-number allocation (race-safe counter),
  5. order-row creation with the kitchen fulfilment axis initialised,
  6. order-item creation + amount roll-up.

Steps 2 onward are the LOAD-BEARING checks. Their counterparts in
``ConOrder.initiate_order`` are preflights that ran in autocommit and may be stale
by the time a request reaches here, so nothing decided up there is trusted.

This replaces the race-prone ``create_order_number`` pre_save signal.
"""
import logging

from django.db import transaction, IntegrityError
from django.utils import timezone

from orders_app.models import Order, RestaurantDailyOrderCounter
from orders_app.controllers.services.order_admission import (
    STAGE_CREATE,
    admit,
)
from orders_app.controllers.services.order_input import (
    client_order_id_rejection, validate_order_items,
    validate_service_client_order_id,
)
from restaurants_app.controllers.lifecycle_policy import orders_are_commercial
from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated,
    PaymentStatus_Pending,
)

logger = logging.getLogger(__name__)

ORDER_SOURCE_DINER = "diner_self_service"


class OrderItemRejected(Exception):
    """
    Sentinel raised INSIDE _create_order's transaction when an order item (or
    one of its extras) is rejected by the ConOrder.add_order_item chokepoint.

    Returning the 400 dict from inside the ``with atomic()`` block would
    COMMIT the draft order and every row already written — the exact partial
    write the tenant boundary forbids. Raising instead unwinds through
    ``atomic.__exit__`` so the draft Order, its OrderItems (parents and child
    extras) and the daily-number counter increment all roll back; the caller
    catches this immediately around the block and translates it back into the
    plain-dict error envelope. Same exception-around-atomic idiom as
    allocate_daily_order_number above and the INSERT-race recovery below.
    """
    def __init__(self, response: dict):
        super().__init__(response.get('message', 'Order item rejected'))
        self.response = response


def allocate_daily_order_number(restaurant, order_date):
    """
    Allocate the next per-restaurant, per-day order number.

    The row lock (``select_for_update``) is the normal path; the unique
    constraint on (restaurant, order_date) is the final guard against the
    first-of-day race where two creators both try to INSERT the counter row.

    The ``try/except`` deliberately wraps the ``with atomic()`` block (rather
    than sitting inside it) so the failed savepoint is rolled back *before* the
    retry — catching the IntegrityError inside the block would leave the
    connection needing rollback and the retry would raise
    ``TransactionManagementError``.
    """
    for _ in range(2):
        try:
            with transaction.atomic():
                counter, _created = (
                    RestaurantDailyOrderCounter.objects
                    .select_for_update()
                    .get_or_create(
                        restaurant=restaurant,
                        order_date=order_date,
                        defaults={"next_number": 1},
                    )
                )
                number = counter.next_number
                counter.next_number += 1
                counter.save(update_fields=["next_number"])
                return number
        except IntegrityError:
            # first-of-day race: a concurrent creator won the INSERT; the row
            # now exists, so the next pass locks it cleanly.
            continue
    raise IntegrityError("Could not allocate a daily order number after retry")


def _create_order(*, restaurant, table, items,
                  customer=None, created_by=None,
                  order_source=ORDER_SOURCE_DINER, client_order_id=None):
    """
    Create an order (and its items) atomically.

    Returns ``{'status': 200, 'order': <Order>, 'idempotent': bool}`` on
    success, or ``{'status': 400, 'message': ..., 'data': {...}}`` when
    table-gating rejects a genuinely new submission, or the 400 dict from the
    item chokepoint when any item/extra is rejected — in which case the WHOLE
    transaction has rolled back (no order, no items, no counter increment; see
    OrderItemRejected).
    """
    # imported lazily to avoid a circular import (con_orders imports this module)
    from orders_app.controllers.con_orders import ConOrder

    # 0. STATIC INPUT VALIDATION (D01) — the AUTHORITATIVE shape/quantity gate.
    #    `initiate_order` runs the same rule above, but this service is a
    #    supported entrypoint in its own right and must not depend on its caller
    #    having validated. The rule is the SAME function, so the two can never
    #    disagree, and it is PURE — no query, so the pinned per-line order-path
    #    query budget is unchanged.
    #
    #    Deliberately OUTSIDE the transaction and BEFORE the replay lookup: it
    #    is deterministic and menu-independent, so it needs no lock and nothing
    #    it decides can go stale, and running it here means a malformed request
    #    can never allocate a daily order number, touch Decimal arithmetic or
    #    write a row. A correctly shaped replay is untouched by it.
    #
    #    The VALIDATED lines are what the rest of this function consumes.
    static_input = validate_order_items(items)
    if static_input.get('status') != 200:
        return static_input
    items = static_input['items']

    #    The OPTIONAL IDEMPOTENCY KEY is validated on the same terms, and for
    #    the same reason: this service consumes it in THREE key-dependent
    #    database operations below — the step-1 replay lookup, the INSERT, and
    #    the insert-race recovery — and it must not rely on a caller having
    #    checked it. Validated here, before the transaction opens, so a bad key
    #    can never take a lock or allocate a daily order number.
    #
    #    Canonicalising to ONE representation is what makes those three
    #    operations agree: a caller may legitimately pass a `uuid.UUID` object
    #    (several in-process callers do) or a UUID string, and both become the
    #    same canonical string before any of them runs.
    client_order_id, key_ok = validate_service_client_order_id(client_order_id)
    if not key_ok:
        return client_order_id_rejection()

    # The try/except sits AROUND the atomic block (the same idiom as the two
    # nested savepoints inside it): OrderItemRejected must unwind through
    # atomic.__exit__ so the whole transaction rolls back BEFORE it is
    # translated back into the plain-dict error envelope.
    try:
        with transaction.atomic():
            # 1. idempotency FIRST — before gating or creation
            #    `is not None`, never truthiness: after validation the key is
            #    either absent or a canonical string, and stating that
            #    explicitly is what stops a falsy value ever being read as
            #    "no key" and skipping both this lookup and the recovery below.
            if client_order_id is not None:
                existing = Order.objects.filter(
                    restaurant=restaurant,
                    client_order_id=client_order_id,
                ).first()
                if existing is not None:
                    return {'status': 200, 'order': existing, 'idempotent': True}

            # 1a. AUTHORITATIVE ADMISSION. Take the shared advisory lock on the
            #     restaurant and decide from status re-read UNDER it — the endpoint
            #     preflight ran in autocommit and can be stale by now, exactly like
            #     the menu-publication preflight at step 2b.
            #
            #     FIRST among the locks, before the Table row lock below: the
            #     advisory lock is the single top level of the documented ordering
            #     (advisory -> Table -> Counter -> Order -> OrderItem), and taking
            #     it after a row lock would reintroduce the cycle that ordering
            #     exists to prevent.
            #
            #     AFTER step 1, so an idempotent replay returns the original order
            #     without taking the lock or being re-admitted — the same contract
            #     publication follows: a replay is never re-validated, so a
            #     lifecycle change cannot retroactively refuse an order that was
            #     already created and acknowledged.
            verdict = admit(
                restaurant_id=restaurant.pk,
                created_by=created_by,
                stage=STAGE_CREATE,
            )
            if not verdict.allowed:
                logger.info(
                    "Order admission refused (restaurant_id=%s, code=%s)",
                    restaurant.pk, verdict.code,
                )
                return {'status': 400, 'message': verdict.message}

            # 1b. Lock the table row so concurrent same-table submissions serialize.
            #     Mirrors allocate_daily_order_number's select_for_update in this file:
            #     the second creator blocks here until the first commits, then its
            #     step-2 gate below sees the first order and returns the 400. Placed
            #     AFTER step 1 so idempotent replays return without taking the lock.
            #     Lazy import keeps this module import-cycle-free (as with ConOrder).
            from restaurants_app.models import Table
            try:
                table = Table.objects.select_for_update().get(pk=table.pk)
            except Table.DoesNotExist:
                return {'status': 400, 'message': 'Invalid table for this restaurant'}

            # 2. table-gating — only for genuinely new submissions
            ongoing = ConOrder.any_present_ongoing_order(table)
            if ongoing.get('present'):
                return {
                    'status': 400,
                    'message': 'The table has an ongoing order',
                    'data': {'order_id': ongoing.get('order_id')},
                }

            # 2b. AUTHORITATIVE menu-publication + extra-applicability validation at
            #     ONE time captured AFTER the blocking table lock and BEFORE any
            #     daily number is allocated or any row is written. This is the
            #     load-bearing check — the endpoint preflight can go stale while a
            #     request waits on the lock, so re-validate against committed menu
            #     state here. A rejection RAISES OrderItemRejected so the whole
            #     transaction unwinds (no counter increment, no Order, no items).
            #     Anonymous orders enforce diner publication; staff/admin (created_by
            #     set) bypass publication but never tenant / extra-applicability
            #     integrity. This self-guards EVERY caller of _create_order, so a
            #     future caller cannot bypass the boundary by skipping the endpoint.
            from restaurants_app.controllers.menu_publication import (
                validate_order_selections,
            )
            selection = validate_order_selections(
                restaurant, items, timezone.localtime(),
                enforce_publication=(created_by is None),
            )
            if selection.get('status') != 200:
                raise OrderItemRejected(selection)

            # 2c. AUTHORITATIVE modifier (option) normalization + limit recheck, in
            #     the SAME transaction as the publication/extras validation above.
            #     validate_order_selections covers tenant + publication + extra
            #     applicability + extras min/max; this canonicalizes each line's
            #     selected_modifiers against the ordered item's OWN server-side options
            #     (group/choice validity, duplicate-choice de-dup, deterministic
            #     menu-definition ordering, and group min/max on the unique set) and
            #     REPLACES the local `items` with the normalized copy. That one
            #     canonical value then drives existing-line comparison, pricing,
            #     snapshots and persistence downstream — closing the gap where the
            #     original client selected_modifiers was still used verbatim for
            #     find_existing_order_item comparison and OrderItem.selected_modifiers
            #     persistence. Group/choice VALIDITY and additional cost are also
            #     re-derived server-side per line in
            #     ConOrder.determine_effective_unit_price (defense in depth). Runs for
            #     new submissions only — a replay returns at step 1 before this point,
            #     exactly like publication — and applies to every caller (staff
            #     included; it is NOT gated on created_by).
            normalization = ConOrder.normalize_order_items(restaurant, items)
            if normalization.get('status') != 200:
                raise OrderItemRejected(normalization)
            items = normalization['items']

            # 3. daily numbering (local business date)
            order_date = timezone.localdate()
            order_number = allocate_daily_order_number(restaurant, order_date)

            # 4. create the order with the fulfilment axis initialised.
            #    Kitchen owns fulfilment_status; order_status/payment_status stay
            #    finance-owned and are only seeded here at creation.
            #
            #    The INSERT is wrapped in a savepoint (nested atomic) so a concurrent
            #    double-tap — two requests carrying the same client_order_id, both past
            #    the step-1 lookup before either committed — degrades to the same
            #    idempotent replay as the sequential case instead of a raw 500. The
            #    partial unique constraint uniq_order_restaurant_client_order_id lets
            #    exactly one INSERT win; the loser catches the IntegrityError below.
            #    As in allocate_daily_order_number, the try/except deliberately WRAPS
            #    the atomic block so the failed savepoint is rolled back before the
            #    re-query, leaving the outer transaction usable.
            try:
                with transaction.atomic():
                    order = Order.objects.create(
                        restaurant=restaurant,
                        table=table,

                        total_cost=0,
                        discounted_cost=0,
                        savings=0,
                        actual_cost=0,
                        prepayment_required=table.prepayment_required,

                        order_status=OrderStatus_Initiated,
                        payment_status=PaymentStatus_Pending,

                        customer=customer,
                        created_by=created_by,

                        order_source=order_source,
                        client_order_id=client_order_id,
                        order_number=order_number,
                        order_date=order_date,
                        fulfilment_status='new',
                        fulfilment_status_updated_at=timezone.now(),

                        # An order is test if EITHER of two independent things is
                        # true, and neither of them is anything the caller sent:
                        #
                        #   TENANT   — the restaurant itself exists for testing
                        #              (`Restaurant.is_test`). Such a tenant never
                        #              produces commerce, whatever its lifecycle
                        #              state, so a test restaurant that has gone
                        #              `live` still writes test orders.
                        #   LIFECYCLE — the order predates go-live, so it is a
                        #              rehearsal: operationally real, commercially
                        #              invisible.
                        #
                        # BOTH values come from `verdict` — the single query the
                        # ADMISSION ran under the advisory lock at step 1a — and not
                        # from `restaurant`, which was loaded in autocommit before
                        # this transaction opened. Those two agreed right up until
                        # they didn't: a go-live committing while this request waited
                        # on the table lock left the instance saying `onboarding`,
                        # and a real commercial order was written `is_test=True` and
                        # vanished from every revenue report. The tenant flag is
                        # read from the same protected moment for exactly the same
                        # reason — an admin could flip it concurrently, and reading
                        # it off the stale instance would reproduce that bug on a
                        # different field. The lock is what makes both values still
                        # true at the INSERT.
                        is_test=(
                            verdict.restaurant_is_test
                            or not orders_are_commercial(verdict.status)
                        ),
                    )
            except IntegrityError:
                # concurrent double-tap: a racing request with the same
                # client_order_id committed between our step-1 lookup and this INSERT.
                # Return the winner as the idempotent result — the SAME shape step 1
                # returns.
                if client_order_id is not None:
                    existing = Order.objects.filter(
                        restaurant=restaurant,
                        client_order_id=client_order_id,
                    ).first()
                    if existing is not None:
                        return {'status': 200, 'order': existing, 'idempotent': True}
                # not the client_order_id constraint (no existing row) — re-raise so a
                # genuinely unexpected IntegrityError is never silently swallowed.
                raise

            # 5. items + amount roll-up — FAIL CLOSED: any non-200 from the
            #    chokepoint aborts the whole transaction (see OrderItemRejected).
            #    Previously the return was ignored, so a rejected item/extra was
            #    silently dropped while the rest of the order committed.
            #    `order` is passed alongside its id: this transaction is holding the
            #    row it just created, so the chokepoint has no reason to re-SELECT it
            #    (and then lazily load its restaurant) once per line. The instance is
            #    the same one every line would have fetched — it was created in this
            #    transaction and nothing has written to it since.
            for item in items:
                item_result = ConOrder.add_order_item(
                    item=item, order_id=str(order.id), order=order,
                )
                if item_result.get('status') != 200:
                    raise OrderItemRejected(item_result)
            order = Order.objects.select_for_update().get(id=order.id)
            ConOrder.update_order_amounts(order=order)
    except OrderItemRejected as rejected:
        return rejected.response

    return {'status': 200, 'order': order, 'idempotent': False}
