"""
handle the submission of an order
"""
import logging

from dataclasses import dataclass
from typing import Union
from django.db import transaction
from django.utils import timezone
from users_app.models import User
from orders_app.controllers.services.quote_protocol import QUOTE_PROTOCOL
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
    capability=None,
    authority=None,
) -> dict:
    """
    update an order

    ``capability`` is the verified diner ``TableCapability`` the caller
    authenticated with, or ``None`` for a staff caller on the module gate. It is
    CONTEXT, never authority: the endpoint has already decided this principal may
    act on this order, and the transition re-checks it only so a revocation that
    commits while the request waits on its locks is honoured. A caller cannot
    gain anything by supplying one — every check it feeds can only refuse.

    ``authority`` is the STAFF channel's counterpart, a ``StaffAuthority``
    naming the principal, the server-resolved restaurant and the module the
    endpoint gated on (D06 completion, G1b). Exactly one of the two is populated
    on a real request; both are optional, so an in-process caller that carried
    nothing before keeps exactly the guarantees it had.
    """
    try:
        # SUBMIT (initiated -> pending) is the transition that turns a draft
        # into a real order and CLAIMS the table, so it must be race-safe.
        # It is handled by its own transactional helper; every other status
        # change below is left exactly as it was (no new locking/transaction).
        if new_status == OrderStatus_Pending:
            return _submit_order(
                order, user, quote_ref, capability, authority)
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


@dataclass(frozen=True)
class _TerminalQuoteOutcome:
    """A saved quote that may never be accepted again, and why.

    Carried as a value rather than rendered on the spot because the two entry
    paths render it DIFFERENTLY and both are right: an acceptance attempt was
    refused, so it is a 400; a retire-for-review asked the server to check and
    got its answer, so it is a 200. The FACT is identical and is decided once.

    ``closure`` is ``None`` for the one outcome that is not terminal at all —
    an anchor this server cannot read — where nothing is written.
    """

    reason: str
    message: str
    closure: object = None
    policy: dict = None

    def _metadata(self):
        body = {}
        if self.policy is not None:
            body['quote_policy'] = self.policy
        if self.closure is not None:
            body['quote_closure'] = quote_closure_projection(self.closure)
        return body

    def as_refusal(self):
        body = {'status': 400, 'message': self.message, 'reason': self.reason}
        body.update(self._metadata())
        return body

    def as_review_result(self):
        """The retire-for-review rendering.

        A 200 IS CLAIMED ONLY WHEN A CLOSURE WAS ACTUALLY WRITTEN. Every other
        outcome that reaches here — an anchor whose age could not be
        established, and the unreachable case where `close` itself refused —
        retired NOTHING, so answering 200 would tell a client the old quote is
        safely dead when the server has said no such thing. That is the one
        answer this route exists to give correctly, and inferring it from the
        presence of a closure object rather than stating it is how it would go
        wrong quietly.

        `quote_still_valid` is deliberately NOT produced here: a quote that
        still stands never becomes a `_TerminalQuoteOutcome` in the first
        place, and its 200 is built by the caller.
        """
        from orders_app.controllers.services import quote_closure
        if self.closure is None:
            return self.as_refusal()
        body = {
            'status': 200,
            'message': self.message,
            'outcome': quote_closure.OUTCOME_CLOSED,
            'reason': self.reason,
        }
        body.update(self._metadata())
        return body


def quote_closure_projection(closure):
    """Indirection so `_TerminalQuoteOutcome` needs no module-level import of a
    service this module imports lazily everywhere else (the con_orders cycle)."""
    from orders_app.controllers.services import quote_closure
    return quote_closure.closure_projection(closure)


def _terminal_quote_outcome(order, live_rows, supplied_quote_ref, now):
    """Is this saved quote still honourable? ``None`` when it is.

    TWO GROUNDS, DELIBERATELY INDEPENDENT, and a third answer that is neither:

      * **EXPIRY** — the monetary promise has run out. Monotone: time only moves
        forward, so a quote expired at one instant is expired at every later one,
        and that is exactly what makes it safe to act on irreversibly.
      * **THE PURCHASE CHANGED** — the saved lines no longer describe what the
        kitchen would prepare. NOT monotone on its own (stock comes back), which
        is precisely why the refusal is RECORDED: without the row, a queued
        acceptance for the old quote would execute the moment the dish came back.
      * **AN UNREADABLE ANCHOR** — this server cannot say how old the quote is.
        A statement of ignorance, so nothing is written and nothing is claimed;
        treating it as expiry would retire a diner's quote over a data fault they
        did not cause, and treating it as live would grant an unbounded one.

    MUST run inside the caller's transaction, under the locks an acceptance
    takes: it can WRITE a closure, and a closure decided outside those locks is
    exactly the double-purchase the record exists to prevent. `close` asserts
    that itself.

    ``supplied_quote_ref`` must already have been proved to name this order's
    saved quote — a closure retires ONE named reference, and retiring one the
    caller did not name would act on an assertion nobody made.
    """
    from orders_app.controllers.services import purchase_integrity
    from orders_app.controllers.services import quote_closure
    from orders_app.controllers.services import quote_policy

    def _retire(reason):
        """Write the closure, or turn its refusal into an answer.

        `close` SELF-GUARDS rather than trusting its caller: it re-resolves
        acceptance and re-checks the reference itself. Both callers here have
        already established every one of those preconditions — an accepted
        order returned at the replay, a non-draft returned at the status check,
        and the reference was proved by `_quote_acknowledgement` — so reaching
        one means the SEQUENCE is broken, not the request.

        It is still translated rather than allowed to escape. `ClosureRefused`
        carries a reason and a diner-readable sentence because it is a CALLER
        OUTCOME; letting it propagate would turn a controlled refusal into a
        500 on the one path whose whole purpose is to answer honestly. The two
        genuine programmer errors `close` raises — an unknown reason, and
        running outside a transaction — are deliberately NOT caught here: those
        are loud on purpose.
        """
        try:
            return quote_closure.close(
                order,
                quote_ref=supplied_quote_ref,
                reason=reason,
                now=now,
                evidence=None,
            ).closure, None
        except quote_closure.ClosureRefused as refused:
            logger.warning(
                'Quote could not be retired (order_id=%s, intended=%s, '
                'reason=%s)',
                order.pk, reason, refused.reason,
            )
            return None, _TerminalQuoteOutcome(
                reason=refused.reason,
                message=refused.message,
                closure=None,
                policy=None,
            )

    age = quote_policy.assess(order, now)
    policy = _quote_policy_projection(age)

    if age.is_expired:
        closure, refused = _retire(quote_closure.REASON_EXPIRED)
        if refused is not None:
            return refused
        return _TerminalQuoteOutcome(
            reason=quote_policy.REASON_QUOTE_EXPIRED,
            message=quote_policy.MESSAGE_QUOTE_EXPIRED,
            closure=closure,
            policy=policy,
        )

    if age.is_unavailable:
        logger.warning(
            'Quote age could not be established (order_id=%s, reason=%s)',
            order.pk, quote_policy.REASON_QUOTE_UNVERIFIABLE,
        )
        return _TerminalQuoteOutcome(
            reason=quote_policy.REASON_QUOTE_UNVERIFIABLE,
            message=quote_policy.MESSAGE_QUOTE_UNVERIFIABLE,
            closure=None,
            policy=policy,
        )

    # THE PURCHASE, against the catalogue as it is NOW — availability, stock,
    # publication, the modifier selections and the allergen declarations the
    # kitchen would be preparing from. The saved MONEY is honoured; what is
    # refused is preparing food from a definition that has changed since the
    # diner agreed to it.
    #
    # It reads the SAME `live_rows` the reference check used, in ONE catalogue
    # statement at the SAME `now`, so no part of this decision is made against a
    # different snapshot.
    #
    # `enforce_publication` follows the ORDER's provenance, matching what
    # `_create_order` enforced when the draft was written: a staff-origin order
    # bypasses publication exactly as it did at creation, and never bypasses
    # tenant, relationship or allergen integrity.
    integrity = purchase_integrity.inspect(
        order, live_rows,
        now=now,
        enforce_publication=(order.created_by_id is None),
    )
    if integrity is not None:
        closure, refused = _retire(quote_closure.REASON_PURCHASE_CHANGED)
        if refused is not None:
            return refused
        return _TerminalQuoteOutcome(
            reason=purchase_integrity.REASON_PURCHASE_NEEDS_REVIEW,
            message=purchase_integrity.MESSAGE_PURCHASE_NEEDS_REVIEW,
            closure=closure,
            policy=policy,
        )

    return None


def _quote_policy_projection(age):
    """What the lifetime rule decided, as a client reads it.

    Deliberately narrow, and deliberately NOT a countdown: `expires_at` is the
    derived deadline under the named policy version, and `status` is the verdict
    at the instant the server decided. A client may render the deadline; it must
    not treat "not expired yet" as a reservation, because it is not one — the
    dish can still sell out inside the window, which is what the purchase-
    integrity check answers.

    `policy_version` rides every answer so a client can tell WHICH rule it was
    held to. It is a separate axis from `pricing_version` (how the money was
    calculated) and from `checkout_protocol` (D04's correlation contract); none
    of the three implies another.
    """
    return {
        'version': age.policy_version,
        'status': age.status,
        'expires_at': (
            age.expires_at.isoformat() if age.expires_at is not None else None
        ),
    }


def _quote_acknowledgement(order, supplied_quote_ref):
    """WHICH saved quote is this request about? ``(refusal_or_None, live_rows)``.

    The question BOTH quote-bearing operations have to answer before they may do
    anything else — accepting a quote and retiring one are opposite acts, and
    each is about exactly one named reference. Keeping it in one function is what
    stops "is this the quote you mean?" from having two answers: a retire route
    with its own reading could retire a reference the acceptance path would have
    called stale, which is a replacement quote minted for a purchase the diner is
    still holding.

    Two invariants, in this order:

    1. **PRICING VERSION.** A draft priced by the LEGACY convention is outside
       this contract entirely. It is refused with an actionable code; it is NOT
       silently recalculated (that would act on an amount nobody reviewed) and
       NOT deleted (it is the diner's own draft).
    2. **THE REFERENCE.** The request must name the exact saved quote. There is
       deliberately NO staff or internal bypass: being a trusted caller is not a
       reason to accept — or retire — an amount no one reviewed, and a bare
       "confirmed" boolean would say nothing about WHICH quote.

    ``live_rows`` is the undeleted population, returned so the callers that need
    it do not fetch it twice and — more importantly — so every check downstream
    examines the SAME population. Under READ COMMITTED a second fetch is a second
    snapshot. It is ``None`` only on the two refusals that return before it.
    """
    from orders_app.controllers.services.order_pricing import (
        PRICING_VERSION_CORRECTED,
    )
    from orders_app.controllers.services import order_quote

    if order.pricing_version != PRICING_VERSION_CORRECTED:
        logger.info(
            'Quote acknowledgement refused (order_id=%s, reason=%s, version=%s)',
            order.pk, REASON_LEGACY_PRICING, order.pricing_version,
        )
        return {
            'status': 400,
            'message': MESSAGE_LEGACY_PRICING,
            'reason': REASON_LEGACY_PRICING,
        }, None

    if not isinstance(supplied_quote_ref, str) or not supplied_quote_ref:
        return {
            'status': 400,
            'message': MESSAGE_QUOTE_REQUIRED,
            'reason': REASON_QUOTE_REQUIRED,
        }, None

    live_rows = list(OrderItem.objects.filter(order=order, deleted=False))

    if not order_quote.matches(order, supplied_quote_ref, rows=live_rows):
        return {
            'status': 400,
            'message': MESSAGE_QUOTE_STALE,
            'reason': REASON_QUOTE_STALE,
        }, live_rows

    return None, live_rows


def _acceptance_refusal(order, supplied_quote_ref):
    """The four ACCEPTANCE invariants, and the population they read.

    Returns ``(refusal_or_None, live_rows_or_None)``. The population is handed
    back rather than kept private because the D06 terminal check runs on the SAME
    rows immediately afterwards, and re-fetching them would both cost a query
    and — worse — let the two checks examine different populations under READ
    COMMITTED. ``live_rows`` is ``None`` only on the two refusals that return
    before it is fetched.

    Runs inside ``_submit_order``'s transaction, on the row re-read under the
    table lock — so what is checked is what is about to be accepted, not a
    possibly-stale instance the caller handed in.

    1 and 2. **WHICH QUOTE?** — delegated to ``_quote_acknowledgement``, shared
       with the retire-for-review operation so the two can never disagree about
       whether a caller has named this order's saved quote.
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
    from orders_app.controllers.services import order_quote

    refusal, live_rows = _quote_acknowledgement(order, supplied_quote_ref)
    if refusal is not None:
        return refusal, live_rows

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
        }, live_rows

    if ConOrder.deliverable_parent_count(order) < 1:
        return {
            'status': 400,
            'message': MESSAGE_NOTHING_TO_PREPARE,
            'reason': REASON_NOTHING_TO_PREPARE,
        }, live_rows
    return None, live_rows


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
                  supplied_quote_ref: Union[str, None] = None,
                  capability=None, authority=None) -> dict:
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
    from restaurants_app.controllers import diner_capability
    from restaurants_app.controllers.diner_capability import DinerCapabilityError
    from orders_app.controllers.services import order_eligibility as eligibility
    from orders_app.controllers.services import quote_closure
    from orders_app.controllers.services.order_authority import (
        StaffAuthorityError, assert_authority_current,
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

        # THE CAPABILITY IS RE-VERIFIED UNDER THE LOCK, BEFORE ANY BUSINESS
        # RULE (D06). The endpoint resolved the diner's table session in
        # autocommit, and this request then WAITED — for the admission advisory
        # lock, the table row and the order row. A QR regeneration committing
        # inside that wait revokes every outstanding session for the table, and
        # nothing here knew what generation had been presented, so a revoked
        # session still placed the order.
        #
        # AUTHORIZATION COMES FIRST, ahead even of the D04 replay: a revoked
        # capability may not read an acceptance any more than it may create one,
        # and answering "already placed" would disclose that an order exists.
        # The refusal is the capability channel's OWN opaque 404, so a revocation
        # landing mid-request is indistinguishable from a session that never
        # resolved — this must not become an oracle, and it must not become a
        # second vocabulary for table state either (see
        # `assert_capability_current`, which deliberately re-checks the
        # generation and nothing else).
        try:
            diner_capability.assert_capability_current(capability, locked_table)
        except DinerCapabilityError as exc:
            return {'status': exc.status, 'message': exc.message}

        # AND THE STAFF SIDE OF THE SAME QUESTION (D06 completion, G1b). The
        # endpoint's module gate ran in autocommit, before the three waits above.
        # A membership deactivated, a role removed or a restaurant leaving the
        # portal-access states inside that wait revokes the authority this
        # request is still acting on — and unlike a QR regeneration, nothing
        # downstream noticed. Asked immediately after the capability check, so
        # both channels linearize at the same point, and answered with the
        # endpoint's own non-disclosing 404.
        try:
            assert_authority_current(authority)
        except StaffAuthorityError as exc:
            return {'status': exc.status, 'message': exc.message}

        # THE DECISION CLOCK, SAMPLED HERE AND NOWHERE ELSE (D06). Every lock
        # above BLOCKS, so a time read before them describes a moment that has
        # already passed by the time anything is decided — and the whole point of
        # a quote lifetime is that the boundary is checked at the decision. One
        # aware instant, read after the last wait, used by the expiry rule and by
        # the catalogue read the purchase-integrity check performs, so both
        # describe the same moment.
        now = timezone.now()

        # D04: WAS THIS ORDER ALREADY ACCEPTED? Asked FIRST, because the
        # `initiated` check below is precisely what used to report a lost
        # response as a failure.
        replay = _acceptance_replay(order, supplied_quote_ref)
        if replay is not None:
            # A1b — AND THE SESSION'S OTHER HALF, ON THE ONE BRANCH NO
            # ELIGIBILITY RULE WILL REACH.
            #
            # `assert_capability_current` above re-checks the QR GENERATION and
            # deliberately nothing else: a table taken out of service is an
            # OPERATIONAL fact binding every provenance, and `order_eligibility`
            # owns it and answers it with a sentence a diner can read. That
            # reasoning holds for a FIRST submission, which reaches that rule a
            # few lines below.
            #
            # A REPLAY NEVER DOES. It is exempt from every new-order rule by
            # design, so the fact reached nothing at all here — and the answer it
            # returns is a disclosure: the order id, the server-resolved
            # restaurant and table, and the exact `quote_ref` the diner
            # confirmed. `_resolve_table` re-reads `is_available_for_scan()` live
            # on every use and treats a table that has stopped being scannable as
            # a REVOKED SESSION, answering this channel's opaque 404 — so a
            # request that reaches here at all is one where the table went out of
            # service inside the lock wait, and handing back the acceptance would
            # answer a session the door would no longer admit.
            #
            # The answer is that same 404, which is what keeps it STABLE across
            # the wait rather than depending on when the operator happened to
            # click. `retire_quote_for_review` asks this question under its lock
            # for the same reason and through the same predicate; it is `True`
            # for a caller with no capability, so a staff or in-process replay is
            # untouched — there is no session for a table to revoke.
            if not diner_capability.session_still_admissible(
                capability, locked_table
            ):
                return {'status': 404, 'message': 'Not found'}
            return replay

        # D06: HAS THIS DRAFT'S QUOTE ALREADY BEEN RETIRED? Asked second, and
        # only after the replay: a closure and an acceptance are mutually
        # exclusive by construction (`quote_closure.close` refuses to write one
        # beside evidence), so an order that carries evidence is answered as the
        # accepted order it is rather than being told to review anything.
        #
        # This is the DURABLE half of a refusal. An earlier attempt that found
        # this quote definitively unacceptable — expired, or no longer the
        # purchase it was — committed a row saying so, and every later attempt
        # reads it here under the same locks. That is what makes it safe for the
        # client to mint a replacement quote: the old one can never execute
        # afterwards, however many requests for it were already in flight.
        closed = quote_closure.read_closure(order)
        if closed is not None:
            return {
                'status': 400,
                'message': quote_closure.MESSAGE_QUOTE_CLOSED,
                'reason': quote_closure.REASON_QUOTE_CLOSED,
                'quote_closure': quote_closure.closure_projection(closed),
            }

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

        # D06: AUTHORITATIVE OPERATIONAL ELIGIBILITY. The three facts nothing on
        # this path has ever consulted: whether the restaurant has paused new
        # diner ordering, whether this table's QR mode permits ordering, and
        # whether the table and the restaurant are still there at all. Before
        # this, a draft initiated while the restaurant was open still reached the
        # kitchen after the owner paused, and a table switched to menu-only or
        # taken out of service still accepted one.
        #
        # THE FACTS ARE THE PROTECTED ONES. The restaurant half rides the single
        # query `admit` ran under the advisory lock at the top of this
        # transaction; the table half is `locked_table`, re-read under its row
        # lock. Neither comes from the instance this function was handed.
        #
        # PROVENANCE IS THE ORDER'S, NEVER THE SUBMITTER'S. `order.created_by_id`
        # is what the rule is given, exactly as `admit` is given it, so a diner's
        # draft stays a diner's draft however senior the person who taps submit —
        # and a member of staff submitting one does not convert it into a
        # management action that walks past a pause.
        #
        # ALL OF THESE ARE TRANSIENT, so none of them closes the quote: the
        # restaurant can resume and the table can come back, and the diner's
        # amount is still good until it expires on its own terms.
        operational = eligibility.evaluate(
            eligibility.facts_from_verdict(verdict, locked_table),
            order.created_by_id,
        )
        if not operational.allowed:
            logger.info(
                'Order submission refused (order_id=%s, reason=%s)',
                order.pk, operational.reason,
            )
            return operational.as_refusal()

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

        # WHICH QUOTE IS THIS REQUEST ABOUT? The acknowledgement invariants come
        # before the two D06 terminal checks deliberately: a closure retires ONE
        # named reference, so the caller has to have named this order's saved
        # quote correctly before the server may retire it on their behalf. A
        # caller holding a stale reference is told their order changed and
        # re-reads it; nothing is closed on an assertion they did not make.
        acceptance, live_rows = _acceptance_refusal(order, supplied_quote_ref)
        if acceptance is not None:
            return acceptance

        # D06: MAY THAT QUOTE STILL BE HONOURED, AND IS IT STILL THE SAME
        # PURCHASE? ONE decision, shared with the retire-for-review operation, so
        # the two entry paths cannot form different opinions about the same draft
        # — a route that answered "still fine" about a quote acceptance would
        # refuse, or the reverse, is a route that hands a client a replacement it
        # did not need or withholds one it does.
        terminal = _terminal_quote_outcome(
            order, live_rows, supplied_quote_ref, now)
        if terminal is not None:
            logger.info(
                'Order submission refused (order_id=%s, reason=%s)',
                order.pk, terminal.reason,
            )
            return terminal.as_refusal()

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
            # THE DECISION CLOCK, not a fresh reading. `now` is the instant this
            # transition was decided at — the same instant the quote lifetime was
            # measured against and the catalogue was read at — so the recorded
            # moment of acceptance cannot sit outside the window that admitted it.
            accepted_at=now,
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


# ---------------------------------------------------------------------------
# D06 — THE SECOND ENTRY PATH: retire a quote WITHOUT attempting to accept it
# ---------------------------------------------------------------------------

MESSAGE_QUOTE_STILL_VALID = (
    'This order is still current and can be placed as it is.'
)
MESSAGE_QUOTE_RETIRED = (
    'This order needs to be reviewed again before it can be placed.'
)


def retire_quote_for_review(order: Order,
                            supplied_quote_ref: Union[str, None] = None,
                            capability=None, authority=None) -> dict:
    """The retire-for-review route, with its answer CORRELATED to the enquiry.

    D06 completion, G4. The work is `_retire_quote_answer`; this states what the
    answer is ABOUT.

    WHY IT HAD TO BE ADDED. A client asks this route whether a saved quote may
    still be honoured, and `quote_still_valid` is the answer that leads to
    SUBMITTING an order. The answers named nothing — no order, no reference — so
    a client had no way to establish that a 200 in its hand was the reply to the
    enquiry it sent, and a late or misrouted one read exactly like the right one.
    D04 closed that for acceptance answers and the enquiry was left behind.

    It is CORRELATION, NOT AUTHORIZATION: the caller has already established
    that it may act on this order (the diner table session, or the staff module
    gate), and this discloses only what that caller just named.
    """
    answer = _retire_quote_answer(
        order, supplied_quote_ref, capability=capability, authority=authority)
    return _correlate_quote_answer(answer, order, supplied_quote_ref)


def _correlate_quote_answer(answer, order, supplied_quote_ref):
    """Stamp an answer with the enquiry it answers.

    NOT STAMPED: the capability channel's opaque 404. That refusal is exactly
    two keys by design — unknown, out of scope and revoked are indistinguishable
    in status and in body — and naming an order in it would turn it into the
    existence oracle it exists not to be. The rule is structural rather than a
    list of statuses: a body that states no `outcome` and no `reason` has said
    nothing about a quote, so there is nothing for it to be about.

    `quote_ref` echoes what the CALLER named, which is what makes this a
    correlation. Where a closure is also present it names the reference the
    server really retired, and the two can legitimately differ — a caller naming
    a foreign reference is told about the closure that exists, and the echo is
    what lets them see the request they sent was not the one it describes.
    """
    if not isinstance(answer, dict):                 # pragma: no cover
        return answer
    if 'outcome' not in answer and 'reason' not in answer:
        return answer

    answer['order'] = str(order.pk)
    if isinstance(supplied_quote_ref, str) and supplied_quote_ref:
        answer['quote_ref'] = supplied_quote_ref
    # The LEVEL, on the surface a client reaches when its quote may be dead —
    # so it can tell a server that publishes closures from one whose silence
    # about them means nothing, without having to have read an order first.
    answer['quote_protocol'] = QUOTE_PROTOCOL
    return answer


def _retire_quote_answer(order: Order,
                         supplied_quote_ref: Union[str, None] = None,
                         capability=None, authority=None) -> dict:
    """Check whether a saved quote can still be honoured, and retire it if not.

    WHY IT EXISTS. A client that has decided its quote is stale — the deadline it
    was told about has passed, the diner is coming back to a basket much later,
    the browser is resuming an interrupted checkout — needs a SAFE way to move to
    a fresh quote. Doing that by simply minting a new one is not safe: the old
    quote is still acceptable until something records that it is not, and an
    acceptance already queued for it can land afterwards. That is the double
    purchase ``OrderQuoteClosure`` exists to prevent, and it is why "the client
    decided to re-price" cannot be the mechanism.

    The alternative — have the client attempt an acceptance and read the refusal
    — is worse: an acceptance that SUCCEEDS claims the table and sends food to a
    kitchen, which is a large thing to do in order to ask a question.

    **IT NEVER CLOSES A QUOTE THAT IS STILL GOOD.** The client does not supply a
    reason and cannot; the server evaluates exactly the two terminal grounds the
    acceptance path evaluates, through the SAME function, and answers
    ``quote_still_valid`` — writing nothing — when neither holds. So this is not
    a "discard my quote" verb, and it cannot be used as one. A diner who simply
    changes their mind edits their basket, which is a different operation on a
    different draft.

    **IT IS NOT A LIFECYCLE OPERATION**, deliberately, and this is the same
    asymmetry the admin owner-invitation cancel already draws: a restaurant that
    has PAUSED is exactly when a client most needs to be able to establish that
    its held quote is dead. It therefore consults no admission verdict and no
    operational rule, and changes no order status: a retired draft stays an
    ``initiated`` draft, still the diner's, still readable. ``accepting_orders``,
    a suspension, an offboarding and a soft-deleted restaurant all leave the
    diner's table session live, so this route is reachable through every one of
    them.

    **IT DOES TAKE THE ADMISSION LOCK SHARED, AND THAT IS NOT A CONTRADICTION**
    (D06 completion, G1a). An earlier draft of this docstring said it "takes no
    admission advisory lock", which conflated two different things the module
    keeps apart on purpose: ``lock_admission_shared`` is SYNCHRONISATION —
    hold this restaurant's admission-relevant state steady for the rest of this
    transaction — while ``order_admission.admit`` is POLICY, the question of
    whether NEW work may be admitted. This route asks the second question of
    nobody and still needs the first, because it runs the very same purchase
    integrity check acceptance runs, and it writes something acceptance does not:
    a CLOSURE, which is irreversible. A catalogue edit committing inside that
    decision would retire a diner's perfectly good quote on the strength of a
    read that was already stale — the worst version of this race, since an
    acceptance racing the same edit merely sends the order back for review.
    Shared, so it never blocks an order and never blocks another retirement.

    **AN UNAVAILABLE TABLE IS THE ONE CASE IT CANNOT ANSWER, AND THAT IS THE
    CHANNEL'S RULE RATHER THAN AN OVERSIGHT.** A diner reaches this route through
    a table session, and ``_resolve_table`` re-checks ``is_available_for_scan()``
    live on every use — so once the table is soft-deleted, disabled, deactivated
    or taken out of service the session is REVOKED and the endpoint answers the
    capability channel's opaque 404 before this function is entered. (An earlier
    draft of this docstring claimed an out-of-service table was reachable here.
    It is not; the claim was wrong, not the code.)

    Do NOT add a retirement-specific resolution that skips that gate. Four
    reasons, in the order they bite. It would let a REVOKED session drive a
    durable write, which is exactly what the live re-check exists to stop. It
    would contradict this very change's central rule — ``order_eligibility``
    makes table liveness bind EVERY provenance, staff included, because an
    unavailable table is not a place an order can exist — so the carve-out would
    undercut one layer down what D06 establishes here. It would be a SECOND
    capability resolution, the kind of second opinion this design exists to
    prevent. And it would buy the diner nothing: a closure is what makes minting
    a REPLACEMENT quote safe, and at an unavailable table no replacement can be
    minted (creation is refused for that table by the same rule), nor can the old
    quote be accepted through any channel. The client treats the 404 as a round
    trip that did not answer — it surfaces a retry and never submits — which is
    the honest outcome. Pinned by ``RetiringAtAnUnavailableTableTests``.

    LOCK ORDER: ``advisory SHARED -> Table -> Order`` — acceptance's order
    exactly, which is what lets this join an ordering already proven acyclic
    rather than adding one. Taking the two rows in the established order is what
    makes an acceptance and a retirement racing for one draft resolve in the
    database rather than by arrival: whichever holds the ``Order`` row first
    wins, and the loser reads what the winner committed.
    """
    from restaurants_app.models import Table
    from restaurants_app.controllers import diner_capability
    from restaurants_app.controllers.diner_capability import DinerCapabilityError
    from orders_app.controllers.services import quote_closure
    from orders_app.controllers.services import quote_policy
    from restaurants_app.controllers.admission_lock import lock_admission_shared
    from orders_app.controllers.services.order_authority import (
        StaffAuthorityError, assert_authority_current,
    )

    with transaction.atomic():
        # SYNCHRONISATION, NOT POLICY, and FIRST — before either row lock, which
        # is the documented `advisory -> rows` order. See the docstring: this
        # route decides on the same catalogue facts acceptance decides on, and
        # writes an irreversible closure from that decision.
        lock_admission_shared(order.restaurant_id)

        locked_table = None
        if order.table_id is not None:
            locked_table = (
                Table.objects.select_for_update().get(pk=order.table_id)
            )
        order = Order.objects.select_for_update().get(pk=order.pk)

        # AUTHORIZATION FIRST, exactly as on the acceptance path: a revoked
        # capability may not learn anything about this order, including whether
        # its quote still stands.
        try:
            diner_capability.assert_capability_current(capability, locked_table)
        except DinerCapabilityError as exc:
            return {'status': exc.status, 'message': exc.message}

        # THE SESSION'S OTHER HALF, RE-ASKED UNDER THE LOCK (D06 completion,
        # G1b). A table that stops being scannable REVOKES every session on it —
        # that is why `_resolve_table` re-checks it live on every use and this
        # route's endpoint answers the channel's opaque 404. Acceptance re-reads
        # the same fact off the same locked row through `order_eligibility` and
        # refuses with a sentence a diner can read; this route runs no
        # eligibility rule by design, so without this the fact reached nothing at
        # all here — and a retirement writes a CLOSURE, which cannot be undone.
        # Answering with the 404 keeps this route's answer STABLE across the lock
        # wait rather than depending on when the operator happened to click.
        if not diner_capability.session_still_admissible(
            capability, locked_table
        ):
            return {'status': 404, 'message': 'Not found'}

        # The staff channel's equivalent, for the same reason as at acceptance.
        try:
            assert_authority_current(authority)
        except StaffAuthorityError as exc:
            return {'status': exc.status, 'message': exc.message}

        now = timezone.now()

        # AN ACCEPTED ORDER HAS NOTHING TO RETIRE. Asked before anything else,
        # because a closure beside an acceptance would be a row reading "this was
        # both placed and permanently unplaceable". `quote_closure.close` refuses
        # to write one; answering here means the client is told the truth — the
        # order was placed — rather than receiving a conflict it cannot act on.
        evidence = OrderAcceptance.objects.filter(order=order).first()
        if evidence is not None:
            return {
                'status': 409,
                'message': quote_closure.MESSAGE_ALREADY_ACCEPTED,
                'reason': quote_closure.REASON_ALREADY_ACCEPTED,
                'checkout': acceptance_result(
                    order,
                    outcome=OUTCOME_ALREADY_ACCEPTED,
                    evidence=evidence,
                ),
            }

        # ALREADY RETIRED: the ORIGINAL row, unchanged. Idempotence here is about
        # HISTORY — a retry must not restate why the quote died or when, and must
        # not produce a second reference for a client to reconcile.
        existing = quote_closure.read_closure(order)
        if existing is not None:
            return {
                'status': 200,
                'message': MESSAGE_QUOTE_RETIRED,
                'outcome': quote_closure.OUTCOME_ALREADY_CLOSED,
                'reason': existing.reason,
                'quote_closure': quote_closure.closure_projection(existing),
            }

        # A NON-DRAFT WITH NO EVIDENCE is D04's `evidence_unavailable` — a
        # statement of ignorance — and retiring its quote would convert that
        # ignorance into a terminal fact about an order that may be in the
        # kitchen. Refused for review; nothing is written.
        if order.order_status != OrderItemStatus_Initiated:
            logger.warning(
                'Quote retirement refused (order_id=%s, reason=%s)',
                order.pk, quote_closure.REASON_EVIDENCE_UNAVAILABLE,
            )
            return {
                'status': 409,
                'message': quote_closure.MESSAGE_EVIDENCE_UNAVAILABLE,
                'reason': quote_closure.REASON_EVIDENCE_UNAVAILABLE,
            }

        # WHICH QUOTE? The same question the acceptance path asks, through the
        # same function. A closure retires ONE named reference, so a caller
        # holding a stale one is told their order changed and re-reads it —
        # nothing is retired on an assertion they did not make.
        refusal, live_rows = _quote_acknowledgement(order, supplied_quote_ref)
        if refusal is not None:
            return refusal

        terminal = _terminal_quote_outcome(
            order, live_rows, supplied_quote_ref, now)
        if terminal is not None:
            logger.info(
                'Quote retired for review (order_id=%s, reason=%s)',
                order.pk, terminal.reason,
            )
            # Returned from INSIDE the transaction: the closure row and the
            # answer describing it must commit together, or a client is told a
            # quote is dead that the database still considers live.
            return terminal.as_review_result()

        # STILL GOOD, AND NOTHING WAS WRITTEN. The deadline is published so the
        # client can stop asking until it matters; it is a deadline, not a
        # reservation — the dish can still sell out inside it, which is the
        # other ground this call just checked and found clear AT THIS INSTANT.
        return {
            'status': 200,
            'message': MESSAGE_QUOTE_STILL_VALID,
            'outcome': quote_closure.OUTCOME_STILL_VALID,
            'quote_policy': _quote_policy_projection(
                quote_policy.assess(order, now)),
        }
