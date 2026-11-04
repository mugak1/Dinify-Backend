"""
Step 2, PR BE-D — the floor's two staff commands on a HELD round: CONFIRM and DECLINE.

A HELD round is accepted but not yet the kitchen's: ``order_status='pending'`` with a
placement whose ``released_at`` is NULL (``round_placement``). BE-C made the kitchen
see released rounds only. Something must therefore move a held round on, and this
module is that something, for a member of staff at the floor:

* ``confirm`` RELEASES the round: ``released_at``, ``released_by`` and a re-stamped
  ``business_date`` on the placement. The round then reaches both kitchen feeds,
  aged from its release. It writes NOTHING on the ``Order``: releasing is not a
  kitchen command and does not move ``fulfilment_revision`` — the kitchen's own
  release boundary (``order_released_while_waiting``) is what stops a command formed
  against the held round applying to the released one.
* ``decline`` CANCELS the round, which was never released: ``order_status``,
  ``cancelled_at``, ``cancelled_by``, ``cancellation_reason='declined_by_staff'``,
  and a bump of ``fulfilment_revision`` so a stale kitchen client cannot act on it.
  The table is freed (occupancy excludes ``cancelled``), and the visit ends with it,
  because until Step 3 a visit's state is derived from its one order.

NOTHING HOLDS A ROUND YET: BE-F (holding at acceptance, behind a switch that
defaults off) comes later, so on today's data every round is released and these
commands answer ``round_released`` / ``changed: false``. Nothing here consults either
switch — ``DINING_VISIT_WRITES_ENABLED`` or BE-F's — so staff can always clear a held
round whatever the writers are doing.

THE LOCK ORDER IS THE CONTRACT.

    confirm   advisory(restaurant) SHARED -> Table -> Order (-> UPDATE OrderPlacement)
    decline                                  Table -> Order (-> UPDATE orders)

CONFIRM IS AN ADMISSION: it puts new work on the kitchen board, so it asks
``order_admission.admit`` (``STAGE_RELEASE``) exactly as acceptance does, with the
ORDER's provenance. The shared lock also holds the catalogue steady: every menu PUT
/ DELETE and the kitchen 86 take the EXCLUSIVE side, so the makeability read below
and the write cannot straddle a menu edit. DECLINE admits nothing and takes no
advisory lock. BOTH take ``Table`` before ``Order`` in SEPARATE statements, and that
is load-bearing, not tidiness: BE-C measured that the kitchen reads a joined placement
from its Order statement's own READ COMMITTED snapshot, so a release that locked only
the Order row is read stale. Every writer of ``released_at`` must take
``Table -> Order``; this one does. No ``Restaurant`` row lock is taken, so neither
command can cycle against the lifecycle transition (advisory -> Restaurant) or the
kitchen (Table -> Order), and two of them on one table serialize on the Table row.

WHAT CONFIRM RE-CHECKS, under the locks, after the idempotency check:

1. LIFECYCLE through ``admit`` — the verdict is read at the top (that is where the
   lock order puts the advisory lock) and APPLIED only once the request is known to
   be new work. A retry of a Confirm whose response was lost is answered
   ``changed: false``, never a lifecycle refusal (the D04 lesson ``_submit_order``
   records). A refusal is ``restaurant_not_trading``.
2. OPERATIONAL ELIGIBILITY through ``order_eligibility.evaluate`` on the protected
   facts (``facts_from_verdict``) and the ORDER's provenance. For a diner round a
   pause, a menu-only table or a table out of service leaves it held
   (``ordering_paused`` / ``table_menu_only`` / ``table_out_of_service``). A STAFF
   round passes the pause and the QR policy, as it does at acceptance — but table
   LIVENESS binds every provenance there, so a staff round at a table out of service
   is refused ``table_out_of_service`` too. That is acceptance's rule, kept.
3. MAKEABILITY through ``purchase_integrity.inspect(..., enforce_publication=False)``:
   a sold-out or deleted dish, a changed extra, modifier or allergen declaration
   means ``cannot_be_made`` naming the item, and the round stays held. It is NEVER
   written as a quote closure: the order was accepted, so its quote is not the
   question. Publication and section schedules are deliberately NOT re-checked — the
   diner placed the round while the dish was published, Confirm is a staff decision,
   and a lunch order placed at 14:58 must not become "cannot be made" at 15:02.
   The quote lifetime is not re-checked either: the accepted price stands.

AUTHORIZATION COMES FIRST, before any of it, and is re-checked UNDER the lock (the
``StaffAuthority`` pattern): the ``tables`` module at the restaurant the ORDER
belongs to, never a restaurant from the request (which may only MATCH). Every
authorization or scope failure is one non-disclosing 404. One consequence, stated
rather than discovered: portal access is lifecycle-filtered, so at a SUSPENDED or
OFFBOARDED restaurant no member of staff holds ``tables`` and both commands answer
404 before ``admit`` is reached. ``restaurant_not_trading`` is what ``admit``'s
refusal produces where access still holds — a diner round at a restaurant that is
still onboarding.

IDEMPOTENCY AND CROSSINGS. Both commands are one-way and safe to resend, so neither
takes a revision precondition. Confirm on a released round is ``changed: false``;
on a declined one ``round_declined``. Decline on a declined round is
``changed: false``; on a released one ``round_released``. Anything else that is not
held (served, cancelled by the kitchen, a draft) is ``round_not_held``. A refusal
writes nothing.

THE ROUND OBJECT (floor contract v1, ``docs/FLOOR_CONTRACT.md``) is projected here,
once, for the two commands and the two floor reads, so the floor cannot show a
round one way and a command answer it another.
"""
import logging
import uuid as uuid_module
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    MODULE_TABLES,
    CancellationReason_DeclinedByStaff,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
)
from misc_app.controllers.money import format_money
from orders_app.controllers.orders.serializers import historical_name
from orders_app.controllers.services import order_eligibility as eligibility
from orders_app.controllers.services import purchase_integrity
from orders_app.controllers.services.order_admission import STAGE_RELEASE, admit
from orders_app.controllers.services.order_authority import (
    StaffAuthority,
    StaffAuthorityError,
    assert_authority_current,
)
from orders_app.controllers.services.round_placement import (
    FIRST_ROUND,
    PAYMENT_UNKNOWN,
    business_date_for,
    is_held,
    placement_of,
)
from orders_app.models import Order, OrderAcceptance, OrderItem
from users_app.controllers.permissions_check import can_user_access_module

logger = logging.getLogger(__name__)

#: The contract tag every floor read carries.
FLOOR_CONTRACT = 'floor.v1'

#: A held round is STALE once ``server_time - held_since >= STALE_AFTER``. A flag
#: only: nothing acts on it, and nothing here runs on a clock.
STALE_AFTER = timedelta(minutes=15)

# --- the round's state -------------------------------------------------------
# The floor lists LIVE rounds only (held / released). A command answer can describe
# a round that has left the floor, so it may carry one of the other four.
STATE_HELD = 'held'
STATE_RELEASED = 'released'
STATE_DECLINED = 'declined'
STATE_CANCELLED = 'cancelled'
STATE_SERVED = 'served'
STATE_DRAFT = 'draft'
LIVE_STATES = (STATE_HELD, STATE_RELEASED)

SOURCE_DINER = 'diner'
SOURCE_STAFF = 'staff'

#: How ``accepted_at`` was obtained. A round accepted since D04/C has an
#: ``OrderAcceptance`` row and its instant is the acceptance. An older order has
#: none; its ``accepted_at`` is then the order's creation (the moment the kitchen
#: card already ages such a ticket from), labelled so nothing reads it as a recorded
#: acceptance.
ACCEPTED_AT_FROM_ACCEPTANCE = 'acceptance'
ACCEPTED_AT_FROM_ORDER_CREATED = 'order_created'

# --- refusal reasons (floor contract v1) ----------------------------------------
REASON_ROUND_DECLINED = 'round_declined'
REASON_ROUND_RELEASED = 'round_released'
REASON_ROUND_NOT_HELD = 'round_not_held'
REASON_RESTAURANT_NOT_TRADING = 'restaurant_not_trading'
REASON_ORDERING_PAUSED = 'ordering_paused'
REASON_TABLE_MENU_ONLY = 'table_menu_only'
REASON_TABLE_OUT_OF_SERVICE = 'table_out_of_service'
REASON_CANNOT_BE_MADE = 'cannot_be_made'

#: ``order_eligibility``'s reasons, in the floor's vocabulary. EXHAUSTIVE over
#: ``eligibility.TRANSIENT_REASONS`` (a test holds the two equal), so a new
#: eligibility reason cannot fall through to a 500 or to a wrong word.
ELIGIBILITY_REASONS = {
    eligibility.REASON_RESTAURANT_PAUSED: REASON_ORDERING_PAUSED,
    eligibility.REASON_TABLE_ORDERING_UNAVAILABLE: REASON_TABLE_MENU_ONLY,
    eligibility.REASON_TABLE_UNAVAILABLE: REASON_TABLE_OUT_OF_SERVICE,
    eligibility.REASON_RESTAURANT_UNAVAILABLE: REASON_RESTAURANT_NOT_TRADING,
}

MESSAGES = {
    REASON_ROUND_DECLINED: 'This order was already declined.',
    REASON_ROUND_RELEASED: 'This order has already been sent to the kitchen.',
    REASON_ROUND_NOT_HELD: 'This order is not waiting for staff.',
    REASON_RESTAURANT_NOT_TRADING: 'This restaurant is not taking orders right now.',
    REASON_ORDERING_PAUSED: (
        'Ordering is paused, so diner orders cannot be sent to the kitchen.'),
    REASON_TABLE_MENU_ONLY: (
        'This table is menu only, so diner orders from it cannot be sent to the '
        'kitchen.'),
    REASON_TABLE_OUT_OF_SERVICE: (
        'This table is out of service, so orders from it cannot be sent to the '
        'kitchen.'),
    REASON_CANNOT_BE_MADE: 'Something on this order cannot be made as ordered.',
}

OUTCOME_CONFIRMED = 'Order sent to the kitchen'
OUTCOME_DECLINED = 'Order declined'
OUTCOME_UNCHANGED = 'No change'


class RoundNotFound(Exception):
    """Unknown, soft-deleted, outside the named restaurant, or the caller may not
    act there. ONE exception, one non-disclosing 404, so these routes cannot tell a
    caller that some UUID is a real order at a restaurant they cannot see."""


class RoundRefusal(Exception):
    """A crossed or blocked command: HTTP 409, a contract ``reason``, the round's
    current ``state`` and the round itself (the caller has cleared the module gate
    for this order's restaurant, so the round is theirs to see)."""

    def __init__(self, reason, round_object, extra=None):
        super().__init__(reason)
        self.status = 409
        self.reason = reason
        self.message = MESSAGES[reason]
        self.round = round_object
        self.extra = dict(extra or {})

    def body(self):
        body = {
            'status': self.status,
            'message': self.message,
            'reason': self.reason,
            'state': self.round['state'],
        }
        body.update(self.extra)
        body['data'] = {'round': self.round}
        return body


# --- the projection -----------------------------------------------------------------

def item_count_expression():
    """``item_count`` as SQL, for an ``Order`` queryset: ONE definition, read by the
    floor read, the drawer and both commands.

    THE DEFINITION: the total QUANTITY of the round's main dishes as the kitchen
    lists them — live (``deleted=False``) top-level lines (``parent_item`` NULL) that
    are ``available``. Extras are not counted, and a line the create path zeroed
    (``available=False``, quantity 0) is not part of what is being made. The same for
    every order, including one older than ``OrderAcceptance``: the lines carry no
    acceptance-dependent fact. A scalar subquery, so it adds no query and no
    ``GROUP BY`` to the statement it rides, a locked one included.
    """
    lines = (
        OrderItem.objects
        .filter(order=OuterRef('pk'), parent_item__isnull=True,
                deleted=False, available=True)
        .order_by()
        .values('order')
        .annotate(total=Sum('quantity'))
        .values('total')[:1]
    )
    return Coalesce(Subquery(lines, output_field=IntegerField()), Value(0))


def iso(value):
    """An aware instant as ISO 8601 WITH ITS OFFSET, in the platform's zone
    (``settings.TIME_ZONE``, Africa/Nairobi), to the millisecond. ``None`` stays
    ``None``. One rendering for every time the floor contract carries."""
    if value is None:
        return None
    local = timezone.localtime(value, timezone.get_default_timezone())
    return local.isoformat(timespec='milliseconds')


def round_state(order):
    """What one round IS: ``held`` / ``released`` (live, listed on the floor) or
    ``declined`` / ``cancelled`` / ``served`` / ``draft`` (left the floor).

    The live half agrees with ``occupancy.ongoing_order_q`` by construction: that rule
    excludes drafts, cancellations and served tickets, and is exactly what is left
    here as ``held`` / ``released``. Select ``placement`` with the order.
    """
    if order.order_status == OrderStatus_Initiated:
        return STATE_DRAFT
    if order.order_status == OrderStatus_Cancelled:
        if order.cancellation_reason == CancellationReason_DeclinedByStaff:
            return STATE_DECLINED
        return STATE_CANCELLED
    if order.fulfilment_status == 'served':
        return STATE_SERVED
    if is_held(order):
        return STATE_HELD
    return STATE_RELEASED


def _acceptance_of(order):
    """The order's ``OrderAcceptance``, or ``None``. Select ``acceptance`` with the
    order: Django then caches the absence too, so this costs no query."""
    try:
        return order.acceptance
    except OrderAcceptance.DoesNotExist:
        return None


def accepted_at_of(order):
    """``(instant, provenance)``: the recorded acceptance, or — for an order older
    than ``OrderAcceptance`` — its creation, labelled ``order_created``."""
    acceptance = _acceptance_of(order)
    if acceptance is not None:
        return acceptance.accepted_at, ACCEPTED_AT_FROM_ACCEPTANCE
    return order.time_created, ACCEPTED_AT_FROM_ORDER_CREATED


def round_object(order, *, now, item_count=None):
    """The floor contract's round, from one order. Pure over what was selected with
    it: ``placement``, ``placement__visit`` and ``acceptance`` (``select_related``)
    and ``item_count`` (``item_count_expression``, or passed in).

    THERE IS NO PAYMENT FIELD. A round carries nothing about money owed, paid or
    settled: ``value`` is what the round is worth ("to pay" while held, never
    "owed"), and the visit's payment is always ``unknown`` (``visit_object``).
    """
    placement = placement_of(order)
    state = round_state(order)
    accepted_at, accepted_from = accepted_at_of(order)
    held = state == STATE_HELD
    # A held round has been held since it was accepted.
    held_since = accepted_at if held else None
    if item_count is None:
        item_count = getattr(order, 'item_count', 0)
    return {
        'order_id': str(order.pk),
        # Until Step 3 every order is round 1 of its own visit, placed or not.
        'round_number': placement.round_number if placement is not None else FIRST_ROUND,
        'state': state,
        'source': SOURCE_DINER if order.created_by_id is None else SOURCE_STAFF,
        'accepted_at': iso(accepted_at),
        'accepted_at_provenance': accepted_from,
        'held_since': iso(held_since),
        # The placement's value: null while held, and null for an order with no
        # placement (its release was never recorded; the kitchen feeds say the same).
        'released_at': iso(placement.released_at) if placement is not None else None,
        'stale': bool(held and held_since is not None
                      and now - held_since >= STALE_AFTER),
        'fulfilment_status': order.fulfilment_status,
        'item_count': int(item_count or 0),
        'value': format_money(order.actual_cost),
    }


def visit_object(visit):
    """The table's visit, or ``None``. ``payment`` is the CONSTANT ``unknown`` and is
    read from nothing: Step 2 records no payment, so no column (``payment_status``,
    ``total_paid``, ``balance_payable`` …) may be allowed to make a claim here."""
    if visit is None:
        return None
    return {
        'id': str(visit.pk),
        'opened_at': iso(visit.opened_at),
        'payment': PAYMENT_UNKNOWN,
    }


def cannot_be_made(order, live_rows, now):
    """The release makeability check, ``None`` or ``{reason, item_name}``.

    ``purchase_integrity.inspect`` with ``enforce_publication=False``: liveness,
    stock, extras, modifier and allergen declarations, never publication or
    schedule. ``live_rows`` is the round's undeleted ``OrderItem`` population.
    ``reason`` is the integrity classification (``item_sold_out``, ``item_deleted``,
    ``modifier_selection_meaning_changed`` …); ``item_name`` is the line's SAVED name
    (``historical_name``), never today's catalogue name. Shared by Confirm and the
    drawer, so the drawer warns about exactly what Confirm would refuse.
    """
    refusal = purchase_integrity.inspect(
        order, live_rows, now=now, enforce_publication=False)
    if refusal is None:
        return None
    return {
        'reason': refusal.classification,
        'item_name': _saved_name(refusal.item_id, live_rows),
    }


def _saved_name(item_id, rows):
    """The saved name of the line the refusal names (a deliverable one first)."""
    matching = [row for row in rows if str(row.item_id) == str(item_id)]
    matching.sort(key=lambda row: not (row.available and (row.quantity or 0) > 0))
    if not matching:
        return ''
    name, _provenance = historical_name(matching[0])
    return name


# --- the commands -------------------------------------------------------------------

def _parse_uuid(value):
    if isinstance(value, uuid_module.UUID):
        return value
    if not isinstance(value, str):
        raise RoundNotFound()
    try:
        return uuid_module.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise RoundNotFound()


def _locate(order_id, actor, restaurant_id):
    """The LOCATOR: which table to lock, and the order's provenance for ``admit``.
    Nothing it returns is trusted after the locks except those two facts.

    The request's restaurant may only MATCH the order's; the gate is then asked about
    the ORDER's restaurant, before any lock, so a caller with no relationship to it
    cannot make anyone wait.
    """
    restaurant_uuid = _parse_uuid(restaurant_id)
    try:
        located = (
            Order.objects
            .filter(id=order_id, deleted=False)
            .values('id', 'table_id', 'restaurant_id', 'created_by_id')
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        raise RoundNotFound()
    if located is None or located['table_id'] is None:
        raise RoundNotFound()
    if located['restaurant_id'] != restaurant_uuid:
        raise RoundNotFound()
    if not can_user_access_module(actor, located['restaurant_id'], MODULE_TABLES):
        raise RoundNotFound()
    return located


def _lock(located):
    """``Table`` then ``Order``, in two statements, ``of=('self',)`` on both.

    The Order statement selects the placement, its visit and the acceptance through
    LEFT JOINs and annotates ``item_count``, so the round object costs no further
    query; ``of=('self',)`` keeps the lock on the ``orders`` row alone. The joined
    placement is current because the Table lock is a SEPARATE, earlier statement:
    any other writer of it takes ``Table`` first and has committed by now.
    """
    from restaurants_app.models import Table          # lazy: import-cycle free

    locked_table = (
        Table.objects
        .select_for_update(of=('self',))
        .filter(pk=located['table_id'])
        .first()
    )
    if locked_table is None:
        raise RoundNotFound()
    order = (
        Order.objects
        .select_for_update(of=('self',))
        .select_related('placement', 'placement__visit', 'acceptance')
        .annotate(item_count=item_count_expression())
        .filter(pk=located['id'], deleted=False)
        .first()
    )
    if order is None:
        raise RoundNotFound()
    if (order.table_id != locked_table.pk
            or order.restaurant_id != located['restaurant_id']
            or locked_table.restaurant_id != located['restaurant_id']):
        # The locks were chosen from a pre-lock read; the row they protect must
        # still be the row they were chosen for. No production path moves an order
        # between tables or tenants, so this is unreachable today — refused having
        # written nothing, and without reaching for another table out of order.
        logger.error(
            'Floor command refused on a scope mismatch (order_id=%s)', order.pk)
        raise RoundNotFound()
    return locked_table, order


def _assert_authority(actor, order):
    """THE LINEARIZATION POINT for authorization: the ``tables`` module, asked again
    about the ORDER's restaurant on the state as it is after the lock waits. A
    revocation committed before this point is respected; one committing after it can
    still overlap, as at every other boundary that re-asks (D05, D06)."""
    try:
        assert_authority_current(
            StaffAuthority(user=actor, restaurant_id=str(order.restaurant_id),
                           module=MODULE_TABLES))
    except StaffAuthorityError:
        raise RoundNotFound()


def _answer(order, now, *, changed, message):
    return {'round': round_object(order, now=now), 'changed': changed,
            'message': message}


def _refuse(order, now, reason, extra=None):
    return RoundRefusal(reason, round_object(order, now=now), extra)


def _cutoff(restaurant_id):
    """The restaurant's business-day cut-off: one plain read, under the shared
    admission lock (it has no writer)."""
    from restaurants_app.models import Restaurant      # lazy: import-cycle free
    return (
        Restaurant.objects
        .filter(pk=restaurant_id)
        .values_list('business_day_cutoff', flat=True)
        .get()
    )


def confirm(order_id, actor, restaurant_id):
    """RELEASE a held round to the kitchen, or refuse.

    Returns ``{'round', 'changed', 'message'}``; raises ``RoundNotFound`` (404) or
    ``RoundRefusal`` (409). See the module docstring for the order of the checks.
    """
    located = _locate(order_id, actor, restaurant_id)

    with transaction.atomic():
        # ADMISSION FIRST: the shared advisory lock is the top of the lock order.
        # The verdict is APPLIED below, once this is known to be new work.
        verdict = admit(
            restaurant_id=located['restaurant_id'],
            created_by=located['created_by_id'],
            stage=STAGE_RELEASE,
        )
        locked_table, order = _lock(located)
        _assert_authority(actor, order)

        # THE DECISION CLOCK, read after every lock wait.
        now = timezone.now()

        state = round_state(order)
        if state == STATE_RELEASED:
            # Resent, or released by someone else first: the round is where the
            # command wanted it. Nothing is written.
            return _answer(order, now, changed=False, message=OUTCOME_UNCHANGED)
        if state == STATE_DECLINED:
            raise _refuse(order, now, REASON_ROUND_DECLINED)
        if state != STATE_HELD:
            raise _refuse(order, now, REASON_ROUND_NOT_HELD)

        if not verdict.allowed:
            logger.info('Round release refused (order_id=%s, code=%s)',
                        order.pk, verdict.code)
            raise _refuse(order, now, REASON_RESTAURANT_NOT_TRADING)

        operational = eligibility.evaluate(
            eligibility.facts_from_verdict(verdict, locked_table),
            order.created_by_id,
        )
        if not operational.allowed:
            raise _refuse(order, now, ELIGIBILITY_REASONS[operational.reason])

        live_rows = list(OrderItem.objects.filter(order=order, deleted=False))
        blocked = cannot_be_made(order, live_rows, now)
        if blocked is not None:
            raise _refuse(order, now, REASON_CANNOT_BE_MADE, extra={
                'item_name': blocked['item_name'],
                'cannot_be_made': blocked,
            })

        placement = placement_of(order)
        placement.released_at = now
        placement.released_by_id = getattr(actor, 'pk', None)
        # The business day the round was RELEASED in: the design dates accepted
        # order value and completed sales by it. Until now it held the acceptance
        # day provisionally.
        placement.business_date = business_date_for(now, _cutoff(order.restaurant_id))
        placement.save(update_fields=['released_at', 'released_by', 'business_date'])

        return _answer(order, now, changed=True, message=OUTCOME_CONFIRMED)


def decline(order_id, actor, restaurant_id):
    """CANCEL a held round that was never released, or refuse.

    Takes no reason from the client: the server records ``declined_by_staff``.
    Returns ``{'round', 'changed', 'message'}``; raises ``RoundNotFound`` (404) or
    ``RoundRefusal`` (409).
    """
    located = _locate(order_id, actor, restaurant_id)

    with transaction.atomic():
        # No admission lock: declining admits no new work.
        locked_table, order = _lock(located)
        _assert_authority(actor, order)
        now = timezone.now()

        state = round_state(order)
        if state == STATE_DECLINED:
            return _answer(order, now, changed=False, message=OUTCOME_UNCHANGED)
        if state == STATE_RELEASED:
            raise _refuse(order, now, REASON_ROUND_RELEASED)
        if state != STATE_HELD:
            raise _refuse(order, now, REASON_ROUND_NOT_HELD)

        order.order_status = OrderStatus_Cancelled
        order.cancelled_at = now
        order.cancelled_by_id = getattr(actor, 'pk', None)
        order.cancellation_reason = CancellationReason_DeclinedByStaff
        # A kitchen client holding this round's revision is now stale. A held round
        # was never commandable (`order_held`), so this is belt and braces — but the
        # revision versions the whole kitchen-order state, cancellation included.
        order.fulfilment_revision = order.fulfilment_revision + 1
        # FIELD-SCOPED: a full save would carry every column read under the lock back
        # over itself, and the placement is a separate row that is not touched.
        order.save(update_fields=[
            'order_status', 'cancelled_at', 'cancelled_by', 'cancellation_reason',
            'fulfilment_revision', 'time_last_updated',
        ])

        return _answer(order, now, changed=True, message=OUTCOME_DECLINED)
