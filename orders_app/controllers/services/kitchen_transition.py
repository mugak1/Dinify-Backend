"""
THE kitchen-order command boundary (D05).

ONE place decides what a kitchen command may do to an order. Every supported
kitchen writer — advance, serve, correct, recall, cancel, priority — goes through
``execute`` here; ``orders_app/endpoints_kitchen.py`` is a thin adapter that
parses HTTP and renders the envelope and holds no business rule of its own.

WHY THE BOUNDARY EXISTS, in terms of what went wrong without it. Each endpoint
used to read an UNLOCKED instance, decide from it, and save — with no transaction
around any of it. Measured on unmodified `fd190dd` with two connections:

  * a cancellation committed between a serve's read and its save, and the serve
    wrote `order_status='served'` straight over it, leaving a row that was
    simultaneously cancelled and sold;
  * the reverse order left a SERVED order carrying `order_status='cancelled'`;
  * an ordinary kitchen user's free void, decided from a `new` they had already
    stopped being, survived preparation starting and wrote a cancellation the
    manager rule forbids;
  * a recall never asked whether the table had since been taken, so two orders
    ended up ongoing at one table;
  * a delayed `{'fulfilment_status': 'preparing'}` was executed as a RECALL,
    undoing another device;
  * and after a serve/recall/serve cycle a delayed recall reopened a LATER
    completion, because the source state was `served` again.

FIVE THINGS ARE LOAD-BEARING HERE.

1. **THE DECISION AND THE WRITE SHARE ONE TRANSACTION AND ONE LOCK ORDER.**
   ``Table`` then ``Order``, matching the tail of acceptance's documented
   ``advisory -> Table -> Order`` (``manage_order._submit_order``). Kitchen
   commands do NOT take the admission advisory lock — not because an advisory
   lock is forbidden here, but because ``order_admission.admit`` decides whether
   NEW work may be admitted, and managing already-accepted work is a different
   question. An operational pause on new orders must not strand a ticket the
   kitchen is already cooking. Lifecycle state still gates access, through the
   central resolver's ``portal_access_states()`` filter, exactly as before.

2. **EVERY FACT IS RE-READ UNDER THE LOCK.** The caller's instance is a LOCATOR
   and nothing more. Status, priority, cancellation, the table, the restaurant,
   the revision, the clock and the ACTOR'S PERMISSIONS are all resolved after the
   blocking wait. A decision made before the wait is a decision about a moment
   that has passed.

3. **THE ACTION IS EXPLICIT AND THE REVISION IS REQUIRED.** An action names one
   edge, so a delayed advance can never be reinterpreted as a correction; the
   revision closes the cycle case, which no source-state check can see. There is
   no fallback, no legacy body form and no bypass — a request without a usable
   precondition is refused.

4. **A REFUSAL WRITES NOTHING.** No timestamp, no provenance, no revision bump,
   no partial state. A stale precondition never performs the effect a second
   time, even when the current state happens to equal what was asked for.

5. **NOTHING HERE TOUCHES THE DINER'S CONTRACT.** No reprice, no snapshot, no
   `OrderItem` row, no `OrderAcceptance`, no `quote_ref`, no payment field. The
   revision is deliberately absent from ``order_quote``'s fingerprint: that
   reference is what the diner agreed to pay, and a kitchen command must not move
   it. D04's stored acceptance reference and its replay are untouched — they
   compare the supplied reference against the STORED ``OrderAcceptance.quote_ref``,
   which no kitchen command writes.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    CANCELLATION_REASONS,
    MODULE_KITCHEN,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Paid,
    OrderStatus_Pending,
    OrderStatus_Preparing,
    OrderStatus_Refunded,
    OrderStatus_Served,
)
from orders_app.models import Order
from users_app.controllers.permissions_check import (
    can_manage_restaurant,
    can_user_access_module,
)

logger = logging.getLogger(__name__)

# --- the command vocabulary -------------------------------------------------
# ONE ACTION NAMES ONE EDGE from any given state. That is the whole point: the
# ambiguity D05 closes is that a TARGET does not identify a command. `preparing`
# is reachable both forwards (from `new`) and backwards (from `ready`), so a
# delayed forward request was executed as a correction. `advance` and `correct`
# can never be confused, whatever the row says when the request lands.
#
# SERVE IS ITS OWN ACTION rather than the last rung of `advance`, because it is
# the one fulfilment command with a commercial consequence: it couples
# `order_status` to `served`, which is what `reports.sale_filters` counts.
ACTION_ADVANCE = 'advance'
ACTION_SERVE = 'serve'
ACTION_CORRECT = 'correct'
ACTION_RECALL = 'recall'
ACTION_CANCEL = 'cancel'
ACTION_SET_PRIORITY = 'set_priority'

#: The actions the overloaded fulfilment route may express. `cancel` and
#: `set_priority` are unambiguous from their own routes and are NOT accepted here.
FULFILMENT_ACTIONS = frozenset({
    ACTION_ADVANCE, ACTION_SERVE, ACTION_CORRECT, ACTION_RECALL,
})

#: EVERY action the service will act on. `execute` checks against this itself
#: rather than trusting that a caller came through an endpoint parser: the
#: service is the authoritative boundary and self-guards a direct caller, the
#: same property `_create_order` states about itself. Without it an unrecognised
#: action fell through `_apply_fulfilment`'s three named branches into the RECALL
#: arm — so `KitchenCommand(action='invalid')` recalled a served order instead of
#: being refused.
ALL_ACTIONS = FULFILMENT_ACTIONS | {ACTION_CANCEL, ACTION_SET_PRIORITY}

#: Declared to clients so a new one can tell a server that supports this contract
#: from one that does not. Deliberately its own narrow name: D04's
#: `checkout_protocol` describes the DINER's checkout and `pricing_version`
#: describes how money was calculated. One value must not answer two questions.
KITCHEN_PROTOCOL = 1

#: ``0 <= now - served_at <= RECALL_WINDOW``. The window runs from the CURRENT
#: completion, so re-serving after a legitimate recall starts a fresh one — and
#: the newer revision is what stops an OLD recall operating on that new cycle.
#: The 24h Completed feed is VISIBILITY; it is not permission to recall, and the
#: two are deliberately different durations.
RECALL_WINDOW = timedelta(minutes=10)

#: PostgreSQL `integer`, which is what `PositiveIntegerField` maps to. The
#: service refuses before the ceiling rather than overflowing into a DataError.
MAX_REVISION = 2147483647

# --- what a command may act on ---------------------------------------------
# The forward edges, one step each. `ready -> served` is `serve`, not `advance`.
ADVANCE_EDGES = {'new': 'preparing', 'preparing': 'ready'}

#: Order statuses that describe an ACTIVE ticket. `preparing` is the legacy
#: vocabulary migration `0029` could leave behind; `pending` is what acceptance
#: and recall write today.
ACTIVE_ORDER_STATUSES = frozenset({OrderStatus_Pending, OrderStatus_Preparing})
ACTIVE_FULFILMENT_STATUSES = frozenset({'new', 'preparing', 'ready'})

#: Finished commercially. Not incoherent — simply not a kitchen ticket any more.
TERMINAL_ORDER_STATUSES = frozenset({OrderStatus_Paid, OrderStatus_Refunded})

# --- refusal reasons (stable, machine-readable) -----------------------------
REASON_ACTION_REQUIRED = 'kitchen_action_required'
REASON_ACTION_UNKNOWN = 'kitchen_action_unknown'
REASON_PRECONDITION_REQUIRED = 'kitchen_precondition_required'
REASON_PRECONDITION_INVALID = 'kitchen_precondition_invalid'
REASON_PRECONDITION_STALE = 'kitchen_precondition_stale'
REASON_BODY_INVALID = 'kitchen_request_invalid'
REASON_REASON_INVALID = 'cancellation_reason_invalid'
REASON_PRIORITY_INVALID = 'priority_invalid'
REASON_FORBIDDEN = 'kitchen_forbidden'
REASON_MANAGE_REQUIRED = 'kitchen_manage_required'
REASON_DRAFT = 'order_is_draft'
REASON_CANCELLED = 'order_cancelled'
REASON_TERMINAL = 'order_terminal'
REASON_INCOHERENT = 'order_state_incoherent'
REASON_ILLEGAL = 'illegal_transition'
REASON_RECALL_EXPIRED = 'recall_window_expired'
REASON_TABLE_OCCUPIED = 'table_occupied'
REASON_SCOPE_MISMATCH = 'order_scope_mismatch'
REASON_ACCEPTED_WHILE_WAITING = 'order_accepted_while_waiting'
REASON_REVISION_EXHAUSTED = 'revision_limit_reached'

OUTCOME_APPLIED = 'applied'
OUTCOME_UNCHANGED = 'unchanged'


class KitchenRefusal(Exception):
    """A controlled refusal. Carries an HTTP status, a stable reason and prose.

    ``state`` is the authorised current-state projection where it is safe to
    include one — i.e. the caller has already cleared the module gate for THIS
    order's restaurant. A refusal that has not established that carries none, so
    this can never become an oracle about a tenant the caller has no relationship
    with.
    """

    def __init__(self, status, reason, message, state=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.state = state


class OrderNotFound(Exception):
    """The order does not exist, is soft-deleted, or is out of the caller's scope.

    ONE exception for all three, deliberately: the endpoint renders the
    repository's established non-disclosing 404, so a caller cannot use this
    route to learn that some UUID is a real order at a restaurant they cannot see.
    """


@dataclass(frozen=True)
class KitchenCommand:
    """A fully validated command. Building one proves the REQUEST is well formed;
    it proves nothing about the order, which is resolved under the lock.

    **THE VALIDATION IS IN THE CONSTRUCTOR, so "fully validated" is a property of
    the TYPE rather than a description of how the three parsers happen to be
    written.** It used to be the latter: this was a bare dataclass, so the
    revision's type and range, the priority boolean and the cancellation
    vocabulary were enforced only by ``parse_fulfilment_command`` /
    ``parse_priority_command`` / ``parse_cancel_command``. A caller that built a
    command another way — a future internal writer, a management command, a retry
    helper — reached ``execute`` with none of it applied, and ``execute``
    checked only the ACTION. The service's own docstring says it "self-guards a
    direct caller"; for everything but the action it did not.

    CROSS-ACTION FIELDS ARE UNREPRESENTABLE, not merely ignored: an ``advance``
    carrying a ``cancellation_reason`` is not a command this contract defines, so
    it cannot be constructed. Ignoring it would leave a caller believing they had
    said something the boundary silently dropped.

    It raises ``KitchenRefusal`` rather than ``TypeError`` so the one adapter
    renders a malformed direct command exactly as it renders a malformed body —
    one refusal vocabulary, whichever way the command was formed.
    """
    action: str
    if_revision: int
    cancellation_reason: Optional[str] = None
    priority: Optional[bool] = None

    def __post_init__(self):
        _assert_known_action(self.action)
        _validate_revision_value(self.if_revision)

        if self.action == ACTION_CANCEL:
            _validate_cancellation_reason(self.cancellation_reason)
        elif self.cancellation_reason is not None:
            raise KitchenRefusal(
                400, REASON_BODY_INVALID,
                'cancellation_reason does not belong to this command.',
            )

        if self.action == ACTION_SET_PRIORITY:
            _validate_priority_value(self.priority)
        elif self.priority is not None:
            raise KitchenRefusal(
                400, REASON_BODY_INVALID,
                'priority does not belong to this command.',
            )


# --- request validation -----------------------------------------------------

def _require_mapping(body):
    """A root that is not a mapping reached ``.get()`` and raised AttributeError
    -> 500. Checked BEFORE any key is read, and it discloses nothing."""
    if not isinstance(body, dict):
        raise KitchenRefusal(
            400, REASON_BODY_INVALID,
            'The request body must be a JSON object.',
        )


def _validate_revision_value(value):
    """``if_revision`` — a real non-negative ``int``, in range.

    ``bool`` is refused explicitly because it is an ``int`` subclass in Python,
    so ``True`` would otherwise pass as revision 1. A float is refused even when
    integral (``3.0``): a client that computed a revision in floating point has
    lost the guarantee this token exists to provide. A numeric STRING is refused
    for the same reason DRF's coercions are avoided throughout — a precondition
    that a coercion table can satisfy is not a precondition. ZERO IS VALID: it is
    the adoption baseline every pre-D05 row carries.

    THE ONE RULE, read by the body parser AND by ``KitchenCommand.__post_init__``,
    so a command formed in process is held to exactly what a command formed from
    JSON is held to.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise KitchenRefusal(
            400, REASON_PRECONDITION_INVALID,
            'if_revision must be a whole number.',
        )
    if value < 0 or value > MAX_REVISION:
        raise KitchenRefusal(
            400, REASON_PRECONDITION_INVALID,
            'if_revision is out of range.',
        )
    return value


def _validate_cancellation_reason(value):
    """The stored reason is a CONTROLLED vocabulary. Without this in the type, a
    directly-built command wrote whatever string it carried onto the row —
    permanently, since a cancellation is never re-cancelled."""
    if not isinstance(value, str) or value not in CANCELLATION_REASONS:
        raise KitchenRefusal(
            400, REASON_REASON_INVALID,
            'A valid cancellation_reason is required.',
        )
    return value


def _validate_priority_value(value):
    """A STRICT JSON boolean. ``1``/``0``/``'yes'`` are not booleans, and the
    column is one — coercing them here would reinstate exactly the guesswork the
    strict parser removed, one layer further in."""
    if not isinstance(value, bool):
        # The PRESENCE message stays with the parser: a body that omitted the key
        # and one that sent `'yes'` are different mistakes, and the second is not
        # improved by being told the field is required.
        raise KitchenRefusal(
            400, REASON_PRIORITY_INVALID,
            'priority must be true or false.',
        )
    return value


def _parse_revision(body):
    """Presence, then the shared rule. Absence and malformation are different
    facts and keep different reason codes: one says reload the ticket, the other
    says the value itself is wrong."""
    if 'if_revision' not in body:
        raise KitchenRefusal(
            400, REASON_PRECONDITION_REQUIRED,
            'if_revision is required. Reload the ticket and try again.',
        )
    return _validate_revision_value(body.get('if_revision'))


def parse_fulfilment_command(body):
    """The overloaded fulfilment route: the action must be stated.

    There is deliberately NO acceptance of the old ``{'fulfilment_status': ...}``
    form. Reading a target and guessing the action is exactly the defect this
    contract removes, and an omitted precondition is refused rather than
    defaulted — see the module docstring's point 3.
    """
    _require_mapping(body)
    action = body.get('action')
    if action is None:
        raise KitchenRefusal(
            400, REASON_ACTION_REQUIRED,
            'action is required (advance, serve, correct or recall).',
        )
    if not isinstance(action, str) or action not in FULFILMENT_ACTIONS:
        raise KitchenRefusal(
            400, REASON_ACTION_UNKNOWN,
            'Unknown action for this request.',
        )
    return KitchenCommand(action=action, if_revision=_parse_revision(body))


def parse_cancel_command(body):
    _require_mapping(body)
    reason = _validate_cancellation_reason(body.get('cancellation_reason'))
    return KitchenCommand(
        action=ACTION_CANCEL,
        if_revision=_parse_revision(body),
        cancellation_reason=reason,
    )


def parse_priority_command(body):
    """A STRICT JSON boolean. Never ``bool(raw)``.

    The coercion this replaces accepted anything: ``'no'`` and ``'false'`` both
    became True, ``{'a': 1}`` became True, ``[]`` and ``''`` became False. And an
    OMITTED value toggled, so a retried request undid itself — the opposite of
    idempotent. The value is now stated, so a retry is a no-op rather than a flip.
    """
    _require_mapping(body)
    if 'priority' not in body:
        raise KitchenRefusal(
            400, REASON_PRIORITY_INVALID,
            'priority is required and must be true or false.',
        )
    value = _validate_priority_value(body.get('priority'))
    return KitchenCommand(
        action=ACTION_SET_PRIORITY,
        if_revision=_parse_revision(body),
        priority=value,
    )


# --- the current-state projection ------------------------------------------

def order_state(order):
    """The small, consistent projection every success and authorised conflict
    returns, built from the row as it stands at that moment.

    ONE shape from every command, so a client has one thing to reconcile against
    rather than slightly different facts per route. Timestamps are explicit
    ISO-8601 strings for the reason ``format_money`` exists: the value a view
    BUILDS is not always the value a client PARSES — this dict reaches the wire
    through DRF's ``JSONEncoder`` while a serializer field would render through
    ``DATETIME_FORMAT``, and the two are configurable apart.

    It carries NOTHING about any other order. A conflict caused by a different
    order occupying the table says so with a reason code and does not hand over
    that order's identity or contents.
    """
    return {
        'id': str(order.pk),
        'fulfilment_revision': order.fulfilment_revision,
        'order_status': order.order_status,
        'fulfilment_status': order.fulfilment_status,
        'priority': order.priority,
        'served_at': order.served_at.isoformat() if order.served_at else None,
        'cancelled_at': (
            order.cancelled_at.isoformat() if order.cancelled_at else None
        ),
        'cancellation_reason': order.cancellation_reason,
    }


def _conflict(order, reason, message):
    return KitchenRefusal(409, reason, message, state=order_state(order))


# --- eligibility ------------------------------------------------------------

def _assert_operable(order):
    """May a kitchen command act on this order AT ALL, before we ask which one?

    THE ORDER OF THESE CHECKS IS THE ANSWER A CLIENT NEEDS. A draft and a
    cancelled order are both refused, but "this ticket is now cancelled" is the
    useful sentence and "the order is a draft" is the accurate one; neither is
    improved by reporting a stale precondition instead, so eligibility is
    established before the precondition is compared.

    LEGACY COMPATIBILITY, STATED EXACTLY. An order that predates
    ``OrderAcceptance`` has no receipt, and requiring one would strand real
    historical service. But "not a draft" is far too broad a classifier and is
    not proof of acceptance, so the coherent combinations are ENUMERATED and
    everything else is refused for manual review rather than normalised into
    eligibility. This is a bounded operational decision under historical
    uncertainty — it does NOT assert those rows were accepted, and it changes
    nothing about D04's ``evidence_unavailable``, which still means the server
    does not know.
    """
    if order.order_status == OrderStatus_Initiated:
        # An unconfirmed draft is not a kitchen ticket. It never reaches the
        # board, and hiding it was never the same as guarding it: every write
        # path used to accept one, and walking a draft to `served` even wrote
        # `order_status='served'` — turning something the diner never placed into
        # a sale, and leaving them unable to place it afterwards.
        raise _conflict(
            order, REASON_DRAFT,
            'This order has not been placed yet, so the kitchen cannot act on it.',
        )

    if order.order_status == OrderStatus_Cancelled:
        # Terminal. A cancelled order is never progressed or recalled back into
        # live service, and that holds whether or not it carries an acceptance
        # receipt — the receipt records that the diner's submission landed, not
        # that the order is still live. Reordering is a new purchase.
        raise _conflict(
            order, REASON_CANCELLED,
            'This order has been cancelled.',
        )

    if order.order_status in TERMINAL_ORDER_STATUSES:
        raise _conflict(
            order, REASON_TERMINAL,
            'This order is closed and the kitchen cannot act on it.',
        )

    has_cancellation_provenance = (
        order.cancelled_at is not None
        or order.cancelled_by_id is not None
        or order.cancellation_reason is not None
    )

    coherent_active = (
        order.order_status in ACTIVE_ORDER_STATUSES
        and order.fulfilment_status in ACTIVE_FULFILMENT_STATUSES
        and order.served_at is None
        and not has_cancellation_provenance
    )
    coherent_served = (
        order.order_status == OrderStatus_Served
        and order.fulfilment_status == 'served'
        and order.served_at is not None
        and not has_cancellation_provenance
    )
    if not (coherent_active or coherent_served):
        # Contradictory rows are real: the pre-D05 races could leave a served
        # order carrying cancellation provenance, or an active one carrying a
        # served stamp. They are REFUSED AND LEFT UNTOUCHED — never repaired,
        # normalised or backfilled on the way past. A human decides.
        logger.warning(
            'Kitchen command refused on an incoherent order '
            '(order_id=%s, order_status=%s, fulfilment_status=%s, '
            'served_at=%s, cancellation_provenance=%s)',
            order.pk, order.order_status, order.fulfilment_status,
            order.served_at is not None, has_cancellation_provenance,
        )
        raise _conflict(
            order, REASON_INCOHERENT,
            'This order is in a state the kitchen cannot act on. '
            'Please ask a manager to review it.',
        )


def _assert_revision(order, command):
    """Compare-and-set, against the LOCKED row.

    A stale precondition NEVER performs the effect, even when the current state
    happens to equal what was asked for — that equality is what made a delayed
    recall reopen a later completion after a serve/recall/serve cycle, and it is
    exactly the case a source-state check cannot see.
    """
    supplied = command.if_revision
    # DEFENCE IN DEPTH AT THE COMPARE-AND-SET ITSELF. `KitchenCommand` now
    # refuses a non-integer at construction, but this is the one line the whole
    # token rests on and `!=` alone reads as exact while Python's numeric tower
    # is not: `False == 0` and `1.0 == 1` are both True, so either value
    # satisfied a precondition it had never been checked against. Re-asserting
    # the type here costs nothing and means the guarantee does not depend on
    # which constructor a future caller reached for.
    if isinstance(supplied, bool) or not isinstance(supplied, int):
        raise KitchenRefusal(
            400, REASON_PRECONDITION_INVALID,
            'if_revision must be a whole number.',
        )
    if supplied != order.fulfilment_revision:
        raise _conflict(
            order, REASON_PRECONDITION_STALE,
            'This ticket changed since you loaded it. '
            'Check its current state before trying again.',
        )
    if order.fulfilment_revision >= MAX_REVISION:
        # Refuse rather than overflow the column. Unreachable in practice; a
        # silent DataError at the UPDATE would not be.
        raise _conflict(
            order, REASON_REVISION_EXHAUSTED,
            'This ticket cannot accept further changes. Please ask a manager.',
        )


# --- the authoritative operation -------------------------------------------

def execute(order_id, actor, command):
    """Apply ONE kitchen command, or refuse. Never anything in between.

    Returns ``{'outcome': applied|unchanged, 'state': <projection>}``.

    THE PERMISSION DECISION LINEARIZES AT THE RE-CHECK BELOW — after the Table
    and Order locks are held and the row has been re-read, and before anything is
    written. The central resolver issues fresh queries on every call (it holds no
    request-level cache), so a membership change COMMITTED BEFORE that point is
    respected. It is NOT claimed that permission writers are serialised against
    this transaction: they do not share a lock with it, so a revocation
    committing AFTER this read can still overlap the command. Closing that would
    need those writers to participate in a shared barrier, which is a
    permissions-platform change and is not in scope here.
    """
    _assert_known_action(command.action)

    # THE LOCATOR. It resolves which table to lock, and records the ONE fact the
    # lock cannot recover afterwards: whether this command was formed against a
    # DRAFT. Everything else it returns is re-read under the lock below.
    # Soft-deleted and unknown ids collapse to one non-disclosing outcome.
    try:
        located = (
            Order.objects
            .filter(id=order_id, deleted=False)
            .values('id', 'table_id', 'restaurant_id', 'order_status')
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        raise OrderNotFound()
    if located is None:
        raise OrderNotFound()

    # Authorise BEFORE taking any lock, so a caller with no relationship to this
    # restaurant cannot make one wait. This is a gate, not the decision — the
    # authoritative re-check is inside the transaction.
    if not can_user_access_module(actor, located['restaurant_id'], MODULE_KITCHEN):
        raise KitchenRefusal(
            403, REASON_FORBIDDEN,
            'You do not have permission for this kitchen',
        )

    from restaurants_app.models import Table          # lazy: import-cycle free
    from orders_app.controllers.con_orders import ConOrder

    with transaction.atomic():
        # LOCK ORDER: Table then Order. The tail of acceptance's
        # `advisory -> Table -> Order`, so the two cannot cycle. `of=('self',)`
        # on both, so neither can silently widen onto a joined row — the lesson
        # PR-E recorded when a `select_related` chained to `select_for_update`
        # locked a whole join and closed a real ABBA cycle.
        locked_table = None
        if located['table_id'] is not None:
            locked_table = (
                Table.objects
                .select_for_update(of=('self',))
                .filter(pk=located['table_id'])
                .first()
            )
            if locked_table is None:
                raise OrderNotFound()

        order = (
            Order.objects
            .select_for_update(of=('self',))
            .filter(pk=located['id'], deleted=False)
            .first()
        )
        if order is None:
            raise OrderNotFound()

        # THE SCOPE RE-VERIFICATION. The locks were chosen from a pre-lock read,
        # so the row they now protect must still be the row they were chosen for.
        # No production path reassigns `Order.table` today, which makes this
        # cheap — but that is a fact about the current tree, not a licence to
        # trust a stale field. A RUNTIME check, never an `assert`, because
        # `python -O` removes those. On a mismatch we refuse having written
        # nothing and WITHOUT reaching for the other table, which would acquire
        # locks out of order.
        if (
            locked_table is not None
            and (order.table_id != locked_table.pk
                 or order.restaurant_id != locked_table.restaurant_id)
        ):
            logger.error(
                'Kitchen command refused on a scope mismatch '
                '(order_id=%s, locked_table=%s, order_table=%s)',
                order.pk, locked_table.pk, order.table_id,
            )
            raise KitchenRefusal(
                409, REASON_SCOPE_MISMATCH,
                'This ticket moved while the request was in flight. '
                'Reload the board and try again.',
            )

        # THE LINEARIZATION POINT. Re-resolved from committed state after the
        # blocking wait — the gate above read a moment that has since passed.
        if not can_user_access_module(actor, order.restaurant_id, MODULE_KITCHEN):
            raise KitchenRefusal(
                403, REASON_FORBIDDEN,
                'You do not have permission for this kitchen',
            )

        # THE ACCEPTANCE BOUNDARY. A command formed against a DRAFT must not
        # become a command against the order the diner has meanwhile placed.
        #
        # The window is this service's own: the locator reads before the table
        # lock, and `_submit_order` holds that same lock while it flips
        # `initiated -> pending`. So a kitchen command can locate a draft, block,
        # and find an ordinary accepted order waiting — `_assert_operable` then
        # passes, and because SUBMISSION IS NOT A KITCHEN COMMAND it does not
        # advance the revision, so the precondition captured against the draft
        # still matches. Measured: a cancel formed against a draft applied to the
        # diner's just-placed order.
        #
        # THE HARM IS A DELAYED COMMAND BECOMING A DIFFERENT COMMAND. The
        # operator formed "cancel this draft" — which the server refuses outright
        # — and timing alone turned it into "cancel this diner's live order".
        #
        # The remedy is here rather than in `_submit_order`: bumping the revision
        # on acceptance would put a NON-kitchen event into a token documented as
        # versioning kitchen state, and would make every ticket's first command
        # depend on how it came to exist.
        if (
            located['order_status'] == OrderStatus_Initiated
            and order.order_status != OrderStatus_Initiated
        ):
            logger.info(
                'Kitchen command refused across the acceptance boundary '
                '(order_id=%s, action=%s)', order.pk, command.action,
            )
            raise _conflict(
                order, REASON_ACCEPTED_WHILE_WAITING,
                'This order was placed while your request was in flight. '
                'Check the board before trying again.',
            )

        _assert_operable(order)

        # The management escalation is STATE-DEPENDENT, so it is decided from the
        # fresh fulfilment status and not from what the caller saw. Ordinary
        # kitchen staff used to keep a free void they had qualified for moments
        # earlier, straight through preparation starting.
        if (
            command.action == ACTION_CANCEL
            and order.fulfilment_status in ('preparing', 'ready')
            and not can_manage_restaurant(actor, order.restaurant_id)
        ):
            raise KitchenRefusal(
                403, REASON_MANAGE_REQUIRED,
                'Only a manager can cancel an order once preparation has started',
            )

        _assert_revision(order, command)

        # ONE server clock, captured AFTER the lock waits, so an age rule is
        # measured from the moment the decision is actually being made rather
        # than from one sampled before the request queued.
        now = timezone.now()

        if command.action == ACTION_SET_PRIORITY:
            return _apply_priority(order, command)
        if command.action == ACTION_CANCEL:
            return _apply_cancel(order, command, actor, now)
        return _apply_fulfilment(order, command, actor, now, locked_table,
                                 ConOrder)


def read_state(order_id, actor):
    """OBSERVE one order. The reconciliation surface the FEEDS CANNOT REPLACE.

    A kitchen command whose reply is lost leaves the client unable to say whether
    the server acted. An ordinary feed answers that for a ticket still on a
    board — but the commands whose outcome matters most are exactly the ones that
    REMOVE the order from both feeds: a cancellation, and a serve past the
    Completed window. "It is not on the board" is then not an answer about
    whether the command ran, and a client with nothing else to ask was left
    either reporting a failure that may not have happened or showing a warning
    nobody could ever clear.

    FIVE THINGS ARE LOAD-BEARING.

    1. **IT IS AN OBSERVATION, NOT A VERDICT.** It returns what the order IS. It
       draws no conclusion about which command produced that state, and there is
       deliberately no field in which it could — attributing a state to a caller's
       earlier command is a causal claim this row cannot support, and the client
       says so in its own words too.

    2. **NO LOCK, NO TRANSACTION, NO WRITE.** Nothing is repaired, stamped,
       normalised or reconciled; the revision does not move. A read that took the
       `Table` lock would queue behind live service to answer a question about the
       past.

    3. **NO ELIGIBILITY FILTER.** A draft, a cancelled order, a served one and a
       paid one all answer. Refusing them would remove exactly the orders this
       exists for. Eligibility is a question about what may be COMMANDED, and
       ``execute`` still owns it.

    4. **THE SAME PROJECTION AS EVERY COMMAND ANSWER** (``order_state``), so a
       client reconciles against ONE shape however it obtained it. A second,
       slightly different read shape is how two surfaces start disagreeing.

    5. **THE SAME SCOPE RULE AS A COMMAND.** The module gate is evaluated against
       the order's own restaurant; an unknown, soft-deleted or unparseable id is
       the one non-disclosing ``OrderNotFound``. It is a strictly narrower answer
       than the feeds this caller may already read.
    """
    try:
        order = Order.objects.filter(id=order_id, deleted=False).first()
    except (ValidationError, ValueError, TypeError):
        raise OrderNotFound()
    if order is None:
        raise OrderNotFound()

    if not can_user_access_module(actor, order.restaurant_id, MODULE_KITCHEN):
        raise KitchenRefusal(
            403, REASON_FORBIDDEN,
            'You do not have permission for this kitchen',
        )
    return order_state(order)


def _assert_known_action(action):
    """The command vocabulary, checked HERE and not only at the adapter.

    An unrecognised action is a controlled refusal, never a reinterpretation.
    It is checked before ANY lookup, so it discloses nothing and costs nothing.
    """
    if not isinstance(action, str) or action not in ALL_ACTIONS:
        raise KitchenRefusal(
            400, REASON_ACTION_UNKNOWN, 'Unknown action for this request.',
        )


def _bump(order, fields, actor=None, stamp_fulfilment=None):
    """Persist one applied command: its own fields, the revision, and nothing
    else. FIELD-SCOPED so a full-model save can never carry stale values from an
    instance loaded earlier over the token or any protected column."""
    order.fulfilment_revision = order.fulfilment_revision + 1
    update_fields = list(fields) + ['fulfilment_revision', 'time_last_updated']
    if stamp_fulfilment:
        order.fulfilment_status_updated_at = stamp_fulfilment
        order.fulfilment_status_updated_by = actor
        update_fields += [
            'fulfilment_status_updated_at', 'fulfilment_status_updated_by',
        ]
    order.save(update_fields=update_fields)
    return {'outcome': OUTCOME_APPLIED, 'state': order_state(order)}


def _apply_priority(order, command):
    """Priority is a statement about WHAT THE KITCHEN SHOULD COOK NEXT, so it
    applies only to a ticket the kitchen is still working on.

    A SERVED ticket used to accept one, and the harm is not that the flag is
    meaningless there — it is that applying it BUMPS THE REVISION. A served
    ticket is recall-eligible for ten minutes, and an operator holding that
    ticket's revision would find their recall refused as stale because a stray
    priority tap had spent it, with the window running down while they reloaded.
    The eligibility question is asked BEFORE the equality one, as it is
    everywhere else here: answering "no change" would say priority applies to a
    completed ticket, which is the thing being denied.

    Setting priority to the value it already holds is a NO-WRITE result.

    It reports the CURRENT state and claims nothing about history: it does not
    say this caller's earlier command succeeded, and it deliberately moves
    neither the revision nor `time_last_updated`, so it leaves no trace that
    could be mistaken for one. It is available only because authorisation,
    eligibility and the supplied revision have all already passed — equality
    with a STALE revision is a conflict, refused above.
    """
    if order.fulfilment_status not in ACTIVE_FULFILMENT_STATUSES:
        raise _conflict(
            order, REASON_ILLEGAL,
            f'Priority does not apply to a ticket that is {order.fulfilment_status}.',
        )
    if order.priority == command.priority:
        return {'outcome': OUTCOME_UNCHANGED, 'state': order_state(order)}
    order.priority = command.priority
    return _bump(order, ['priority'])


def _apply_cancel(order, command, actor, now):
    """Cancel writes `order_status` and the cancellation provenance, and leaves
    the fulfilment axis exactly where it was — the established contract, and the
    reason a cancelled row legitimately retains its last fulfilment state.

    An already-cancelled order never reaches here: `_assert_operable` refuses it
    as a no-write conflict, which preserves the ORIGINAL actor, reason and time.
    A retry is told what the ticket is now, never that its own command ran.
    """
    if order.fulfilment_status == 'served':
        raise _conflict(
            order, REASON_ILLEGAL,
            'Cannot cancel a served order; recall it first',
        )
    order.order_status = OrderStatus_Cancelled
    order.cancelled_at = now
    order.cancelled_by = actor
    order.cancellation_reason = command.cancellation_reason
    return _bump(order, [
        'order_status', 'cancelled_at', 'cancelled_by', 'cancellation_reason',
    ])


def _apply_fulfilment(order, command, actor, now, locked_table, ConOrder):
    current = order.fulfilment_status

    if command.action == ACTION_ADVANCE:
        target = ADVANCE_EDGES.get(current)
        if target is None:
            raise _conflict(
                order, REASON_ILLEGAL,
                f'This ticket cannot be advanced from {current}.',
            )
        order.fulfilment_status = target
        return _bump(order, ['fulfilment_status'], actor, stamp_fulfilment=now)

    if command.action == ACTION_SERVE:
        if current != 'ready':
            raise _conflict(
                order, REASON_ILLEGAL,
                f'This ticket cannot be served from {current}.',
            )
        # The completion transition: the fulfilment axis and the commercial one
        # move together, atomically, so no response can describe half of it.
        # `_assert_operable` has already established the order is neither a draft
        # nor cancelled, so this can no longer resurrect either into a sale.
        order.fulfilment_status = 'served'
        order.served_at = now
        order.order_status = OrderStatus_Served
        return _bump(
            order, ['fulfilment_status', 'served_at', 'order_status'],
            actor, stamp_fulfilment=now,
        )

    if command.action == ACTION_CORRECT:
        # Active correction. It carries NO time rule: nothing was completed, so
        # there is no completion to be within a window of.
        if current != 'ready':
            raise _conflict(
                order, REASON_ILLEGAL,
                f'This ticket cannot be sent back to preparing from {current}.',
            )
        order.fulfilment_status = 'preparing'
        return _bump(order, ['fulfilment_status'], actor, stamp_fulfilment=now)

    # ACTION_RECALL — served -> ready.
    if current != 'served':
        raise _conflict(
            order, REASON_ILLEGAL,
            f'This ticket cannot be recalled from {current}.',
        )
    if order.served_at is None:
        # `_assert_operable` already requires a served stamp on a coherent served
        # order, so this is belt and braces — but a missing stamp must never read
        # as an unbounded window.
        raise _conflict(
            order, REASON_INCOHERENT,
            'This order is in a state the kitchen cannot act on. '
            'Please ask a manager to review it.',
        )
    age = now - order.served_at
    if age < timedelta(0) or age > RECALL_WINDOW:
        # A completion stamped in the FUTURE is not an unlimited window; it is an
        # inconsistency, and it is refused by the same rule.
        raise _conflict(
            order, REASON_RECALL_EXPIRED,
            'This order was completed too long ago to be recalled.',
        )

    # OCCUPANCY, under the SAME table lock acceptance takes. A served order frees
    # its table, so another order may legitimately have claimed it since — and a
    # recall that ignored this produced two ongoing orders on one table, both in
    # a race and deterministically. The recalled order is itself still `served`,
    # so the predicate already excludes it; the not-this-order guard is defence
    # in depth. The occupying order is NOT named in the refusal.
    if locked_table is not None:
        ongoing = ConOrder.any_present_ongoing_order(locked_table)
        if ongoing.get('present') and ongoing.get('order_id') != order.pk:
            raise _conflict(
                order, REASON_TABLE_OCCUPIED,
                'That table already has an ongoing order, so this one cannot be '
                'recalled.',
            )

    order.fulfilment_status = 'ready'
    order.served_at = None
    fields = ['fulfilment_status', 'served_at']
    # Undo ONLY the coupling serving set. A row whose `order_status` is something
    # else is left alone rather than being pushed to `pending` on a guess —
    # though `_assert_operable` means a coherent served order always carries
    # `served`, so this is the ordinary path rather than an exception.
    if order.order_status == OrderStatus_Served:
        order.order_status = OrderStatus_Pending
        fields.append('order_status')
    return _bump(order, fields, actor, stamp_fulfilment=now)
