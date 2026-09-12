"""
handle the submission of an order
"""
import logging

from typing import Union
from django.db import transaction
from django.utils import timezone
from users_app.models import User
from orders_app.controllers.services.acceptance_result import (
    OUTCOME_ALREADY_ACCEPTED, OUTCOME_NEWLY_ACCEPTED, acceptance_result,
)
from orders_app.models import Order, OrderAcceptance, OrderItem
from dinify_backend.configss.messages import (
    OK_ORDER_UPDATED, ERR_ORDER_UPDATED
)
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated, OrderStatus_Pending,
    OrderItemStatus_Preparing, OrderStatus_Preparing
)

logger = logging.getLogger(__name__)


def update_order_status(
    order: Order,
    new_status: str,
    user: Union[User, None],
    quote_ref: Union[str, None] = None,
) -> dict:
    """
    update an order
    """
    try:
        # SUBMIT (initiated -> pending) is the transition that turns a draft
        # into a real order and CLAIMS the table, so it must be race-safe.
        # It is handled by its own transactional helper; every other status
        # change below is left exactly as it was (no new locking/transaction).
        if new_status == OrderStatus_Pending:
            return _submit_order(order, user, quote_ref)
        # an unauthenticated diner arrives as AnonymousUser (not None); never
        # assign it to a User FK — normalise to None so attribution stays null.
        if user is not None and user.is_anonymous:
            user = None
        order.order_status = new_status
        logger.debug("The submitted user is %s", user)
        if user is not None:
            order.last_updated_by = user
        if new_status == OrderStatus_Preparing:
            order.waiter = user
            # set all oder items
            order_items = OrderItem.objects.filter(order=order)
            for item in order_items:
                item.status = OrderItemStatus_Preparing
                item.last_updated_by = user
                item.save()
        # time_last_updated is auto_now on BaseModel — the save stamps it.
        order.save()
        return {
            'status': 200,
            'message': OK_ORDER_UPDATED
        }
    except Exception:
        # log the full traceback so this failure class is diagnosable; keep the
        # user-facing message generic.
        logger.exception("ErrorUpdateOrderStatus")
        return {
            'status': 400,
            'message': ERR_ORDER_UPDATED
        }


# --- acceptance invariants (D02 / P9 / P10) ---------------------------------
#
# Stable reason codes. A client uses these to decide what to DO; the messages are
# what a diner reads. Neither is ever a silent reprice, a second order or a fake
# success.
REASON_LEGACY_PRICING = 'legacy_pricing_version'
REASON_QUOTE_REQUIRED = 'quote_ref_required'
REASON_QUOTE_STALE = 'quote_ref_stale'
REASON_QUOTE_INCOMPLETE = 'quote_incomplete'
REASON_NOTHING_TO_PREPARE = 'no_deliverable_items'
#: D04 — an order that already carries acceptance evidence, replayed with a
#: DIFFERENT quote. Not a failure of the original acceptance, and never a
#: second one.
REASON_ALREADY_ACCEPTED = 'order_already_accepted'

MESSAGE_LEGACY_PRICING = (
    'This order was prepared before a pricing update and cannot be placed as it '
    'is. Please review the updated order and place it again.'
)
MESSAGE_QUOTE_REQUIRED = (
    'Please review your order total before placing it.'
)
MESSAGE_QUOTE_STALE = (
    'Your order changed since you reviewed it. Please review the updated order '
    'and place it again.'
)
MESSAGE_QUOTE_INCOMPLETE = (
    'We could not itemise this order in full, so it cannot be placed as it is. '
    'Please ask a member of staff for help.'
)
MESSAGE_NOTHING_TO_PREPARE = (
    'None of the items on this order are available right now, so there is '
    'nothing to send to the kitchen.'
)
MESSAGE_ALREADY_ACCEPTED = (
    'This order has already been placed. Please check its status rather than '
    'placing it again.'
)


def _acceptance_refusal(order, supplied_quote_ref):
    """The four invariants that must hold to ACCEPT a draft, or ``None``.

    Runs inside ``_submit_order``'s transaction, on the row re-read under the
    table lock — so what is checked is what is about to be accepted, not a
    possibly-stale instance the caller handed in.

    1. **PRICING VERSION.** A draft priced by the LEGACY convention must not
       enter the corrected acceptance path, however few such drafts a rollout
       happens to leave behind. It is refused with an actionable code; it is NOT
       silently recalculated (that would accept an amount nobody reviewed) and
       NOT deleted (it is the diner's own draft).
    2. **QUOTE ACKNOWLEDGEMENT.** The submission must name the exact saved quote
       it is accepting. There is deliberately NO staff or internal bypass: being
       a trusted caller is not a reason to accept an amount no one reviewed, and
       a bare "confirmed" boolean would say nothing about WHICH quote.
    3. **A QUOTE THAT CAN REPRESENT THE PAYABLE.** A live child whose parent is
       not in the live population belongs under no quoted line, yet its amount is
       still in the saved payable — so no itemised quote built from the remaining
       lines can add up to what the diner would be charged. The response already
       DISCLOSES this (``order_details.quote_complete``), but a disclosure a
       client may ignore is not an invariant: an older client, or any caller
       holding the diner session, could return the perfectly valid reference and
       move the order to the kitchen with part of its amount unrepresented. The
       server refuses it here, on the population it is about to accept. It is
       checked AFTER the acknowledgement because being told "your order changed"
       is the accurate and more useful answer when the reference is stale, and
       BEFORE "nothing to prepare" because that is a statement about the itemised
       lines — an order whose itemisation cannot represent the payable has not
       earned one.
    4. **SOMETHING TO PREPARE.** An order whose every parent line is
       undeliverable must not become an empty kitchen ticket. The test is
       DELIVERABILITY AND QUANTITY, never a payable amount — a legitimately free
       dish (0.00) is orderable and still counts.

    A refusal changes nothing: no reprice, no replacement order, no partial
    acceptance. Nothing here rewrites an amount to make a population add up.
    """
    # Local imports keep this off the module import graph and dodge the
    # con_orders <-> create_order cycle, as _submit_order already does.
    from orders_app.controllers.con_orders import ConOrder
    from orders_app.controllers.services.order_pricing import (
        PRICING_VERSION_CORRECTED,
    )
    from orders_app.controllers.services import order_quote

    if order.pricing_version != PRICING_VERSION_CORRECTED:
        logger.info(
            'Order submission refused (order_id=%s, reason=%s, version=%s)',
            order.pk, REASON_LEGACY_PRICING, order.pricing_version,
        )
        return {
            'status': 400,
            'message': MESSAGE_LEGACY_PRICING,
            'reason': REASON_LEGACY_PRICING,
        }

    if not isinstance(supplied_quote_ref, str) or not supplied_quote_ref:
        return {
            'status': 400,
            'message': MESSAGE_QUOTE_REQUIRED,
            'reason': REASON_QUOTE_REQUIRED,
        }

    # ONE fetch of the live population, shared by the reference check and the
    # completeness check — `matches` would otherwise run this exact query itself,
    # so the submit path costs the same as before.
    live_rows = list(OrderItem.objects.filter(order=order, deleted=False))

    if not order_quote.matches(order, supplied_quote_ref, rows=live_rows):
        return {
            'status': 400,
            'message': MESSAGE_QUOTE_STALE,
            'reason': REASON_QUOTE_STALE,
        }

    _, orphaned = order_quote.group_live_children(live_rows)
    if orphaned:
        # Bounded: an order id and a count. No amounts, no order contents, no
        # personal data — the same disclosure the serializer logs.
        logger.warning(
            'Order submission refused (order_id=%s, reason=%s, orphaned=%d)',
            order.pk, REASON_QUOTE_INCOMPLETE, len(orphaned),
        )
        return {
            'status': 400,
            'message': MESSAGE_QUOTE_INCOMPLETE,
            'reason': REASON_QUOTE_INCOMPLETE,
        }

    if ConOrder.deliverable_parent_count(order) < 1:
        return {
            'status': 400,
            'message': MESSAGE_NOTHING_TO_PREPARE,
            'reason': REASON_NOTHING_TO_PREPARE,
        }
    return None


def _acceptance_replay(order, supplied_quote_ref):
    """Has this order ALREADY been accepted, and is this the same acceptance?

    D04. Without this, a client whose submit response was lost retried and was
    told ``This order cannot be submitted.`` — a FAILURE reported for an
    operation that had SUCCEEDED, which is the single worst answer a checkout
    can give: the diner believes nothing was ordered, and the kitchen is
    already cooking it.

    The evidence is a row, not the order's STATE. `order_status` is unusable
    for this: the kitchen moves it on to `preparing` and `served`, a recall
    moves it back, and a cancellation moves it somewhere else again — so by
    the time a retry arrives it says nothing about whether the diner's
    submission landed. `OrderAcceptance` is written once, by this transition
    only, and never moves.

    THREE OUTCOMES, and the third is the one worth stating:

      no evidence          -> ``None``; this is a genuine first submission and
                              the ordinary invariants decide it. An order
                              accepted BEFORE D04 also lands here, correctly:
                              nothing recorded its acceptance, so nothing can
                              be claimed about it.
      the SAME quote       -> 200, ``idempotent``. The acceptance already
                              happened and is not repeated; the client reads
                              current state from the order-details read.
      anything else        -> 409. A DIFFERENT quote is a different acceptance
                              being attempted against an order already
                              accepted, and an ABSENT one cannot prove it is
                              the same. Neither is a failure of the original
                              acceptance and neither may produce a second.

    A CANCELLED order that carries evidence still replays as accepted, and
    that is deliberate: the submission DID land, and the cancellation is a
    later, separate fact the client reads off the order. Answering "your
    submission failed" would be false about the only thing this route is
    asked.

    It comes BEFORE the ``initiated`` check, because that check is exactly
    what produced the false failure. It changes nothing on any path: no save,
    no second evidence row, no state flip, no table claim.
    """
    evidence = OrderAcceptance.objects.filter(order=order).first()
    if evidence is None:
        return None

    if (
        isinstance(supplied_quote_ref, str)
        and supplied_quote_ref
        and supplied_quote_ref == evidence.quote_ref
    ):
        return {
            'status': 200,
            'message': OK_ORDER_UPDATED,
            'idempotent': True,
            # THE CORRELATED ANSWER. `idempotent: True` says "this was not a
            # second acceptance"; it does not say WHICH acceptance, of WHICH
            # order, at WHOSE table, against WHICH quote — so a client had
            # nothing to check the reply against and a late or misrouted 200
            # was indistinguishable from the right one. `evidence` is the row
            # already in hand, so this costs no query.
            'checkout': acceptance_result(
                order,
                outcome=OUTCOME_ALREADY_ACCEPTED,
                evidence=evidence,
            ),
        }

    logger.info(
        'Order submission replayed against a different quote '
        '(order_id=%s, reason=%s)',
        order.pk, REASON_ALREADY_ACCEPTED,
    )
    return {
        'status': 409,
        'message': MESSAGE_ALREADY_ACCEPTED,
        'reason': REASON_ALREADY_ACCEPTED,
    }


def _submit_order(order: Order, user: Union[User, None],
                  supplied_quote_ref: Union[str, None] = None) -> dict:
    """
    Submit a draft order (order_status 'initiated' -> 'pending').

    This is the transition that turns a draft into a real order and CLAIMS the
    table, so it is transactional and race-safe:
      * ADMIT the submission first (shared advisory lock on the restaurant, then
        lifecycle state re-read under it),
      * lock the order's table row (matching _create_order's advisory->table->order
        lock order),
      * RE-READ the order under that lock — the instance handed in was fetched
        outside the transaction and may be stale,
      * re-check the "must still be a draft" rule and table occupancy on the
        FRESH row before flipping.
    Two diners submitting for the same table therefore serialize on the table
    lock: the first claims it, the second gets a clean 400.

    THE ADMISSION IS NOT A DUPLICATE OF THE ONE AT CREATION. Creation and
    submission are separate moments and the restaurant's lifecycle state can
    change between them — that gap is precisely how an order used to reach the
    kitchen after trading had stopped: initiated while `live`, suspended by an
    administrator, then submitted with nothing on this path ever asking. Both
    stages now ask the same question of the same rule, each at its own moment.
    """
    # Local imports keep this off the module import graph and dodge the
    # con_orders <-> create_order import cycle.
    from restaurants_app.models import Table
    from orders_app.controllers.con_orders import ConOrder
    from orders_app.controllers.services.order_admission import (
        STAGE_SUBMIT,
        admit,
    )

    # an unauthenticated diner arrives as AnonymousUser (not None); never
    # assign it to a User FK — normalise to None so attribution stays null.
    if user is not None and user.is_anonymous:
        user = None

    with transaction.atomic():
        # Admission FIRST, before any row lock — the advisory lock is the top of
        # the documented ordering, and `admit` re-reads the lifecycle state under
        # it. `created_by` is the ORDER's creator, not the submitting user: a
        # draft placed by an anonymous diner stays a diner order through submit,
        # so it is judged by the diner rule (the launch boundary) rather than the
        # laxer staff one. Passing `user` here would let a diner draft be
        # submitted at a restaurant that has not gone live, simply because a staff
        # member happened to be the one who tapped submit.
        #
        # THE LOCK IS TAKEN HERE; THE VERDICT IS APPLIED BELOW, once the request
        # is known to be NEW WORK. Acquiring the advisory lock and applying a
        # new-submission policy are different actions, and only the first belongs
        # at this point in the ordering. Applying it here reported a COMPLETED
        # acceptance as a failure: accepted while `live`, response lost,
        # restaurant suspended, diner retries — and the lifecycle 400 fired
        # before the evidence was ever read, which is the exact
        # failure-after-success this change exists to remove, over a suspension
        # the diner neither caused nor can see. The identical split is already
        # written out in `create_order._create_order` (steps 1a and 1d); this
        # is the same reasoning, carried across. The lock ORDER
        # (advisory -> Table -> Order) is unchanged.
        verdict = admit(
            restaurant_id=order.restaurant_id,
            created_by=order.created_by_id,
            stage=STAGE_SUBMIT,
        )

        # Table-first lock where there is one, then re-read the order under it.
        # (`Order.table` is non-nullable today, so the None branch is
        # defensive.) THE SEQUENCE BELOW IS SHARED rather than duplicated per
        # branch: it used to be written out twice, which is how an invariant
        # ends up added to one of them and not the other.
        locked_table = None
        if order.table_id is not None:
            locked_table = (
                Table.objects.select_for_update().get(pk=order.table_id)
            )
        order = Order.objects.select_for_update().get(pk=order.pk)

        # D04: WAS THIS ORDER ALREADY ACCEPTED? Asked FIRST, because the
        # `initiated` check below is precisely what used to report a lost
        # response as a failure.
        replay = _acceptance_replay(order, supplied_quote_ref)
        if replay is not None:
            return replay

        # NOW the admission verdict applies: this submission really is new
        # work, so the rule about new work governs it. A draft that was never
        # accepted still cannot reach the kitchen at a restaurant that has
        # stopped trading.
        if not verdict.allowed:
            logger.info(
                "Order submission refused (order_id=%s, code=%s)",
                order.pk, verdict.code,
            )
            return {'status': 400, 'message': verdict.message}

        # Status check on the FRESH row: a concurrent double-submit that
        # already flipped this order loses here with the existing 400 — now
        # only when there is no evidence to replay, i.e. for an order that was
        # never accepted through this path at all.
        if order.order_status != OrderItemStatus_Initiated:
            return {
                'status': 400,
                'message': 'This order cannot be submitted.'
            }

        if locked_table is not None:
            # Re-check occupancy under the lock. After the drafts-are-invisible
            # change this order (still 'initiated') is excluded from the
            # predicate anyway; the not-this-order guard is defense-in-depth.
            ongoing = ConOrder.any_present_ongoing_order(locked_table)
            if ongoing.get('present') and ongoing.get('order_id') != order.id:
                return {
                    'status': 400,
                    'message': 'The table has an ongoing order'
                }

        acceptance = _acceptance_refusal(order, supplied_quote_ref)
        if acceptance is not None:
            return acceptance

        order.order_status = OrderStatus_Pending
        if user is not None:
            order.last_updated_by = user
        # time_last_updated is auto_now on BaseModel — the save stamps it.
        order.save()

        # THE EVIDENCE COMMITS WITH THE TRANSITION OR NOT AT ALL. An accepted
        # order with no record of its acceptance would report a retry as a
        # failure — the defect this closes — and a record with no transition
        # would tell a client an order was placed that the kitchen never saw.
        # `supplied_quote_ref` is what `_acceptance_refusal` has just proved
        # names this order's saved quote, so the stored value is the exact
        # figure the diner confirmed rather than one re-derived afterwards.
        evidence = OrderAcceptance.objects.create(
            order=order,
            accepted_at=timezone.now(),
            quote_ref=supplied_quote_ref,
        )

        # Built INSIDE the transaction, from the row this transition just
        # wrote and the order as it just saved it, so the reply describes the
        # state that actually committed. `evidence` is passed rather than
        # looked up: the acceptance path's query cost is pinned to an exact
        # integer by `tests_order_acceptance.WhatTheEvidenceCostsTests`.
        correlated = acceptance_result(
            order, outcome=OUTCOME_NEWLY_ACCEPTED, evidence=evidence,
        )

    return {
        'status': 200,
        'message': OK_ORDER_UPDATED,
        'idempotent': False,
        'checkout': correlated,
    }
