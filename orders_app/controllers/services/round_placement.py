"""
Dining model, Step 1 — writing a just-accepted order's visit, bill and placement.

A VISIT is one party's stay at one table; a BILL is what that party owes (one per visit
in the first release); an ``Order`` is one ROUND on that bill, and ``OrderPlacement``
records where it sits. The schema is PR 1 (``orders_app/0043``, ``restaurants_app/
0060``). This module is the ONLY production writer of those three tables, and
``orders_app/tests_dining_schema.py`` fails the build if a module outside its stated
allowlist starts referencing them.

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
- one ``OrderPlacement``: ``released_at`` = the acceptance instant, OR NULL for a
  round HELD at acceptance (Step 2, PR BE-F, below); ``business_date`` from that
  instant and the restaurant's cut-off; ``round_number`` 1.

COST: one plain read (the cut-off and the payment timing, together, with no lock) and
three INSERTs. Submit goes 14 -> 18 queries; pinned in ``tests_order_acceptance``. A
held round costs the same 18 (``tests_round_hold``).

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

THE RELEASE RULE (Step 2, PR BE-C). A round reaches the kitchen only once it is
RELEASED: it has NO placement (it predates Step 1, or was accepted with the writer off)
or its placement's ``released_at`` is set. A HELD round is ``order_status='pending'``
with a placement whose ``released_at`` is NULL. ``released_round_q`` is the rule, written
once; ``round_is_held`` is its Python twin for one round in hand, and a test holds the
two equal over every shape.

HOLDING A ROUND AT ACCEPTANCE (Step 2, PR BE-F). ``place_first_round`` writes
``released_at`` NULL, so the round is HELD until staff Confirm it on the floor
(``round_release.confirm``), when ``held_at_acceptance`` says so, and the acceptance
instant otherwise. A round is held if, and only if, ALL THREE hold:
- ``DINING_ROUND_HOLDS_ENABLED`` is on (default OFF; ``holds_enabled``);
- the visit's copied timing is ``pay_after``;
- the ORDER was created by a diner (``created_by_id`` is NULL). The hold follows the
  order's creator, as D06's provenance does, never whoever submitted it, so a member
  of staff submitting a diner's draft still gets a held round.
Everything else is released at acceptance exactly as before: a staff-created order,
``not_chosen`` (no configuration row, or the axis unset) and ``pay_first`` (refused
for new values by BE-B until Step 4; a restaurant already on it releases at once).
With the visit writer off there is no placement, so the round is released. A held
round's ``business_date`` provisionally holds the acceptance business day, which
Confirm re-stamps at release. Nothing releases or expires a held round on a clock:
``stale`` is a flag only. Confirm and Decline read no switch, so a held round can
always be cleared. The decision uses only facts the writer already has (the timing
from its one read, ``order.created_by_id``), so it adds no query. THE SWITCH IS
REMOVED, OR DEFAULTS TO ON, BEFORE STEP 3.

HELD ROUNDS AT A RESTAURANT (Step 2, PR BE-G). ``held_rounds`` is the ONE definition
of "the rounds waiting for staff at this restaurant": a placement whose ``released_at``
is NULL, on an order that is neither cancelled nor deleted. Two readers count through
it: the kitchen active feed's ``waiting_for_staff`` (``held_round_count``) and the
bell's ``round_waiting`` notification (``held_rounds_summary``). When a round is HELD
here, ``place_first_round`` schedules ``notifications_app.producers.
notify_round_waiting`` after commit; a released round schedules nothing.

THE DINER'S VIEW OF A ROUND (Step 2, PR BE-E, diner round state v1). ``diner_round``
is the ONE derivation of ``{state, held_since, stale}`` the diner is told, read by the
correlated checkout projection (``acceptance_result``) and so by every response that
publishes it. ``held_lookups`` / ``held_from_row`` are the same held rule for the
table scan's one ``.values()`` read. All of it is pure over what was selected with the
order: no query is added anywhere.
"""
from datetime import time, timedelta

from django.conf import settings
from django.db.models import Count, Exists, Min, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from commercial_app.models import PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST
from dinify_backend.configss.string_definitions import (
    CancellationReason_DeclinedByStaff, OrderStatus_Cancelled,
)
from notifications_app import producers
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
    admission lock, so neither a ``restaurants`` PUT nor the payment-timing writer
    (``commercial_app.service_configuration.set_payment_timing``, which takes the
    exclusive one before the ``Restaurant`` row since Step 2, PR BE-B) can change the
    cut-off or the timing underneath it: a timing write either committed before this
    acceptance was admitted, or waits for it to commit. The source of the timing lives
    only here, so a later per-dining-area timing changes this function and nothing
    else.
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


def holds_enabled():
    """The holds switch (Step 2, PR BE-F), read at call time like ``writes_enabled``.

    OFF by default: with it off every round is released at acceptance, whatever the
    restaurant's payment timing. Removed, or defaulted to on, before Step 3.
    """
    return bool(getattr(settings, 'DINING_ROUND_HOLDS_ENABLED', False))


def held_at_acceptance(*, holds_on, payment_timing, created_by_id):
    """THE decision: is a round being accepted now held for staff? Pure.

    ``holds_on`` is the switch's value; ``payment_timing`` the timing the visit
    records (``resolve_payment_timing``'s result); ``created_by_id`` the ORDER's
    creator (``None`` for a diner's order), never the submitter.

    Held if, and only if, the switch is on, the timing is ``pay_after`` and a diner
    created the order. ``pay_first`` and ``not_chosen`` are released at acceptance
    (pay-first is enforced only from Step 4), and so is every staff-created order.
    """
    return (
        bool(holds_on)
        and payment_timing == PAYMENT_TIMING_PAY_AFTER
        and created_by_id is None
    )


def place_first_round(order, accepted_at):
    """Write the visit, bill and placement for an order being accepted now.

    ``order`` MUST be the row ``_submit_order`` re-read under its Table and Order
    locks; ``accepted_at`` MUST be the instant the acceptance records
    (``OrderAcceptance.accepted_at``), so the released time and the business date
    describe the moment the order was accepted. Returns the placement, or ``None``
    when the switch is off (nothing is read or written).

    The placement is released at ``accepted_at`` unless ``held_at_acceptance`` says
    the round is held, when ``released_at`` is NULL and the round waits for the
    floor's Confirm. The business date is the acceptance's either way (for a held
    round, provisionally: Confirm re-stamps it). The placement is cached on
    ``order`` by the create, so the submit reply's ``current.round`` reads it with
    no query.
    """
    if not writes_enabled():
        return None

    cutoff, configured = _cutoff_and_timing(order.restaurant_id)
    payment_timing = resolve_payment_timing(configured)
    held = held_at_acceptance(
        holds_on=holds_enabled(),
        payment_timing=payment_timing,
        created_by_id=order.created_by_id,
    )

    visit = Visit.objects.create(
        restaurant_id=order.restaurant_id,
        table_id=order.table_id,
        opened_at=accepted_at,
        opened_by_id=order.created_by_id,
        payment_timing=payment_timing,
    )
    bill = Bill.objects.create(visit=visit)
    placement = OrderPlacement.objects.create(
        order=order,
        visit=visit,
        bill=bill,
        payment_timing=payment_timing,
        released_at=None if held else accepted_at,
        business_date=business_date_for(accepted_at, cutoff),
        round_number=FIRST_ROUND,
    )
    if held:
        # The bell (Step 2, PR BE-G): after commit and best-effort, so a rolled-back
        # acceptance announces nothing and a producer failure never changes the
        # submit response. A released round schedules nothing.
        producers.schedule(producers.notify_round_waiting, order.restaurant_id)
    return placement


def released_round_q():
    """A filter over ``Order`` matching the rounds the kitchen may see: no placement,
    or a placement whose ``released_at`` is set.

    THE "NO PLACEMENT" ARM IS LOAD-BEARING: every order accepted before Step 1, or while
    ``DINING_VISIT_WRITES_ENABLED`` was off, has no placement and must stay on the board.
    Both arms sit in ONE ``Q`` so they share one LEFT JOIN to ``order_placements``,
    which ``select_related('placement')`` then reuses: no query is added anywhere.

    NEVER REVERT A CONSUMER OF THIS WHILE HELD ROUNDS EXIST: without it a held round
    reaches the kitchen board before it was released.
    """
    return Q(placement__isnull=True) | Q(placement__released_at__isnull=False)


def held_rounds(restaurant_id):
    """THE held rounds at one restaurant, as an ``Order`` queryset: a placement whose
    ``released_at`` is NULL, on an order neither cancelled nor deleted.

    Written ONCE, for the kitchen active feed's ``waiting_for_staff`` and the bell's
    ``round_waiting`` notification. ``placement__isnull=False`` sits in the same
    ``filter`` as the ``released_at`` test, so the join is an INNER one and an order
    with NO placement is never counted (it is released, as ``round_is_held`` says).
    A declined round keeps its NULL ``released_at`` but is cancelled, so it is out.
    """
    return (
        Order.objects
        .filter(
            restaurant_id=restaurant_id,
            deleted=False,
            placement__isnull=False,
            placement__released_at__isnull=True,
        )
        .exclude(order_status=OrderStatus_Cancelled)
    )


def held_round_count(restaurant_id):
    """How many rounds are waiting for staff at the restaurant: ONE ``COUNT``."""
    return held_rounds(restaurant_id).count()


def held_rounds_summary(restaurant_id):
    """``{'waiting', 'oldest_held_since', 'latest_table'}`` for the bell.

    ``waiting`` is ``held_round_count``'s number, from the same queryset.
    ``oldest_held_since`` is the earliest ``held_since`` (an aware datetime, or
    ``None``) and ``latest_table`` the display name of the table of the most recently
    held round (``display_name``, else ``str_number``, else the number, as the kitchen
    ticket and the floor label it), or ``None``. A round has been held since it was
    accepted (``OrderAcceptance.accepted_at``), falling back to the order's creation,
    as the floor and ``diner_round`` do. One aggregate, plus one row read when
    anything is waiting. Nothing about a diner is read.
    """
    rounds = held_rounds(restaurant_id).annotate(
        held_since=Coalesce('acceptance__accepted_at', 'time_created'),
    )
    totals = rounds.aggregate(waiting=Count('pk'), oldest=Min('held_since'))
    latest_table = None
    if totals['waiting']:
        latest = (
            rounds
            .order_by('-held_since', '-pk')
            .values('table__display_name', 'table__str_number', 'table__number')
            .first()
        )
        if latest is not None:
            latest_table = (
                latest['table__display_name'] or latest['table__str_number']
                or (None if latest['table__number'] is None
                    else str(latest['table__number']))
            )
    return {
        'waiting': int(totals['waiting'] or 0),
        'oldest_held_since': totals['oldest'],
        'latest_table': latest_table,
    }


def round_is_held(has_placement, released_at):
    """THE held rule for one round, the Python twin of ``~released_round_q()``.

    Held means a placement exists AND its ``released_at`` is unset. An order with no
    placement is never held. Pure, so it can judge both a locked row and a pre-lock
    ``.values()`` read.
    """
    return bool(has_placement) and released_at is None


def placement_of(order):
    """The order's placement, or ``None`` when it has none.

    Select ``placement`` with the order (``select_related('placement')``): Django then
    caches the absence too, and this costs no query either way.
    """
    try:
        return order.placement
    except OrderPlacement.DoesNotExist:
        return None


def released_at_of(order):
    """The placement's ``released_at``, or ``None`` when the order has no placement.

    ``None`` therefore means "no placement" OR "held"; ask ``is_held`` to tell them
    apart.
    """
    placement = placement_of(order)
    return None if placement is None else placement.released_at


def is_held(order):
    """True when ``order`` is a held round (see ``round_is_held``)."""
    placement = placement_of(order)
    return round_is_held(
        placement is not None,
        None if placement is None else placement.released_at,
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


# --- the diner's view of a round (Step 2, PR BE-E) --------------------------------

#: The three words the diner is told (diner round state v1). Deliberately fewer than
#: the floor's: a served, kitchen-cancelled or draft order is ``released`` here,
#: because the diner's screens already describe those through ``current``.
DINER_ROUND_HELD = 'held'
DINER_ROUND_DECLINED = 'declined'
DINER_ROUND_RELEASED = 'released'

#: A held round is STALE once ``now - held_since >= HELD_STALE_AFTER``: the exact
#: threshold instant is stale. A flag only; nothing acts on it. The same value as the
#: floor's ``round_release.STALE_AFTER`` (a test holds the two equal), so the floor
#: and the diner's phone call a round stale at the same moment.
HELD_STALE_AFTER = timedelta(minutes=15)


def diner_round(order, *, accepted_at, now):
    """``{'state', 'held_since', 'stale'}`` for one order: THE rule, written once.

    Pure. Select ``placement`` with the order (``select_related('placement')``, or
    the instance a placement was just created for), so this costs no query.
    ``accepted_at`` is the order's ``OrderAcceptance.accepted_at`` or ``None``;
    ``now`` is the caller's clock.

    - ``declined``: cancelled, and the reason is ``declined_by_staff`` (the floor's
      Decline). Cancelled for any other reason is NOT declined: that is the kitchen
      cancelling an accepted order, which the diner is told as today, through
      ``current.order_status``.
    - ``held``: ``is_held(order)`` and not cancelled. An order with NO placement is
      never held (it predates Step 1, or the writer was off), as for the kitchen.
    - ``released``: everything else.

    ``held_since`` is the acceptance instant for a held round (falling back to the
    order's creation, as the floor does, for a held round with no recorded
    acceptance, which no production path produces), ``null`` otherwise. It is
    rendered with ``isoformat()``, exactly as ``acceptance_result`` renders
    ``acceptance.accepted_at``, so the two are byte-identical for a held round.
    """
    cancelled = order.order_status == OrderStatus_Cancelled
    if cancelled and order.cancellation_reason == CancellationReason_DeclinedByStaff:
        state = DINER_ROUND_DECLINED
    elif not cancelled and is_held(order):
        state = DINER_ROUND_HELD
    else:
        state = DINER_ROUND_RELEASED
    held_since = None
    if state == DINER_ROUND_HELD:
        held_since = accepted_at if accepted_at is not None else order.time_created
    return {
        'state': state,
        'held_since': None if held_since is None else held_since.isoformat(),
        'stale': bool(held_since is not None
                      and now - held_since >= HELD_STALE_AFTER),
    }


def held_lookups():
    """The two ``.values()`` lookups ``held_from_row`` needs, joined into the read
    that already selects the order (one LEFT JOIN, no query added)."""
    return ('placement__id', 'placement__released_at')


def held_from_row(row):
    """``round_is_held`` over a ``.values()`` row carrying ``held_lookups()``."""
    return round_is_held(
        row.get('placement__id') is not None, row.get('placement__released_at'),
    )
