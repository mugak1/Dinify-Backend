"""
Dining model, Step 1 — writing a just-accepted order's visit, bill and placement.

A VISIT is one party's stay at one table; a BILL is what that party owes (one per visit
in the first release); an ``Order`` is one ROUND on that bill, and ``OrderPlacement``
records where it sits. The schema is PR 1 (``orders_app/0043``, ``restaurants_app/
0060``). This module is the ONLY production writer of those three tables, and
``orders_app/tests_dining_schema.py`` fails the build if any other module starts
referencing them.

WHEN IT WRITES. Exactly once per order: on its FIRST acceptance, inside
``manage_order._submit_order``'s transaction, after every refusal and every replay has
already returned, and before the order flips to ``pending``. So a retried submit (D04
replay), a refused one and a draft that is never submitted write nothing, and the rows
commit with the acceptance or not at all. A fault here fails the acceptance and rolls
it back; that is accepted, and ``DINING_VISIT_WRITES_ENABLED`` is the remedy.

WHAT IT WRITES, until Steps 2 and 3:
- one ``Visit`` per order (round 1). Restaurant and table are copied from the ORDER
  ROW RE-READ UNDER THE LOCK, never from a request or an earlier instance; tenant
  isolation depends on it. ``opened_by`` is the order's ``created_by`` (the staff
  member who placed a staff order, NULL for a diner's), never whoever submitted it;
- one ``Bill`` for that visit. It stores no amount: totals are computed, never stored
  (non-custodial);
- one ``OrderPlacement``: ``released_at`` = the acceptance instant (Step 2 holds
  rounds), ``business_date`` from that instant and the restaurant's cut-off,
  ``round_number`` 1.

COST: one plain read (the cut-off and the payment timing, together, with no lock) and
three INSERTs. Submit goes 14 -> 18 queries; pinned in ``tests_order_acceptance``.

LOCKS. The INSERTs come after the Table and Order row locks the acceptance already
holds. New rows are invisible to every other transaction until COMMIT, so they cannot
be waited on. Their foreign keys are checked at COMMIT (Django creates them
``DEFERRABLE INITIALLY DEFERRED``) and take ``FOR KEY SHARE`` on the referenced
``restaurants``, ``tables`` and ``users`` rows. The Table row is already ours; the
Restaurant key share is new to submit (the create path has always taken it). See the
lock-order table in ``restaurants_app/controllers/admission_lock.py``.

NOTHING HERE READS A VISIT ON A HOT PATH. The kitchen keeps ``Table -> Order``, and no
kitchen command writes these tables: until Step 3 a visit's state is DERIVED from its
one order (``visit_state`` below).
"""
from datetime import time, timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from commercial_app.models import PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST
from orders_app.controllers.occupancy import ongoing_order_q
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN, Bill, Order, OrderPlacement, Visit,
)

#: Until Step 3 every order is its own visit's first and only round.
FIRST_ROUND = 1

#: A Step 1 visit's payment state. Nothing records payment yet, so an ended visit
#: cannot claim it was paid, and an open one cannot claim it was not.
PAYMENT_UNKNOWN = 'unknown'
VISIT_OPEN = 'open'
VISIT_ENDED = 'ended'


def business_date_for(accepted_at, cutoff):
    """The business date an order accepted at ``accepted_at`` belongs to.

    Pure. ``accepted_at`` is an aware instant; ``cutoff`` is the restaurant's
    ``business_day_cutoff`` (a ``datetime.time``).

    THE RULE: take the instant in the DEFAULT time zone (``settings.TIME_ZONE``,
    Africa/Nairobi), never whichever zone happens to be active for the request. At or
    after the cut-off on that local day, the business date is that day; before it, the
    previous day. The cut-off itself starts the new day (04:00:00.000000 is the new
    day; 03:59:59.999999 is the previous one), the same inclusive start section
    schedules use. A cut-off of 00:00 therefore always gives the local calendar day.
    """
    if timezone.is_naive(accepted_at):
        raise ValueError('business_date_for needs an aware datetime.')
    if not isinstance(cutoff, time):
        raise TypeError('business_date_for needs a datetime.time cut-off.')
    local = timezone.localtime(accepted_at, timezone.get_default_timezone())
    if local.time() >= cutoff:
        return local.date()
    return local.date() - timedelta(days=1)


def resolve_payment_timing(configured):
    """The timing a visit records, from the restaurant's configured value.

    ``configured`` is ``RestaurantServiceConfiguration.payment_timing`` read through a
    LEFT JOIN, so it is ``None`` both when the configuration row does not exist and
    when the axis is unset. Both mean nobody chose, recorded as ``not_chosen``.

    Anything outside the vocabulary is refused rather than mapped to ``not_chosen``:
    the source column is constrained to the same two values, so an unknown one is a
    data fault, and silently recording "nobody chose" would hide it.
    """
    if configured is None:
        return PAYMENT_TIMING_NOT_CHOSEN
    if configured in (PAYMENT_TIMING_PAY_FIRST, PAYMENT_TIMING_PAY_AFTER):
        return configured
    raise ValueError('Unrecognised payment timing in the service configuration.')


def _cutoff_and_timing(restaurant_id):
    """ONE plain read, no lock: the cut-off and the configured payment timing.

    Taken inside the acceptance transaction, after ``admit()`` has taken the shared
    admission lock, so a ``restaurants`` PUT (which takes the exclusive one) cannot
    change the cut-off underneath it. The payment-timing writer does not take that
    lock yet; Step 2 makes it, when timing starts to matter at acceptance. The source
    of the timing lives only here, so a later per-dining-area timing changes this
    function and nothing else.
    """
    from restaurants_app.models import Restaurant
    return (
        Restaurant.objects
        .filter(pk=restaurant_id)
        .values_list('business_day_cutoff', 'service_configuration__payment_timing')
        .get()
    )


def writes_enabled():
    """The switch, read at call time so a restart is all it takes to change it."""
    return bool(getattr(settings, 'DINING_VISIT_WRITES_ENABLED', True))


def place_first_round(order, accepted_at):
    """Write the visit, bill and placement for an order being accepted now.

    ``order`` MUST be the row ``_submit_order`` re-read under its Table and Order
    locks; ``accepted_at`` MUST be the instant the acceptance records
    (``OrderAcceptance.accepted_at``), so the released time and the business date
    describe the moment the order was accepted. Returns the placement, or ``None``
    when the switch is off (nothing is read or written).
    """
    if not writes_enabled():
        return None

    cutoff, configured = _cutoff_and_timing(order.restaurant_id)
    payment_timing = resolve_payment_timing(configured)

    visit = Visit.objects.create(
        restaurant_id=order.restaurant_id,
        table_id=order.table_id,
        opened_at=accepted_at,
        opened_by_id=order.created_by_id,
        payment_timing=payment_timing,
    )
    bill = Bill.objects.create(visit=visit)
    return OrderPlacement.objects.create(
        order=order,
        visit=visit,
        bill=bill,
        payment_timing=payment_timing,
        released_at=accepted_at,
        business_date=business_date_for(accepted_at, cutoff),
        round_number=FIRST_ROUND,
    )


def open_visits_q():
    """A filter over ``Visit`` matching the visits that are open now.

    Until Step 3 a visit is open exactly when one of its orders meets today's
    occupancy rule (``occupancy.ongoing_order_q``, the ONE definition of "is the table
    busy?"). It is read from the orders, so nothing is written on serve, cancel,
    recall or soft delete.
    """
    return Exists(
        Order.objects.filter(ongoing_order_q(), placement__visit=OuterRef('pk'))
    )


def visit_state(visit):
    """``{'state': 'open'|'ended', 'payment': 'unknown'}`` for one visit.

    One query. An ended Step 1 visit reads payment "unknown": nothing records
    payment, and the kitchen's serve is not a settlement.
    """
    is_open = Visit.objects.filter(pk=visit.pk).filter(open_visits_q()).exists()
    return {
        'state': VISIT_OPEN if is_open else VISIT_ENDED,
        'payment': PAYMENT_UNKNOWN,
    }
