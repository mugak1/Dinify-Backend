from rest_framework.serializers import ModelSerializer, SerializerMethodField
from misc_app.controllers.money import format_money
from orders_app.controllers.orders.serializers import (
    _quote_line, quote_policy_projection,
)
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED,
)
from orders_app.controllers.services.acceptance_result import (
    acceptance_result, read_evidence,
)
from orders_app.controllers.services.checkout_protocol import (
    CHECKOUT_PROTOCOL,
)
from orders_app.controllers.services.quote_closure import (
    closure_projection, read_closure,
)
from orders_app.controllers.services.quote_protocol import QUOTE_PROTOCOL
from orders_app.controllers.services.order_quote import group_live_children
from orders_app.models import Order, OrderItem


class SerializerPutOrderItem(ModelSerializer):
    """
    Internal-only serializer for creating an order item during order build.

    order / item / parent_item and the audit FKs are SERVER-DERIVED: the
    order-build service (con_orders) passes the already-resolved, tenant-scoped
    objects via ``save()`` kwargs — they are never accepted from request input
    (read_only). A caller therefore cannot construct an OrderItem that links a
    foreign menu item to an order through this serializer.
    """
    class Meta:
        model = OrderItem
        fields = (
            # server-derived relations (set via save() by the order-build service)
            'id', 'order', 'item', 'parent_item',
            # business values written by the order-build service
            'available', 'option', 'option_choice', 'option_cost',
            'quantity', 'unit_price', 'discounted_price', 'discounted',
            'unit_cost_of_options', 'options', 'selected_modifiers',
            'item_name_snapshot', 'modifiers_snapshot', 'allergen_tags_snapshot',
            'total_cost', 'discounted_cost', 'savings', 'cost_of_options',
            'actual_cost', 'status',
            # audit / lifecycle (output-only)
            'last_updated_by', 'created_by', 'deleted_by', 'deleted',
            'time_deleted', 'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'order', 'item', 'parent_item', 'last_updated_by',
            'created_by', 'deleted_by', 'deleted', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )


class SerializerListOrderItem(ModelSerializer):
    item = SerializerMethodField()
    extra_items = SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'item', 'available',
            'quantity', 'unit_price',
            'discounted_price', 'savings',
            'options', 'cost_of_options',
            'actual_cost', 'status',
            'deleted', 'deletion_reason',
            'time_last_updated', 'extra_items'
        )

    def get_item(self, item):
        return {
            'id': item.item.pk,
            'name': item.item.name,
            'is_special': item.item.is_special,
        }

    def get_extra_items(self, item):
        extras = []
        extra_items = OrderItem.objects.filter(parent_item=item)
        for extra in extra_items:
            extras.append({
                'id': extra.pk,
                'name': extra.item.name,
                'quantity': extra.quantity,
                'unit_price': extra.unit_price,
                'discounted_price': extra.discounted_price,
                'savings': extra.savings,
                'actual_cost': extra.actual_cost,
                'status': extra.status,
                'deleted': extra.deleted,
                'deletion_reason': extra.deletion_reason,
                'time_last_updated': extra.time_last_updated
            })
        return extras


class SerializerPublicOrderDetails(ModelSerializer):
    """The diner's own read of an order on their table's session.

    D04/U1 ADDS TWO KEYS, AND THEY ARE THE SAME FACTS THE INITIATE RESPONSE
    ALREADY PUBLISHES — read here through the SAME formatter and the SAME live
    population rule, never a second definition.

    WHY THEY WERE MISSING. ``quote_total`` and ``quote_complete`` were produced
    only by ``serialize_order_details``, which assembles the *initiate*
    response. So the one read a diner can make about an order AFTER it is
    accepted published neither, and the browser journey failed on both at
    f426eba/d5d886e. That failure was a DISAGREEMENT ABOUT AVAILABLE FIELDS —
    it never showed that the stored payable was wrong.

    AND THE GAP WAS THE NAME, NOT THE PRECISION — worth stating because the
    opposite is the natural assumption. ``actual_cost`` is lossy on the
    INITIATE response, where a raw ``Decimal`` sits in a plain dict and the
    renderer encodes it through ``float()``. Here it is a ``ModelSerializer``
    ``DecimalField``, which coerces to a string, so it was already exact. What
    was absent was the key every surface can be held to, and any statement at
    all about completeness.

    WHAT THEY ARE. A description of the SAVED RECORD AS THIS READ OBSERVES IT.
    They are NOT a durable historical acceptance receipt: nothing here records
    which quote a diner accepted or when. ``quote_complete`` claims exactly
    what the shared rule proves and no more — that every live child sits under
    a live parent, so an itemisation built from these rows can represent the
    payable. It is not a statement that the order is correct, paid, priced by
    any particular convention, or still deliverable.

    NOTHING IS REPRICED. No catalogue is read, no quantity is inferred, no
    amount is recomputed; the historical values on the row are formatted and
    returned. Every existing field keeps its established meaning and type,
    including the legacy numeric ones.
    """
    items = SerializerMethodField()
    quote = SerializerMethodField()
    quote_total = SerializerMethodField()
    quote_complete = SerializerMethodField()
    accepted = SerializerMethodField()
    accepted_at = SerializerMethodField()
    checkout_protocol = SerializerMethodField()
    checkout = SerializerMethodField()
    quote_protocol = SerializerMethodField()
    quote_policy = SerializerMethodField()
    quote_closure = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'table',
            'total_cost', 'discounted_cost', 'savings',
            'actual_cost', 'prepayment_required',
            'payment_status', 'order_status',
            'items', 'order_number',
            'total_paid', 'balance_payable', 'payment_status',
            'time_last_updated',
            # D04/U1 — see the class docstring.
            'quote', 'quote_total', 'quote_complete',
            # D04/C — the acceptance fact, and what this server can promise.
            'accepted', 'accepted_at', 'checkout_protocol',
            # D04 completion — the correlated projection. See `get_checkout`.
            'checkout',
            # D06 completion, G3a — the quote's own life, on the one surface a
            # client that lost a response can still reach. See `get_quote_closure`.
            'quote_protocol', 'quote_policy', 'quote_closure',
        )

    def _rows(self, order):
        """Every row of this order, fetched ONCE for every method field.

        ``items`` has always included soft-deleted rows and continues to; the
        quote and the completeness rule need the LIVE population and filter
        this list in Python rather than issuing more queries, so the three new
        keys cost no extra round trip. ``OrderItem`` carries ``Meta.ordering``,
        so materialising the queryset preserves the order ``items`` already had.

        ``select_related('item')`` matches ``serialize_order_details`` and is a
        REDUCTION: ``SerializerListOrderItem.get_item`` already dereferenced
        ``item.item`` per row, one query apiece.
        """
        cached = getattr(self, '_row_cache', None)
        if cached is None or cached[0] != order.pk:
            cached = (order.pk, list(
                OrderItem.objects.filter(order=order).select_related('item')
            ))
            self._row_cache = cached
        return cached[1]

    def _live_split(self, order):
        """The live population and its parent/child map — ``order_quote``'s
        split, never a local copy of it."""
        live_rows = [row for row in self._rows(order) if not row.deleted]
        by_parent, orphaned = group_live_children(live_rows)
        return live_rows, by_parent, orphaned

    def get_items(self, order):
        return SerializerListOrderItem(self._rows(order), many=True).data

    def get_quote(self, order):
        """THE canonical itemised review of the SAVED rows.

        Built by ``_quote_line`` — the same function the initiate response
        uses — so the lines a diner reviews before accepting and the lines this
        read publishes afterwards cannot be two different opinions about one
        order. It repeats no arithmetic: every amount is formatted from a
        persisted column.

        WHY THE READ CARRIES IT AT ALL. A checkout recovered after a lost
        response has to be reviewable, and a review means the server's lines
        and the server's total — not the browser's arithmetic over a basket it
        still happens to hold. Publishing it here is what lets D04's recovery
        reuse this projection instead of defining a second one.
        """
        _live, by_parent, _orphaned = self._live_split(order)
        corrected = order.pricing_version == PRICING_VERSION_CORRECTED
        return [
            _quote_line(row, by_parent.get(row.pk, []), corrected)
            for row in self._rows(order)
            if not row.deleted and row.parent_item_id is None
        ]

    def get_quote_total(self, order):
        """The payable as a CANONICAL DECIMAL STRING.

        ``format_money`` is the one sanctioned way a monetary value leaves the
        server as text, and it is used here for the reason it exists: DRF
        encodes a ``Decimal`` through ``float()``, so the ``actual_cost`` key
        beside this one reaches the browser having already lost digits. This
        key is the exact saved amount; that one keeps its legacy numeric form
        for older clients.
        """
        return format_money(order.actual_cost, field='quote_total')

    def get_quote_complete(self, order):
        """Can the itemisation above represent the saved payable?

        ``group_live_children`` is THE definition, shared with the initiate
        serializer and with the check that decides whether an acceptance may be
        honoured. A live child whose parent is not in the live population
        belongs under no quoted line while its amount is still inside
        ``actual_cost``, so a quote built from the remaining lines cannot add
        up. Two copies of this rule would disagree exactly where it matters.

        It claims nothing beyond that. Not that the order is correct, paid,
        still deliverable, or priced by any particular convention.
        """
        _live, _by_parent, orphaned = self._live_split(order)
        return not orphaned

    def _evidence(self, order):
        """The acceptance row, read ONCE for every key below.

        Through `read_evidence`, which prefers a JOINED relation — the same
        reading `acceptance_result` uses, so the two cannot form different
        opinions, and a caller that joined (the diner's recovery read does)
        never re-reads the row from a newer snapshot than the order's.
        """
        cached = getattr(self, '_acceptance_cache', None)
        if cached is None or cached[0] != order.pk:
            cached = (order.pk, read_evidence(order))
            self._acceptance_cache = cached
        return cached[1]

    def get_accepted(self, order):
        """DID THE DINER'S SUBMISSION LAND? (D04/C)

        The one question a client whose response was lost actually has, and
        the one no field here could previously answer. `order_status` cannot:
        the kitchen moves it to `preparing` and `served`, a recall moves it
        back and a cancellation moves it elsewhere, so by the time a retry
        looks it describes the KITCHEN's progress, not whether the order was
        ever accepted.

        A CANCELLED ORDER THAT WAS ACCEPTED STILL READS `true`, deliberately:
        the submission did land, and the cancellation is a later, separate
        fact this same response already carries in `order_status`. Collapsing
        the two would tell a diner their order never went through.

        FALSE MEANS "NO EVIDENCE", which covers a genuine draft and an order
        accepted BEFORE D04 alike — nothing recorded the latter, and inventing
        a value for it would be a claim this server cannot support.
        """
        return self._evidence(order) is not None

    def get_accepted_at(self, order):
        """When the acceptance COMMITTED, or null.

        Distinct from `time_last_updated`, which every later kitchen action
        moves — which is exactly why that field could never serve as the
        answer.
        """
        evidence = self._evidence(order)
        return None if evidence is None else evidence.accepted_at

    def get_checkout_protocol(self, order):
        """What THIS server can promise a checkout client.

        The same constant the initiate response carries, published here too
        because this is the surface a RECOVERING client reads — and a client
        deciding whether a retry is safe needs the answer from whichever
        response it actually has. An ABSENT value means level 0: promise
        nothing. It is deliberately not `pricing_version`, which describes how
        the money was calculated.
        """
        return CHECKOUT_PROTOCOL

    def get_quote_protocol(self, order):
        """What THIS server can promise about the LIFE of a saved quote.

        The same constant the initiate response carries, for the same reason
        `checkout_protocol` is here: this is the surface a RECOVERING client
        reads, and the promise has to be readable from whichever response it
        actually holds. A SEPARATE level from `checkout_protocol` — that one
        answers "can an uncertain checkout be retried and recovered", this one
        "may this quote still be accepted" — and an ABSENT value is level 0,
        which promises nothing rather than meaning "quotes never expire here".
        """
        return QUOTE_PROTOCOL

    def get_quote_policy(self, order):
        """The DEADLINE, read through the same projection the initiate response
        uses so the two cannot become two opinions about one rule.

        ADVISORY, as it is everywhere: the decision is made inside the
        acceptance transaction from a clock sampled after its locks, and the two
        can legitimately disagree by the width of a request. That is why a
        client is given `expires_at` rather than a boolean.
        """
        return quote_policy_projection(order)

    def get_quote_closure(self, order):
        """HAS THIS QUOTE BEEN RETIRED? `null` when it has not.

        THE GAP THIS CLOSES (D06 completion, G3a). A closure is the durable half
        of a terminal refusal — it is what makes minting a replacement quote
        safe, because an acceptance still in flight for the old one can never
        execute afterwards. It was published ONLY on the refusal response, which
        is the one thing a client can lose; this read published neither the
        deadline nor the closure, so a quote retired for `purchase_needs_review`
        INSIDE its window was invisible on every surface a recovering client
        could reach. Its only remaining move was to attempt an acceptance — the
        exact thing `retire-quote` exists to avoid, because when the quote IS
        still good that attempt succeeds, claims a table and sends food to a
        kitchen in order to ask a question.

        THE SAME PROJECTION THE REFUSAL CARRIES, from the same function, so a
        response that says closed and a read that says nothing is not a state
        this code can reach. Bounded deliberately: what happened, when, to which
        reference, under which policy — no actor, no amounts, no catalogue
        detail, no order contents.

        IT COSTS NO QUERY on the diner's read, which folds the relation into the
        order fetch. That is a CORRECTNESS rule before it is a cost one, and the
        same lesson D04 records: under READ COMMITTED each statement takes its
        OWN snapshot, so reading the order in one statement and the closure in
        another lets a closure commit between them and publishes a correlated
        answer describing a moment that never existed.
        """
        return closure_projection(read_closure(order))

    def get_checkout(self, order):
        """THE CORRELATED ANSWER, identical to the one the submit result
        carries.

        `acceptance_result` is THE projection, shared with
        `manage_order._submit_order`, so a client that lost its acceptance
        response and recovers through this read is told the same facts in the
        same shape — rather than having to reconcile two surfaces that each
        describe an acceptance their own way.

        WHY THE `accepted` / `accepted_at` KEYS ABOVE STAY. They are the
        level-2 contract and a deployed client reads them; removing them would
        break it for the width of a deploy, which is precisely the failure
        #661 and the `quote_total` window taught. They keep their EXACT old
        meaning — including the draft/legacy conflation `get_accepted`'s own
        docstring concedes — because a compatibility key that quietly changed
        semantics would be worse than one that is merely coarse. The
        distinction lives in `checkout.acceptance.state`, and a client must
        read `checkout_protocol >= 3` before relying on it.

        IT COSTS NO EXTRA QUERY: `_evidence` has already fetched and cached the
        acceptance row for the two keys above, and it is passed straight in.
        """
        return acceptance_result(order, evidence=self._evidence(order))
