import uuid
from decimal import Decimal

from django.db import models
from users_app.models import User, BaseModel
from restaurants_app.models import Restaurant, MenuItem, Table
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_LEGACY,
)
from dinify_backend.configss.string_definitions import (
    PaymentStatus_Pending, OrderStatus_Initiated,
    OrderItemStatus_Initiated,
    CancellationReason_CustomerChangedMind, CancellationReason_ItemUnavailable,
    CancellationReason_KitchenError, CancellationReason_Duplicate,
    CancellationReason_Other,
)


# Create your models here.
class Order(BaseModel):
    """
    the orders that have been placed
    """
    waiter = models.ForeignKey(
        User,
        null=True,
        on_delete=models.SET_NULL,
        related_name='waiter'
    )
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='restaurant')
    # PROTECT: a table (or, by chain, a restaurant) with orders cannot be hard-deleted,
    # so financial/order history is never silently destroyed by a cascade.
    table = models.ForeignKey(Table, on_delete=models.PROTECT, related_name='table')
    order_number = models.IntegerField(null=True)

    customer_phone = models.CharField(max_length=50, null=True, blank=True)
    customer_email = models.EmailField(max_length=50, null=True, blank=True)
    customer = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='user')
    customer_match_attempted = models.BooleanField(default=False)

    total_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the total cost of the order using primary prices
    discounted_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the total cost of the order using discounted prices
    savings = models.DecimalField(max_digits=50, decimal_places=2)  # the total savings from the order i.e. discounted cost  - total cost  # noqa
    actual_cost = models.DecimalField(max_digits=50, decimal_places=2)  # the actual cost that is payable by the customer
    prepayment_required = models.BooleanField(default=False)

    total_paid = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    balance_payable = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)

    payment_status = models.CharField(max_length=50, default=PaymentStatus_Pending, db_index=True)
    order_status = models.CharField(max_length=50, default=OrderStatus_Initiated, db_index=True)
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='last_updated_by')  # noqa

    # === order provenance + idempotency (Phase 2) ===
    order_source = models.CharField(
        max_length=32,
        choices=[
            ("diner_self_service", "Diner self-service"),
            ("server_assisted", "Server assisted"),
        ],
        default="diner_self_service",
        db_index=True,
    )
    # idempotency key supplied by the diner app (Phase 3); absent today
    client_order_id = models.UUIDField(null=True, blank=True, db_index=True)

    # === what that key was bound to (D04) ===
    # The canonical purchase this order was created for, as `v1:<sha256>` —
    # see `orders_app.controllers.services.order_intent`. It is what makes the
    # idempotency key mean something: without it a replay could only ask "has
    # this key been used?", never "for THIS?", and a request naming three
    # burgers received the one-burger order the key had been used for.
    #
    # SERVER-DERIVED AND NEVER CLIENT-SUPPLIED, like `is_test` and
    # `pricing_version`: computed from the D01-validated request inside the
    # creation transaction, absent from every serializer and from
    # EDIT_INFORMATION.
    #
    # NULLABLE, AND NULL IS NOT AN EMPTY PURCHASE — it means the order predates
    # D04 (or was created without a key). Equivalence to an arriving request
    # CANNOT BE PROVEN for such a row, and it is never populated after the fact
    # from a retry: that would certify a request nobody recorded.
    request_fingerprint = models.CharField(
        max_length=128, null=True, blank=True, db_index=True,
    )

    # === test orders (PR-D, TEST-RESTAURANT-PARITY-00) ===
    # A FLAG, written once when the order is created and never changed afterwards.
    # It is true for either of two reasons:
    #   - TENANT: the restaurant is a test restaurant (`Restaurant.is_test`), so
    #     every order it takes is flagged, in any lifecycle state;
    #   - LIFECYCLE: the restaurant was still `onboarding`, so the order is a
    #     rehearsal the owner placed to prove the flow before going live.
    #
    # A test order is always OPERATIONALLY REAL: it occupies its table, reaches the
    # kitchen board and is served or cancelled like any other order. What the flag
    # changes depends on the restaurant. At a TEST restaurant it changes nothing:
    # the orders count in its reports and dashboards, can be reviewed and are
    # matched to customers. At a REAL restaurant a flagged order is a PRACTICE
    # order, left out of that restaurant's figures, reviews and customer matching.
    # That rule lives in ONE place, `orders_app.controllers.test_orders`; never
    # filter on `is_test=False` directly.
    #
    # SERVER-DERIVED, NEVER CLIENT-SUPPLIED: set in `_create_order` from the
    # admission verdict — `Restaurant.is_test` and
    # `lifecycle_policy.orders_are_commercial(status)`, both read under the
    # admission lock. There is no request field for it, so it cannot be spoofed in
    # either direction — a diner cannot mark a real order as test, and an owner
    # cannot mark a rehearsal as real.
    is_test = models.BooleanField(default=False, db_index=True)

    # === pricing provenance (D02) ===
    # WHICH CALCULATION CONVENTION produced this order's stored amounts.
    #
    # LEGACY (0) is the pre-D02 calculation, under which a line's reference
    # excluded modifiers while its effective included them (so `savings` could be
    # negative and a net could exceed its gross), an extra was persisted at
    # quantity 1 however many dishes it was attached to, and a merged line's
    # `actual_cost` was never refreshed. CORRECTED (1) is the convention in
    # orders_app/controllers/services/order_pricing.py.
    #
    # BOTH the model default AND the database default are LEGACY, deliberately.
    # An existing row, an insert from an older application version that omits the
    # column, and any future writer that forgets to opt in must never be mistaken
    # for a certified corrected order. Only the corrected creation service writes
    # CORRECTED, and it does so inside the same atomic operation that persists the
    # corrected parent and child values.
    #
    # SERVER-OWNED: there is no request field for it, it is absent from every
    # serializer, and it is not in EDIT_INFORMATION. It must never become
    # client-writable.
    pricing_version = models.PositiveSmallIntegerField(
        default=PRICING_VERSION_LEGACY,
        db_default=PRICING_VERSION_LEGACY,
        db_index=True,
    )

    # === kitchen-owned fulfilment axis (Phase 2) ===
    # Kitchen writes these fields on every transition. It ALSO writes
    # order_status on two transitions — the serve/recall completion transition
    # (serve -> 'served', recall -> 'pending', in
    # KitchenOrderFulfilmentStatusView) and cancellation (see below) — but never
    # payment_status.
    fulfilment_status = models.CharField(
        max_length=20,
        choices=[
            ("new", "New"),
            ("preparing", "Preparing"),
            ("ready", "Ready"),
            ("served", "Served"),
        ],
        default="new",
        db_index=True,
    )
    fulfilment_status_updated_at = models.DateTimeField(null=True, blank=True)
    fulfilment_status_updated_by = models.ForeignKey(
        "users_app.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_fulfilment_updated",
    )
    served_at = models.DateTimeField(null=True, blank=True)
    priority = models.BooleanField(default=False, db_index=True)

    # === the kitchen-order concurrency token (D05) ===
    # WHICH VERSION of this order's KITCHEN-ORDER STATE a command is acting on.
    # Its domain is the whole state a kitchen command reads and writes —
    # fulfilment status, cancellation and priority — not merely the text of
    # `fulfilment_status`. Every command names the revision it believes it saw
    # (`if_revision`), and `kitchen_transition` refuses when that no longer
    # matches the locked row.
    #
    # WHY IT EXISTS: a target-only payload cannot say which command was intended.
    # A delayed `preparing` was indistinguishable from a deliberate `ready ->
    # preparing` correction, and a delayed recall could reopen a LATER completion
    # because the source state was `served` again after a serve/recall cycle. An
    # explicit action answers the first; only a revision answers the second,
    # because a cycle returns the row to a state a source-state check accepts.
    #
    # IT IS A COMPARE-AND-SET TOKEN, NOT HISTORY. It counts nothing, proves
    # nothing about the past, and 0 on an existing row is the ADOPTION BASELINE —
    # never a claim that the order has never been touched, and never evidence
    # that it was accepted (that is `OrderAcceptance`, whose meaning D04 fixed
    # and this does not alter).
    #
    # SERVER-OWNED, like `is_test`, `pricing_version` and `request_fingerprint`:
    # no request field assigns it, it is absent from every write serializer and
    # from EDIT_INFORMATION, and `if_revision` is a PRECONDITION rather than a
    # value a caller may set. It is incremented exactly once per applied command
    # and never on a refusal, a read, a rollback or the no-change priority
    # result; it never resets on a serve/recall cycle and never wraps — the
    # service refuses before the column's 32-bit ceiling rather than overflowing.
    #
    # DELIBERATELY NOT part of `order_quote`'s fingerprint: that reference is
    # about what the DINER agreed to pay, and a kitchen command must not move it.
    fulfilment_revision = models.PositiveIntegerField(default=0, db_default=0)
    # local business date the order belongs to; authoritative for daily numbering
    order_date = models.DateField(null=True, db_index=True)

    # === cancellation (order_status='cancelled') — provenance only ===
    # Cancelling is one of the kitchen writes that set order_status (the other
    # is the serve/recall completion transition); these capture who/when/why.
    # cancelled_by mirrors fulfilment_status_updated_by's FK signature with its
    # own related_name. payment_status and the fulfilment axis are deliberately
    # untouched by a cancellation.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        "users_app.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_cancelled_by",
    )
    cancellation_reason = models.CharField(
        max_length=50, null=True, blank=True,
        choices=[
            (CancellationReason_CustomerChangedMind, "Customer changed mind"),
            (CancellationReason_ItemUnavailable, "Item unavailable"),
            (CancellationReason_KitchenError, "Kitchen error"),
            (CancellationReason_Duplicate, "Duplicate"),
            (CancellationReason_Other, "Other"),
        ],
    )

    class Meta:
        db_table = 'orders'
        ordering = ['-time_created']
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "client_order_id"],
                condition=models.Q(client_order_id__isnull=False),
                name="uniq_order_restaurant_client_order_id",
            ),
            models.UniqueConstraint(
                fields=["restaurant", "order_date", "order_number"],
                condition=models.Q(order_number__isnull=False),
                name="uniq_order_restaurant_date_number",
            ),
        ]


class OrderItem(BaseModel):
    """
    the order items
    """
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order')
    item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='item')
    available = models.BooleanField(default=True)

    # tracking options and choices
    option = models.CharField(max_length=50, null=True)
    option_choice = models.CharField(max_length=50, null=True)
    option_cost = models.DecimalField(max_digits=50, decimal_places=2, null=True)

    # for extras
    parent_item = models.ForeignKey(
        'self',
        null=True,
        on_delete=models.SET_NULL,
        related_name='parent_order_item'
    )  # noqa

    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=50, decimal_places=2)
    discounted_price = models.DecimalField(max_digits=50, decimal_places=2)
    discounted = models.BooleanField(default=False)
    unit_cost_of_options = models.DecimalField(max_digits=50, decimal_places=2, null=True)

    options = models.JSONField(default=list)

    selected_modifiers = models.JSONField(default=dict, null=True, blank=True)
    # Stores the diner's grouped modifier selections:
    # { "group_id": ["choice_id", ...], ... }

    # === kitchen snapshots (Phase 2): resolved at creation, immutable ===
    # item_name_snapshot preserves the name even if the menu item is renamed.
    item_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    # modifiers_snapshot holds resolved human-readable labels e.g. ["Size: Large"]
    modifiers_snapshot = models.JSONField(default=list, blank=True)
    # allergen_tags_snapshot holds [{name, icon, colour}] from item.tags (allergen)
    allergen_tags_snapshot = models.JSONField(default=list, blank=True)

    total_cost = models.DecimalField(max_digits=50, decimal_places=2)
    discounted_cost = models.DecimalField(max_digits=50, decimal_places=2)
    savings = models.DecimalField(max_digits=50, decimal_places=2)
    cost_of_options = models.DecimalField(max_digits=50, decimal_places=2, default=Decimal('0'))
    actual_cost = models.DecimalField(max_digits=50, decimal_places=2)

    status = models.CharField(max_length=50, default=OrderItemStatus_Initiated)
    last_updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='order_item_last_updated_by')  # noqa

    class Meta:
        db_table = 'order_items'
        ordering = ['-time_created', 'item__name']
        constraints = [
            # D01 backstop. NOT a claim that the database validates the incoming
            # JSON request — it validates ONE persisted fact: a stored line
            # quantity is never negative. It exists because the request
            # validator cannot reach a direct ORM write, a bulk `update()` or a
            # future writer, and a negative quantity silently reduces what a
            # diner is charged.
            #
            # `>= 0`, NEVER `> 0`: zero is a legitimate, load-bearing internal
            # representation — `add_order_item` and `process_item_extras` zero a
            # line whose item is unavailable or sold out so it is neither
            # prepared nor charged while still surfacing in the diner's
            # reconciliation. A positive-only constraint would break that.
            #
            # No UPPER bound: the per-line request ceiling bounds what a caller
            # may SUBMIT, not what legitimate merging may accumulate in a row.
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name='orderitem_quantity_non_negative',
            ),
        ]


class RestaurantDailyOrderCounter(BaseModel):
    """
    Per-restaurant, per-day monotonic source of order_number.

    Replaces the race-prone count()+1 pre_save signal: allocation takes a
    row lock (select_for_update) on the (restaurant, order_date) row, and the
    unique constraint is the final guard against the first-of-day race.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    order_date = models.DateField()
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'restaurant_daily_order_counters'
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "order_date"],
                name="uniq_daily_counter",
            ),
        ]


class OrderAcceptance(models.Model):
    """
    D04 — THE DURABLE FACT THAT A DINER'S ORDER WAS ACCEPTED.

    WHY A ROW AND NOT TWO COLUMNS ON ``Order``. The evidence has to survive
    ORDINARY ``Order`` saves, and a column only does that as long as every
    writer happens to have re-read the row first. Django's ``save()`` writes
    every field from the in-memory instance, so a caller holding an instance
    loaded BEFORE acceptance writes the pre-acceptance values back — silently,
    with no error, and the order then reads as never accepted. No production
    path does that today; the point is that a separate row makes it
    IMPOSSIBLE rather than currently-unreached. ``tests_order_acceptance``
    pins it by saving a deliberately stale ``Order`` instance and asserting
    the evidence is intact — a test the two-column design fails.

    WHAT IT IS NOT. Not the order's STATE: ``order_status`` moves on through
    ``preparing`` / ``served``, and a kitchen recall moves it back, while this
    row never changes again. Not a payment, and not a claim that the food
    arrived. It records exactly one thing — at this instant, this order was
    accepted against this quote — which is what a client whose response was
    lost needs in order to distinguish "my submit never landed" from "it
    landed and the kitchen has moved on".

    WRITTEN ONCE. The ``OneToOneField`` makes at-most-one a database fact, and
    the acceptance transition is its only writer: nothing updates an existing
    row, so a replay reads it rather than rewriting it. ``CASCADE`` because
    the evidence has no subject without its order; orders are soft-deleted,
    never hard-deleted, so it is not reachable in practice.
    """
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='acceptance',
    )
    #: When the acceptance COMMITTED — the diner's own moment, distinct from
    #: `time_last_updated`, which every later kitchen action moves.
    accepted_at = models.DateTimeField(db_index=True)
    #: The exact reference the diner's acceptance was bound to (D02). A retry
    #: naming a DIFFERENT quote is not a replay of this acceptance: it is an
    #: attempt to accept something else against an order already accepted.
    quote_ref = models.CharField(max_length=128)

    class Meta:
        db_table = 'order_acceptances'

    def __str__(self):                             # pragma: no cover
        return f'acceptance of {self.order_id} at {self.accepted_at}'


#: D06 quote-closure reasons. Module level because a `CheckConstraint` in a
#: nested `Meta` cannot see the enclosing class body, and because the vocabulary
#: is imported by the closure service — one spelling, two readers.
#:
#: EXPIRED is MONOTONE: time only moves forward, so a quote expired at one
#: instant is expired at every later one, which is what makes closing on it
#: irreversible by nature rather than by decree. PURCHASE_CHANGED is NOT monotone
#: — stock can come back — which is exactly why the fact has to be recorded
#: durably instead of re-derived later from a catalogue that has moved on.
QUOTE_CLOSURE_REASON_EXPIRED = 'quote_expired'
QUOTE_CLOSURE_REASON_PURCHASE_CHANGED = 'purchase_needs_review'
QUOTE_CLOSURE_REASONS = (
    QUOTE_CLOSURE_REASON_EXPIRED,
    QUOTE_CLOSURE_REASON_PURCHASE_CHANGED,
)


class OrderQuoteClosure(models.Model):
    """
    D06 — THE DURABLE FACT THAT AN UNACCEPTED QUOTE MAY NEVER BE ACCEPTED.

    WHAT IT MEANS, EXACTLY. This previously unaccepted draft is permanently
    barred from first acceptance under the supported protocol. That is the
    whole claim.

    WHAT IT DOES NOT MEAN, and the distinctions are the reason it is its own
    table rather than a reused status. It is NOT a service cancellation: no
    meal was cancelled, because no meal was ever ordered — the kitchen never
    saw this draft and never will. It is NOT a payment event: nothing was
    charged and nothing is refunded. It is NOT a statement that the diner was
    told: a closure can commit and its response be lost, which is precisely
    the case the recovery read exists for. And it is NOT evidence about
    acceptance in either direction — ``OrderAcceptance`` remains the only
    positive evidence, and its absence still means what D04 says it means.

    WHY NOT ``order_status = 'cancelled'``. That column records a COMMERCIAL
    outcome, and the kitchen's cancellation path writes it together with
    ``cancelled_at`` / ``cancelled_by`` / ``cancellation_reason``. Writing it
    here would file "the diner's price went stale" as "the restaurant
    cancelled an order", would make a draft indistinguishable from a real
    cancellation in every report and audit that reads that axis, and — worse
    in the other direction — would let an OLD cancelled row with no acceptance
    evidence be mistaken for a D06 closure by any code that later learns to
    look for one. A distinct table cannot be confused with a history it was
    not present for: absence here means "unknown, or not closed", never
    "accepted" and never "already safe to replace".

    WRITTEN ONCE, BY ONE SERVICE. ``OneToOneField`` makes at-most-one a
    database fact; ``quote_closure`` is the only writer and never updates an
    existing row, so a repeat closes nothing twice and the original reason,
    moment and reference survive verbatim. A caller arriving with a different
    reference gets a controlled answer rather than a rewritten history.

    IT IS A SEPARATE ROW FOR THE REASON ``OrderAcceptance`` IS. Django's
    ``save()`` writes every field from the in-memory instance, so a caller
    holding an ``Order`` loaded before the closure would write the pre-closure
    values back — silently. A separate row makes that impossible rather than
    merely currently-unreached, and the same argument applied to positive
    acceptance evidence in D04.
    """

    REASON_EXPIRED = QUOTE_CLOSURE_REASON_EXPIRED
    REASON_PURCHASE_CHANGED = QUOTE_CLOSURE_REASON_PURCHASE_CHANGED
    REASONS = QUOTE_CLOSURE_REASONS

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='quote_closure',
    )
    #: When the closure COMMITTED. Distinct from `time_last_updated`, which
    #: every unrelated write moves.
    closed_at = models.DateTimeField(db_index=True)
    #: The exact saved reference this closure retires. A later caller naming a
    #: different one is not repeating this closure.
    quote_ref = models.CharField(max_length=128)
    #: Why, from the frozen vocabulary above.
    reason = models.CharField(max_length=32, choices=[(r, r) for r in REASONS])
    #: WHICH policy decided it. A future policy version must be able to say
    #: that a closure was made under an earlier rule rather than silently
    #: inheriting today's meaning.
    policy_version = models.PositiveSmallIntegerField()

    class Meta:
        db_table = 'order_quote_closures'
        constraints = [
            # The vocabulary is a database fact, not merely `choices=`. A row
            # that cannot say why a quote was retired is worse than no row.
            models.CheckConstraint(
                condition=models.Q(reason__in=QUOTE_CLOSURE_REASONS),
                name='order_quote_closure_reason_vocabulary',
            ),
        ]

    def __str__(self):                             # pragma: no cover
        return f'quote closure of {self.order_id} ({self.reason})'
