"""
Diner-facing order serialization.

D02 changed what some of these values MEAN internally, so this module is where the
public contract is held steady. The mapping is explicit rather than assumed:

  FIELD           LEGACY MEANING (kept on the wire)      CORRECTED INTERNAL VALUE
  unit_price      reference BASE per unit, no modifiers  reference base + modifiers
  total_cost      reference base x quantity              (base + modifiers) x qty
  discounted_cost effective (base + modifiers) x qty     unchanged
  actual_cost     payable                                unchanged (now correct)
  savings         reference - effective                  unchanged FORMULA, but the
                                                         reference now includes the
                                                         same modifiers, so it can
                                                         no longer be negative

``unit_price`` and ``total_cost`` are emitted in their LEGACY form for a CORRECTED
order — recovered exactly by subtracting the modifier component, which is what they
held before — so an older client reading them still reads the number it expects.
The corrected, modifier-inclusive figures are exposed ADDITIVELY as
``reference_unit_price`` / ``reference_total_cost``.

``savings`` is the one changed contract, and it is changed deliberately: its old
value was not a meaning but a defect. Any paid modifier made it NEGATIVE and made
an order's net exceed its gross, because the reference excluded modifiers while the
effective included them. It is documented in BREAKING_CHANGES.md rather than
preserved.
"""
import logging
from decimal import Decimal

from django.utils import timezone

from misc_app.controllers.money import format_money, working_context
from orders_app.models import Order, OrderItem
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED,
)
from orders_app.controllers.services.quote_protocol import QUOTE_PROTOCOL
from orders_app.controllers.services.quote_policy import (
    assess as assess_quote_age,
)
from orders_app.controllers.services.checkout_protocol import (
    CHECKOUT_PROTOCOL,
)
from orders_app.controllers.services.order_quote import (
    group_live_children, quote_ref,
)

logger = logging.getLogger(__name__)


def _legacy_view(item, corrected):
    """``(unit_price, total_cost, savings)`` as the established wire contract.

    For a CORRECTED order the modifier component is subtracted back out of the
    reference figures, reproducing exactly what the pre-D02 code stored. For a
    LEGACY order the stored values are already in that form and pass through.

    THE SUBTRACTION IS EXACT, AND THE CONTEXT LIVES HERE RATHER THAN AT THE
    CALL SITES. It is composite Decimal arithmetic on money, so ``money.py``'s
    rule applies to it directly: the process default is 28 significant digits
    against columns that hold 50, and ``-`` ROUNDS SILENTLY there rather than
    raising. ``_legacy_total`` wrapped its own ``sum`` and so looked covered,
    but the two OTHER callers — ``_quote_line`` and
    ``serialize_order_item_details`` — invoke this from outside any context, so
    the rounding happened before either of them could act on the value. Making
    exactness a property of this function is what closes all three at once.
    """
    if not corrected:
        return item.unit_price, item.total_cost, item.savings
    with working_context():
        unit = item.unit_price - (item.unit_cost_of_options or 0)
        total = item.total_cost - item.cost_of_options
    return unit, total, item.savings


def _legacy_total(rows, corrected):
    """The legacy order total, summed UNDER THE MODULE'S DECIMAL CONTEXT.

    The process default is 28 significant digits and the monetary columns hold
    50, so `+` here ROUNDS SILENTLY rather than raising: an order combining a
    large schema-valid line with a small one came back short of what its own
    rows store, and nothing downstream could tell. See money.working_context.
    """
    with working_context():
        total = sum((_legacy_view(r, corrected)[1] for r in rows), Decimal('0'))
    return total


def quote_policy_projection(order):
    """What the D06 lifetime rule says about this saved quote, right now.

    READ-ONLY AND ADVISORY. This is a serializer: it takes no lock, opens no
    transaction and writes nothing, so what it publishes is a snapshot the
    client may act on but must never treat as a decision. The DECISION is made
    inside the acceptance transaction, from a clock sampled after its locks —
    the two can legitimately disagree by the width of a request, and when they
    do the transaction is right.

    That is exactly why a client is given the DEADLINE rather than a boolean:
    a boolean computed here would be a verdict this code is in no position to
    reach, while `expires_at` stays true for as long as the quote exists and
    lets the client decide for itself when to stop waiting.

    It reads no database — `quote_policy.assess` is pure over a column the
    caller already has — so it adds no query to a read whose cost is pinned.
    """
    age = assess_quote_age(order, timezone.now())
    return {
        'version': age.policy_version,
        'status': age.status,
        'expires_at': (
            age.expires_at.isoformat() if age.expires_at is not None else None
        ),
    }


def serialize_order_details(order: Order) -> dict:
    # ONE fetch, `select_related` so the per-row `item.name` reads below do not
    # become a query apiece, and ONE population derived from it.
    rows = list(
        OrderItem.objects.filter(order=order).select_related('item')
    )
    corrected = order.pricing_version == PRICING_VERSION_CORRECTED

    # THE LIVE POPULATION, DEFINED ONCE AND USED FOR EVERYTHING. It used to be
    # defined here and then applied to only two of the five things that read
    # the rows: the legacy total and the quote reference took `live_rows` while
    # the parent/child map, the flat collections and the availability counts
    # took the unfiltered list. A soft-deleted parent or child was therefore
    # presented as an active quoted purchase while the reference and the rollup
    # the diner's acceptance is bound to excluded it — one response holding two
    # answers to "what is in this order". `update_order_amounts` reconciles
    # over undeleted rows, so this is the basis the saved payable was built on.
    live_rows = [r for r in rows if not r.deleted]

    # Children are attached only to a parent that is ITSELF in the population.
    # A live child of a non-live parent belongs under no quoted line, so it is
    # tracked separately rather than silently dropped — see `quote_complete`.
    # The split is `order_quote`'s, NOT a local copy: the acceptance transition
    # refuses exactly the population this call declares incomplete, and a second
    # implementation is how a response and a transition come to disagree.
    by_parent, orphaned_children = group_live_children(live_rows)

    if orphaned_children:
        # Bounded: an order id and a count. No amounts, no order contents, no
        # personal data.
        logger.warning(
            'Order %s has %d live order item(s) whose parent is not in the '
            'live population; its quote cannot represent the saved payable',
            order.pk, len(orphaned_children),
        )

    non_extra_items = [r for r in live_rows if r.parent_item_id is None]
    extra_items = [r for r in live_rows if r.parent_item_id is not None]
    parent_items = [r for r in non_extra_items if r.available]
    unavailable_parent_items = [r for r in non_extra_items if not r.available]
    unavailable_parent_ids = {r.pk for r in unavailable_parent_items}
    available_extras_selected = [r for r in extra_items if r.available]
    # P5: an extra that was zeroed only BECAUSE its parent is undeliverable is
    # not a second, separate loss — the diner lost the dish, and the extra went
    # with it. Reporting both would show one dropped selection twice.
    unavailable_extras_selected = [
        r for r in extra_items
        if not r.available and r.parent_item_id not in unavailable_parent_ids
    ]

    serialized = {
        'id': str(order.pk),
        'time_created': order.time_created,
        'order_number': order.order_number,

        'restaurant': str(order.restaurant.pk),
        'table': str(order.table.pk),
        'table_number': str(order.table.number),

        # Legacy wire meaning: the reference total EXCLUDING modifiers, which is
        # what this key held before D02. Recovered exactly by subtracting the
        # modifier component the corrected reference now carries.
        'total_cost': (
            _legacy_total(live_rows, corrected) if corrected else order.total_cost
        ),
        'discounted_cost': order.discounted_cost,
        'savings': order.savings,
        'actual_cost': order.actual_cost,
        'prepayment_required': order.prepayment_required,

        # ADDITIVE (D02): the corrected, modifier-inclusive reference total, and
        # the acknowledgement the diner's review is bound to.
        'reference_total_cost': order.total_cost,
        'pricing_version': order.pricing_version,
        'quote_ref': quote_ref(order, rows=live_rows),

        # ADDITIVE (R2): does `quote` below represent every live row that the
        # saved payable includes? It is FALSE only when the record cannot
        # supply a coherent quote — today, when a live child's parent is not in
        # the live population, so the child's amount is in `actual_cost` with
        # no line to sit under.
        #
        # THE SAVED AMOUNTS ARE NEVER REWRITTEN TO MAKE THE LINES ADD UP. The
        # alternative — quietly trimming the total to the representable lines —
        # would produce a quote that appears to reconcile while charging
        # something else, which is the precise failure the itemised quote
        # exists to prevent. The response states the shortfall instead and the
        # result is non-confirmable: the client's reconciliation refuses it,
        # and this flag says so independently of that arithmetic.
        'quote_complete': not orphaned_children,

        # ADDITIVE (D02/A): the payable as a CANONICAL DECIMAL STRING — the exact
        # figure the review sheet states and the diner confirms. The legacy
        # `actual_cost` above keeps its established numeric form for older
        # clients, and it is not the same thing on the wire: DRF renders a
        # `Decimal` through `float()`, so that key reaches the browser as
        # `899.1` rather than `899.10` and loses digits outright above ~15
        # significant figures. An amount a diner is asked to agree to must not be
        # carried by a type that cannot represent it.
        'quote_total': format_money(order.actual_cost, field='quote_total'),

        # ADDITIVE (D04): WHAT THIS DEPLOYMENT CAN PROMISE about retrying an
        # uncertain checkout. A LEVEL, not a boolean, because D04's halves
        # deploy separately and a client must not be told the second exists
        # when only the first does. It is stated directly because every value
        # already on the wire answers a different question — `pricing_version`
        # describes how the MONEY was calculated, and #661 is the standing
        # lesson about collapsing two contract introductions into one flag.
        # An ABSENT value means level 0: promise nothing.
        'checkout_protocol': CHECKOUT_PROTOCOL,

        # ADDITIVE (D06): WHAT THIS DEPLOYMENT CAN PROMISE about the life of
        # this saved quote, and what the rule currently says about THIS one.
        #
        # A SEPARATE LEVEL FROM `checkout_protocol`, deliberately — that one
        # answers "can an uncertain checkout be retried and recovered", this one
        # answers "may this quote still be accepted". A client can want either
        # without the other, and widening level 3 to cover a promise it never
        # made would be #661 in the direction that matters most.
        #
        # `quote_policy` is a DEADLINE, not a reservation: it says when the
        # reviewed amount stops being honoured, and says nothing about whether
        # the dish will still be available when the diner gets there. `status`
        # is the verdict at the instant this response was built — a client that
        # renders a countdown from `expires_at` is doing the right thing; one
        # that treats `live` as a guarantee of acceptance is not.
        'quote_protocol': QUOTE_PROTOCOL,
        'quote_policy': quote_policy_projection(order),

        'no_items': len(parent_items),
        'no_unavailable_items': len(unavailable_parent_items),
        'no_available_items': len(parent_items),
        'no_available_extras': len(available_extras_selected),
        'no_unavailable_extras': len(unavailable_extras_selected),
        'order_status': order.order_status,
        'payment_status': order.payment_status,
    }

    def details(item):
        return serialize_order_item_details(
            item=item, corrected=corrected,
            children=by_parent.get(item.pk, []),
        )

    return {
        'order': serialized,
        'order_items': [details(i) for i in non_extra_items],
        'available_items': [details(i) for i in parent_items],
        'unavailable_items': [details(i) for i in unavailable_parent_items],
        'extras': [details(i) for i in extra_items],
        'available_extras': [details(i) for i in available_extras_selected],
        'unavailable_extras': [details(i) for i in unavailable_extras_selected],
        # ADDITIVE (D02/P9): THE canonical review. Parent lines with their extras
        # nested underneath, with parent-only and parent-plus-extras amounts
        # explicitly distinguished so the same child can never be counted twice.
        # Derived from the saved priced rows — it repeats no arithmetic.
        'quote': [
            _quote_line(item, by_parent.get(item.pk, []), corrected)
            for item in non_extra_items
        ],
    }


def _quote_line(item, children, corrected):
    """One canonical review line: the dish, its extras, and BOTH aggregates.

    ``line_actual_cost`` is the parent alone. ``line_total_with_extras`` is the
    parent plus its extras — a DISPLAY aggregate, named so it cannot be mistaken
    for the stored payable, and never written back onto the parent row. Summing
    the parents' ``line_actual_cost`` plus every extra's own amount, or summing
    ``line_total_with_extras`` across parents, both give the order's payable;
    mixing the two would double-count, which is why they are labelled.
    """
    unit_price, total_cost, savings = _legacy_view(item, corrected)
    with working_context():
        extras_actual = sum(
            (child.actual_cost for child in children), Decimal('0'))
        line_total_with_extras = item.actual_cost + extras_actual
    return {
        'id': str(item.pk),
        'item': str(item.item_id),
        'item_name': item.item_name_snapshot or item.item.name,
        'quantity': item.quantity,
        'available': item.available,
        'status': item.status,
        'selected_modifiers': item.selected_modifiers or {},
        'modifiers': item.modifiers_snapshot or [],
        'options': item.options or [],

        # EVERY AMOUNT HERE IS A CANONICAL DECIMAL STRING, and that is the
        # whole point of this block rather than a formatting preference. DRF
        # encodes a `Decimal` as `float(obj)`, so an exact `Decimal('899.10')`
        # assembled above reaches the browser as `899.1` and a large exact
        # amount as `1e+28`. The quote is the one payload a diner is asked to
        # agree to, so it carries values a JSON number cannot misrepresent.
        # The LEGACY keys on `order_details` are untouched — this is the new
        # contract, not a global renderer change.
        'unit_price': format_money(unit_price, field='unit_price'),
        'reference_unit_price': format_money(item.unit_price,
                                             field='reference_unit_price'),
        'discounted_price': format_money(item.discounted_price,
                                         field='discounted_price'),
        'unit_cost_of_options': format_money(item.unit_cost_of_options or 0,
                                             field='unit_cost_of_options'),
        'discounted': item.discounted,

        'total_cost': format_money(total_cost, field='total_cost'),
        'reference_total_cost': format_money(item.total_cost,
                                             field='reference_total_cost'),
        'discounted_cost': format_money(item.discounted_cost,
                                        field='discounted_cost'),
        'savings': format_money(savings, field='savings'),
        'line_actual_cost': format_money(item.actual_cost,
                                         field='line_actual_cost'),
        'line_total_with_extras': format_money(line_total_with_extras,
                                               field='line_total_with_extras'),

        'extras': [
            {
                'id': str(child.pk),
                'item': str(child.item_id),
                'item_name': child.item_name_snapshot or child.item.name,
                'quantity': child.quantity,
                'available': child.available,
                'status': child.status,
                'unit_price': format_money(child.unit_price,
                                           field='extra.unit_price'),
                'discounted_price': format_money(
                    child.discounted_price, field='extra.discounted_price'),
                'actual_cost': format_money(child.actual_cost,
                                            field='extra.actual_cost'),
            }
            for child in children
        ],
    }


def serialize_order_item_details(item: OrderItem, corrected=None,
                                 children=None) -> dict:
    if corrected is None:
        corrected = item.order.pricing_version == PRICING_VERSION_CORRECTED
    if children is None:
        children = list(OrderItem.objects.filter(parent_item=item))
    unit_price, total_cost, savings = _legacy_view(item, corrected)
    extras_list = [
        {
            'item_name': child.item.name,
            'quantity': child.quantity,
            'actual_cost': child.actual_cost,
            'available': child.available,
            'status': child.status
        }
        for child in children
    ]
    return {
        'item': str(item.item.id),
        'item_name': item.item.name,
        'quantity': item.quantity,

        'unit_price': unit_price,
        'discounted_price': item.discounted_price,
        'discounted': item.discounted,

        'total_cost': total_cost,
        'discounted_cost': item.discounted_cost,
        'savings': savings,
        'actual_cost': item.actual_cost,

        # ADDITIVE (D02): the corrected, modifier-inclusive reference figures and
        # the line's stable identity, which is what lets a client match a server
        # line to the basket line it came from.
        'id': str(item.pk),
        'reference_unit_price': item.unit_price,
        'reference_total_cost': item.total_cost,
        'selected_modifiers': item.selected_modifiers or {},

        'available': item.available,
        'status': item.status,

        # `parent_item_id`, never `parent_item`: the object form lazily
        # SELECTs the parent row, which was one query per extra on every
        # order detail read. The id is already on the row.
        'is_extra': item.parent_item_id is not None,
        'no_extras': len(children),
        'extras': extras_list
    }
