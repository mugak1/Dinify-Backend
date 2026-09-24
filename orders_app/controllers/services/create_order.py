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
from orders_app.controllers.services.order_authority import (
    StaffAuthorityError, assert_authority_current,
)
from orders_app.controllers.services.order_intent import (
    REFUSALS, fingerprint, resolve_intent,
)
from orders_app.controllers.services.catalogue_snapshot import build_snapshot
from orders_app.controllers.services import order_eligibility as eligibility
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED,
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


#: The constraint that makes one key one order, restaurant-wide. Named here so
#: the recovery below can tell THAT conflict from any other IntegrityError
#: rather than relabelling an unexpected failure an idempotent success.
INTENT_KEY_CONSTRAINT = 'uniq_order_restaurant_client_order_id'


def _is_intent_key_conflict(error):
    """Did this IntegrityError come from the intent-key uniqueness rule?

    Matched on the constraint NAME, which psycopg surfaces on the diagnostics
    of the underlying error and repeats in its text. Anything else — a NOT
    NULL violation, a foreign key, the daily-number constraint — is a
    different failure and must propagate.
    """
    diagnostics = getattr(getattr(error, '__cause__', None), 'diag', None)
    if getattr(diagnostics, 'constraint_name', None) == INTENT_KEY_CONSTRAINT:
        return True
    return INTENT_KEY_CONSTRAINT in str(error)


def _insert_order(*, restaurant, table, customer, created_by, order_source,
                  client_order_id, request_fingerprint, order_number,
                  order_date, verdict):
    """The order row itself. Extracted so the allocation and the INSERT can
    share one savepoint without burying either in a long block."""
    return Order.objects.create(
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

        # WHAT THAT KEY IS BOUND TO (D04). Written in the same statement as the
        # key, so an order can never hold one without the other: a row with a
        # key and no binding would be indistinguishable from a pre-D04 row and
        # would fall into the undecidable branch forever.
        request_fingerprint=request_fingerprint,

        order_number=order_number,
        order_date=order_date,
        fulfilment_status='new',
        fulfilment_status_updated_at=timezone.now(),

        # An order is test if EITHER of two independent things is
        # true, and neither of them is anything the caller sent:
        #
        #   TENANT   — the restaurant itself exists for testing
        #              (`Restaurant.is_test`), so a test restaurant
        #              that has gone `live` still writes test orders.
        #   LIFECYCLE — the order predates go-live, so it is a
        #              rehearsal.
        #
        # FLAGGED IS ALL IT MEANS AT A TEST RESTAURANT. The flag keeps
        # these orders identifiable; it does not switch anything off
        # there — a test restaurant's orders count in its reports and
        # dashboards, can be reviewed and are matched to customers
        # exactly like a live restaurant's. Only a PRACTICE order (a
        # test order at a REAL restaurant) is left out, and that rule
        # lives in ONE place: `orders_app.controllers.test_orders`.
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

        # The corrected calculation convention (D02). Written
        # HERE, by the service that actually performs it, inside
        # the same atomic operation that persists the corrected
        # parent and child values — so an order is certified
        # corrected only if its rows really were. It is never
        # accepted from a client and is absent from every
        # serializer and from EDIT_INFORMATION.
        pricing_version=PRICING_VERSION_CORRECTED,
    )


def _table_row_now(table_pk):
    """The table AS IT IS NOW, with no lock taken.

    For the one question a replay needs answered — has the QR generation moved?
    A plain statement takes its own snapshot under READ COMMITTED, so a
    regeneration that has COMMITTED is visible; one committing a moment later is
    not, and that is stated rather than claimed away, exactly as the acceptance
    boundary states it about the point its own checks linearize at.

    ``None`` when the row is gone, which `assert_capability_current` reads as a
    revocation — correctly: a table a diner's session names and that no longer
    exists is not a table they hold a session for.
    """
    from django.core.exceptions import ValidationError as DjangoValidationError
    from restaurants_app.models import Table
    try:
        return Table.objects.get(pk=table_pk)
    except (Table.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        # A missing row, or a pk that is not one — both mean "not a table this
        # capability can still be current against", which is what the caller
        # reads ``None`` as.
        #
        # NARROW ON PURPOSE. This used to be a bare ``except Exception``, which
        # also swallowed ``OperationalError``, ``InterfaceError`` and
        # ``ProgrammingError`` — a lost connection, a statement timeout or a
        # query defect — and reported every one of them as the capability
        # channel's opaque 404. That is the same answer a REVOKED session gets,
        # so an outage would have read to a diner as "your table session is no
        # longer valid" and to us as an authorization event rather than the
        # infrastructure failure it was. An unexpected database error is not
        # evidence about authority and must propagate to ordinary error
        # handling, exactly as it does on every other read in this service.
        return None


def _once(produce):
    """Memoise a zero-argument producer.

    The replay branch asks TWO questions about the same non-locking snapshot of
    the table — is the capability still current, and would this table still mint
    the session at all — and neither may pay for the read when no capability was
    presented. Fetching twice would cost a second query AND, worse, let the two
    answers describe two different moments, which is the half-observed snapshot
    `catalogue_snapshot` exists to rule out.
    """
    cache = {}

    def _get():
        if 'row' not in cache:
            cache['row'] = produce()
        return cache['row']

    return _get


def _create_order(*, restaurant, table, items,
                  customer=None, created_by=None,
                  order_source=ORDER_SOURCE_DINER, client_order_id=None,
                  capability=None, authority=None):
    """
    Create an order (and its items) atomically.

    Returns ``{'status': 200, 'order': <Order>, 'idempotent': bool}`` on
    success, or ``{'status': 400, 'message': ..., 'data': {...}}`` when
    table-gating rejects a genuinely new submission, or the 400 dict from the
    item chokepoint when any item/extra is rejected — in which case the WHOLE
    transaction has rolled back (no order, no items, no counter increment; see
    OrderItemRejected).

    A1 — ``capability`` AND ``authority`` ARE THE AUTHORITY THIS REQUEST
    PRESENTED, carried here so it can be RE-ASKED where the decision is made.

    The acceptance boundary has done this since D06/G1b; creation had neither.
    The endpoint resolves a diner's table session, or gates a staff caller on
    the ``tables`` module, IN AUTOCOMMIT — and this transaction then waits for
    the admission advisory lock and the table row. A QR regeneration, a
    membership deactivated, a role removed or a restaurant leaving the
    portal-access states committing inside that wait revokes exactly the
    authority this request is still acting on, and nothing here noticed: the
    draft was written, a daily ticket number was spent on it, and only the
    acceptance boundary later refused it.

    BOTH ARE CONTEXT, NOT AUTHORITY, and neither carries a credential. They are
    conclusions a verifying step already drew, so re-checking them can only ever
    REFUSE — never admit a request the door did not. ``None`` means that channel
    was not used and the check is a no-op, so an in-process caller keeps exactly
    the guarantees it had.

    NOTHING HERE IS TAKEN FROM A REQUEST BODY. The capability's facts come from
    the table a session resolved to; the authority names the principal the
    endpoint authorized and the module it gated on, and its restaurant is
    cross-checked against the row THIS service loaded before it is trusted.
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

    #    WHAT THAT KEY IS FOR (D04), computed HERE and not accepted from the
    #    caller — for the same reason the validation above is repeated: this
    #    service is a supported entrypoint and must not depend on its caller.
    #    It is the SAME pure function over the SAME validated lines, so the
    #    controller's preflight and this cannot disagree.
    #
    #    THE POSITION IN THIS FUNCTION IS THE CONTRACT. It is taken from the
    #    D01-validated request and BEFORE `normalize_order_items` at step 2c,
    #    which reorders choices into menu-definition order against the live
    #    catalogue. A fingerprint taken after that would change when the MENU
    #    changed, and a diner recovering a lost response would be told their
    #    purchase was different because the restaurant edited a dish.
    request_fingerprint = fingerprint(items)

    # A1 — ONE RE-ASSERTION, ASKED AT EVERY POINT THAT DISCLOSES OR WRITES.
    #
    # Written once and called from both points rather than inlined at each,
    # because that is exactly how an invariant ends up added to one branch and
    # not the others — the lesson `_submit_order` records about its own pair of
    # branches. It returns a refusal dict or `None`, so callers stay in this
    # service's established plain-dict contract.
    #
    # THE ORDER IS CAPABILITY THEN STAFF, matching the acceptance boundary, so
    # both channels linearize at one point wherever this is called. Each answers
    # with ITS OWN non-disclosing 404 — the diner channel's, and the endpoint's
    # — so a revocation landing mid-request is indistinguishable from a
    # principal that never had access, and this cannot become an oracle.
    def _authority_refusal(table_row):
        # Imported here for the same reason the Table import below is: this
        # module sits in the middle of the order import graph.
        from restaurants_app.controllers import diner_capability
        from restaurants_app.controllers.diner_capability import (
            DinerCapabilityError,
        )
        # `table_row` MAY BE A CALLABLE, and is evaluated only when a capability
        # channel was actually used. The row is needed by the capability half
        # and by nothing else, so a caller that presented none — a staff request,
        # or an in-process caller — must not pay a query for a check that is a
        # no-op for it. That is what keeps the pinned replay budget where it was
        # for everyone except the one caller the check is about.
        if capability is not None:
            row = table_row() if callable(table_row) else table_row
            try:
                diner_capability.assert_capability_current(capability, row)
            except DinerCapabilityError as exc:
                return {'status': exc.status, 'message': exc.message}
        if authority is not None and str(authority.restaurant_id) != str(
                restaurant.pk):
            # A DEFENSIVE IDENTITY CHECK, the staff counterpart of the one
            # `assert_capability_current` makes. The endpoint authorized a
            # principal against ONE restaurant; this service loaded the
            # restaurant it is about to create at. If the two are not the same
            # row, the carried context is not about this order and re-asking the
            # module gate would authorize the wrong thing. That is what makes
            # the restaurant SERVER-DERIVED here rather than merely
            # server-checked at the door.
            return {'status': 404, 'message': 'Not found'}
        try:
            assert_authority_current(authority)
        except StaffAuthorityError as exc:
            return {'status': exc.status, 'message': exc.message}
        return None

    # A1b — WOULD THIS TABLE STILL MINT THE SESSION THE CALLER IS HOLDING?
    #
    # A SEPARATE NAMED PREDICATE, asked on exactly ONE branch, and the scoping
    # is the whole design rather than an oversight.
    # `assert_capability_current` re-checks the QR GENERATION and deliberately
    # nothing else, because a table taken out of service is an OPERATIONAL fact
    # that binds every provenance and `order_eligibility` owns it — answering it
    # in the capability channel too would give one fact two answers depending on
    # how the caller authenticated. That holds wherever an eligibility rule runs,
    # which on the CREATE path is step 1e, on the locked row.
    #
    # THE REPLAY RETURN IS THE BRANCH WHERE NONE RUNS. It is exempt from every
    # new-order rule by design, so it never reaches 1e and this fact reached
    # nothing at all — while the return itself is a disclosure: an existing order
    # and, since G3a, the closure recorded against it. `_resolve_table` re-reads
    # `is_available_for_scan()` live on every use and treats a table that has
    # stopped being scannable as a REVOKED SESSION, answering this channel's
    # opaque 404, so a request reaching here at all is one where the table went
    # out of service after that resolution. The answer is that same 404, which is
    # what keeps it stable across the wait instead of depending on when the
    # operator happened to click. `retire_quote_for_review` asks the identical
    # question under its lock for the identical reason (G1b).
    #
    # `True` for a caller with no capability, so a staff or in-process replay is
    # untouched: there is no session for a table to revoke, and this must never
    # become a new requirement to present one.
    def _session_refusal(table_row):
        from restaurants_app.controllers import diner_capability
        if capability is None:
            return None
        row = table_row() if callable(table_row) else table_row
        if diner_capability.session_still_admissible(capability, row):
            return None
        # THE CHANNEL'S OWN ENVELOPE, DERIVED — never a second literal. The
        # door renders `DinerCapabilityDenied` as `exc.message`, and the
        # deployed client matches that body exactly to decide whether a 404
        # means "your QR is dead, rescan"; a periodless copy here was not
        # recognised, so a revocation landing inside the lock wait showed the
        # diner no rescan panel at all.
        return diner_capability.denial_envelope()

    # The try/except sits AROUND the atomic block (the same idiom as the two
    # nested savepoints inside it): OrderItemRejected must unwind through
    # atomic.__exit__ so the whole transaction rolls back BEFORE it is
    # translated back into the plain-dict error envelope.
    try:
        with transaction.atomic():
            # 1. THE INTENT, FIRST — before gating or creation.
            #
            #    It used to be a bare `(restaurant, key)` lookup that returned
            #    whatever it found, so a request naming three burgers received
            #    the one-burger order the key had been used for, and a request
            #    naming another table received the first table's order. The
            #    binding is now checked by ONE shared policy that every
            #    return-existing site in this file calls, so they cannot form
            #    different opinions about the same key.
            #
            #    `for_update` locks the row: from here to commit this
            #    transaction is the one that may act on it.
            verdict_intent = resolve_intent(
                restaurant_id=restaurant.pk,
                client_order_id=client_order_id,
                table_id=table.pk,
                request_fingerprint=request_fingerprint,
                created_by_id=getattr(created_by, 'pk', created_by),
                customer_id=getattr(customer, 'pk', customer),
                for_update=True,
            )
            if verdict_intent.is_match:
                # A1(B) — AUTHORIZATION BEFORE DISCLOSURE, and nothing else.
                #
                # A replay hands back an existing order AND (since G3a) the
                # closure recorded against it. Authorization is the one thing
                # that must still hold for that: a revoked session may not read
                # an acceptance any more than it may create one, which is the
                # rule `_submit_order` states about its own replay.
                #
                # IT IS ONLY AUTHORIZATION. A replay stays exempt from every
                # NEW-ORDER business rule — the pause, menu-only ordering, an
                # item that has since sold out, a quote that has since expired —
                # because refusing an order that was already created and
                # acknowledged on the strength of a rule about NEW work is the
                # retroactive refusal D04 exists to stop. That is why this is
                # here and the admission verdict is not.
                #
                # THE TABLE IS RE-READ WITHOUT A LOCK, deliberately. This return
                # is before step 1b on purpose, so a replay never waits on the
                # table row or takes the advisory lock; a plain statement takes
                # its own snapshot under READ COMMITTED and therefore sees any
                # committed revocation, which is the whole question. Taking the
                # lock here to answer it would invert nothing but would make
                # every recovery queue behind live ordering.
                #
                # IT COSTS ONE QUERY, AND ONLY ON AN ACTUAL REPLAY. The hot
                # create paths reach step 1b, where the locked row is already in
                # hand and the same check is free.
                #
                # TWO QUESTIONS, ONE SNAPSHOT: is the capability still current
                # (generation), and would this table still mint a session at all
                # (scannability). `_once` is what keeps them describing the same
                # moment and the same single query.
                table_now = _once(lambda: _table_row_now(table.pk))
                refusal = (
                    _authority_refusal(table_now)
                    or _session_refusal(table_now)
                )
                if refusal is not None:
                    return refusal
                return {'status': 200, 'order': verdict_intent.order,
                        'idempotent': True}
            if verdict_intent.outcome in REFUSALS:
                return REFUSALS[verdict_intent.outcome]()

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
            #
            #     THE LOCK IS TAKEN HERE; THE VERDICT IS APPLIED AT STEP 1d.
            #     Acquiring the advisory lock and applying a NEW-ORDER policy
            #     are different actions, and only the first belongs at this
            #     point in the ordering. A request that has waited behind the
            #     table lock may turn out to be a replay of an order that
            #     committed while it waited, and refusing that replay because
            #     a NEW order would now be disallowed is exactly the
            #     retroactive refusal D04 exists to stop. The lock order
            #     (advisory -> Table -> ...) is unchanged.
            verdict = admit(
                restaurant_id=restaurant.pk,
                created_by=created_by,
                stage=STAGE_CREATE,
            )

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

            # 1b'. A1(A) — THE AUTHORITY, RE-ASKED ON THE ROW THIS TRANSACTION
            #      NOW HOLDS.
            #
            #      Every wait is behind us: the advisory lock at 1a and the table
            #      row above. This is the first point at which the question "does
            #      the caller still hold what they presented?" can be answered
            #      about state nothing else can move — and it is asked BEFORE the
            #      step-1c replay return, the admission verdict, the eligibility
            #      rules, the daily counter and the INSERT, so no disclosure and
            #      no write happens on revoked authority.
            #
            #      IT COSTS NO QUERY. The locked row above is the re-read, and
            #      the staff half re-runs a resolver that reads no table at all.
            refusal = _authority_refusal(table)
            if refusal is not None:
                return refusal

            # 1c. THE POST-WAIT RECHECK. The step-1 lookup ran BEFORE the table
            #     lock, so a competing request carrying the same key may have
            #     committed while this one waited on that lock. Asking again
            #     now — under the lock, through the same policy — is what stops
            #     the loser doing any new-order work at all: no admission
            #     refusal, no occupancy rejection, no daily number, no INSERT.
            #
            #     THE BINDING IS RE-VALIDATED, NOT ASSUMED. A winner that
            #     appeared during the wait is only this caller's order if it is
            #     the same purchase at the same scope; same key with different
            #     contents conflicts here rather than adopting whatever exists.
            #
            #     It does NOT replace the rollback boundary at step 3/4. This
            #     recheck only sees a race the TABLE lock serialised; the
            #     unique constraint is what covers every other one.
            if client_order_id is not None:
                verdict_intent = resolve_intent(
                    restaurant_id=restaurant.pk,
                    client_order_id=client_order_id,
                    table_id=table.pk,
                    request_fingerprint=request_fingerprint,
                    created_by_id=getattr(created_by, 'pk', created_by),
                    customer_id=getattr(customer, 'pk', customer),
                    for_update=True,
                )
                if verdict_intent.is_match:
                    # A1b — AND ON THIS RETURN TOO.
                    #
                    # THE SAME DISCLOSURE, REACHED BY THE OTHER DOOR. The
                    # step-1 lookup found nothing, so this request never took
                    # that branch; a competing request carrying the same key
                    # committed while this one waited, and the answer here is
                    # an existing order and (since G3a) the closure recorded
                    # against it.
                    #
                    # Step 1b' above re-asked the capability's GENERATION and
                    # the staff module gate on this locked row. Neither answers
                    # whether the table is still a place a diner can be, and
                    # going out of service bumps no `qr_version` — so a table
                    # disabled inside the wait passed 1b' and was disclosed
                    # here. That is the one branch A1b's first cut did not
                    # reach (Codex P1 on PR #325, valid), and it is exactly the
                    # shape this file warns about above: an invariant added to
                    # one branch and not the others.
                    #
                    # IT COSTS NO QUERY. `table` IS the row locked at step 1b,
                    # so this reads the authoritative snapshot already in hand
                    # rather than the lock-free re-read the step-1 branch pays
                    # for. The answer is the capability channel's own opaque
                    # 404, for the reason that branch records — never
                    # `order_eligibility`'s diner-readable 400, which stays the
                    # answer for a FIRST creation at step 1e.
                    #
                    # THE THIRD RETURN-EXISTING SITE — the unique-conflict
                    # recovery below — needs nothing: it sits AFTER step 1e,
                    # which evaluates table liveness on this same locked row
                    # for EVERY provenance, so an unscannable table has already
                    # been refused there and the row cannot move while this
                    # transaction holds it.
                    refusal = _session_refusal(table)
                    if refusal is not None:
                        return refusal
                    return {'status': 200, 'order': verdict_intent.order,
                            'idempotent': True}
                if verdict_intent.outcome in REFUSALS:
                    return REFUSALS[verdict_intent.outcome]()

            # 1d. NOW the admission verdict from step 1a applies: this request
            #     really is new work, so the rule about new work governs it.
            if not verdict.allowed:
                logger.info(
                    "Order admission refused (restaurant_id=%s, code=%s)",
                    restaurant.pk, verdict.code,
                )
                return {'status': 400, 'message': verdict.message}

            # 1e. AUTHORITATIVE OPERATIONAL ELIGIBILITY (D06). May new work
            #     happen at this restaurant, at this table, right now?
            #
            #     `initiate_order` asks the SAME function as a preflight, on
            #     instances loaded in autocommit. That preflight was the ONLY
            #     place these three facts were ever checked, so a pause, a
            #     switch to menu-only or a table taken out of service that
            #     committed while this request waited on the table lock above
            #     was invisible to the request that then wrote the draft — the
            #     widest window being exactly when contention is highest. This
            #     is the load-bearing check, and it is the same
            #     preflight/authoritative split admission and menu publication
            #     already use on this path.
            #
            #     THE FACTS COME FROM PROTECTED READS, NOT FROM `restaurant`
            #     AND THE CALLER'S TABLE. The restaurant half rides the ONE
            #     query `admit` ran under the advisory lock at step 1a; the
            #     table half is the row locked at step 1b and re-read there.
            #     Reading `restaurant.accepting_orders` off the instance this
            #     function was handed would reintroduce precisely the drift the
            #     locks were taken to prevent.
            #
            #     AFTER the replay recheck at step 1c for the same reason the
            #     admission verdict is: a pause is a statement about NEW work,
            #     and applying it to a replay would retroactively refuse a draft
            #     the diner already holds.
            #
            #     PROVENANCE IS NOT BRANCHED ON HERE. The rule itself decides
            #     which of its three facts bind a staff-origin order (liveness:
            #     all of them) and which bind only the QR public (the two policy
            #     gates), so a caller cannot accidentally hold a different
            #     opinion by writing the `created_by is None` test twice.
            operational = eligibility.evaluate(
                eligibility.facts_from_verdict(verdict, table),
                created_by,
            )
            if not operational.allowed:
                logger.info(
                    "Order creation refused (restaurant_id=%s, table_id=%s, "
                    "reason=%s)",
                    restaurant.pk, getattr(table, 'pk', None),
                    operational.reason,
                )
                return operational.as_refusal()

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
            # ONE COHERENT CATALOGUE READ AND ONE PRICING INSTANT (D02).
            #
            # The instant is captured HERE — after the blocking admission and the
            # table lock — so an order is priced at the moment it was actually
            # admitted, not at one sampled before the request waited. The rows
            # come from a single restaurant-scoped statement (plus one batched
            # allergen-label read; see catalogue_snapshot for why that second
            # statement cannot make anything disagree).
            #
            # It replaces THREE independent reads of the same rows inside this
            # transaction — publication's batch, canonicalisation's batch and
            # add_order_item's per-line guard — each of which took its own
            # database snapshot under READ COMMITTED, and 28 separate clock
            # readings for a four-line order. No lock is taken on menu_items: see
            # that module for why one would decide which valid instant wins
            # rather than make the read more correct.
            snapshot = build_snapshot(restaurant, items, timezone.localtime())
            selection = validate_order_selections(
                restaurant, items, snapshot.now,
                enforce_publication=(created_by is None),
                resolved=snapshot.as_map(),
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
            normalization = ConOrder.normalize_order_items(
                restaurant, items, snapshot=snapshot,
            )
            if normalization.get('status') != 200:
                raise OrderItemRejected(normalization)
            items = normalization['items']

            # 3 + 4. THE DAILY NUMBER AND THE INSERT SHARE ONE ROLLBACK
            #        BOUNDARY, and that is the fix rather than an arrangement.
            #
            # The allocation used to sit OUTSIDE the savepoint that wraps the
            # INSERT. So when a racing request carrying the same key committed
            # between the step-1 lookup and this INSERT, the savepoint rolled
            # the failed INSERT back — and the number this transaction had
            # already taken was committed by the outer block on the way out.
            # One order, two numbers consumed, measured as
            # `next_number == 3`. A successful replay must produce NO new
            # creation effect, and the counter is a creation effect.
            #
            # Both statements are now inside the same nested atomic, so the
            # loser unwinds both. As in `allocate_daily_order_number`, the
            # try/except deliberately WRAPS that block rather than sitting
            # inside it: the savepoint has to be rolled back before the
            # re-query, or the connection needs rollback and the recovery
            # raises `TransactionManagementError` instead.
            order_date = timezone.localdate()
            try:
                with transaction.atomic():
                    order_number = allocate_daily_order_number(
                        restaurant, order_date)
                    order = _insert_order(
                        restaurant=restaurant, table=table, customer=customer,
                        created_by=created_by, order_source=order_source,
                        client_order_id=client_order_id,
                        request_fingerprint=request_fingerprint,
                        order_number=order_number, order_date=order_date,
                        verdict=verdict,
                    )
            except IntegrityError as conflict:
                # A racing request with the same key committed between the
                # recheck and this INSERT. ONLY the intent-key constraint is
                # recoverable: anything else is an unexpected failure and must
                # surface as one rather than be relabelled an idempotent
                # success.
                if not _is_intent_key_conflict(conflict):
                    raise
                winner = resolve_intent(
                    restaurant_id=restaurant.pk,
                    client_order_id=client_order_id,
                    table_id=table.pk,
                    request_fingerprint=request_fingerprint,
                    created_by_id=getattr(created_by, 'pk', created_by),
                    customer_id=getattr(customer, 'pk', customer),
                    for_update=True,
                )
                if winner.is_match:
                    return {'status': 200, 'order': winner.order,
                            'idempotent': True}
                if winner.outcome in REFUSALS:
                    # The SAME policy as every other site: a winner that is a
                    # different purchase is a conflict, never adopted.
                    return REFUSALS[winner.outcome]()
                # The constraint fired but no row explains it. Re-raise rather
                # than invent an outcome.
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
            # The per-order line index (D03). On this path the order was created
            # moments ago in this very transaction, so the index starts EMPTY and
            # is filled from the rows written below — the merge lookup therefore
            # costs NO query at all, where the pre-fix path issued a candidate
            # fetch plus an extras fetch per repeated line. add_order_item keeps
            # its bounded database fallback for every caller that has no index,
            # so this is an optimisation and never a trust boundary.
            line_index = {}
            for item in items:
                item_result = ConOrder.add_order_item(
                    item=item, order_id=str(order.id), order=order,
                    snapshot=snapshot, index=line_index,
                )
                if item_result.get('status') != 200:
                    raise OrderItemRejected(item_result)
            order = Order.objects.select_for_update().get(id=order.id)
            ConOrder.update_order_amounts(order=order)
    except OrderItemRejected as rejected:
        return rejected.response

    return {'status': 200, 'order': order, 'idempotent': False}
