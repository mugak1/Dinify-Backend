# Dinify Backend — Claude Code Context

## Project Overview
Dinify is a QR-code-based digital ordering and restaurant management platform
built for Uganda and mobile-money-first markets. Django/DRF backend on AWS EC2
with PostgreSQL on AWS RDS.
A parallel `AGENTS.md` at the repo root carries Codex/other-agent instructions
that defer to this file — `CLAUDE.md` remains the authoritative project guide,
so keep it current when conventions change.

## Tech Stack
- Python 3.12.3 on Ubuntu 24.04 (the live EC2 runtime, cut over 2026-08 from
  Ubuntu 22.04 / Python 3.10.12). CI pins this exact patch — see the CI section
- Django 5.2 LTS / Django REST Framework
- PostgreSQL on AWS RDS (primary database)
- MongoDB Atlas (action logs and archiving only — currently unreachable from EC2)
- Apache / mod_wsgi on AWS EC2 (35.177.46.58)
- Repo: mugak1/Dinify-Backend

## Current Implementation Status
- Menu module (all phases 1a–1d): ✅ Complete
- Menu item ordering: ✅ `listing_position` column + `reorder-section-items`
  endpoint (handled by `ConMenuItem`) + section reorder via `restaurant_setup`
- Dashboard endpoint: ✅ Exists at `api/v1/reports/restaurant/dashboard/`
  Do NOT create a new dashboard endpoint — it already exists. Its metrics are
  DEFINED ONCE in `dashboard.py` (`_orders_placed` / `_sales` / `_cancelled` /
  `_refunded` / `_payment_captured` / `_rate`) and every figure derives from a
  definition — do not restate a status list inline. See the Reports section
- Tables module: ✅ Backend complete — `reservations`, `waitlist`,
  `table_actions`, `dining_areas`, full QR/floor-plan fields wired through
  EDIT_INFORMATION
- Kitchen module: ✅ Backend ready — driven by `Order`/`OrderItem` fulfilment
  fields (`fulfilment_status`, `priority`, `served_at`) via
  `orders_app/endpoints_kitchen.py`, mounted at `api/v1/kitchen/`
  (`urls_kitchen.py`). Endpoints: `orders/active/` + `orders/completed/`
  (served tickets move to the Completed feed), `orders/<pk>/fulfilment-status/`,
  `orders/<pk>/priority/`, `orders/<pk>/cancel/` (state-aware cancel writing
  `Order.cancellation_reason`/`cancelled_at`/`cancelled_by`, migration
  `0033`), and the sold-out ("86") panel — `menu-items/` (list) +
  `menu-items/<pk>/stock/` (toggles `MenuItem.in_stock`). The legacy
  `KitchenTicket`/`KitchenTicketItem` KDS models were RETIRED (migration
  `0030_retire_kitchen_tickets`) — do not reintroduce them. Kitchen
  authorization now routes through the CENTRAL permission resolver (PR #183):
  all eight module gates call `can_user_access_module(user, restaurant_id,
  MODULE_KITCHEN)` and the in-progress goodwill-cancel escalation calls
  `can_manage_restaurant`, so owner-configured Roles & Access grid overrides
  finally take effect for kitchen. The old hardcoded
  `user_can_access_kitchen`/`user_can_manage_restaurant` local helpers were
  removed (behaviour-neutral for the seeded owner/manager/kitchen default
  matrix; no migration). Kitchen serve/recall now also couples
  `Order.order_status` to the completion transition (PR #206): serving sets
  `order_status='served'` (unless the order is cancelled — never resurrect a
  cancelled order into a sale), recall reverts it to `pending`, so served orders
  satisfy the reports `SALE_STATUSES` {served, paid} and count as sales.
  `order_status` is written from server constants (never the request body) and
  `payment_status` is never touched; every non-completion transition still
  writes only the fulfilment axis.
  **EVERY KITCHEN ORDER COMMAND NOW GOES THROUGH ONE BOUNDARY (D05)** —
  `orders_app/controllers/services/kitchen_transition.py`. The three write views
  are thin adapters holding no business rule; the service owns the lock order,
  the re-read, the authoritative permission decision, eligibility, the
  precondition, the recall window and occupancy. **The request contract CHANGED
  and there is no legacy form** — see `BREAKING_CHANGES.md` §15. What it closed,
  each reproduced on unmodified `fd190dd` with real HTTP and (for the races) two
  connections: a DRAFT was fully writable and could be walked to `served`,
  turning something the diner never placed into a SALE and leaving them unable to
  place it; a CANCELLED order was still preparable and could be stamped served,
  because the cancelled guard existed only on the serve branch; a cancel
  committing between a serve's unlocked read and its save was OVERWRITTEN on the
  sale axis (and the reverse left a SERVED order marked cancelled); an ordinary
  kitchen user's free void, qualified against a `new` the order had already
  stopped being, survived preparation starting; recall never consulted occupancy,
  so two orders ended up ongoing at one table; a delayed `{'fulfilment_status':
  'preparing'}` executed as a RECALL; and after a serve/recall/serve cycle a
  delayed recall reopened a LATER completion — the case no source-state check can
  see, which is why `Order.fulfilment_revision` exists (migration
  `orders_app/0040`, additive, `default=0` AND `db_default=0`). It versions the
  WHOLE kitchen-order state (fulfilment, cancellation and priority), is
  server-owned, is deliberately NOT in `order_quote`'s fingerprint, and 0 on an
  existing row is the ADOPTION BASELINE — never a claim about history and never
  evidence of acceptance. **LOCK ORDER: `Table -> Order`**, the tail of
  acceptance's `advisory -> Table -> Order`; kitchen commands do NOT take the
  admission advisory lock, because `order_admission.admit` decides whether NEW
  work may be admitted and managing accepted work is a different question.
  **THE PERMISSION DECISION LINEARIZES at the post-lock re-check** (the resolver
  holds no request-level cache, so a revocation committed before that point is
  respected; one committing after it can still overlap, and that is stated rather
  than claimed away). `priority` is a STRICT JSON boolean — the omitted-value
  toggle is gone, since a retry undid itself. **Recall has a server-enforced
  10-minute window** from the current completion; the 24h Completed feed is
  VISIBILITY and is unchanged. **`OrderItem.status` is still never written** — it
  has no production writer and it IS inside the quote fingerprint. Coherent
  historical orders with no `OrderAcceptance` stay operable; contradictory rows
  are refused for manual review and left untouched, never repaired. D05 closed
  the kitchen PRODUCER of D04's `evidence_unavailable` for new rows without
  resolving the rows already produced, so that state remains a statement of
  ignorance.
  **THE BOUNDARY NOW VALIDATES, AND THERE IS ONE PER-ORDER OBSERVATION (K4).**
  `KitchenCommand` validates in `__post_init__`, so "fully validated" is a
  property of the TYPE rather than a description of how the three `parse_*`
  functions happen to be written: the revision's type and range, the `priority`
  boolean and the `cancellation_reason` vocabulary were enforced ONLY at the
  parsers, so a caller that built a command another way reached `execute` with
  none of them — and `execute` checked only the ACTION, despite documenting that
  it self-guards a direct caller. A directly-built cancel could write ANY string
  as the stored reason, permanently, since a cancellation is never re-cancelled.
  Cross-action fields are UNREPRESENTABLE rather than ignored (an `advance`
  carrying a `cancellation_reason` cannot be built), because silently dropping one
  leaves a caller believing they said something the boundary threw away.
  `_assert_revision` also RE-ASSERTS THE TYPE at the compare-and-set: `!=` reads
  as exact while Python's numeric tower is not, so `False` satisfied a
  precondition of 0 and `1.0` one of 1 — a token whose whole purpose is exactness,
  satisfied by a coercion. **PRIORITY APPLIES ONLY TO A TICKET THE KITCHEN IS
  STILL WORKING ON** (`new`/`preparing`/`ready`): a served ticket accepted one,
  and the harm is not that the flag is meaningless there but that applying it
  BUMPED THE REVISION — a served ticket is recall-eligible for ten minutes, so a
  stray priority tap spent the precondition an operator was holding and their
  recall came back stale with the window running down. The eligibility question is
  asked BEFORE the equality one, as everywhere else here, so the no-op branch
  cannot answer "no change" about a ticket the rule does not apply to. **NONE OF
  THIS CHANGES AN HTTP REQUEST SHAPE** — every rule was already enforced at the
  parser for a request arriving over the wire; the one ANSWER that changes is
  priority on a served ticket, 200 → 409 `illegal_transition`, which no shipped
  board can reach. `GET kitchen/orders/<pk>/state/`
  (`KitchenOrderStateView` → `kitchen_transition.read_state`) is the per-order
  OBSERVATION that settles an uncertain command, and it is the one thing the FEEDS
  CANNOT REPLACE: a cancellation — and a serve past the 24h Completed window —
  removes the order from BOTH feeds, so "it is not on the board" is not an answer
  about whether the command ran. It answers for ANY order it can see (cancelled,
  served, terminal, DRAFT), returns the SAME projection every command answers
  with, and takes no lock, opens no transaction, writes nothing and asserts no
  causal link between the state and any earlier command. Eligibility stays with
  `execute`. **EVERY REFUSAL IS ONE NON-DISCLOSING 404** — unknown, malformed,
  soft-deleted AND out of the caller's scope alike, in status and in body. The
  scope case is where this READ parts company with the three command routes,
  which answer `403 kitchen_forbidden`: on a read, 403-for-foreign beside
  404-for-unknown is an existence oracle over the whole orders table, free and
  silent for any authenticated kitchen user, and it contradicts both the rule
  above (a tenant-scoped detail read answers 404 so existence is not confirmed)
  and `OrderNotFound`'s own docstring. The COMMAND routes still answer 403 and
  are unchanged — `kitchen_forbidden` is a reason the board renders — but that is
  the same exposure to a caller willing to attempt a mutation, and narrowing it
  is its own contract decision rather than this change's. It is DELIBERATELY ABSENT from the delegated `ALLOWED_ROUTES` — a
  delegated session can issue none of the three commands, so it can never hold an
  uncertain one to reconcile; the reasoning is recorded in `delegation_scopes.py`
  beside the command exclusions. See `BREAKING_CHANGES.md` §15a
- Order-creation hardening: ✅ (PRs #198–#201, #210) — the live v2 `initiate`
  create path enforces tenant consistency (table + menu items must belong to the
  same restaurant, BUG-P1-1), rejects orders when the restaurant is not
  `accepting_orders` (BUG-P2-1), is idempotent under concurrent
  same-`client_order_id` double-taps, and serializes concurrent same-table
  creation/submit with a table row lock (BUG-P2-4). An `initiated` order is now a
  true DRAFT that neither occupies its table nor reaches the kitchen board — the
  table is claimed only at `submit` (BUG-P2-2), whose transition is transactional
  and race-safe. Preserve these invariants in any future order-create work
- Order-input integrity (D01): ✅ ONE contract for customer-supplied order input,
  `orders_app/controllers/services/order_input.py` — PURE and DATABASE-FREE, so it
  adds ZERO queries and the pinned order-path budget below is unchanged. It answers
  only *is this request structurally a well-formed order?*; the catalogue, tenant,
  publication and price questions stay with `validate_order_selections` and the
  in-transaction re-check, which still run after it. Enforced at THREE boundaries
  that all call the SAME functions: the endpoint (a root-must-be-a-mapping guard
  BEFORE any `.get()`, then full validation AFTER authority resolution, so no
  catalogue-shaped feedback precedes authorization), `ConOrder.initiate_order`, and
  `_create_order` — which is AUTHORITATIVE and self-guards a direct caller rather
  than trusting its caller. Each consumes the VALIDATED lines; raw input is not
  passed onward beside them. **The optional `client_order_id` is validated at all
  three too** (completed after the merge of PR #312, which validated it only at
  the endpoint): both services consume it in ORM operations — the replay lookup,
  the INSERT and the insert-race recovery — so an unvalidated key reached a
  `UUIDField` filter, where a malformed value raised `ValidationError` OUT of the
  service and an **integer or boolean was silently COERCED by `uuid.UUID(int=...)`
  into a fabricated key** (`5` → `…-000000000005`; `0` and `False` both → the nil
  UUID, colliding two unrelated callers onto one idempotency key). `validate_
  service_client_order_id` is the shared rule: **absence is `value is None`, never
  falsiness** — the old `if client_order_id:` gate let a falsy key skip the replay
  lookup, persist the nil UUID, and then skip the race recovery too, so a second
  attempt surfaced an uncaught `IntegrityError`. It accepts the public form (a
  UUID string) plus ONE widened internal form, an actual `uuid.UUID` object, which
  in-process callers legitimately pass (`tests_kitchen.py` throughout); both
  canonicalise to the same lowercase STRING before any key-dependent operation.
  The HTTP contract is unchanged — `validate_public_client_order_id` still refuses
  a `uuid.UUID` object — and there is deliberately no trusted/skip-validation
  switch: both take exactly one argument, pinned by a test. What it closed: `quantity` was checked only for
  PRESENCE (`is None`) at both `menu_publication._parse_selection` and
  `add_order_item`, so **0 and -3 persisted at HTTP 200** — a valid positive line
  and an invalid negative line combined into a reduced payable amount — while
  `True` / `2.5` / `2.0` / `"3"` / `[1]` / `{}` / `10**20` each produced a 500 out
  of Decimal arithmetic or the serializer; a non-object body reached `.get()`
  (`AttributeError`) and a non-sized `items` reached `len()`; and a malformed
  `client_order_id` reached a `UUIDField` ORM filter. **`quantity_error` is THE
  rule** — a real positive `int` (never `bool`), no floating representation
  coerced, within the per-line ceiling — and the request validator, `add_order_item`
  AND `update_item_quantity` all call it. `update_item_quantity` guards ITSELF
  rather than relying on its caller: an existing 5 plus an incoming -1 yields 4,
  which satisfies the database constraint perfectly while halving the charge, so
  rejecting before the arithmetic is the only place it can be caught.
  **VALIDATION IS IDEMPOTENT AND NON-MUTATING**: `item` and `extras` come back as
  canonical lowercase UUID **strings** (never `uuid.UUID` objects, which the next
  boundary would reject as non-strings), modifier group/choice ids are returned
  EXACTLY as submitted, absent keys stay absent and `None` stays `None`, and every
  line is a new dict. **An id-shaped failure keeps `NOT_ON_MENU_MESSAGE`**, imported
  rather than restated: answering a malformed id differently would have created
  exactly the malformed-vs-foreign distinction that opaque message exists to remove.
  Static checks sit BEFORE the replay lookup at both service boundaries — they are
  menu-independent, so nothing they decide can go stale, and a correctly shaped
  replay is unaffected; a malformed one is refused, and no compatibility is promised
  for a previously accepted malformed body. **REQUEST CEILINGS (new D01 application
  safety limits — NOT prior Dinify policy, not payment-provider limits, and not a
  statement about menu size):** quantity/line 99, lines/order 100, total units 500,
  modifier groups/line 32, raw choices/group 64, raw extras/line 64, opaque modifier
  id 128 chars, total raw choice+extra entries 2,048. Collection ceilings count RAW
  entries BEFORE de-duplication (a group of 64 repeats of one id is at the ceiling),
  and the per-line quantity ceiling bounds what may be SUBMITTED — never the merged
  row, which may legitimately exceed it. This is POST-PARSE APPLICATION validation:
  it does not replace an HTTP body-size limit, edge rate limiting or any other DoS
  control, and claims nothing about them.
  **THE CEILINGS ARE NOW A SOURCE-AUTHORITATIVE CONTRACT A PEER CAN AGREE WITH
  (D08/B1).** `orders_app/contracts/checkout_limits.py` derives the published set from
  these constants — no number typed twice — and `checkout_limits.contract.json` beside
  it is the committed export, asserted against the live constants **UNCONDITIONALLY**
  by `CrossRepositoryCeilingContractTests`. That matters because the cross-repository
  assertion that gave the class its name `skipTest`s whenever Dinify-Frontend is not
  checked out beside this repository, which in CI is always — so it had never once
  run there, and two independently checked copies are not parity: they are two things
  that each agree with themselves. The tie to the other repository is a DIGEST over
  the ceiling VALUES in a canonical form both languages produce byte for byte
  (`json.dumps(sort_keys=True, separators=(',', ':'))`); keys beginning `_` are
  provenance notes and are excluded, so the two copies may annotate themselves
  differently and still agree. Dinify-Frontend's release gate reads this export AT A
  SELECTED COMMIT of this repository, through a peer receipt its own producer builds
  from git, and refuses to publish a FRONTEND candidate whose compiled copy disagrees
  (D08 B1 completion — it used to compare against a digest literal typed into its own
  policy, which checked the literal, not this repository). `manage.py
  export_checkout_limits_contract` writes or checks the export; the gate is the test,
  not the command.
  **WHAT THAT DOES NOT ENFORCE — corrected, because this file used to claim "a
  one-sided change cannot be released whichever side moved".** The refusal lives on
  the frontend's publication path only, and that path is itself not yet active.
  THIS repository's deploy (`deploy-uat.yml`) makes no compatibility decision at all,
  so a ceiling changed here deploys to UAT unimpeded and the disagreement surfaces at
  the next frontend publication decision, or in this repository's own tests if the
  export was not regenerated. Changing a ceiling is therefore an ORDERED, MANUAL
  two-repository sequence — frontend copy and receipt approval coordinated with the
  backend deploy — and nothing on this side enforces the order
- Stored modifier definitions (D01): ✅ `restaurants_app/controllers/modifier_definition.py`
  is the ONE structural reading of a `MenuItem.options` row, shared by
  `ConOrder.normalize_selected_modifiers` and the read-only preflight so the two
  cannot drift into a second opinion. `options` is an unvalidated `JSONField`, and
  six stored shapes used to raise out of checkout as a 500: an unhashable group or
  choice id (`dict.fromkeys` / `set()`), and a non-integer `minSelections` /
  `maxSelections` (`int < str`). Three outcomes, deliberately apart — **inactive**
  (`hasModifiers` falsy, `groups` absent/`None`/`[]`, or a non-mapping `options`:
  legal and unchanged), **valid**, and **invalid** (FAIL CLOSED: checkout refuses
  the line, and it is NEVER downgraded to "no modifiers required", which would
  delete a required selection to make a broken item orderable). A non-list `groups`
  with `hasModifiers: true` is INVALID where it used to return `200 {}` — that was
  the required-selection bypass — while an absent `groups` stays inactive, because
  then there are no requirements to lose. Bounds must be real non-negative `int`s
  (`bool` refused), `maxSelections` 0 still means unlimited, and a positive maximum
  below the minimum is refused rather than defaulted. **DUPLICATE group ids, and
  duplicate choice ids within one group, are REFUSED** — validation resolved them
  first-wins (`setdefault`) while `determine_effective_unit_price` and
  `construct_option_items` resolved them last-wins, so one id was validated against
  one definition and priced against another; both now read the one unambiguous map.
  The same choice id in DIFFERENT groups stays legal. Ids are OPAQUE — the operator
  UI mints UUIDs but the column is unvalidated and the repo's own fixtures use
  `'g-req'`/`'c1'`, so they are never parsed as UUIDs, trimmed or case-folded.
  **CAPACITY IS NOT VALIDITY**: a 400-choice optional group is ordinary capacity and
  stays orderable; only an unsatisfiable REQUIREMENT is reported, as a compatibility
  concern rather than a definition error. Checkout returns one controlled diner
  message and logs a bounded classification plus a truncated identifier — never the
  catalogue JSON. This validates NO monetary configuration; D02 stays separate
- `OrderItem.quantity >= 0` (D01): ✅ migration `orders_app/0036`, one additive
  reversible `AddConstraint` named `orderitem_quantity_non_negative`. A BACKSTOP for
  the paths the request validator cannot reach (a direct ORM `save()`, a bulk
  `update()`, a future writer) — **not** a claim that the database validates the
  incoming JSON. **`>= 0`, never `> 0`**: zero is load-bearing, the server-set state
  of a line whose item is unavailable or sold out, pinned by `tests_checkout_policy`
  and `tests_tenant_isolation_closure`. **No upper bound**, so legitimate merging
  stays legal. Adding it takes `ACCESS EXCLUSIVE` and validates every row, including
  soft-deleted/archived/vacuumed ones; **the duration is proportional to the target
  table's row count, which this repository cannot observe** — do not describe it as
  brief. A violation ABORTS the migration and changes nothing; historical negatives
  are real under-charged orders needing an explicit remediation decision, never a
  clamp or a delete. The reverse operation drops the rule and RESTORES NOTHING,
  because nothing was ever altered. `manage.py check_order_input_compatibility` is
  the read-only preflight (see Existing Management Commands); a staged
  `NOT VALID` + `VALIDATE CONSTRAINT` alternative is documented in the migration and
  should be adopted only against a measured row count
- Order pricing and line identity (D02/D03): ✅ ONE arithmetic contract for checkout
  money, and ONE canonical identity for an order line. Three new modules, layered so
  nothing above them can hold a second opinion:
  - `misc_app/controllers/money.py` — THE monetary parsing and rounding rule. It sits
    BELOW both the catalogue and the order services deliberately: either would
    otherwise have to import the other. `parse_money` is the only reader of a stored
    monetary value and it FAILS rather than guesses — a malformed, non-finite,
    over-long or out-of-range figure is a named refusal, never a coerced `0`, which is
    the difference between refusing to sell an item and selling it for nothing.
    **Rounding is `ROUND_HALF_EVEN`, applied per UNIT COMPONENT, and the extension to
    a line is then EXACT INTEGER arithmetic** — round-then-multiply, never
    multiply-then-round, so a line of 3 is exactly three times the unit the diner was
    shown. `MAX_MONEY_DIGITS`/`MAX_MONEY_TEXT_LENGTH` bound the work BEFORE parsing;
    `working_context()` gives the wide precision the intermediate arithmetic needs.
    **IT IS NOT OPTIONAL DECORATION, and it belongs around EVERY composite Decimal
    step on money** — the process default is 28 significant digits, under the 50 the
    columns hold, so ambient arithmetic decides the fate of a schema-valid figure by
    a limit nothing here chose: `quantize` RAISES (a 29-digit price was reported
    `out_of_range`, hiding an item the database stores happily and refusing it at
    checkout) while `*`, `+` and `-` ROUND SILENTLY, which is worse — an inexact
    product the range check then waves through. `quantize_money` and `extend_money`
    now carry it internally; `price_unit` and `extend` wrap their own sums, as do
    `update_order_amounts` and the serializer's legacy-total and line aggregates.
    **`format_money` is THE only sanctioned way a monetary value leaves the server as
    text**, and it exists because the RENDERED JSON is not what the view assembled:
    DRF's `JSONRenderer` encodes a `Decimal` through `float(obj)`, so an exact
    `Decimal('899.10')` in `response.data` reaches the browser as `899.1` and
    `Decimal('1e28')` as `1e+28`. **A test asserting on `response.data` compares the
    value the view BUILT, never the value the client PARSES**, so it cannot see any of
    that — `tests_order_money_wire.py` decodes `response.content` with a `parse_float`
    hook that marks every JSON float, which is the only way the distinction is
    observable. `str(Decimal)` is NOT a substitute (scientific notation for a large
    exponent — the one case that most needs a plain fixed string), and NEGATIVE ZERO
    is normalised, since `Decimal('-0.00')` formats as a signed amount while comparing
    equal to zero. **The QUOTE — `quote_total` and every money key on every
    `data.quote` line — is canonical decimal STRINGS**; the legacy `order_details`
    keys keep their established numeric form for older clients, so this is the new
    contract rather than a global renderer change
  - `restaurants_app/controllers/pricing_policy.py` — `resolve_price` is THE answer to
    *what does this item cost right now*, returning a `PriceVerdict` that both the
    public menu read and the order path consume. **The discount WINDOW is checked
    BEFORE the magnitude is parsed**, so only a currently-scheduled broken discount
    makes an item unpriceable; an expired one with unreadable figures is simply not
    applied. `MenuItem.effective_base_price()` now RAISES on an unusable price rather
    than clamping to zero, and `item_priceable` joins `item_visible_in_menu` /
    `item_orderable`, so an item the server cannot price is neither shown nor sold.
    **THE SIGN OF AN OVER-100% DISCOUNT IS DECIDED ON THE RAW VALUE, BEFORE
    ROUNDING**, and that ordering is the whole guard rather than a refinement of it:
    quantizing a small negative payable to two places yields `Decimal('-0.00')`, which
    `== Decimal('0')`, so the post-quantize `effective < 0` test could not see it — a
    0.01 dish at 100.5% off resolved USABLE at negative zero and was published and
    sold FREE at HTTP 200, the same outcome the pre-D02 zero clamp produced, reached
    by arithmetic instead of by a clamp. The post-quantize check is KEPT as defence in
    depth: it alone catches an effective price ABOVE the reference, which no sign test
    can express
  - `orders_app/controllers/services/order_pricing.py` — `PricedUnit`/`PricedLine`,
    the modifier adjustment, and **`line_identity`**: `(item, modifiers, extras,
    reference_unit, effective_unit, deliverable, name_snapshot, modifiers_snapshot)`.
    Two lines merge only when ALL of that agrees, so a plain dish never merges into a
    modified one (absence used to be a WILDCARD), a line priced under a discount never
    merges into one priced without it, and an unavailable line is never resurrected by
    a merge. `modifier_identity` DE-DUPLICATES choices (canonicalisation already
    collapses a repeat, and legacy rows shipped without a migration) while
    `extras_identity` keeps its multiset shape (duplicate extras are REFUSED upstream,
    not collapsed) — the asymmetry is deliberate and spelled out at both declarations.
    **EXTRAS CARRY THEIR OWN FULLY RESOLVED IDENTITY, not just an id**
    (`ResolvedExtra` + `extra_identity`: item, reference unit, effective unit,
    deliverability, name snapshot, allergen snapshot). An id-only extras signature
    merged two lines whose CHILDREN differed — a repriced, sold-out, renamed or
    re-tagged extra merging into a line priced before the change — so the stored row
    disagreed with the quote about what the kitchen was preparing. **AND THE
    REQUIRED-EXTRAS OUTCOME IS DETERMINED BEFORE THE FINAL MERGE IDENTITY**, never
    after: computing deliverability afterwards decides identity on a property the line
    does not yet have. `ConOrder.resolve_line` is the ONE resolution both
    `add_order_item` and `find_existing_order_item`'s fallback go through, so the two
    cannot form different opinions about one line. **Defect 2 is NOT HTTP-REACHABLE** —
    no live route adds an item to an existing order — and
    `tests_order_merge_identity.py` says so in terms rather than implying a tenant
    bypass that does not exist
  `catalogue_snapshot.py` resolves the whole order's catalogue in **exactly ONE
  statement**, under one captured `now`, so every line of an order is priced against
  the same menu and the same clock. It is what makes a concurrent operator edit
  either wholly before or wholly after an order, never halfway through one.
  **IT USED TO BE TWO** — a `MenuItem` read and a batched allergen-tag read — and
  under READ COMMITTED each statement takes its OWN snapshot, so ONE operator
  transaction changing a dish definition AND its allergen links could commit between
  them and be HALF-OBSERVED: the new price with the old allergens, or the reverse, on
  a ticket a kitchen works from. The labels are now aggregated INTO the item read by
  `JSONBAgg(JSONB_BUILD_OBJECT(...))`, so name/icon/colour alignment is STRUCTURAL
  rather than inferable from three parallel `ArrayAgg`s. It is **PostgreSQL-only with
  no fallback**, deliberately — a portable second path would be a second opinion, and
  this repository already requires PostgreSQL. Rejected alternatives, for the next
  reader: `prefetch_related` (still two statements), a per-transaction
  `REPEATABLE READ` (cannot be set mid-transaction) and an optimistic re-read (adds a
  query, breaking the pinned budget). The proof is a two-connection
  `TransactionTestCase` firing a REAL competing commit from a `connection.execute_wrapper`
  seam at the statement boundary — a seam that SURVIVES the fold, so the test still
  means something after the defect is fixed. The fix REMOVES a query — see the read
  budget below
- The diner's review describes ONE live population, and the legacy view is exact
  (D02/D03 residual R2 + R3): ✅ two coherence defects in
  `orders_app/controllers/orders/serializers.py`, both closed without changing a
  single saved amount.
  **ONE LIVE POPULATION.** `serialize_order_details` fetched every `OrderItem` of
  the order and then split that list TWO WAYS: `live_rows` (undeleted) fed the
  legacy total and `quote_ref`, while the parent/child map, the flat collections
  and the availability counts came from the UNFILTERED list. A soft-deleted parent
  or child was therefore presented as an active quoted purchase while the
  reference and the rollup the diner's acceptance is bound to excluded it — one
  response holding two answers to "what is in this order". It is a COHERENCE defect
  rather than an executed exploit: no live delete operation on the reviewed
  ordering journey produces such a row, and `tests_order_live_quote.py` writes
  `deleted` directly, which is exactly what makes those fixtures fixtures. The
  population is now defined ONCE (`deleted=False`) and supplies the review, the
  counts, the relationships and the digest. A healthy draft's `quote_ref` is
  BYTE-IDENTICAL to what the canonical helper derives, so reconstructing the
  population churned no reference.
  **AN ORPHANED LIVE CHILD IS DISCLOSED, NEVER HIDDEN.** A live child whose parent
  is not in the live population belongs under no quoted line, and its amount is
  still in `actual_cost`. Silently dropping it would leave a quote that APPEARS to
  reconcile while charging something else — the exact failure the itemised quote
  exists to prevent. So the saved payable is left untouched, the child still
  appears in the flat `extras` collection, a bounded warning is logged (order id
  and a count; no amounts, no order contents), and an ADDITIVE
  `order_details.quote_complete` says so. **Never rewrite `quote_total` /
  `actual_cost` to match the representable lines.**
  **AND THE SERVER REFUSES TO ACCEPT ONE — a disclosure a client may ignore is not
  an invariant** (Codex P1 on PR #316, valid). The flag made the response honest
  and left the transition unchanged: `_acceptance_refusal` checked the reference
  and that one deliverable parent existed, and the reference of an incomplete
  draft is perfectly VALID (an orphan is a live row, so it is inside the
  fingerprint), so any caller holding the diner session — an older client, or a
  direct one — could return it and move the order to the kitchen with part of its
  payable represented by no quoted line. Acceptance now carries a FOURTH
  invariant, `quote_incomplete`, checked on the population it is about to accept:
  AFTER the acknowledgement, because "your order changed" is the accurate answer
  when the reference is stale, and BEFORE `no_deliverable_items`, because that is
  a statement about the itemised lines and an order whose itemisation cannot
  represent the payable has not earned one. **The split is ONE function**,
  `order_quote.group_live_children`, read by the serializer that RENDERS the quote
  and by the check that decides whether one may be honoured — two copies would
  disagree exactly where it matters, a response saying incomplete while the
  transition accepts. **IT COSTS NO QUERY**: `_acceptance_refusal` now fetches the
  live rows once and passes them to `matches`, which ran that same query itself.
  The result is a CONTROLLED NON-CONFIRMABLE one on BOTH sides: the client refuses
  it twice over (the flag AND the reconciliation that fails anyway) and the server
  refuses to accept it at all. The refusal rewrites nothing — no reprice, no
  replacement order, no partial acceptance, and the reference it refused is
  unchanged. Pinned by `IncompleteQuoteIsRefusedAtAcceptanceTests`, whose fixture
  is a REAL `initiate_order` draft with one parent soft-deleted directly, and
  whose negative control asserts that BOTH pre-existing invariants pass on it —
  the reference matches and a deliverable parent remains — so only the new rule
  stands between that draft and the kitchen.
  **TWO PER-LINE QUERIES WENT WITH IT**, both from the same fetch: the read now
  carries `select_related('item')` (the per-row `item.name` was a query apiece)
  and `serialize_order_item_details` reads `parent_item_id` rather than
  `parent_item`, whose object form lazily SELECTed the parent row once per extra.
  A 20-row order went 31 → 1 query and is pinned FLAT against a 2-row one. No
  budget was raised; `tests_order_path_queries.py` is untouched and green, which
  also confirms serialization sits outside the pinned create-transaction window.
  `serialize_order_item_details`'s standalone `children=None` branch is
  deliberately NOT changed — it has other callers, and this was a narrow fix.
  **`_legacy_view` IS EXACT, AND THE CONTEXT LIVES IN THE ADAPTER.** Recovering the
  pre-D02 `unit_price` / `total_cost` subtracts the modifier component back out —
  composite Decimal arithmetic on money, so `money.working_context()` applies to it
  directly. `_legacy_total` wrapped its own `sum` and so LOOKED covered, but
  `_quote_line` and `serialize_order_item_details` both call `_legacy_view` from
  outside any context, so the silent 28-digit rounding happened before either could
  act on the value: a saved reference unit of `10000000000000000000000000001.01`
  less a `1.00` modifier came back as `1.000000000000000000000000000E+28`, a cent
  short. Making exactness a property of the function closes all three callers at
  once. **THE SAME OMISSION EXISTED IN `option_breakdown`** (`con_orders.py`): its
  `group_total += adjustment` accumulated under the ambient context while
  `price_unit` summed the very same adjustments inside `working_context`, so the
  LABEL a diner is shown for a group could round while the CHARGE stayed exact —
  reopening precisely the shown-vs-charged split that one traversal exists to
  close. Its `cost_amount` now renders through `format_money` rather than `str()`,
  which spells a large exponent in scientific notation; the two are byte-identical
  for every ordinary amount, and `options` is not part of the quote fingerprint, so
  no stored value and no `quote_ref` moved. The oracles in
  `tests_order_money_wire.py::LegacyViewExactnessTests` /
  `OptionGroupLabelExactnessTests` are INDEPENDENT LITERALS, never the production
  helper run twice, and each class carries an ordinary-amount negative control.
  The amounts are at the edge of what `max_digits=50` supports — technical capacity,
  deliberately NOT a claim about any real Kampala order
- Order acceptance is bound to the reviewed quote (D02): ✅ `Order.pricing_version`
  (migration `orders_app/0037`, additive, `db_default` LEGACY) plus an opaque
  `quote_ref` derived from the PERSISTED lines and totals (`order_quote.py`, SHA-256
  over a canonical fingerprint). `PUT orders/submit/` requires it and refuses on
  `quote_ref_required` / `quote_ref_stale` / `legacy_pricing_version` /
  `nothing_to_prepare`, checked INSIDE the locked transaction against the re-read row.
  **There is no staff or internal bypass** — a path that skipped it would be a path on
  which the diner's agreement was never established. The acknowledgement authorises
  NOTHING: the diner table session remains the sole authority for whose order this is.
  A LEGACY draft is never repriced or deleted, only refused, and the client re-prices
  the unchanged basket. See `BREAKING_CHANGES.md` §13
- THE IDEMPOTENCY KEY IS BOUND TO THE PURCHASE IT WAS USED FOR (D04/B): ✅ ONE policy,
  `orders_app/controllers/services/order_intent.py`, plus `Order.request_fingerprint`
  (migration `orders_app/0038`, additive, nullable, NO backfill and none possible).
  The key used to identify an ATTEMPT and be bound to NOTHING: `_create_order`
  selected an existing order by `(restaurant, key)` and returned it, so **a request
  naming three burgers received the one-burger order that key had been used for, and
  a request naming a DIFFERENT TABLE received the first table's order** — both at
  HTTP 200, with no conflict reported. `resolve_intent` is now THE question every
  return-existing site asks, and there are FOUR of them (the controller preflight, the
  service lookup, the post-wait recheck and the unique-conflict recovery); they used to
  interpret the key independently. Five things are load-bearing.
  **THE BINDING HAS TWO HALVES, and SCOPE IS CHECKED FIRST.** Scope is the
  server-resolved restaurant and table plus the command's provenance (`created_by`,
  `customer`); the fingerprint is the canonical purchase. A caller at another table is
  answered `checkout_intent_unusable` — OPAQUE, carrying no order id, no amount and no
  attribution — and the comparison never reaches the fingerprint, because they are not
  entitled to a diagnostic about a purchase that is not theirs. Same scope, different
  purchase, is `checkout_intent_mismatch`; a row with no readable binding is
  `checkout_intent_binding_unavailable`. All three are **409 and deliberately WITHOUT
  `data.order_id`**, which on this endpoint is the established table-occupied signal
  and would send a client down an unrelated recovery.
  **THE FINGERPRINT IS TAKEN FROM THE D01-VALIDATED REQUEST, BEFORE
  `normalize_order_items`.** That ordering IS the contract: normalisation reorders
  choices into menu-definition order against the live catalogue, so a fingerprint taken
  afterwards would move when the MENU moved and a diner recovering a lost response
  would be told their purchase was different because the restaurant edited a dish. It
  is equally never reconstructed from the PRICED ROWS — an unavailable line is
  persisted at quantity 0 and identical configurations are merged, so the saved rows
  cannot say what was asked for. Excluded deliberately: labels, every price,
  publication and stock state, the clock, and the signed session token (renewed
  routinely for the same diner, so it would turn an ordinary renewal into a different
  purchase).
  **QUANTITY 3 IS 1 + 2, AND COALESCING RUNS ONLY AFTER THE RAW D01 CEILINGS.** The
  server merges identical configurations into one stored row, so both spellings produce
  the same order and treating them as different intents would hand a spurious conflict
  to any client that tidied its basket between attempts. But the ceilings count RAW
  entries — a per-line quantity of 100 is refused whether or not 99 + 1 would be
  accepted — so coalescing before them would let a forbidden request in through an
  equivalent spelling. Modifier choices are sorted and DE-DUPLICATED (matching the
  server's own canonicalisation); extras are sorted and NOT de-duplicated (a duplicate
  extra is REFUSED upstream, and collapsing one here would turn a request the server
  rejects into a valid replay of one it accepted) — the same asymmetry `order_pricing`
  documents. Modifier ids stay OPAQUE: never case-folded, trimmed or parsed as UUIDs.
  **NULL MEANS "PREDATES D04" AND IS NEVER GUESSED EITHER WAY.** A pre-D04 order has no
  record of the request that created it, and populating the column from a retry that
  arrives later would certify an equivalence nobody observed. Equivalence is simply not
  decidable, which is a third outcome with its own honest answer rather than a match or
  a mismatch. The version rides INSIDE the value (`v1:<sha256>`), so an encoding this
  build cannot read takes the same branch.
  **TWO RACE WINDOWS, TWO PROTECTIONS, NEITHER REDUNDANT.** A POST-WAIT RECHECK closes
  the same-table window: the step-1 lookup runs before the table lock, so a competing
  request may commit while this one waits, and asking again under the lock is what
  stops the loser doing any new-order work at all — no admission refusal, no occupancy
  rejection, no daily number, no INSERT. The table lock serialises nothing else, so the
  UNIQUE CONSTRAINT closes the rest, and **the daily number and the INSERT now share
  ONE savepoint**: the allocation used to sit outside it, so a loser's failed INSERT
  rolled back while the number it had taken was committed on the way out — one order,
  two numbers, measured as `next_number == 3`. A successful replay must produce NO new
  creation effect, and the counter is a creation effect. The ADMISSION VERDICT is
  therefore read at step 1a (for the lock) but APPLIED at step 1d, after the recheck:
  refusing a replay because a NEW order would now be disallowed is a lifecycle change
  retroactively hiding a diner's own draft.
  **KEYLESS CALLERS ARE RETAINED WITH EXPLICITLY WEAKER GUARANTEES** — `resolve_intent`
  returns ABSENT on a `None` key before it queries and the recheck is skipped outright,
  so they keep the table lock, the occupancy gate and no recovery, at exactly the query
  budget they had. And the server STATES WHAT IT CAN PROMISE: `order_details.
  checkout_protocol` (`checkout_protocol.py`) is a LEVEL — 1 binding, 2 recoverable —
  raised only by the change that makes the next level true. It is deliberately NOT
  `pricing_version`, which describes how the MONEY was calculated; #661 is the standing
  lesson about collapsing two contract introductions into one flag. An ABSENT value is
  level 0 and promises nothing, and a 404 from a future recovery route is a missing
  route, never an intent that never existed.
  **LEVEL 2 LANDED IN D04/C** (next bullet); the frontend coordinator is D04/D
- A LOST CHECKOUT RESPONSE IS RECOVERABLE, AND A RETRY IS NEVER TOLD IT FAILED
  (D04/C): ✅ `OrderAcceptance` (migration `orders_app/0039`, ONE new table, nothing
  else touched) plus an intent-key selector on the diner's existing order read.
  Two failures closed, both of them a checkout reporting FAILURE after SUCCESS —
  the worst answer a checkout can give, because the diner believes nothing was
  ordered while the kitchen is already cooking it. A RETRY AFTER A LOST RESPONSE got
  `This order cannot be submitted.`, because the re-check that produced it reads
  `order_status` — which by then says `pending`, `preparing`, `served` or
  `cancelled`, none of which is a statement about whether the submission LANDED. And
  a client that lost the response ENTIRELY held no order id (that is what it lost),
  so it could not look up what the key it still holds resolved to.
  **THE EVIDENCE IS A ROW, NOT TWO COLUMNS ON `Order`, and that is load-bearing.**
  Django's `save()` writes every field from the in-memory instance, so a caller
  holding an instance loaded BEFORE acceptance writes the pre-acceptance values back
  — silently, with no error, leaving an order that reads as never accepted. No
  production path does that today; a separate row makes it IMPOSSIBLE rather than
  currently-unreached. `tests_order_acceptance` proves the hazard is real on an
  ordinary column first (a stale save DOES revert `order_status`) and then that the
  evidence survives it — tests a two-column design fails.
  **IT IS WRITTEN WITH THE TRANSITION OR NOT AT ALL.** Same transaction: an accepted
  order with no record reports a retry as a failure, and a record with no transition
  tells a client an order was placed the kitchen never saw. It stores WHEN and the
  exact `quote_ref` the acceptance was bound to, and it NEVER MOVES AGAIN — the
  kitchen advancing, recalling or cancelling the order leaves it untouched.
  **THE LIFECYCLE VERDICT IS TAKEN WHERE THE LOCK ORDER REQUIRES AND APPLIED AFTER
  THE REPLAY CHECK** (Codex P1 on PR #317, valid). `admit()` acquires the advisory
  lock AND decides whether new work is permitted, and applying that decision at the
  point of acquisition reported a COMPLETED acceptance as a failure: accepted while
  `live`, response lost, restaurant suspended, diner retries — the lifecycle 400
  fired before the evidence was ever read. That is the exact failure-after-success
  this change exists to remove, over a suspension the diner neither caused nor can
  see, and it swallowed the 409 too. `_create_order` already made this split for
  the same reason (its steps 1a and 1d); it simply was not carried across. The lock
  ORDER is unchanged, and a FIRST submission at a suspended restaurant is still
  refused — pinned as the negative control.
  **THE REPLAY MATRIX.** No evidence → the ordinary invariants decide (which is also
  the pre-D04 case: nothing recorded that acceptance, so nothing may be claimed about
  it). SAME `quote_ref` → **200 `idempotent`**, no second acceptance, no second table
  claim. A DIFFERENT or ABSENT one → **409 `order_already_accepted`** — a different
  acceptance being attempted, or one that cannot be proven the same; neither is a
  failure of the original and neither may produce a second. **A CANCELLED ORDER THAT
  WAS ACCEPTED STILL REPLAYS AS ACCEPTED**: the submission did land, and the
  cancellation is a later, separate fact the same response already carries in
  `order_status`. The check runs BEFORE the `initiated` re-check, because that check
  is exactly what produced the false failure, and UNDER the same locks an acceptance
  takes (advisory → table → order re-read → evidence), so two concurrent submissions
  cannot both conclude there is no evidence.
  **THE RECOVERY READ IS THE EXISTING ONE, WITH A SECOND SELECTOR**:
  `orders/journey/order-details/?intent=<client_order_id>`. ONE decision reached by
  two identifiers, not two routes — a dedicated route would have to be added to the
  diner capability allowlist in BOTH repositories, widening the anonymous surface to
  say what the existing route already says. **Scoped to the session's restaurant AND
  table exactly as the order-id form is**, so holding a key is not authority; the key
  is validated by the SAME rule the write path uses, so a malformed value never
  reaches a `UUIDField` filter. Exactly one selector: naming both is a 400. The
  projection is BYTE-IDENTICAL to the order-id form, and the read now also publishes
  `accepted` / `accepted_at` (the question a recovering client actually has) and
  `checkout_protocol`, because a client deciding whether a retry is safe needs the
  answer from whichever response it has. `CHECKOUT_PROTOCOL` is therefore **2**, and
  it was raised by the change that made BOTH halves of level 2 true at once.
  COST: submit 10 → **12** (one SELECT to ask, one INSERT to write); a replay is
  **7** and returns at the evidence read. Both pinned
- THE ANSWER IS CORRELATED TO THE COMMAND, AND A LEGACY ACCEPTANCE IS NOT A DRAFT
  (D04 completion): ✅ ONE shared projection,
  `orders_app/controllers/services/acceptance_result.py`, read by the acceptance
  transition AND by the diner's own order read, so a client that lost its submit
  response and recovers later is told the same facts in the same shape. No
  migration; no model, endpoint or route added. It closes three gaps D04/C left.
  **`accepted` IS A BOOLEAN OVER "IS THERE A ROW", AND ITS TWO FALSE CASES ARE
  OPPOSITE INSTRUCTIONS.** `get_accepted`'s own docstring conceded it: false covers
  "a genuine draft and an order accepted BEFORE D04 alike". A draft may still be
  accepted; an order accepted before `OrderAcceptance` existed IS ALREADY IN THE
  KITCHEN and must never be accepted again — and the deployed client reads that
  boolean and treats false as reviewable. `acceptance.state` is therefore
  THREE-VALUED, derived and stored nowhere: a row present → `accepted`; no row and
  `order_status == 'initiated'` → `not_accepted`, DEFINITIVE; no row and no longer a
  draft → `evidence_unavailable`. The discriminator is "is this still a draft",
  because that is the only fact available that can rule never-accepted IN.
  **ONLY TWO OF THE THREE ARE VERDICTS, AND SAYING OTHERWISE WAS THE FIRST CUT'S
  ERROR** (Codex P2 on PR #318, valid). `evidence_unavailable` was documented as
  "the order is NOT a draft, so a submission did land" — on the reasoning that only
  `_submit_order` leaves `initiated`. FALSE: the kitchen writes resolve an order by
  primary key through `_get_order_or_none` (which filters only `deleted=False`) and
  neither guards on `initiated`, so a DRAFT can be cancelled outright
  (`KitchenOrderCancelView` — a draft's fulfilment status is still `new`, so it takes
  the free-void branch and needs no manager) or walked
  `new → preparing → ready → served`, whose completion step also writes
  `order_status`. Either leaves a non-draft order with no evidence row. So the state
  has TWO PRODUCERS and **no fact on the row separates them** — no `order_status`
  value is exclusive to acceptance (cancel yields `cancelled`, serve `served`, recall
  `pending`, each reachable both ways), `cancelled_by` is written on both paths, and
  inferring a deploy date is not something this repo does. It is therefore a
  STATEMENT OF IGNORANCE, and `ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING` is that
  contract, kept as a VALUE so a test can pin it. **The client instruction is
  UNCHANGED and conservative — never accept such an order again — and it is
  conservative BECAUSE the server does not know**, since one producer really is an
  order in the kitchen. **THE KITCHEN-DRAFT PRODUCER IS PRE-EXISTING AND REPORTED,
  NOT FIXED HERE**: adding an `initiated` guard changes what the kitchen may do to an
  order, which is a D05 transition decision with its own blast radius.
  **`evidence_unavailable` IS NEVER BACKFILLED** into an
  acceptance and carries NO invented moment or reference — a fabricated receipt is
  indistinguishable from a real one afterwards. A CANCELLED or SERVED order is never
  pushed back to `not_accepted`: the kitchen's later progress says nothing about
  whether the submission landed, and reporting it as a draft is exactly what invites
  a second acceptance.
  **THE SUCCESS RESULTS NAMED NOTHING.** Both were `{status, message, idempotent}` —
  no order, key, scope or reference — so a client validating "is this the outcome of
  MY command?" had nothing to validate against and a late or misrouted 200 was
  indistinguishable from the right one. The projection names `order_id`,
  `intent_key` (the `client_order_id`, `null` when there is none) and the
  SERVER-RESOLVED `scope` (`restaurant` + `table`, off the ORDER, never off the
  request). It is **NOT AUTHORIZATION** — it discloses scope, so every caller must
  already have established that this principal may see this order (the diner table
  session on the read, the session/module gate on the write); it performs no check
  of its own.
  **THE ORIGINAL REFERENCE IS PUBLISHED, AND IT IS READ, NEVER RECOMPUTED.**
  `OrderAcceptance.quote_ref` is the exact figure the diner confirmed and no surface
  returned it, so a client could only recompute `quote_ref(order)` from the CURRENT
  rows — a different question that answers differently the moment anything about the
  order changes. `acceptance.quote_ref` is the stored value verbatim. Pinned from two
  sides: a sentinel no recomputation could produce, and a real order whose rows move
  after acceptance so the current reference genuinely differs.
  **`acceptance.outcome` IS `newly_accepted` / `already_accepted` ON A MUTATION AND
  `null` ON A READ.** A read OBSERVES; it is not the result of an attempt, and
  inventing a third word for "I merely looked" would put a claim in the response no
  caller made. The key is always present so the shape never varies.
  **`current` IS LABELLED APART FROM `acceptance`** (`order_status`,
  `fulfilment_status`, `cancelled_at`, `served_at`) — two independent facts, and
  collapsing them is how a cancelled-but-accepted order reads as never placed.
  **TIMESTAMPS ARE EXPLICIT ISO-8601 STRINGS**, for the reason `format_money` exists:
  the value a view BUILDS is not the value a client PARSES. This projection reaches
  the wire by two paths — a plain dict rendered by DRF's `JSONEncoder` (`.isoformat()`)
  and a serializer method field rendered through `api_settings.DATETIME_FORMAT` —
  which can be configured apart, so formatting here is what makes the two
  byte-identical.
  **`CHECKOUT_PROTOCOL` IS 3 (`CORRELATED`), A NEW LEVEL AND NOT A NEW MEANING FOR 2.**
  A client pinned to 2 keeps exactly the promises 2 made; only one that recognises 3
  may rely on the correlation fields or the three-state verdict. Widening 2 in place
  would be #661 again and worse — a level-2 client reading the old `accepted` boolean
  is RIGHT to treat it as two-valued, because for it, it is. **`accepted` /
  `accepted_at` STAY, with their exact old (conflating) meaning**, because a deployed
  client reads them and a compatibility key that quietly changed semantics is worse
  than one that is merely coarse.
  **THE ANSWER DESCRIBES ONE SNAPSHOT, AND THAT IS A CORRECTNESS RULE RATHER THAN A
  QUERY-COUNT ONE** (the second Codex P2 on #318, also valid). The recovery read
  fetched the order in one statement and the serializer looked the evidence up in
  another; under READ COMMITTED each takes its OWN snapshot, so a submission
  committing between them published `acceptance.state == accepted` beside
  `current.order_status == initiated` — a correlated answer describing a moment that
  never existed, on the one surface whose whole job is to be verifiable.
  `transaction.atomic()` would NOT have closed it (READ COMMITTED re-snapshots per
  statement inside a transaction too); folding the reads does, the same lesson
  `catalogue_snapshot` records. `handle_show_order_details` now fetches with
  `select_related('acceptance')`, and `acceptance_result.read_evidence` is THE one
  reading — it prefers the joined relation by plain attribute access, so the
  preference is automatic rather than something each caller must remember, and the
  serializer's `_evidence` delegates to it. **THE STALE DIRECTION IS SAFE AND THE
  FRESH ONE IS NOT**, which is why the unjoined fallback stays tolerable for an
  ad-hoc caller: reading late can only ADD an acceptance the order row does not
  reflect (the incoherent pair), while reading early yields at worst
  `evidence_unavailable` — ignorance the server is entitled to state — or
  `not_accepted` on a snapshot where the order really was a draft, which the
  acceptance path's replay protection covers. Pinned from BOTH sides: one spec drives
  the REAL journey read on both selectors (so dropping the `select_related` fails),
  another hands the serializer an order IT joined (so reverting `read_evidence` to a
  second query fails), and a third produces the incoherent pair directly from a stale
  instance.
  **IT COSTS NO QUERY.** Both hot callers pass the `OrderAcceptance` row they already
  hold (`_acceptance_replay`'s lookup, the `create()` return, the serializer's cached
  `_evidence`). That is not a micro-optimisation: the acceptance path's cost is pinned
  to exact integers by `WhatTheEvidenceCostsTests` and `tests_order_path_queries`, and
  both are unchanged (submit **12**, replay **7**). Omitting `evidence=` looks the row
  up, which is right for an ad-hoc caller and is what those pinned counts catch if a
  hot path ever starts doing it.
  **A REFUSAL CARRIES NO `checkout` KEY** — a conflict is not an acceptance and must
  not look like one. Pinned by `orders_app/tests_acceptance_correlation.py`
  (37 tests; of the original 29, 27 error on the pre-change tree and the two that
  pass are the compatibility controls that must NOT change; of the 8 added for the
  two Codex P2 findings, 4 fail against the head that carried them, and each of the
  three fixes has its own negative control). See `BREAKING_CHANGES.md` §14
- A SAVED QUOTE HAS A LIFETIME, AND ACCEPTANCE RE-CHECKS THE AGREED PURCHASE
  (D06): ✅ the conditions under which a draft may become a NEWLY ACCEPTED order
  are now defined in one place and enforced at the AUTHORITATIVE boundary rather
  than at a preflight. Four new services, one new table
  (`orders_app/0041_order_quote_closure`, `CreateModel` only), one new route.
  **Every defect below was reproduced over real HTTP on unmodified `main`.**
  Three whole classes of condition were read ONCE, in `ConOrder.initiate_order`,
  off instances loaded in autocommit before any transaction opened, and only for
  anonymous diners. **So a pause did not pause**: an owner setting
  `accepting_orders=False` mid-service stopped new drafts and every draft already
  initiated still reached the kitchen — the settings copy said the switch stopped
  ordering and it stopped roughly half of it. A table taken out of service,
  disabled or switched to `menu_only` still accepted an order initiated while it
  was usable, and the staff/internal entry points checked no table fact AT ALL.
  **No catalogue fact was re-read at acceptance**, so a draft priced against a
  dish since sold out, unpublished, re-configured or re-tagged for allergens was
  accepted unchanged and the kitchen worked from a definition nobody agreed to.
  And a saved quote had no lifetime — a draft priced last week was acceptable at
  last week's prices, indefinitely.
  - `orders_app/controllers/services/order_eligibility.py` — ONE PURE RULE ASKED
    AT THREE MOMENTS (the controller preflight, `_create_order` after the table
    lock, `_submit_order` after the table and order locks). The three cannot
    drift because they call the SAME function; they differ only in WHICH snapshot
    of the facts they hand it. **THREE FACTS, AND THE DIFFERENCE DECIDES WHO THEY
    BIND.** `accepting_orders` is a COMMERCIAL PAUSE on new diner ordering and
    `qr_mode` is ORDERING POLICY for the QR public — an authorized member of staff
    taking an order on a diner's behalf walks past both, which is what that
    exemption is for and it is PRESERVED. Table and restaurant LIVENESS are
    neither: a soft-deleted, disabled, inactive or out-of-service table is not a
    place an order can exist, so it binds EVERY provenance, **staff included —
    that is the one behaviour change, and two tests that pinned the old bypass
    were updated rather than deleted**. PROVENANCE IS THE ORDER'S, NEVER THE
    REQUESTER'S: acceptance passes `order.created_by_id`, exactly as `admit`
    does, so a diner's draft stays a diner's draft however senior the person who
    taps submit. The three legacy messages are BYTE-IDENTICAL; the machine
    `reason` is additive
  - `orders_app/controllers/services/quote_policy.py` — THE LIFETIME.
    `QUOTE_POLICY_VERSION = 1` names ONE complete rule: **anchor
    `Order.time_created`, 30 minutes, `now < anchor + lifetime` so the exact
    deadline instant is EXPIRED**. `time_created` is `auto_now_add` and no later
    write moves it (not a D04 replay, not the submit save, not a kitchen
    `update_fields` save, not `determine-customers`' full save) — which is
    exactly why `time_last_updated` is not the anchor. NO NEW COLUMN: the
    deadline is DERIVED, so an existing draft gets the rule from the timestamp it
    already carries and a rollback removes the rule rather than stranding data.
    **THE VERSION IS FROZEN** — a future change of duration is a NEW version with
    its own number, never an edit to a constant under this one. `assess` takes
    `now` and READS NO CLOCK ITSELF, so the requirement that the comparison
    happen after every lock wait cannot be quietly bypassed. **AN UNUSABLE ANCHOR
    IS ITS OWN ANSWER** (`quote_unverifiable`): never silently fresh (an
    indefinite quote) and never silently expired (refusing a diner over a data
    fault they did not cause), and it closes nothing
  - `orders_app/controllers/services/purchase_integrity.py` — IS THIS STILL THE
    SAME PURCHASE? The saved lines against the catalogue as it is now, reusing
    `build_snapshot` so the whole order resolves in **exactly ONE statement** at
    ONE captured `now` rather than becoming a second catalogue reader. The saved
    MONEY is honoured — that is the lifetime's promise; what is refused is
    preparing food from a definition that changed. One controlled diner message
    plus a bounded classification and a truncated item id in the log — never
    catalogue JSON, amounts or order contents
  - `orders_app/controllers/services/quote_closure.py` + `OrderQuoteClosure` —
    THE DURABLE HALF OF A TERMINAL REFUSAL. A refusal message is one process's
    opinion at one moment; it is not durable and another worker holding a stale
    request knows nothing about it. So minting a replacement quote is only safe
    if the first can NEVER later execute, and a committed row is what makes that
    true: every acceptance path reads it under the same locks before it can
    accept anything. **ONLY TWO REASONS CLOSE A QUOTE** (`quote_expired`,
    `purchase_needs_review`) and the vocabulary is a `CheckConstraint` precisely
    so a future caller cannot widen it — a pause, a menu-only table, a lost
    response or a permission failure says "not now", never "finished", and
    closing on one would destroy a perfectly good quote. Expiry is MONOTONE,
    which is what makes it safe to act on irreversibly; availability is NOT,
    which is why it must be recorded (stock comes back, and an unrecorded refusal
    would let a queued acceptance execute the moment it did). Re-closing returns
    the ORIGINAL row unchanged; an accepted order can never be closed
    (acceptance is resolved FIRST) and a non-draft with no evidence is D04's
    `evidence_unavailable`, refused for review rather than converted into a
    terminal fact
  **THE ACCEPTANCE SEQUENCE, AND ITS ORDER IS THE CONTRACT**: capability
  re-verification → STAFF AUTHORITY re-verification → D04 replay → closure →
  admission verdict → `initiated` →
  operational eligibility → occupancy → acknowledgement (`_quote_acknowledgement`)
  → expiry → purchase integrity → transition. Authorization is FIRST, ahead even
  of the replay: a revoked capability may not read an acceptance any more than it
  may create one. The TRANSIENT checks precede the TERMINAL ones, so an
  irreversible write is never made on behalf of a request the operational rules
  would have refused anyway. And the ACKNOWLEDGEMENT precedes both terminal
  checks, because **a closure retires ONE named reference** — a caller holding a
  stale one is told their order changed and re-reads it, and nothing is retired
  on an assertion they did not make. `_quote_acknowledgement` is shared with the
  retire route so "is this the quote you mean?" has exactly one answer
  - **THE SECOND ENTRY PATH**: `PUT api/v1/orders/retire-quote/`
    (`retire_quote_for_review`), same authority as `submit`. It asks whether a
    saved quote can still be honoured and retires it if not, and **it never
    retires one that is still good** — the client supplies no reason and cannot.
    Both alternatives are worse: minting a replacement unilaterally leaves the old
    quote acceptable, and attempting an acceptance to read the refusal SUCCEEDS
    when the quote is fine, claiming a table and sending food to a kitchen in
    order to ask a question. The CONTROLLER consults no lifecycle state and no
    operational rule and takes NO admission advisory lock — a PAUSED restaurant
    is exactly when a client most needs to establish that its held quote is
    dead, the same asymmetry Step 2E's owner-invitation cancel draws, and
    `accepting_orders`, a suspension, an offboarding and a soft-deleted
    restaurant all leave the diner's session live, so the route is reachable
    through every one of them. **AN UNAVAILABLE TABLE IS THE ONE CASE IT CANNOT
    ANSWER, and that is the CHANNEL's rule rather than the controller's**: a
    diner arrives on a table session, `_resolve_table` re-checks
    `is_available_for_scan()` live on every use, so a soft-deleted, disabled,
    deactivated or out-of-service table REVOKES the session and the endpoint
    answers the capability channel's opaque 404 before the controller is
    entered. Do NOT add a retirement-specific resolution that skips that gate —
    it would let a revoked session drive a durable write, contradict D06's own
    rule that table liveness binds every provenance, add a SECOND capability
    resolution, and buy the diner nothing, since no replacement quote can be
    minted at that table either and the old one cannot be accepted through any
    channel. The client reads the 404 as a round trip that did not answer and
    retries rather than submitting. Pinned by
    `RetiringAtAnUnavailableTableTests`, whose two controls (a live session, a
    paused restaurant) must keep passing. **AND IT IS NOW RE-ASKED UNDER THE
    LOCK** (G1b): the endpoint gate runs in autocommit, so a table going out of
    service inside the wait used to reach nothing at all here — see
    `session_still_admissible` below. LOCK ORDER `Table -> Order`, the
    kitchen's shape and the tail of acceptance's
  - **THE CAPABILITY IS RE-VERIFIED UNDER THE LOCK**
    (`diner_capability.TableCapability` / `assert_capability_current`). The
    endpoint resolves the diner session in autocommit and the transition then
    WAITS for three locks; a QR regeneration inside that wait revokes the
    session, and nothing downstream knew what generation had been presented. It
    carries THREE facts and NO TOKEN — a credential must not travel past the
    point that verifies it — and re-checks the GENERATION and nothing else, so
    it can only ever refuse. It deliberately does NOT re-check
    `is_available_for_scan()`: that is an operational fact binding every
    provenance, and answering it here too would give one fact two answers
    depending on how the caller authenticated. The refusal is the capability
    channel's own opaque 404
  - **AND THE STAFF CHANNEL IS RE-ASKED TOO** (D06 completion, G1b —
    `orders_app/controllers/services/order_authority.py`). The endpoint comment
    used to say why it was not: *"No capability channel was used, so there is
    nothing to re-verify. A staff caller's authority is the module gate above,
    which is not revoked by a QR regeneration."* True, and beside the point — it
    IS revoked by a membership being deactivated, a role being removed or the
    restaurant leaving `portal_access_states()`, any of which can commit inside
    the same wait, after which an order reaches a kitchen on authority nobody
    holds. `StaffAuthority` is the capability's shape exactly: THREE FACTS AND NO
    CREDENTIAL (the principal, the SERVER-RESOLVED restaurant read off the order,
    the module the endpoint gated on), re-running the SAME
    `can_user_access_module` call, so it can only ever REFUSE and cannot widen
    anything. There is no token, no client-selectable actor field and no
    trusted-caller switch. The principal travels as the OBJECT rather than an id
    because a delegation is an in-memory attribute the middleware set on it
    (`permissions_check._DELEGATION_ATTR`), so re-fetching the row would silently
    ask a different question. Asked immediately after the capability check so
    both channels LINEARIZE AT ONE POINT, and answered with the endpoint's own
    non-disclosing 404. As at D05's kitchen boundary, a revocation committed
    before that point is respected and one committing after it can still overlap
    — stated rather than claimed away
  - **RETIREMENT RE-ASKS WHETHER THE SESSION STILL EXISTS** (same change).
    `retire_quote_for_review` runs no eligibility rule by design, so the
    table-liveness fact reached NOTHING inside it — while the route writes a
    CLOSURE, which is irreversible. `diner_capability.session_still_admissible`
    is that one question under the lock, kept a SEPARATE named predicate from
    `assert_capability_current` precisely so acceptance goes on answering it with
    the sentence a diner can read. It returns True for a caller with no
    capability: a staff principal holds no table session, so there is none for a
    table going out of service to revoke. The answer is this route's established
    one — the channel's opaque 404 — which is what keeps it STABLE across the
    lock wait instead of depending on when the operator happened to click
  - **THREE WRITERS NOW PARTICIPATE, AND TWO OF THEM WERE REVERTING COMMITTED
    POLICY.** `first_time_batch_approval` did a full-row `restaurant.save()` from
    an instance loaded before its transaction, so a menu approval silently
    reverted `accepting_orders` AND `status` — walking straight past the two
    walls (`EDIT_INFORMATION` + `read_only`) that make lifecycle single-writer.
    It now writes only the two columns it decides; **narrowing the write is the
    fix, not a lock**, because that block holds a transaction across a MongoDB
    query and a `Restaurant` row lock there would stall every order behind a
    remote call (the PR #306 lesson). `table_actions._update_status` did a
    full-row `table.save()` from an unlocked read, so a concurrent QR
    regeneration was reverted — **un-revoking every diner credential the owner
    had just revoked** — and it now re-reads under `select_for_update` and writes
    `update_fields=['status', 'is_active']`. The `tables` DELETE blocker was
    evaluated in autocommit while Secretary opened its own transaction to write
    the soft-delete, so an order accepted in the gap was invisible; it now runs
    in `_delete_table`, deciding and deleting under the same row lock
  - **THE PAUSE WRITER TAKES THE EXCLUSIVE ADMISSION LOCK.** A `restaurants` PUT
    now takes `lock_admission_exclusive` FIRST, before Secretary's row lock —
    the lifecycle transition's exact order. A `Restaurant` ROW lock would not do:
    `admit` reads `accepting_orders` with a plain `values_list().get()` and under
    MVCC that read does not block on a row held FOR UPDATE, so a pause could
    commit while an admission that had already read `True` was still waiting on
    the table lock. Same reasoning `mark_restaurant_test` records for `is_test`,
    which rides the same protected read. SCOPED TO `restaurants` ONLY — taking a
    per-restaurant exclusive lock for a menu-item rename would queue every diner
    order behind an edit no admission reads
  - **`AdmissionVerdict` NOW CARRIES FOUR VALUES FROM ONE QUERY** — `status`,
    `is_test`, `accepting_orders`, `deleted` — so the operational rule is asked
    about the same protected instant the lifecycle was decided at, and **at no
    extra query**. The two new fields FAIL CLOSED in the opposite direction from
    `restaurant_is_test`: a verdict that never touched the database must not be
    able to claim a restaurant is open and present
  - **RESPONSE CONTRACT**: `order_details.quote_protocol` (level **2** as of
    G3a) and `order_details.quote_policy` `{version, status, expires_at}`, both
    additive.
    **`checkout_protocol` STAYS 3 and is untouched** — D04 answers "can an
    uncertain checkout be retried and recovered", D06 answers "may this quote
    still be accepted", and raising the first for a change that added nothing to
    what it promises would be #661 in the direction that matters most. The
    refusals are HTTP 400 with the established `{status, message, reason}`
    envelope, so the deployed `ErrorInterceptor` forwarding rule already delivers
    the code to the basket. `quote_policy` is a DEADLINE, NOT A RESERVATION: the
    dish can still sell out inside the window, which is the other question
    entirely
  - **A RETIRED QUOTE IS READABLE BACK, AND THE LEVEL IS 2 (G3a).** A closure is
    the DURABLE half of a terminal refusal, and it was published on exactly one
    response — the refusal that created it, which is the one thing a client can
    lose. The diner's own order read published NEITHER the level, the deadline
    nor the closure (those three were added to the INITIATE response only), so a
    quote retired for `purchase_needs_review` INSIDE its window was invisible on
    every surface a recovering client could reach and its only remaining move was
    to attempt an acceptance — precisely what `retire-quote` exists to avoid,
    since when the quote IS good that attempt succeeds, claims a table and sends
    food to a kitchen in order to ask a question. `GET orders/journey/
    order-details/` now carries all three on BOTH selectors, through the SAME
    constant and the SAME projections the initiate response uses (the rule D04/U1
    applied to `quote_total`/`quote_complete` on that serializer), and
    `quote_closure` joins the initiate response too because a D04 REPLAY returns
    an order created earlier whose quote may have been retired since. **THE
    DEADLINE AND THE CLOSURE ARE INDEPENDENT FACTS AND ARE LABELLED APART**, for
    the reason D04 keeps `current` apart from `acceptance`: a quote closed for a
    changed purchase is finished while `quote_policy.status` legitimately still
    reads `live`, and the CLOSURE is what decides acceptability. The projection is
    bounded to `{closed_at, reason, quote_ref, policy_version}` — no actor, no
    amounts, no catalogue detail. **A NEW LEVEL, NEVER A NEW MEANING FOR 1**, and
    the raise was CHECKED against the deployed client rather than assumed: it
    gates with `level < REQUIRED_QUOTE_PROTOCOL` (1), so 2 passes and it keeps
    consulting the deadline unchanged. **IT COSTS NO QUERY** — the diner read
    folds the relation (`select_related('acceptance', 'quote_closure')`) and the
    initiate re-read replaced a plain `refresh_from_db`; that is a CORRECTNESS
    rule before a cost one, the same READ COMMITTED lesson D04 records. Pinned by
    `orders_app/tests_quote_closure_recovery.py` (26 tests; 14 of the first 19
    failed on the pre-change tree)
  - **THE ENQUIRY'S ANSWER SAYS WHAT IT IS ABOUT (G4).** `retire-quote`'s
    answers named nothing — no order, no reference — and `quote_still_valid` is
    the answer that leads to SUBMITTING an order, so a client had no way to
    establish that a 200 in its hand was the reply to the enquiry it sent and a
    late or misrouted one read exactly like the right one. D04 closed that for
    acceptance answers and the enquiry was left behind. Every answer that states
    an `outcome` or a `reason` now carries `order`, the caller's `quote_ref` and
    `quote_protocol`. It is CORRELATION, NOT AUTHORIZATION — the caller has
    already established it may act on this order, and it discloses only what that
    caller just named. **THE OPAQUE 404 IS NEVER STAMPED**, and the rule is
    STRUCTURAL rather than a status list: a body stating no `outcome` and no
    `reason` has said nothing about a quote, so there is nothing for it to be
    about — naming an order inside the channel's non-disclosing refusal would
    turn it into the existence oracle it exists not to be. The echoed
    `quote_ref` is what the CALLER named and can legitimately differ from
    `quote_closure.quote_ref`, which names what the server really retired; both
    are true and they are deliberately not collapsed
  - COST: submit **12 → 14** (the closure read plus ONE catalogue statement,
    flat in the size of the order); a REPLAY is unchanged at **7**, returning at
    the evidence read before any of this; the create path is unchanged, since the
    operational verdict is decided from facts already in hand
  - **CUTOVER IS FRONTEND FIRST, the REVERSE of D05/§15** — and the reverse of
    the usual additions-go-backend-first rule, so do not carry §15's order
    across. The client gates the deadline on `quote_protocol`, so against a
    pre-D06 backend it consults none, never calls `retire-quote` and classifies
    the only two codes that backend emits exactly as the branches it replaced —
    inert. Backend first is NOT inert: a deployed client handles two refusal
    codes and falls through for the rest, so a `quote_expired` refusal surfaces
    a Retry that REPLAYS the same acceptance (only `quote_ref_stale` settles the
    issued command) and is refused identically, while `reserveIntent` answers
    `outstanding` to a fresh checkout — a stuck diner with no in-app escape.
    Uncommon, and completely avoidable by ordering. See `BREAKING_CHANGES.md`
    §16
- CREATION RE-ASKS THE AUTHORITY, AND A REPLAY IS AUTHORIZED BEFORE IT IS
  DISCLOSED (D06/A1): ✅ the acceptance boundary has re-verified its caller under
  the lock since G1b; CREATION asked neither half of it. The endpoint resolves a
  diner's table session or gates a staff caller on the `tables` module IN
  AUTOCOMMIT, and `_create_order`'s transaction then waits for the admission
  advisory lock and the table row — so a QR regeneration, a membership
  deactivated, a role removed or a restaurant leaving `portal_access_states()`
  committing inside that wait revoked exactly the authority the request was still
  acting on. The draft was written and a daily ticket number spent on it, and
  only the LATER acceptance refused it. Both channels now travel through
  `initiate_order` to `_create_order` as the SAME three-facts-and-no-credential
  records G1b introduced (`TableCapability`, `StaffAuthority`), and one shared
  `_authority_refusal` is asked at every point that DISCLOSES or WRITES —
  written once and called from three places, because that is exactly how an
  invariant ends up added to one branch and not the others.
  **NOTHING IS TAKEN FROM A REQUEST BODY.** The capability's facts come from the
  table the session resolved to; the authority names the principal the endpoint
  authorized and the module it gated on, and its `restaurant_id` is
  CROSS-CHECKED against the `Restaurant` row `_create_order` itself loaded before
  the module gate is re-consulted — the staff counterpart of the identity check
  `assert_capability_current` already makes. There is no actor field, no trust
  flag and no `trusted=True` switch.
  **A REPLAY IS EXEMPT FROM NEW-ORDER POLICY, NOT FROM AUTHORIZATION.** The
  idempotent branch hands back an existing order and (since G3a) the closure
  recorded against it, so a revoked session may not read an acceptance any more
  than it may create one. It stays exempt from EVERY new-order rule — a pause,
  menu-only ordering, an item that has since sold out, a quote that has since
  expired — because refusing an order that was already created and acknowledged
  on the strength of a rule about NEW work is the retroactive refusal D04 exists
  to stop. That is why the authority check is there and the admission verdict is
  not.
  **THE TABLE IS RE-READ WITHOUT A LOCK ON THE REPLAY PATH, deliberately.** That
  return is before the table lock on purpose, so recovery never queues behind
  live ordering; a plain statement takes its own snapshot under READ COMMITTED
  and therefore sees any COMMITTED revocation, which is the whole question. A
  revocation committing a moment later can still overlap, and that is stated
  rather than claimed away — the same honest limit the acceptance boundary states
  about where its checks linearize.
  **COST: UNCHANGED ON EVERY CREATE PATH.** The new-order check runs on the row
  step 1b already locks, so it is free; the capability half is skipped outright
  when no capability was presented. The one added query is the lock-free table
  re-read, paid ONLY on a matched replay by a caller that presented a diner
  capability — **1 SELECT → 2**, pinned by its own test beside the unchanged
  keyless one. Every other pinned count in `tests_order_path_queries.py` is
  untouched.
  **THE CLOSURE SERVICE'S STATED PRECONDITIONS ARE NOW VERIFIED, AND ONE
  OVERCLAIM CORRECTED.** `quote_closure.close`'s docstring said "inside the
  caller's transaction, holding the locks an acceptance takes — asserted rather
  than assumed" about the whole sentence, and only the TRANSACTION half is
  asserted. The lock half is not assertable: PostgreSQL records a row lock on
  the tuple rather than in `pg_locks`, so no query answers "do I hold FOR UPDATE
  on this row", and a `FOR UPDATE NOWAIT` probe succeeds exactly when nobody
  holds it. The guarantee is STRUCTURAL instead — every production path takes
  `Order.objects.select_for_update()` first, and `tests_closure_preconditions.py`
  scans the source to keep that true rather than leaving it a convention nobody
  checks, the same shape as the membership-serialization and ambient-authority
  ratchets. There is deliberately NO caller-supplied "already locked" flag: a
  trusted switch would let the one caller that gets it wrong assert its way past
  the only barrier there is. Pinned by
  `orders_app/tests_authority_during_lock_wait.py` (16 new tests; **9 of them
  fail when the check is neutralised and the other 7 are the controls that must
  not change**) and `orders_app/tests_closure_preconditions.py` (12).
  **AND AN UNEXPECTED DATABASE ERROR IS NOT AN AUTHORIZATION ANSWER** (Codex P2
  on PR #324, valid). `_table_row_now` caught a bare `Exception` and returned
  `None`, which `assert_capability_current` reads as a REVOCATION — so an
  `OperationalError`, `InterfaceError` or `ProgrammingError` (a dropped
  connection, a statement timeout, a query defect) came back as the capability
  channel's opaque 404. That is the identical answer a revoked session gets, so
  an outage would have read to a diner as "your table session is no longer
  valid" and to us as an authorization event, indefinitely. The handler now
  names the two conditions its own docstring claimed —
  `(Table.DoesNotExist, ValidationError, ValueError, TypeError)`, a missing row
  or a pk that is not one — and everything else propagates to ordinary error
  handling, as it does on every other read in this service. Pinned by four tests
  beside the existing ones: two propagation cases, the vanished-table control
  that must STAY a 404, and a keyless control proving the read is skipped
  entirely when no capability was presented (which is what keeps that pinned
  query budget flat). Reverting to `except Exception` fails exactly the two
  propagation cases. See `BREAKING_CHANGES.md` §16b
- A REPLAY DISCLOSURE ASKS WHETHER THE SESSION STILL EXISTS (D06/A1b): ✅ G1b
  re-asked whether the presented CAPABILITY was still the current generation;
  that is one half of a diner's session, and the other half is whether the table
  is still a place a diner can be. `_resolve_table` re-checks
  `is_available_for_scan()` live on every ordinary use, so a soft-deleted,
  disabled, deactivated or out-of-service table revokes the session at the door
  — but the TWO REPLAY BRANCHES return before any eligibility rule runs, so that
  fact reached NOTHING inside them: `_create_order`'s idempotent replay handed
  back the order and the closure recorded against it, and `_submit_order`'s
  accepted-submission replay handed back a 200 `idempotent` acceptance result.
  Both now ask `diner_capability.session_still_admissible(capability, table_row)`
  and answer the capability channel's OWN OPAQUE 404 — never
  `order_eligibility`'s diner-readable 400, which stays the answer for a FIRST
  submission, because a refusal that must be stable across a lock wait cannot
  depend on when the operator happened to click.
  **A CALLER THAT PRESENTED NO CAPABILITY IS UNAFFECTED** — a staff principal
  holds no table session, so there is none for a table going out of service to
  revoke, and the predicate returns True for them by definition. **THE EXEMPTION
  A REPLAY KEEPS IS STILL THE RIGHT ONE**: a pause, menu-only ordering, an item
  that has since sold out and a quote that has since expired all leave it
  untouched (the retroactive refusal D04 exists to stop), because table and
  restaurant LIVENESS is not a new-order rule — a table that is not a place an
  order can exist is not a place one can be read back either.
  **NO NEW LOCK AND NO NEW QUERY.** The create path re-uses the single lock-free
  row read A1 already pays for on a capability-carrying replay, memoised by a
  module-level `_once`, so ONE snapshot answers both the authority question and
  the session question and the pinned counts move by nothing; that return sits
  BEFORE the table lock deliberately, so recovery never queues behind live
  ordering and nothing reaches for `Table` or the advisory lock after an `Order`
  is already held. The acceptance path asks inside the locks it already holds.
  **AND THE CREATE PATH HAS TWO REPLAY RETURNS, NOT ONE** (Codex P1 on PR #325,
  valid). The first cut reached the step-1 branch only. A request whose key has
  not been used yet does not take that branch at all: it waits for the advisory
  lock and the table row, and step 1c asks again UNDER the lock precisely
  because a competing request carrying the same key may have committed inside
  that wait. That second return is the SAME disclosure reached by the other
  door. Step 1b' re-asks the capability's GENERATION on the locked row and the
  staff module gate, and **going out of service bumps no `qr_version`** — so a
  table disabled inside the wait passed 1b' and was disclosed at 1c. It asks
  `_session_refusal(table)` now, on the row step 1b already locked, so it costs
  NO query. **THE THIRD RETURN-EXISTING SITE NEEDS NOTHING**: the
  unique-conflict recovery sits AFTER step 1e, which evaluates table liveness on
  that same locked row for EVERY provenance, and the row cannot move while the
  transaction holds it. **THE SESSION QUESTION STAYS OFF STEP 1b'**, where it
  would look tidier — asking it beside the authority check would replace a FIRST
  creation's diner-readable 400 with an opaque 404, and that control is pinned.
  **WHERE IT LINEARIZES IS STATED RATHER THAN CLAIMED AWAY**: under READ
  COMMITTED the lock-free statement takes its own snapshot and therefore sees any
  COMMITTED revocation, and one committing a moment later can still overlap.
  **ONE ORACLE MOVED AND IS RECORDED RATHER THAN REWRITTEN** —
  `test_a_table_taken_out_of_service_still_replays` asserted the OPPOSITE through
  a carried diner capability and is replaced by
  `test_THE_REGRESSION_an_unscannable_table_does_not_replay_to_a_diner`; the
  keyless/internal, staff, paused-restaurant, menu-only, stock-change and
  valid-rescan controls are all kept separately, because an internal call with NO
  capability is not an oracle for a public request carrying a revoked one. Pinned
  by `orders_app/tests_authority_during_lock_wait.py` (72 tests across eight
  classes; the 7 added for the post-wait branch reproduce it as 3 FAILED / 4
  controls on the head that carried it).
  **AND THE REFUSAL IS THE DOOR'S REFUSAL, BYTE FOR BYTE** (Codex P2 on PR #325,
  valid). Every one of these boundaries is documented as answering "the
  capability channel's OWN opaque 404", and three of them answered something
  else: the door raises `DinerCapabilityDenied` and both order endpoints render
  it as `exc.message` — `'Not found.'` — while `_session_refusal`, the
  accepted-submission replay and `retire_quote_for_review` each wrote out
  `'Not found'` by hand. ONE route, two spellings, decided by WHEN the
  revocation landed. As an oracle that separates a door refusal from a
  post-wait one, and liveness revocation from generation revocation, in a
  channel whose whole design is non-disclosure — **but the consequence that
  reaches a diner is larger than that**: the deployed client matches the body
  EXACTLY (`DinerSessionService.CAPABILITY_DENIED_404`, compared with `===`
  after a `trim()` that does not strip a period), so the periodless form was
  not recognised as a capability denial at all and **the rescan panel never
  appeared** — six production call sites consult that predicate, the checkout
  handlers among them. `diner_capability.denial_envelope()` is now the ONE
  answer, DERIVED from the exception rather than re-spelled, and the third site
  (`retire_quote_for_review`, which Codex did not name) is fixed with the other
  two. **THE STAFF CHANNEL IS DELIBERATELY NOT THIS**: its door is the orders
  endpoints' own periodless `'Not found'` and `StaffAuthorityError` already
  matches it, so routing staff through the diner envelope would introduce there
  exactly the mismatch this removes here — pinned by its own control.
  **WHY THE SUITE DID NOT SEE IT**: four tests named "the channel's own 404,
  NOT A NEW WORD" compared only the KEY SET, never the word — the half that had
  drifted. They now go through `assertChannelEnvelope`, and the new
  byte-identity regression drives BOTH refusals over real HTTP on one route and
  compares the responses to each other, so it names no literal and cannot be
  satisfied by two copies that happen to agree. Reverting the three sites fails
  6 of 72. See `BREAKING_CHANGES.md` §16c and `D06_CONSUMER_GATES_CLOSURE.md`
- Historical order-line names (D12, reader only): ✅ PARTIAL — D12 itself stays OPEN.
  Every diner-facing historical name returns the SAVED `OrderItem.item_name_snapshot`
  verbatim through ONE helper, `historical_name()`, with an additive `*_provenance`
  of `snapshot` / `missing`. Before this, a catalogue rename or a soft delete's inline
  vacuum (`<name>_autodelN`) rewrote what a past order said was bought. No migration
  and no backfill. The rule and what it still does not close are under "Key
  Serializer Notes"; the wire contract is `BREAKING_CHANGES.md` §18.
  **B2 (#360) added retention**: `OrderItem.item` is PROTECT, so an ORM delete of
  an ordered catalogue row is refused instead of taking the order's lines with it.
  See "Deletion & Referential Integrity" and `BREAKING_CHANGES.md` §23
- Request correlation and order-command trace (D15 R2): ✅ `dinify_backend/request_context.py`,
  installed ONCE and OUTERMOST on both planes (`settings_admin.py` re-adds it first, then
  ClientIP; `RequestIDMiddleware` is now an alias). Every HTTP request gets one fresh server
  `uuid4().hex` as `X-Request-ID` (exposed to allowed CORS origins only), NEVER read from a
  client, and every console line carries `rid=<32 hex>` or `-` — Django's late
  `log_response` line recovers it from `record.request`; the delegated audit row reuses it.
  **ONE ID PER HTTP REQUEST, NOT PER CHECKOUT**: the validated intent key and the authorized
  order relate a checkout's requests. Only `initiate`, `submit` and `retire-quote` are traced:
  one `dinify.outcome` line with the FINAL status and fixed words (`order_returned` for any
  initiate success, never "new"/"draft"; `refused` only with a reason in `orders.py`'s explicit
  allowlist; `unhandled`/`unclassified` never prove nothing committed). For those requests
  only, console output keeps exception classes and project frames and WITHHOLDS exception text;
  other routes are unchanged. A log line is diagnostic, not durable and not a ledger — an absent
  line proves nothing, and host log parsing/retention is unknown. D15 itself stays OPEN
- Order-path READ BUDGET: ✅ (PR-H §4, tightened by D02) — the per-line cost inside
  `_create_order`'s transaction is **1 query** (the INSERT, and nothing else); a
  4-line order runs **22** and a 1-line order **19**. The ladder, measured on one
  fixture across every pass: 4-line 54 → 35 (D01) → 23 (D02) → 22 (the allergen read
  folded into the snapshot); 1-line 27 → 23 → 20 → 19; per-line 9 → 4 → 1. **The most
  recent repin went DOWN, and it went down BECAUSE of the coherence fix** — folding
  two statements into one is what removed both the half-observed snapshot and the
  query. Pinned by
  `orders_app/tests_order_path_queries.py` with EXACT counts plus a flatness case,
  so a re-introduced N+1 fails CI. **THOSE NUMBERS ARE THE KEYLESS PATH** — a request
  carrying a `client_order_id` costs exactly TWO more, FLAT at any order size (1-line
  19 → **21**, 4-line 22 → **24**): the D04 step-1 intent lookup and the post-wait
  recheck, both pinned. A keyless caller pays nothing, because `resolve_intent`
  returns ABSENT on a `None` key BEFORE it queries and the recheck is skipped outright.
  A MATCHED REPLAY costs **one SELECT** (plus its savepoint pair) — recovery has to be
  cheap or a client cannot use it. What made the difference: `add_order_item` takes
  an optional `order=` (the instance `_create_order` already holds, restaurant FK
  cached — it used to re-`SELECT` the row per line and lazily load its
  `Restaurant`); `find_existing_order_item` and `construct_option_items` take an
  optional `menu_item=` instead of re-fetching it unscoped;
  `find_existing_order_item` evaluates the extras queryset ONCE (four independent
  `if` sites each called `.count()` on it, all four running, plus a fifth and an
  iteration) and uses `.first()` (identical SQL — `OrderItem` carries
  `Meta.ordering`, so Django injects none) and `extra.item_id` rather than
  `extra.item.pk`; and `normalize_order_items` batch-resolves the whole order in one
  scoped query, keying on parsed UUIDs so lookup normalizes exactly as `pk=` did.
  **The optional kwargs are an optimisation, never a trust boundary** — every
  fallback path is preserved and `add_order_item` still self-guards for any caller
  that passes only ids. Do NOT collapse a PREFLIGHT read into the transaction: the
  preflight/authoritative split is deliberately re-read after the table lock
- Order admission vs lifecycle transitions — ATOMIC (Closure PR 2): both stages of
  order creation now consult ONE admission primitive,
  `orders_app/controllers/services/order_admission.py` (THE RULE), synchronised with
  `transition_restaurant` by a **PostgreSQL advisory lock** whose primitive lives in
  `restaurants_app/controllers/admission_lock.py` (the thing locked is a RESTAURANT,
  so the lock sits with that domain and the lifecycle service imports its own app —
  `restaurants_app` must NOT import `orders_app`). Two defects closed:
  (1) SUBMIT ignored lifecycle state entirely — `initiate` while `live`, suspend,
  then `submit` put the order on the kitchen board after trading was supposed to
  have stopped; (2) admission read `Restaurant.status` in autocommit while the
  transaction that WRITES the order locked only the `Table`, so a transition
  committing in between was invisible and `is_test` was derived from a state that
  no longer existed. The module exposes `evaluate` (THE RULE — pure, no DB: diner →
  `allows_diner_ordering`, staff → `allows_order_creation`), `admit` (the
  AUTHORITATIVE check — takes the shared lock, re-reads status under it, then
  calls `evaluate`; raises if not inside a transaction, since an `_xact_` lock in
  autocommit protects nothing), and `lock_admission_shared` /
  `lock_admission_exclusive`. `con_orders.initiate_order` calls `evaluate` as a
  PREFLIGHT only (fast feedback on a possibly-stale instance) and `_create_order`
  calls `admit` as the load-bearing gate — the same preflight/authoritative split
  `validate_order_selections` already uses, and both call the SAME function so the
  two can never disagree about the rule. `_create_order` is therefore now
  SELF-GUARDING for lifecycle as it already was for menu publication.
  **`Order.is_test` is derived from the VERDICT** — `verdict.status` plus
  `verdict.restaurant_is_test`, both read in ONE query under the lock — never from the
  caller's instance. `AdmissionVerdict` carries the tenant flag alongside the status
  for exactly that reason; it defaults False so a bare `evaluate()` verdict (which has
  no database access) can never be mistaken for an authoritative statement about the
  tenant. `_submit_order` passes the ORDER's
  `created_by_id`, not the submitting user, so a diner's draft stays judged by the
  diner rule whoever taps submit. `_xact_` (transaction-scoped) is MANDATORY given
  `CONN_MAX_AGE=600`; the helpers no-op off PostgreSQL. Advisory locks were chosen
  over a `Restaurant` row lock deliberately — Django cannot express `FOR SHARE`, so
  a row lock would serialise every diner at a restaurant behind every other
  (~70–90 ms per order) to guard an event that happens once in its lifetime, and it
  would not span the two WSGI daemon processes the customer and admin planes run in.
  Proven by `orders_app/tests_order_admission_concurrency.py` (7 of its 10 tests
  fail on the pre-PR code)
- Restaurant tag catalog: ✅ Per-restaurant tag catalog (migrations 0044–0045) +
  `restaurant_tags.py` endpoint + `EI_RESTAURANT_TAG`; menu items reference
  catalog tags via `tag_ids`. The endpoint also serves tag reorder
  (`RestaurantTagReorderEndpoint`) and per-tag `usage-count`
  (`RestaurantTagUsageCountEndpoint`) — added additively to match the shipped
  frontend (FEU-02/03, PR #245; no model change / no migration). See URL Structure
- Menu item extensions: ✅ Per-restaurant `menu_item_sort_mode`, `age_restricted`
  flag, and extras `extras_min_selections`/`extras_max_selections`
- Restaurant profile & settings fields: ✅ Identity/contact (`contact_email`,
  `contact_phone`, `landmark`, `tagline`, `cuisine_types`, `socials`),
  availability (`accepting_orders`, `opening_hours`), and tax/receipt
  (`vat_registered`, `vat_rate`, `tin`, `receipt_footer`) fields (migrations
  0049–0051) — all editable via Secretary (`EDIT_INFORMATION['restaurants']`).
  `vat_rate` is a `DecimalField` (default 18.00); `socials` defaults via
  `default_socials`, `opening_hours` via `default_opening_hours` (per-weekday
  `{closed, open, close}` shape)
- Auth: ✅ Refresh-token rotation + blacklist-on-logout + 7-day refresh lifetime
  (SimpleJWT, `JWT_REFRESH_LIFETIME_DAYS`). `users_app/urls.py` mounts
  `GatedTokenRefreshView` (`users_app/endpoints/token_refresh.py`), NOT the stock
  `TokenRefreshView`: refresh is a place a customer token is produced, so it
  carries the same `account_type` refusal as login. Platform staff get SimpleJWT's
  own `token_not_valid` 401 — byte-identical to a bad token, so it is not an
  account-type oracle — and the success body is unchanged. Do not revert the mount
- Customer-plane JWT is `account_type`-gated ON EVERY PRESENTED TOKEN (Closure PR 1):
  `users_app/authentication.py::CustomerJWTAuthentication` subclasses
  `JWTAuthentication` and refuses `platform_staff` in `get_user`. Login and refresh
  bind only where a token is MINTED, so an access token issued moments before a
  promotion stayed valid for its full `ACCESS_TOKEN_LIFETIME` (30 min); migration
  `users_app/0013` blacklists outstanding REFRESH tokens but access tokens are
  stateless. **`USER_AUTHENTICATION_RULE` CANNOT do this job** — in SimpleJWT 5.5.1 it
  is read only by `TokenObtainSerializer`/`TokenRefreshSerializer`, never by
  `JWTAuthentication.get_user`, so a custom rule would gate only the two paths already
  gated. It is WIRED IN TWO PLACES and both are load-bearing: the
  `DEFAULT_AUTHENTICATION_CLASSES` entry, and
  `misc_app/controllers/decode_auth_token.py`, which instantiates an authenticator
  DIRECTLY outside the DRF chain for ~30 customer-plane call sites. The refusal reuses
  SimpleJWT's own `user_inactive` 401, byte-identical to a deactivated customer, so it
  is not an oracle. It must stay on the JWT path specifically — on this plane
  `request.user.account_type == 'platform_staff'` is true IFF the request is
  DELEGATED, so a middleware/permission-class check would break delegated drill-in.
  `users_app/tests_customer_jwt_gate.py` pins both wirings and fails if any
  customer-plane module imports the stock `JWTAuthentication`
- OTP verification hardening: ✅ (PR #192) — the OTP verify path is no longer
  brute-forceable. `UserOtp` gains `attempts`/`consumed_at`/`salt`/`identifier`
  (migration `users_app/0009_otp_hardening`, which also purges pre-existing
  short-lived old-scheme rows for a clean cutover). `make_otp` uses a `secrets`
  RNG + per-row salt + server-pepper HMAC-SHA256 (pepper from `OTP_HMAC_PEPPER`,
  else derived deterministically from `SECRET_KEY` — zero config for prod/CI).
  `verify_otp` fetches the single active challenge BY IDENTITY under
  `select_for_update`, locks out at `OTP_MAX_ATTEMPTS=5`, marks it single-use
  (`consumed_at`), and compares with `hmac.compare_digest`. A per-identifier
  `OtpIdentifierThrottle` (`users_app/throttles.py`) sits alongside the per-IP
  throttle on `verify-otp`/`resend-otp` so brute force can't be spread across
  IPs. The dev `ENV=dev` `1234` override and MSISDN canonicalisation are
  preserved. `make_otp` now returns the delivery TRUTH per environment
  (failure-visibility PR): dev is unchanged (threaded fire-and-forget,
  immediate True); test sends SMS synchronously (3s) and falls back to a
  SYNCHRONOUS email on SMS failure; prod is SMS-only (3s, synchronous). All
  three callers (`login`, `initiate_password_reset`, `resend_otp`) fail CLOSED
  on False with a 500 "We couldn't send your verification code" envelope —
  login previously FELL THROUGH to the token branch on a falsy make_otp, which
  would have bypassed OTP for privileged users
- OTP evidence collection (D11 B2-C): ✅ COLLECTION ONLY — **it is not budget
  enforcement and it does not close D11** (policy, enforcement and operational
  acceptance stay open). `users_app/otp_accounting.py` is the only writer of two tables
  (migration `users_app/0015_otp_accounting`, expand-only): `otp_issuances`, one row per
  challenge `make_otp` wrote, and `otp_verification_failures`, one row per wrong code
  actually compared. The rows serve their own provenance, finalization and cleanup;
  **no rate-limit, shadow, admission or refusal decision reads them.**
  - **Keys are PSEUDONYMOUS, not anonymous**: `u:<User UUID>` or `p1:` + a versioned,
    domain-separated HMAC of the CANONICAL phone under the existing OTP pepper. The OTP
    HASH is not stored here; the phone HMAC key is. Rotating the pepper (or
    `SECRET_KEY` while the pepper derives from it) changes phone keys and splits
    historical grouping; no rotation or reconciliation exists. An unusable legacy phone
    gives a NULL key, and a failed wrong-code observation writes nothing, so coverage
    has gaps. `CHECK`s enforce shapes, vocabularies and state consistency, not the
    truth of a producer's claim.
  - **Origin is server-owned** (`password_login`, `reset_initiation`,
    `owner_claim_challenge`, `login_resend`, `resend_request`, `unattributed`; a
    failure on a pre-ledger challenge records `unrecorded`).
  - **`make_otp` is now one short `atomic(durable=True)`**: `User` `FOR KEY SHARE`
    first (user-backed only), then the replacement delete, the challenge and the
    `pending` row, committed BEFORE any sender runs. The key share first is what keeps
    a replacement from deadlocking an owner-claim redemption. A caller's transaction is
    refused before any write or send; a ledger failure sends nothing and returns
    `False`. Nothing — no `Restaurant`/ownership barrier, no `User`/`UserOtp` lock, no
    transaction — is held across transport.
  - **Finalization is one conditional update after the send**, never changing the
    return value: dev `not_dispatched` (no e-mail recipient) or `unknown`; test/prod
    `accepted` when the existing sender REPORTED acceptance (not handset delivery),
    else `unknown`. The intentional `ENV=dev` OTP `1234`, async dev notifications and
    every return/failure contract are unchanged.
  - **Wrong codes are observed in a savepoint AFTER the attempt counter is saved**: an
    origin-read or row-write failure costs only the observation (the counter, and an
    owner claim's `claim_failed_attempts`, still commit with the ordinary refusal); a
    lost connection re-raises.
  - **Retention is ELIGIBILITY, not a maximum age**: strictly older than 7 days by the
    row's own timestamp, any state, exact boundary kept. One bounded oldest-first batch
    per table is attempted after each finalization ATTEMPT (a finalization error does
    not skip it), outside the issuance transaction; and `manage.py
    prune_otp_accounting` (see Existing Management Commands). Nothing schedules either.
  - **Rollback** keeps the tables and rows; old code stops collection AND automatic
    cleanup. No reverse migration or deletion is authorized.
  - Notes: `docs/engineering/d11-b2-collection.md`; contract: `BREAKING_CHANGES.md` §20
- Password-reset acknowledgement and purpose binding (D11 E-R1): ✅ PARTIAL — D11
  itself stays open. Reset INITIATION (`initiate-reset-password`, and legacy
  `reset-password` with no `otp`) answers ONE body for every identity it acknowledges —
  an eligible account whose challenge was issued, an unknown identifier, platform staff,
  a `pending_initial_claim` owner and an email several accounts share exactly:
  `200 {"status":200,"message":"If these details match an eligible account, check its
  registered phone or email for a reset code."}`, with **no `user_id`**. Refused
  identities get no challenge, no accounting row, no SMS/email and no session.
  **FAILURES ARE NOT ACKNOWLEDGED**: a `make_otp` `False` (pre-send ledger/DB failure,
  sender failure) keeps its existing 500 and a sender exception still propagates.
  `NO_RESET_IDENTIFIER` and the identifier-over-`phone_number` precedence are
  unchanged. COMPLETION answers `400 "Invalid OTP."` for every failure — unknown,
  refused or ambiguous identity, wrong code, or no live reset challenge — and calls
  `verify_otp(..., expected_purpose='reset-password')` with **no destination binding**,
  because initiation issues reset codes with a NULL msisdn and the generic resend with
  the phone. A newer `login` or `owner-claim` challenge for the same account is
  therefore never selected, charged, observed as a failure or spent by a reset; before
  this its code could complete one. An exactly shared email is caught as
  `MultipleObjectsReturned` IN `reset_password._resolve_user` ONLY (bounded,
  address-free log); `get_user_by_email` and login are unchanged and login still raises.
  Each duplicate still resets by its own phone. `ENV=dev`'s `1234`, single use, expiry,
  the attempt cap and both `save_action` contracts are unchanged; E-R1 also left
  `make_otp`'s replacement alone, which D11 E-R2 (next bullet) scoped to the purpose.
  **STILL OPEN, stated rather than implied:** the 500 and
  response timing still distinguish an eligible identity — the 500 PERMANENTLY for an
  account with no destination the environment can send to; the generic `resend-otp`
  route still answers `purpose='reset-password'` differently for an absent account
  (400), a pending one (500) and an eligible one (200), so the same question can still
  be asked there; registration and every rate/abuse limit are unchanged. (An anonymous
  initiation replacing an in-flight LOGIN challenge in the shared NULL bucket, and the
  generic resend's open purpose list, were left by E-R1 and closed by E-R2.) No
  migration, so a rollback restores the old disclosures. Pinned by
  `users_app/tests_reset_acknowledgement.py`.
  The Frontend wording (Dinify-Frontend `forgot-password`) merges FIRST — see
  `BREAKING_CHANGES.md` §21
- Purpose-safe challenge replacement (D11 E-R2): ✅ PARTIAL — D11 itself stays open.
  THREE CHANGES THAT SHIP AS ONE DEPLOYABLE UNIT; none is safe on its own.
  **P1 — REPLACEMENT IS PER PURPOSE.** `make_otp`'s replacement DELETE is
  `filter(user=user, msisdn=msisdn, purpose=purpose)`, inside the existing durable
  transaction (`purpose=None` matches `IS NULL`). A password login and an anonymous reset
  initiation (both `msisdn IS NULL`) no longer delete each other's live challenge, and an
  account-resolving resend no longer deletes the owner-claim challenge — whose loss used
  to charge the INVITATION's claim budget, five times over, into `verification_locked`.
  Same-purpose replacement, the `User`-first lock, accounting, the five-minute expiry,
  the five-attempt cap, single use and sender ordering are unchanged; no lock, migration
  or cleanup was added.
  **P2 — `verify-otp` IS THE LOGIN ROUTE.** `users_app/endpoints/auth.py` passes
  `expected_purpose='login'`, so the locked query never selects, charges, observes or
  consumes a reset, claim, `register` or null-purpose challenge, and a code of one of
  those purposes answers `Invalid OTP` there. CLIENT-VISIBLE: before, a correct reset or
  claim code answered `valid: true` with no token and was spent. The `verify_otp`
  default is unchanged; `self_register` and `create_employee` still pass no binding.
  **P1 NEEDS P2**: with P1 alone, a newer reset row coexisting in the NULL bucket would
  shadow the login code at an unbound route (the mutation removing P2 fails S1 that way).
  **P3 — GENERIC RESEND TAKES FOUR PURPOSES.** `resend_otp` accepts exactly `login`,
  `reset-password`, `register` and `None` (omitted or JSON null) —
  `GENERIC_RESEND_PURPOSES`, a TUPLE so a JSON array or object gets an answer rather than
  an unhashable-type error. Anything else (`owner-claim`, `first-time-payment`, unknown,
  empty or differently cased) is `400 {"status":400,"message":"Invalid purpose"}`,
  decided FIRST — before any account lookup and before the presence check — identically
  for a known, unknown or authenticated caller, with no issuance, deletion, ledger row
  or delivery. P1 cannot do this alone: a resend naming `owner-claim` lands in the claim
  challenge's own bucket. `owner-claim/challenge/` is the only issuer of that purpose.
  `ENV=dev`'s `1234`, the five-minute password anchor for a login resend and the E-R1
  reset contract are unchanged.
  **THE ISSUER/REDEEMER MAP** (a bucket is `(user, msisdn, purpose)`): password login
  `(user, NULL, login)`; reset initiation `(user, NULL, reset-password)`; the claim
  challenge `(user, canonical phone, owner-claim)`; an account-resolving resend
  `(user, phone, purpose)`; an msisdn-only resend `(NULL, msisdn, purpose)`. `verify-otp`
  binds `login`, reset completion `reset-password` (E-R1), redemption `owner-claim` plus
  the destination; registration and `create_employee` are unbound.
  **STILL OPEN, stated rather than implied:** registration verification is
  purpose-unbound and its `msisdn` selector does not require `user IS NULL` (the route
  refuses a phone an account already holds before it verifies); a reset, initiated or
  completed, revokes no outstanding login challenge (the resend path already let one
  survive, P1 extends that to ordinary initiation, and coordinated invalidation is an
  unmade policy); two concurrent same-purpose issuances can still leave two live rows (an
  existing race, not a guarantee; no uniqueness DDL was added, by scope rather than SQL
  impossibility); allowed resend purposes still answer per account (the E-R1
  `reset-password` residual above). Requester-bound resend, registration's future,
  purpose-binding policy, numerical abuse budgets, enforcement and any scheduler are
  undecided. **ROLLBACK** needs no schema step and restores nothing: rows written under
  this change stay as written, `claim_failed_attempts` and ledger rows are not rewound,
  and the old build's cross-purpose replacement and selection apply again, live rows
  included. Pinned by `users_app/tests_challenge_replacement.py` (54 tests: route-level,
  plus four PostgreSQL schedules on independent connections). See
  `BREAKING_CHANGES.md` §22
- MSISDN canonicalisation: ✅ Complete (PR #189) — `256XXXXXXXXX` (12 digits, no
  `+`) is the canonical stored/compared form for `User.phone_number` /
  `User.username`, enforced at every write site (registration, profile update,
  payment intake, OTP create/verify) via `normalise_msisdn()` and backfilled by
  migration `users_app/0008_backfill_canonical_msisdn`. See the "Phone Numbers /
  MSISDN" CRITICAL section below
- Self-service profile update: ✅ (PR #208) — `PUT users/user-profile/`
  (`self_update_user_profile`) now applies for EVERY role (owner/manager/staff/
  admin, previously blocked with a "refer to your manager" stub; only role-less
  diners could edit). Phone is NOT self-editable (canonicalised via
  `normalise_msisdn`; a real change is 400, an echo of the stored value is a
  no-op) and an email already held by another user is rejected 400. The
  never-finished profile-update approval queue was DELETED
  (`profile_update_approvals.py` and `COL_PROFILE_UPDATE_APPROVALS` removed).
  `PUT users/user-profile/` is now the ONLY profile-write path: the manager-OTP
  route went with it — `V2UserProfileEndpoint`, the `update_user_profile`
  controller and the `SerPutUserProfile` write serializer were all DELETED as
  dead surface (C5, PR #247), the frontend never having called them.
  `SerGetUserProfile` is kept
- Ambient administrator authority — REMOVED: ✅ (Phase 0.5 PR-A) `User.roles` is no
  longer an authority vocabulary. `account_type` decides the plane; `roles` carries
  restaurant roles only. `is_dinify_admin` / `is_dinify_superuser` / `dinify_roles`
  and the `DINIFY_ADMIN` / `DINIFY_ACCOUNT_MANAGER` constants are DELETED, along
  with every grant they fed: the `check_permission` write bypass, the full-access
  module map, the `None` "unrestricted" sentinel on both id resolvers (and with it
  `build_scoped_instance_queryset`'s `model.objects.all()`), the `can_manage_restaurant`
  short-circuit, the login `require_otp` arm, the `user-lookup` disjunct and the
  first-time menu self-approval exemption. Four surfaces with no reachable
  principal went with them — `admin-register-restaurant`, the subscription write,
  `reports/dinify/*` and `support/admin/issues/` (see their sections). The refresh
  route is now gated and migration `users_app/0013` blacklists outstanding
  platform-staff tokens. Cross-tenant reach on the customer plane comes from a
  delegation grant or from nowhere; `scripts/check_ambient_authority.py` +
  `dinify_backend/tenancy/tests_ambient_authority.py` keep it that way. PR-A is the
  first rung of the four-PR Phase 0.5 ladder closed in `PHASE_0_5_CLOSURE.md`
- Tenant isolation / role-permission ENFORCEMENT: ✅ Portal gates enforce
  per-module access via `can_user_access_module` / `get_module_restaurant_ids`
  (`users_app/controllers/permissions_check.py`) — see the "Tenant Isolation /
  Role-Permission ENFORCEMENT" section below (PR C)
- Canonical diner menu-publication + checkout-eligibility policy: ✅ ONE
  server-owned policy module `restaurants_app/controllers/menu_publication.py`
  now defines "what a diner may SEE and ORDER" for BOTH the public menu read
  (`handle_show_menu` + the public serializers) and the anonymous order path
  (`ConOrder.initiate_order` / `_create_order`). Split into STRUCTURAL
  (`approved`&`enabled`&not-`deleted`, identical read+order) vs OPERATIONAL
  (`available` + section schedule) visibility; for sections & groups both are
  enforced on both paths, and the ONLY read-vs-checkout difference is an item's
  OWN `available`/`in_stock` (read hides, checkout keeps the established
  zero-and-flag reconciliation — never a hard reject). Predicates:
  `restaurant_can_serve_menu` (active + not-deleted; NOT `accepting_orders`),
  `section_operationally_visible`, `group_operationally_visible`,
  `item_visible_in_menu` (read), `item_orderable` (checkout),
  `extra_publishable` (structural-only inheritance: `is_extra` + same-restaurant
  + structural section/group, NO schedule), `normalize_extras_applicable`
  (defensive dedup/UUID parse of the persisted allowlist), `resolve_public_restaurant`
  (fail-closed: missing→400, malformed/unknown/deleted/non-active→one generic
  404), `build_safe_extras_map` (batch), and `validate_order_selections`
  (tenant + publication + extra applicability/`is_extra`/`has_extras`/no-dup/no-self
  + min-max on the unique set; returns a dict, `_create_order` raises
  `OrderItemRejected`). The read path threads ONE captured local `now`
  (`timezone.localtime()`) + a `menu_policy` serializer CONTEXT that is the sole
  switch into strict public mode (absent → operator/unit-test behaviour
  unchanged; `SerializerPublicGetMenuItem` is shared with `restaurant_setup.py`
  management). Anonymous checkout re-validates AUTHORITATIVELY inside
  `_create_order`'s transaction — after the table lock, before the daily counter
  — closing the check-then-write TOCTOU; idempotent replay stays first (a replay
  is NOT re-validated, so it returns the original even after the menu changes).
  `NOT_ON_MENU_MESSAGE` moved to the policy module (re-exported from
  `con_orders`). Extras integrity applies to STAFF too (they bypass publication,
  never tenant/relationship integrity). `MenuItem.extras_applicable` write-time
  integrity has since LANDED (PR #234, migration `0055_sanitize_menu_item_extras`)
  — see the "Write-time menu relationship integrity" bullet below; its
  `non_fk_tenant_inventory.py` entry is now `remediated` (was `pending-audit`) and
  the runtime read/order paths still fail closed as defence-in-depth. Do NOT
  re-scatter these predicates or gate public filtering on a caller-supplied HTTP flag
- Write-time menu relationship integrity: ✅ (PRs #226, #234) —
  `restaurants_app/controllers/menu_relationships.py` is the WRITE-TIME companion
  to `menu_publication.py` (runtime read/order): the authority behind
  `SerializerPutMenuItem` and the `deletion_blockers()` on
  `MenuItem`/`MenuSection`/`SectionGroup`. It enforces that `extras_applicable` is
  a canonical ordered list of unique lowercase-UUID strings, each an existing,
  non-deleted, same-restaurant `is_extra` MenuItem and never the item itself
  (fresh caller input is REJECTED on any invalid member, never silently dropped —
  unlike the tolerant parse of an already-persisted allowlist); that
  `has_extras=False` cannot retain an allowlist or non-zero selection limits; that
  the extras selection limits validate against the COMPLETE effective state
  (persisted merged with the partial update); that a non-null `section_group`
  always belongs to the item's EXACT section — a partial update that moves the
  section but OMITS `section_group` re-validates against the new section rather
  than silently keeping it (the section-group cohesion rule, PR #226); and that a
  referenced extra cannot be demoted (`is_extra` True→False) or soft-deleted while
  an active same-restaurant parent still depends on it. Migration
  `0055_sanitize_menu_item_extras` deterministically repaired the persisted corpus
  (data-only, idempotent, LOSSY → irreversible). Do NOT re-scatter these
  write-time predicates
- Anonymous diner capability — capability-only, header-only entry + fail-closed
  key: ✅ The QR scan → table-session flow
  (`restaurants_app/controllers/diner_capability.py`; `django.core.signing`,
  salt-separated QR credential vs table session) is the SOLE anonymous authority.
  A signed QR credential in the `X-Diner-Credential` header is the ONLY input that
  mints a session (`handle_table_scan`); the raw `?table=<uuid>` legacy scan, the
  `DINER_ALLOW_LEGACY_TABLE_SCAN` flag, and `_resolve_legacy_table` were REMOVED —
  a raw table UUID never grants authority (no setting re-enables it). The QR
  credential is bound to the table's `qr_version` (migration `0054_table_qr_version`)
  and verified WITHOUT expiry — bumping `qr_version` (QR regeneration in
  `controllers/tables.py`) REVOKES every credential previously issued for that
  table; the short-lived table SESSION is the separate expiring token. The
  authenticated management reads hand the owner each table's CURRENT credential,
  minted per read via `issue_qr_credential` (never stored; token bytes vary per
  read, the authority doesn't): the flat `restaurant-setup/tables/` list
  (`SerializerPublicGetTable.qr_credential` — the portal Setup View's page-load
  read, added later because PR 7A only covered the other two and the portal lost
  every credential on reload), the grouped `?grouping` read, and the
  regenerate-qr response — all SIX of those sites now gated on
  `qr_disclosure_policy`, which withholds from a delegated caller and from anything
  that cannot positively establish ordinary `tables` authority (see the QR
  disclosure bullet below; that gate is the reason "hands the owner" is literally
  true rather than approximately). Both tokens
  travel HEADER-ONLY (`credential_from_request` → `X-Diner-Credential`,
  `session_token_from_request` → `X-Diner-Session`); the `?credential=` /
  `?session=` / body-`session` fallbacks were removed. `DINER_CAP_KEY` is
  FAIL-CLOSED (`dinify_backend/diner_cap_config.py::resolve_diner_cap_key`): a
  deployed (`DEBUG=False`) env MUST set an explicit key ≥32 chars, ≠ `SECRET_KEY`,
  non-placeholder, or the app raises `ImproperlyConfigured` at settings import —
  it never derives from `SECRET_KEY` in prod (DEBUG-only derived fallback +
  warning; error messages never print the key). Capability responses
  (table-scan / order-details / payment-details + order/review writes) are
  `no-store` via `misc_app/controllers/http.py` (`no_store` /
  `NoStoreResponseMixin`). Downstream ops derive scope from the session (body ids
  may only MATCH, never widen), and an invalid diner session never falls back to
  staff JWT. `DINER_CAP_KEY` MUST be configured in every deployed env before the
  auto-deploy runs (migrate / check --deploy fail closed without it)
- QR CREDENTIALS ARE WITHHELD FROM A DELEGATED READ, DENIED BY DEFAULT: ✅ one
  decision, `restaurants_app/controllers/qr_disclosure.py`, and one invariant —
  **QR material is emitted only after ORDINARY, NON-DELEGATED authority for the
  relevant table scope has been positively established; otherwise it is withheld
  BEFORE SIGNING.** `SETUP_READABLE_RECORDS` admits `tables` to the delegated
  restaurant-setup GET, and that read MINTED a credential per row — so a delegated
  administrator holding even the READ-ONLY `view` scope received working ordering
  authority for every table in the restaurant as an ordinary consequence of opening
  a list. The credential is verified WITHOUT expiry and revoked only by a
  `qr_version` bump (a reprint), so it OUTLIVED the delegated session and the grant.
  Measured against unmodified `main` before anything changed: the credential
  minted a real diner session, and `support` scope disclosed identically.
  **THE CONTAINMENT IS FIELD-LEVEL, NOT THE REMOVAL OF THE TABLE VIEW** — number,
  area, capacity, status, geometry, `has_qr` and `qr_mode` are untouched, and both
  scopes keep them.
  **"NON-DELEGATED" IS A VETO OVER THE MODULE CHECK, NOT A REFINEMENT OF IT.**
  `can_user_access_module` / `get_module_restaurant_ids` INTENTIONALLY resolve a
  delegated principal from the grant, so the resolver correctly says a delegate may
  read tables. Reading a table is not the authority to mint the credential that
  orders from it, and the resolver cannot tell those apart because it was never
  asked to. `request_is_delegated` reads BOTH server-derived signals the platform
  already establishes (`delegation_context` on the request, and the
  `PRINCIPAL_DELEGATION_ATTR` marker the delegated authenticator sets on the user),
  and ANY failure to read either one answers DELEGATED — the only safe answer to
  "I could not tell" is the one that withholds.
  **THE POLARITY IS THE OPPOSITE OF THE `menu_policy` PRECEDENT, AND THAT IS THE
  WHOLE THING.** There, an ABSENT context correctly means the ordinary operator
  path. Copying it here produces a containment that reads as applied and discloses
  anyway — measured: `Secretary.read()` passed NO serializer context, so a
  `if delegation_context(request): return None` guard in the serializer evaluated
  against `{}` and the credential still minted a session. So **`Secretary` now
  threads the request into both its paginated and unpaginated serializer
  constructions** (`_read_context`, which carries the request and decides no
  policy), and a missing request, an anonymous principal, a builder handed no
  policy and a serializer with no context ALL resolve to `WITHHOLD_ALL`.
  **THAT CONTEXT CHANGE ALSO BROKE EVERY PORTAL IMAGE, AND THE REQUEST MUST
  STAY IN IT.** DRF renders a file field as an absolute URL once a request is
  in the context, so the portal's menu, section and restaurant images all
  broke. The fix is on the FIELD, not on the context. See MEDIA-PATH-00 under
  Key Serializer Notes.
  **THE WIRE CONTRACT IS OMISSION**, never `null`, `''`, the table UUID, a
  placeholder or a credential-bearing URL under another key: the serializer POPS
  the field per instance when the policy withholds everything (the signer is then
  invoked ZERO times, measured), and a per-row refusal returns a sentinel the
  builder removes rather than calling the signer. Scope is a SET resolved ONCE per
  response, so a row is checked against its own restaurant and there is no
  permission query per row. Six sites pass it explicitly — the flat list, the
  grouped builder, and the five ordinary table-action responses (`seat`, `clear`,
  `transfer` source+destination, `update-status`, `regenerate-qr`). `regenerate-qr`
  ADDS the key only when permitted rather than computing and dropping it.
  Pinned by `restaurants_app/tests_qr_disclosure_boundary.py` (35) and the CONVERTED
  `platform_admin_app/tests_delegated_qr_disclosure.py` (11) — the three assertions
  that asserted the disclosure are INVERTED and the two fixtures that depended on it
  are REBUILT against an ordinary authorized read, recorded in that file's docstring
  rather than done by deleting tests. **IT REVOKES NOTHING ALREADY DISCLOSED** —
  see `DELEGATED_QR_TRIAGE.md` §7 and `DELEGATED_QR_OPERATOR_NOTE.md`, which records
  the grant/session evidence, its limits (above all that a delegated GET that
  SUCCEEDS writes no audit row, so absence of one proves nothing) and the per-table
  rotation option. No production record was inspected and no table was rotated
- Role-permission MANAGEMENT surface: ✅ Owner-only GET/PUT
  `api/v1/restaurant-setup/role-permissions/` (`RolePermissionsEndpoint`,
  `restaurants_app/endpoints/role_permissions.py`) reads all four role grids and
  writes a non-owner role's grid for the team-settings screen. Backed by the
  `RestaurantRolePermission` override model (migration `0052`) + idempotent
  default-row backfill (`0053`); coded defaults live in
  `restaurants_app/configs/role_defaults.py` (`DEFAULT_ROLE_MODULES`: owner &
  manager all-True, kitchen → kitchen-only, staff → tables-only) over the 7
  `GRID_MODULES` (dashboard, kitchen, tables, menu, reviews, reports, settings;
  `billing`/`team` are off-grid owner-only, `support` ungated). Controllers
  (`restaurants_app/controllers/role_permissions.py`): `ensure_role_permissions`
  (seeder), `get_role_permissions`, `update_role_permission` — explicit
  validation (NOT Secretary/EDIT_INFORMATION), owner row immutable & all-True
  (`editable:false`), partial PUT merges over the effective grid under
  `select_for_update`. `create_employee` also surfaces the one-time
  `temp_password` in its owner-only response (PR #173)
- Platform-admin identity layer: ✅ `User.account_type` (migration
  `users_app/0010_user_account_type`, backfilled by the reversible data migration
  `0011_flip_admin_account_type`) is the discriminator separating
  `restaurant_user` from `platform_staff`; constants live in
  `string_definitions.py` (`ACCOUNT_TYPE_*`). It is NEVER Secretary-editable and
  never writable on a serializer — `platform_admin_app/services.py` is the only
  writer, so promotion always runs through the invariant service. It enforces
  MUTUAL REJECTION between the two planes (PR-2b): customer `login` refuses
  platform staff AFTER `authenticate()` but BEFORE the unconditional
  `RefreshToken.for_user` — deliberately above the client-controlled
  `source='diner'` branch that skips the OTP gate — and `reset_password`'s single
  `_resolve_user` carries the same guard, closing an email-only path to minting a
  customer JWT as an administrator. `User.phone_number` also became `unique=True`
  in the same PR (migration `0012_alter_user_phone_number`), staying `null=True`
  so platform-staff accounts (which have no MSISDN) do not collide
- Platform-admin control plane: ✅ `platform_admin_app` — a SEPARATE Django plane,
  not a section of the customer API. Its isolation is structural: its own settings
  (`dinify_backend/settings_admin.py`), root urlconf (`urls_admin.py`) and WSGI
  entry (`wsgi_admin.py`), served on `admin.dinifyapp.com` behind a dedicated
  Apache `WSGIDaemonProcess` (Topology A, same-origin — no CORS). The customer
  urlconf carries NO admin routes and the admin urlconf carries NO customer
  routes; every admin route is an explicit deny-by-default path, never a
  `<str:action>/` catch-all. Built across PR-1 → PR-4b (migrations
  `platform_admin_app/0001`–`0006`):
  - `PlatformStaffAuth` — TOTP secret (encrypted via `ADMIN_SECRET_ENCRYPTION_KEY`),
    SHA-256 recovery-code hashes, DB-backed lockout (NOT the DRF throttle, whose
    LocMemCache counters are per-mod_wsgi-worker; throttles ship as defence in
    depth), and `last_totp_counter` so a matched time step cannot be replayed
  - VERIFICATION IS ATOMIC AND LOCKED (PR-C): `auth/verify/` and `auth/elevate/` each
    do ALL their work in ONE `transaction.atomic()` under `select_for_update`, and the
    raw session token reaches the response cookie only after it commits. They used to
    consume the second factor OUTSIDE the session transaction and audit AFTER it
    committed, so a recovery code could be burned or a TOTP counter advanced with no
    session produced, a live `AdminSession` could exist with no success audit row, and
    two concurrent requests could both mint. **LOCK ORDER: `User` then
    `AdminLoginChallenge` then `PlatformStaffAuth`, never out of order** (verify takes
    the last two; elevate and `lockout.register_failure` take only the last;
    `challenges.create_challenge` takes only the FIRST) — keep it that way or you
    introduce a deadlock cycle. `create_challenge` deliberately locks `User` and NOT
    `PlatformStaffAuth`: it goes on to write-lock challenge rows through its consuming
    `UPDATE`, so holding the auth row first would give
    `PlatformStaffAuth → AdminLoginChallenge`, the exact reverse of verification's
    order. `User` is safe to take first because `resolve_challenge(for_update=True)`
    passes `of=('self',)`, so the join does not lock `User` as a side effect and the
    admin-auth transactions therefore acquire it in one consistent position.
    (CORRECTION, Closure PR 2, CLOSED by PR-E: this used to read "precisely because
    nothing else holds it". That was FALSE — `delegated_sessions.exchange_code` called
    `select_for_update()` with `select_related('administrator', 'restaurant')` and no
    `of=`, which on PostgreSQL locks every row in the join, so it HELD a `User` row
    lock AND a `Restaurant` one. Its lock ORDER was always sound — `exchange_code`
    takes its rows in a single statement, so it can neither self-deadlock nor
    interleave — but the BREADTH was wrong, and it closed a real ABBA cycle against
    `transition_restaurant`: redemption held `User` and waited for `Restaurant` while
    the transition held `Restaurant` and waited for `User` (the `FOR KEY SHARE` its
    `AdminAuditLog.actor` FK insert takes). PostgreSQL aborted one side — a 500, not
    corruption. **PR-E added `of=('self',)`, so redemption now locks the
    `DelegationGrant` row and nothing else**, and the only row both transactions still
    touch is `User`, which both take `FOR KEY SHARE` — compatible, so the cycle cannot
    re-form. Proved by `platform_admin_app/tests_delegation_lock_scope.py`, whose two
    tests fail on the pre-PR-E code. The advisory lock on the order path predates this
    and stands on its own reasoning — shared/exclusive semantics and cross-process
    reach — not on redemption's since-removed `Restaurant` lock.)
    `challenges.consume` is now CONDITIONAL and returns bool (a lost race rolls the
    whole request back); `resolve_challenge(..., for_update=True)` locks with
    `of=('self',)` so the joined `User` row is not locked as a side effect;
    `record_attempt` and `lockout.register_failure` are lost-update-free (an `F()`
    update and a `select_for_update` re-read respectively). FAILURE-AUDIT RULE:
    ordinary denials audit INSIDE the transaction and commit with their own failure
    accounting (either a failure is both counted and recorded, or neither); the ONE
    path that must roll back — losing the challenge race, factor already consumed —
    audits AFTER the block via `_LostChallengeRace`, the house carry-out pattern from
    `restaurants_app.controllers.lifecycle._deny` and
    `platform_admin_app.delegated_sessions.exchange_code`. The repo uses NO
    `transaction.on_commit` / `durable=` / `set_rollback` / `savepoint=` anywhere —
    statement order is the whole mechanism. `_ineligible_reason` is module-level and
    re-checked by all three of login/verify/elevate
  - LOCKOUT POLICY (PR-C): threshold **10** then progressive backoff — the window
    doubles per further failure, 1 min → 60 min cap (`ADMIN_LOCKOUT_THRESHOLD`,
    `ADMIN_LOCKOUT_BACKOFF_BASE`, `ADMIN_LOCKOUT_BACKOFF_CAP`; the flat
    `ADMIN_LOCKOUT_DURATION` is GONE). `failed_attempts` is CUMULATIVE — an elapsed
    window does not forgive it, which is what makes the backoff escalate. The
    per-challenge attempt cap is now its OWN `ADMIN_CHALLENGE_MAX_ATTEMPTS` (5); it
    used to read `ADMIN_LOCKOUT_THRESHOLD`, so raising the threshold would have
    silently widened it. **BREAK-GLASS:** `login/` no longer refuses a locked account
    before the password check — with the correct password it mints a
    `recovery_only=True` challenge (migration `0007`), `verify/` accepts ONLY
    `method='recovery'` against it, and success clears the lock and emits
    `ADMIN_AUTH_LOCKOUT_CLEARED` INSTEAD of the ordinary success entry. An attacker
    cannot ride this path: it needs the password AND a one-shot recovery code. The
    shell equivalent is `manage.py unlock_platform_admin`. Responses stay generic
    throughout — lockout is never an account-existence oracle
  - ONE LIVE CHALLENGE PER USER (Closure PR 1): `challenges.create_challenge` runs in
    ONE `transaction.atomic()` under a `User` row lock, and migration `0008` adds the
    partial unique index `one_live_admin_challenge_per_user`
    (`UniqueConstraint(fields=['user'], condition=Q(consumed_at__isnull=True))`) so the
    invariant survives a caller that forgets. It used to consume and insert in two
    autocommitted statements with no lock, so two simultaneous correct-password logins
    interleaved `UPDATE → UPDATE → INSERT → INSERT` and left TWO spendable challenges.
    The index condition CANNOT reference `expires_at` (a partial-index predicate must
    be immutable), so "live" means UNCONSUMED — which is why the consuming `UPDATE`
    stays load-bearing: it is what frees the slot. The login view wraps
    `create_challenge` AND its `ADMIN_AUTH_CHALLENGE_ISSUED` audit in one
    `transaction.atomic()` — a challenge is a credential, so it is audit-atomic
  - `AdminLoginChallenge` + `AdminSession` — two-step login as separate models, so
    "has a session" never stops meaning "fully authenticated": `auth/login/` checks
    the password and mints a short-lived challenge in its own `__Host-` cookie, and
    only `auth/verify/` (TOTP or recovery code) mints the opaque cookie session.
    Every login failure returns one byte-identical body (the password check runs
    against a dummy hash for unknown users so timing does not leak existence); the
    real reason goes to the audit log. TOTP NEVER consults `ENV` — it shares no code
    path with the restaurant OTP flow's `ENV=dev` `1234` shortcut. `auth/elevate/`
    marks a session recently-elevated for the destructive routes
  - The SECOND FACTOR IS METHOD-EXPLICIT (PR-B): `auth/verify/` and `auth/elevate/`
    both require `{"method": "totp"|"recovery", "code": "..."}`, dispatched by
    `platform_admin_app/second_factor.py` (`check()` → a total `FactorVerdict`,
    never raising). They used to read a bare `code` and try TOTP first, falling
    through to recovery — but `totp.verify` decrypts the secret BEFORE it can
    reject a wrong code and `crypto` fails closed, so a lost/corrupt
    `ADMIN_SECRET_ENCRYPTION_KEY` raised out of the TOTP attempt and the recovery
    branch was never reached. Two deliberately-independent failure paths were
    chained through one key. **`method='recovery'` must never touch `totp` or
    decrypt anything** — that is what makes a lost key recoverable, and
    `endpoints/auth.py` no longer imports `totp` at all so the property is visible
    rather than buried in a branch. `method` has NO default (a default reinstates
    the flawed ordering); the TOTP branch converts `ImproperlyConfigured` into an
    ordinary failure so a broken key is never a 500 or a config leak, while
    enrolment (`totp.encrypt_for_storage`) still fails LOUDLY. Every denial is
    byte-identical — wrong code, wrong method for the code, unknown method and
    unusable key are indistinguishable; the cause rides the audit `error_code` and
    the attempted method the audit `reason`. Break-glass sequence:
    `BACKGROUND_TASKS.md`; contract: `BREAKING_CHANGES.md` §7. Key rotation /
    `MultiFernet` is NOT built
  - A COMMAND CAN NAME THE SESSION IT WAS ISSUED UNDER (D10 B1,
    `platform_admin_app/command_owner.py`). `auth/verify/` and `auth/session/` publish
    `command_owner` — `{version: 1, actor: <User.pk>, session: <AdminSession.id>}`,
    canonical lowercase UUID strings, never the token or its hash — and a client that
    echoes it as `X-Admin-Command-Owner: 1;<actor>;<session>` on an unsafe request is
    refused when the session behind the cookie is not that one: `400
    admin_command_owner_malformed`, `409 admin_command_actor_changed`, `409
    admin_command_session_changed`. **A CSRF PAIR IS NOT A SESSION BINDING**, and the
    old docstring claim that `verify/`'s `rotate_token` "ties the CSRF secret's lifetime
    to the AdminSession" was false (corrected in `endpoints/auth.py`, a `tests_auth.py`
    docstring and `BREAKING_CHANGES.md` §19, which also corrects §12 item 3): `get_token` in
    `session/` RE-EMITS whatever secret it is sent, so a delayed `session/` response
    puts an old CSRF cookie back beside a newer session cookie, and the old tab's token
    matches. `AdminSessionAuthentication` checks the owner AFTER resolving the session
    and re-checking eligibility (both still the existing 401s) and BEFORE CSRF,
    permissions and the handler, so a mismatch is never a CSRF failure (whose one
    re-bootstrap-and-retry would run the command under the new session) and no second
    factor is checked or spent. The SESSION comparison enforces; the actor comparison
    only classifies. ONLY AN ABSENT HEADER is legacy: parsing is exact (one length, no
    trimming or case-folding), so an empty, partial, extra-field, other-version,
    uppercase or proxy-joined (`a, b`) value is `400`, never "absent". Safe methods
    ignore the header. Refusals are fixed sentences, name no id and are NOT audited.
    `auth/logout/` bypasses the authenticator and applies it itself: a malformed header
    is `400` with or without a session; a named sign-out with no live session is the
    ordinary `200` body with NO `Set-Cookie`, no revoke and no audit; a mismatch is
    `409` with none of those; absent or matching is unchanged. **THIS IS B1 ONLY, NOT
    D10 CLOSURE**: a client that sends no header (the deployed Admin client) is still
    exposed; a delayed MATCHING sign-out can still clear a later session's cookie (its
    row survives); a proxy that strips the header silently turns protection off, which
    a `GET` cannot detect; and rolling the backend back to one that ignores the header
    silently unprotects any client that sends it. Pinned by
    `platform_admin_app/tests_command_owner.py`
  - `AdminAuditLog` — append-only (an update raises `AppendOnlyViolation`),
    recording actor / session / action / resource / restaurant / delegation /
    reason / before+after state / result / request id. THE AUDIT CONTRACT, stated
    exactly (`platform_admin_app/audit.py`): privileged successful state changes and
    credential issuance are AUDIT-ATOMIC — the action rolls back if its audit write
    fails. Denials, failure accounting and safety-reducing revocations (logout,
    session revocation) may be audited best-effort or in a separate transaction,
    DELIBERATELY: losing a revocation because its audit failed would be worse than an
    unaudited revocation. The asymmetry is the design, not an unfinished edge — do not
    restate it as a universal "no audit, no action"
  - DELEGATED WRITES ARE AUDITED TRANSACTIONALLY (PR-D): both tenant writes a
    delegation can reach — `POST api/v1/support/issues/` and
    `PUT api/v1/kitchen/menu-items/<pk>/stock/` — call
    `platform_admin_app.delegated_audit.audit_delegated_write` INSIDE their own
    `transaction.atomic()`, so a failed audit rolls the write back. The middleware
    used to write that row from `_finalize` after the view had committed, where it
    could only swallow a failure and log it — the one place the no-audit-no-action
    contract did not hold. Each call sets `_delegation_audited` (on the UNDERLYING
    `HttpRequest`, since DRF's wrapper does not proxy `__setattr__`) so the middleware
    does not double-write. The middleware still owns DENIALS and `process_exception`.
    The third non-safe allowlisted route, `POST api/v1/delegation/end/`, is
    admin-plane only and already self-audits `session_ended`. A NEW delegated tenant
    write must be given a transactional audit or kept off `ALLOWED_ROUTES` — a test
    asserts the non-safe route set so it cannot grow silently
  - `DelegationGrant` + `DelegatedSession` (PR-4a/4b) — time-boxed, reason-required,
    elevation-gated delegated access to ONE restaurant. Minting/listing/revoking
    live on the admin plane; the CUSTOMER plane serves only redeem/inspect/end at
    `api/v1/delegation/`. Authority is bounded TWICE and a request must clear both:
    `SCOPE_MODULES` (inner — the same grid vocabulary every other principal is
    evaluated by; `billing` and `team` are False for both scopes) and
    `ALLOWED_ROUTES` (outer — (route, method) pairs enforced by
    `DelegatedAccessMiddleware` BEFORE dispatch and before the credential binds to
    `request.user`; no wildcard). Both live in
    `platform_admin_app/configs/delegation_scopes.py`, which mirrors
    `restaurants_app/configs/role_defaults.py` and must stay import-light — it is
    imported by the customer-plane permission resolver. Credentials ride the
    `X-Delegation-Session` / `X-Delegation-Code` headers. ONE route is never
    evaluated at all: `EXEMPT_ROUTES` in `delegated_middleware.py`, exactly
    `api/v1/health/ready/` (D15 readiness), matched on the URL pattern before the
    header is read, so a request to it is an undelegated one whatever it carries —
    no lookup, no audit row, no authority. Resolving the header is a query on the
    request's own connection, which let a header stall readiness past its 2 s bound
- Admin restaurant directory + detail READS: ✅ (Phase 1, Step 1 — backend slice)
  `GET admin/v1/restaurants/` and `GET admin/v1/restaurants/<uuid:id>/`, projected by
  `platform_admin_app/restaurant_reads.py` (the views are thin). Session-gated, NOT
  elevation-gated and NOT audited — both deliberate; see the "Admin Restaurant Reads"
  section. Ships with the platform-owned `Restaurant.is_test` flag (migration
  `restaurants_app/0057`, no backfill), which also became a second, independent source
  for `Order.is_test` alongside the existing launch-boundary rule. Readiness delegates
  to the `check_go_live_readiness` seam (still failing closed); payment mode and
  subscription are reported as UNCONFIGURED / LEGACY rather than inferred. The
  readiness ENGINE, the owner-invitation FLOW (Step 2A adds the model; nothing mints,
  delivers or redeems a token), subscription models, receivables, QR generation,
  restaurant creation, support triage writes and the Activity screen are **NOT built**
  — those are later Phase-1 steps
- Admin onboarding domain: ✅ SCHEMA (Phase 1, Step 2A) — `RestaurantOnboarding`
  + `OwnerInvitation` in `platform_admin_app/models.py` (migration
  `platform_admin_app/0009`, purely additive, NO backfill) and the read-only
  owner-FK / owner-membership invariant `assert_owner_consistency`
  (`platform_admin_app/onboarding.py`). Provenance is `admin_created |
  legacy_adopted` with no
  default; claim state and "expired" are DERIVED, never stored; one unresolved
  invitation per onboarding is a partial unique index that deliberately ignores the
  clock; only a token HASH is persisted; there is no delivery model. Nothing creates
  these rows automatically, so absence means "not yet represented in the Admin
  onboarding domain". Owner go-live approval
  remains Step 3. See the "Admin Onboarding Domain" section
- Legacy restaurant adoption WRITER: ✅ (Phase 1, Step 2B) — the first thing that
  writes the onboarding domain, and the only writer of `legacy_adopted` provenance
  (Step 2D added the `admin_created` one; neither can write the other's). `platform_admin_app/onboarding_adoption.py`
  (`adopt_existing_restaurant`) plus the thin operator adapter
  `manage.py adopt_restaurant_onboarding`. NO MIGRATION — the Step 2A schema was
  sufficient. It targets ONE canonical `Restaurant` by immutable UUID (never a name,
  no bulk mode, lifecycle state is not a blocker), requires a platform-staff actor
  and a ≥10-char reason, and creates exactly one `RestaurantOnboarding`
  (`source=legacy_adopted`, `adopted_at`, `adopted_by`; `created_by` NULL) plus one
  `admin.restaurant.onboarding_adopted` audit row in ONE transaction — a failed
  audit rolls the adoption back. A NEW adoption requires `assert_owner_consistency`
  to pass under the lock and **NEVER REPAIRS** an inconsistency (it refuses with the
  canonical `OwnerConsistencyError` code and leaves the drift for a human). It
  creates NO `OwnerInvitation`, leaves the owner-control attestation triple NULL
  (attestation is a separate, still-unimplemented decision), sends no email/SMS, and
  never calls `restaurant.save()` — no lifecycle, `is_test`, owner, employee, menu,
  table, QR or order row is touched. LOCKING: `select_for_update()` on the target
  `Restaurant` FIRST — that row is the serialization point — then
  `RestaurantOnboarding`, then `AdminAuditLog`; deliberately NO admission advisory
  lock (adoption changes nothing an order path reads). Rerun is an idempotent no-op
  that preserves the ORIGINAL `adopted_at`/`adopted_by` and writes no second audit
  row, and does NOT re-check owner consistency — historical provenance is not
  invalidated by later drift. `admin_created` provenance is NEVER converted:
  `onboarding_source_conflict`, no mutation, no audit. Step 2C exposes the resulting
  provenance on the DETAIL read (see the next bullet). **NO RESTAURANT IS
  ADOPTED AUTOMATICALLY and Baba House is NOT adopted**; running the command against
  a tenant is a separate explicit operational action
- Admin onboarding READ projection: ✅ (Phase 1, Step 2C) —
  `platform_admin_app/onboarding_reads.py` (`onboarding_summary`), surfaced as a
  top-level `onboarding` object on the restaurant **DETAIL** read only. NO MIGRATION,
  no writer, no new endpoint. The **directory/list row is deliberately UNCHANGED** —
  the onboarding record belongs in the restaurant workspace, and a list column would
  add per-row joins to every page before a screen asks for them (pinned by a
  directory query-count test). Three INDEPENDENT axes, never flattened into one word:
  `owner_relationship` (delegates to `assert_owner_consistency`, rendering its three
  canonical codes as DATA at 200 — a drifted tenant must not 500), `owner_control`
  (`unavailable` / `not_established` / `attested` / `invitation_redeemed` /
  `stale_attestation`, each with `evidence` + `evidence_at`), and `invitation`
  (`unavailable` / `not_applicable` for legacy / `not_issued` / `pending` /
  `expired` / `consumed` / `cancelled` / `superseded`). **OWNER CONTROL IS EVIDENCE,
  NEVER INFERENCE** — derived only from a legacy attestation triple or a consumed
  `OwnerInvitation`, and never from `last_login`, `is_active`,
  `prompt_password_change`, an OTP row, the owner FK, an owner membership, prior
  orders or portal activity. A legacy attestation counts only when
  `owner_control_attested_user_id == restaurant.owner_id`; otherwise it is
  `stale_attestation` (the payoff of Step 2A storing the attested SUBJECT — a
  replacement owner never inherits evidence), and the read NEVER clears or rewrites
  it. Admin-created control requires an invitation consumed BY THE CURRENT OWNER;
  a consumed invitation for a previous owner still reports `invitation: consumed`
  while `owner_control` stays `not_established` — two axes, not a contradiction.
  `recorded_at` means WHEN THE RESTAURANT ENTERED THE ADMIN DOMAIN (`adopted_at` for
  legacy, the onboarding row's `created_at` for admin-created) — never the tenant's
  own creation and never a claim moment. `owner.claim_tracked` / `owner.claim_status`
  survive as COMPATIBILITY ALIASES derived from the same summary (`tracked`, and
  `owner_control.status` or null) — the `onboarding` object is canonical. Reads are
  pure: no write, no lock, no transaction, no audit row, no repair, no stamping of an
  expired invitation, and no onboarding row created on read. Invitation CREDENTIALS
  are never projected — no `token_hash`, no raw token, no claim URL; an invitation is
  a state word plus, where it is evidence, a timestamp
- Admin-created restaurant + initial owner invitation: ✅ (Phase 1, Step 2D) — the
  authoritative CREATION primitive for a new canonical tenant, and the second writer
  of the onboarding domain. `POST admin/v1/restaurants/` (the existing collection
  route now answers GET **and** POST — see URL Structure), a thin adapter over
  `platform_admin_app/onboarding_creation.py::create_admin_restaurant`. **NO
  MIGRATION** — the Step-2A schema was sufficient; the only settings addition is
  `ADMIN_OWNER_INVITATION_TTL` (7 days). ONE request is ONE decision and ONE
  transaction, creating up to six rows: the owner `User` (only in `mode=new`), the
  `Restaurant`, the owner's `RestaurantEmployee`, `RestaurantOnboarding(source=
  admin_created, created_by=<actor>)`, one unresolved `OwnerInvitation`, and exactly
  one `admin.restaurant.created` audit entry. See the "Admin Restaurant Creation"
  section for the full contract; the load-bearing points:
  **THE OWNER MODE IS EXPLICIT** (`new` | `existing`) and each mode REFUSES the
  other's fields — a phone already in use is a **409 `owner_account_already_exists`**,
  never a silent reuse, because a restaurant attached to the wrong person looks
  exactly like one attached to the right person. **A NEW OWNER GETS AN UNUSABLE
  PASSWORD** — no generated temp password, no `self_register`, no credential email or
  SMS; the retired `admin_register_restaurant` architecture is refused by an AST scan.
  **AN EXISTING OWNER IS NEVER MODIFIED OR REACTIVATED** — locked and re-read under
  the transaction; inactive or `platform_staff` is a 409.
  **RESTAURANT STARTS `onboarding`** and `is_test` is REQUIRED, strict-boolean and
  never inferred. **ZERO `RestaurantRolePermission` ROWS ARE SEEDED** — the resolver
  already falls back to `DEFAULT_ROLE_MODULES`. **ONLY THE TOKEN HASH IS PERSISTED**
  (`sessions.hash_token`); the raw claim token is returned ONCE in a `no-store`
  response and is unrecoverable afterwards — a lost response is repaired by a future
  REISSUE, never by plaintext storage. **ISSUANCE IS NOT DELIVERY** and owner control
  stays `not_established` until a future redemption consumes the invitation. No
  commercial, readiness, QR, menu, table or lifecycle side effect. Redemption,
  reissue/cancel, delivery and the Admin creation UI are **NOT built**
- Admin owner-invitation lifecycle: ✅ (Phase 1, Step 2E) — the administrative
  CREDENTIAL LIFECYCLE between Step-2D issuance and the still-unbuilt Step-2F
  redemption. `platform_admin_app/onboarding_invitations.py` (the domain) plus
  `endpoints/owner_invitation.py` and two routes, `POST
  admin/v1/restaurants/<uuid>/owner-invitation/reissue/` and `.../cancel/`. **NO
  MIGRATION** — the Step-2A schema already carried `superseded_at`, the cancellation
  pair and the one-unresolved index. Both routes are elevated, CSRF-protected,
  reason-required and audited exactly once, and both take a REQUIRED
  `expected_invitation_id` that asserts IDENTITY, not status. REISSUE supersedes any
  unresolved credential and mints a fresh one for the CURRENT canonical owner
  (requiring `assert_owner_consistency`), returning one raw token ONCE under
  `no-store`; CANCEL terminates the exact unresolved credential, creates no
  replacement, and deliberately does NOT require owner consistency — revoking a
  credential must stay possible when a tenant's state is messy. Reissue and creation
  now share ONE mint primitive. Neither operation touches `customer_access_state`,
  and nothing consumes an invitation: `owner_control` still has no path to
  `invitation_redeemed`. See the "Admin Owner-Invitation Lifecycle" section
- Owner-membership serialization: ✅ (Phase 1, Step 2E.1) — the repository-wide
  remediation of PR #305's Codex P1, which was VALID.
  `assert_owner_consistency` is a validator that reads a SNAPSHOT (its own docstring
  says so), so it is only as authoritative as the transaction around it. The
  onboarding writers always did their half — `onboarding_adoption` and
  `onboarding_invitations` take the `Restaurant` row FIRST and call the assertion
  inside it — but the customer plane's `RestaurantEmployee` writers took NO
  `Restaurant` lock, so a membership could change between the check and the
  credential or provenance row it was guarding. **EVERY PRODUCTION MEMBERSHIP
  MUTATION NOW TAKES THE PARENT `Restaurant` ROW**, via
  `restaurants_app/controllers/employee_membership_lock.py`
  (`lock_restaurant_for_membership_mutation`). NO MIGRATION — this is transaction and
  locking discipline over existing canonical data. **INSERTS AND REACTIVATIONS ARE
  COVERED, not merely updates**: a row lock cannot predicate-lock a row that does not
  exist yet, and reactivating a soft-deleted owner membership produces a second live
  owner out of a row the assertion (which filters `deleted=False`) never read. Adoption
  and invitation reissue INHERIT the guarantee with no change of their own; cancellation
  stays independent of owner consistency; Step 2F may rely on the same barrier and is
  still unbuilt. See the "Owner-Membership Serialization" section
- Owner claim challenge: ✅ (Phase 1, Step 2F.1) — the FIRST HALF of owner-invitation
  redemption, and the first thing that consumes a claim token. `POST
  api/v1/users/owner-claim/challenge/` with the raw token in `X-Owner-Claim-Token`
  resolves the invitation and sends the invited owner an OTP under the new
  `owner-claim` purpose. NO MIGRATION. Claim is TWO-FACTOR — the token proves
  POSSESSION, the OTP proves CURRENT CONTROL — so the token alone never establishes
  customer access. The endpoint is `authentication_classes = []` (an ambient JWT,
  delegated session or admin cookie has zero influence on who is resolved), every
  unclaimable state collapses to ONE public 400, and the success body carries a single
  `credential_setup_required` boolean read from `customer_access_state` and nothing
  else. **Its eligibility check is a PREFLIGHT: no lock, no transaction** — the
  invitation, the access state and the password are all untouched, and `owner_control`
  stays `not_established`. Since D11 B2-C the OTP it then issues is written in
  `make_otp`'s own short durable transaction (`User` `FOR KEY SHARE`, the replacement
  delete, the challenge and its ledger row), which commits before the SMS is sent; see
  "THIS IS A PREFLIGHT" below. Step 2F.2 (the atomic consume) HAS SINCE LANDED — see the
  next bullet — so two things this bullet used to describe as future work are now
  facts: `verify_otp` purpose-binds, and the challenge passes the canonical
  destination explicitly so `UserOtp.msisdn` records where the code went. See the
  "Owner Claim" section
- Owner claim redemption: ✅ BACKEND-COMPLETE (Phase 1, Step 2F.2) — the AUTHORITY
  TRANSACTION. `POST api/v1/users/owner-claim/redeem/` turns the claim token
  (possession) and an `owner-claim` OTP (current control) into durable owner-control
  evidence in ONE transaction: for a brand-new owner the invitation is consumed, the
  chosen password persisted, `pending_initial_claim -> established` and
  `prompt_password_change` cleared, all or none, then a customer session is minted; for
  an ESTABLISHED owner claiming an additional restaurant the invitation is consumed and
  a session minted while the identity is not modified at all. Migration
  `platform_admin_app/0010` adds the invitation-level `claim_failed_attempts` budget.
  The response is `token + refresh + restaurant_id` and DELIBERATELY carries no profile
  — see the next bullet for how the client hydrates one. Details in the "Owner Claim
  Redemption" section
- Customer profile bootstrap: ✅ (Phase 1, Step 2F.3) — `GET
  api/v1/users/user-profile/`, the canonical authenticated read that makes the
  redemption handoff usable. Added to the EXISTING user-profile resource (PUT is its
  write side) rather than as a new route, with NO MIGRATION and no new serializer,
  model or session concept. Authority is the default customer stack —
  `CustomerJWTAuthentication` + `IsAuthenticated` — so platform staff on a customer
  token, a `pending_initial_claim` identity and a deactivated user are all refused
  inside `get_user` before a handler runs, and none of that is restated in the view.
  It returns the canonical `SerGetUserProfile`, which with no context delegates to
  `get_any_restaurant_roles` — the SAME call `login` makes — so login and bootstrap
  cannot disagree about the principal they hydrate. **The owner-claim UI must hydrate
  through this read and must NEVER invent `restaurant_roles` or permissions from the
  `restaurant_id` redemption returned**: that id is CONTEXT, the membership resolver is
  authority, and an owner may hold several memberships. **No extra OTP is required to
  bootstrap after a successful claim** — that is the whole reason the claimant is not
  sent back through ordinary login, which sets `require_otp` for an owner membership.
  Genuinely read-only (no `last_login`, no token mint or rotation, no OTP, no
  invitation or `customer_access_state` write, no action log, no audit row) and
  `no-store`. See the "Customer Profile Bootstrap" section
- Pre-claim customer-access gate: ✅ (Phase 1, Step 2D.1) — the follow-up that makes
  Step 2D's central claim TRUE rather than aspirational. A new owner had an unusable
  password, and generic password reset needed only their phone number to replace one
  and hand out a customer session, leaving `owner_control: not_established` /
  `invitation: pending` on an account already exercising owner authority.
  `User.customer_access_state` (`established` | `pending_initial_claim`, migration
  `users_app/0014`, additive with a `db_default` and a vocabulary `CheckConstraint`)
  is the identity-level fact; `users_app/customer_access.py` is the one policy and
  the one sanctioned customer token mint. Every existing identity and every ordinary
  creation path is `established`; only Step-2D `mode=new` writes pending, and
  `mode=existing` never touches it. See the "Pre-Claim Customer Access" section
- Commercial & service configuration: ✅ SCHEMA + WRITERS + ADMIN READ + ADMIN
  SERVICE-CONFIG WRITES (Phase 1, Steps 3B / 3C / 3D.1 / 3D.2a) — a new
  first-party app `commercial_app` with `RestaurantServiceConfiguration` (payment
  timing `pay_first`/`pay_after` + payment collection mode `offline`/`psp_online`,
  both nullable with NO default, each with its own attribution triple) and
  `RestaurantSubscriptionTerms` (the recurring restaurant→Dinify software fee, 0..N
  with at most one OPEN row). Migration `commercial_app/0001_initial` is purely
  additive with NO backfill and creates zero rows — and no PSP model, no
  tax-obligation field, no owner-approval model. Step 3C then added the INTERNAL
  domain writers (`service_configuration.py`, `subscription_terms.py`, `errors.py`,
  `mutation_context.py`): five named mutations, every one serializing on the
  `Restaurant` row, with optimistic concurrency and same-state no-ops. Step 3D.1 then
  added the READ projection — `platform_admin_app/commercial_reads.py`, surfaced as a
  canonical top-level `commercial` object on BOTH admin restaurant reads (no
  migration, no new route, zero extra queries). Step 3D.2a then added the FIRST Admin
  WRITE surface — two elevated, reasoned, audited POST routes for the two
  service-configuration axes (`platform_admin_app/endpoints/commercial.py`), each a
  thin adapter over the Step-3C writer. Step 3D.2b then completed the surface with the
  three SUBSCRIPTION-TERMS writes (`platform_admin_app/endpoints/subscription_terms.py`
  — record / replace / end), on the same elevated + reasoned + audited contract, and
  factored the shared control-plane mechanics both adapters use into
  `platform_admin_app/endpoints/commercial_base.py`. All five Step-3C mutations are
  now reachable over HTTP and NOTHING ELSE IS — `commercial_app` itself still has no
  HTTP surface, no serializer and no management command. See the "Commercial &
  Service Configuration Domain" section for the four concepts these models keep apart
  and the writer contract, "The canonical `commercial` object" under Admin Restaurant
  Reads for what the projection does and does not assert, "Admin Commercial Writes"
  for the service-configuration adapter contract and "Admin Subscription-Terms
  Writes" for the terms one
- Deletion integrity: ✅ Tables-domain deletion model — `Order.table` is
  `on_delete=PROTECT`; dining areas and tables expose `deletion_blockers()`
  and the restaurant-setup DELETE endpoint returns HTTP 409 when a dependent
  still exists
- Support module: ✅ `support_app` — restaurant-facing `SupportIssue`
  ticketing, Secretary-pattern
  endpoints at `api/v1/support/` (`support_app/urls.py`): `issues/` and
  `issues/<uuid:issue_id>/`. The dinify-admin `admin/issues/` triage endpoint was
  RETIRED by PR-A (it read/wrote ANY tenant's issues on the strength of a
  `dinify_admin` role string, via an unrestricted `SupportIssue.objects.all()`;
  Phase 1 rebuilds triage on `/api/admin/v1`). Support is an UNGATED module:
  list/detail/create are widened to ANY active employee of the restaurant via
  `get_employed_restaurant_ids`; references are sequential, collision-safe
  `SUP-000123`. Migration
  `support_app/0001_initial`. The legacy `crm_app.ServiceTicket` app it
  superseded was fully DELETED (PR #193) — its `api/v1/crm/service-tickets/`
  endpoint was `IsAuthenticated`-only with no tenant scoping (any token,
  including a diner's, could read/rewrite EVERY restaurant's tickets);
  `misc_app/0004_drop_service_tickets` drops the orphaned `service_tickets`
  table. Do not reintroduce crm_app
- Reviews module: ✅ `reviews_app` — visit-level `Review` model (one per
  `Order`, `OneToOneField` via `related_name='review_record'`,
  `db_table='reviews'`), Secretary-pattern endpoints at `api/v1/reviews/`
  (`reviews_app/urls.py`): `submit/` (diner submission, AllowAny),
  `summary/` + `analytics/` (owner/manager analytics — `summary/` takes the
  dashboard's selected `from`/`to` and then counts AND lists the reviews in that
  window over inclusive EAT days, the same bounding `analytics/` uses; with neither
  it keeps its original contract, a rolling last 30 days plus the three newest
  reviews of all time, and one without the other is a 400 — REVIEWS-WINDOW-00),
  `<int:review_id>/resolution/` (owner/manager mark-handled write, optional
  `resolution_note` that persists across reopen/re-resolve), and `` root
  (owner/manager retrieval). Diner `submit/` needs a table session and accepts
  any SUBMITTED order — `REVIEWABLE_ORDER_STATUSES` ({pending, preparing,
  served, paid} in `submit_review.py`, deliberately NOT the Reports
  `SALE_STATUSES`: kitchen tapping Served is never a review precondition) —
  rejecting drafts (`initiated`) and cancelled/refunded orders with one
  restrained 400. `overall_rating` mandatory (1–5) + five optional
  dimension ratings; `is_public` seeded from `PUBLIC_RATING_THRESHOLD` (≥4 →
  public-eligible) but stays owner-overridable. List/analytics gate on the
  `reviews` module (`get_module_restaurant_ids` / `can_user_access_module`);
  resolution stays manage-level (`can_manage_restaurant`). Diner submissions may
  also carry quick-chip `tags` — a JSON list of stable `ReviewTag` keys
  (`great_flavour`/`quick_service`/`friendly_staff`/`good_value`/`spotless`,
  the enum is the single source of truth for the allowed set); the write
  serializer DROPS unknown keys (never rejects, logs via stdlib logging) so the
  stars/comment always save, persisting only the valid subset for Reports to
  aggregate later (the one 400 is a non-list payload). Migrations
  `reviews_app/0001_initial`, `0002_review_resolution_note`,
  `0003_review_tags`. `Review` is the system of record — the legacy
  inline-review fields on `Order`/`OrderItem` were dropped (orders_app
  migration `0034`)
- Payments — non-custodial migration: 🚧 In progress (custodial teardown
  essentially complete). Dinify must operate as a software vendor, NOT a
  custodial payment institution (Uganda NPS Act 2020 — see
  `REGULATORY_AUDIT.md`). The custodial money-flow has been REMOVED across
  stages 1–8a (PRs #149–#162): dead payment code; the fund-disbursement/payout
  path (`tx_disbursement.py`, Yo `momo_disburse`/`bank_disburse`); the
  Dinify-initiated refund payout (`initiate_refund.py`, Flutterwave
  `send_mobile_money`); the OVA subscription fee-netting; tips (`tx_tip.py`);
  the custodial balance ledger (`update_wallet_balance.py` + the EOD
  daily-reporting machinery in `reports_app`); the payment-aggregator
  integration layer (the DPO and Flutterwave controllers DELETED, Yo's payment
  paths stripped — its SMS dispatch remains — and the `initiate_*` order-payment
  flows stubbed aggregator-free, PR #158); and the custodial models themselves —
  `DinifyAccount` (with its 24 balance/cumulative fields) and `BankAccountRecord`
  are now DELETED, and `DinifyTransaction`'s `account` FK and `tip_amount` column
  are dropped (finance_app migrations `0024`–`0028`); dead
  surcharge/transaction-type figures were trimmed from reports (PR #162). Only
  `DinifyTransaction` survives, as a record-only structure. Funds must settle
  restaurant-direct — do NOT reintroduce held balances, disbursement,
  Dinify-initiated refunds, the OVA wallet, tip wallets, or the
  `DinifyAccount` / `BankAccountRecord` custodial models. The orphaned,
  AllowAny order-payment WRITE PATH is now RETIRED — the
  `initiate-order-payment/` route, `OrderPaymentsEndpoint`
  (`finance_app/endpoints/order_payments.py`), and `OrderPaymentTransaction`
  (`finance_app/controllers/tx_order_payment.py`) were DELETED; it wrote a
  `DinifyTransaction` for any order UUID with no auth, no ownership check, and a
  client-supplied `split` amount (closing BUG-P2-3e / BUG-P2-7). It will be
  REBUILT at PSP integration (authenticated, ownership-gated, server-bounded
  amounts, non-custodial Pattern A). The record-only `DinifyTransaction` model,
  its serializers, the subscription writer (`tx_subscription.py` via the
  surviving `TransactionsEndpoint`), and BOTH Transactions reports are UNCHANGED
  — do NOT delete or migrate the model
- Payments posture (current): the codebase holds NO PSP credentials and NO
  payment-execution code of any kind — the empty `payment_integrations_app`
  aggregator shell was deleted and its dead test-only env-var stubs removed from
  `test_settings.py`. **The subscription flow is now REFUSED, not record-only (D07)** —
  `tx_subscription.initiate()` answers **501 `subscription_collection_unavailable`**
  and writes nothing at all. It used to write a Pending `DinifyTransaction` and stop,
  under a message reading "The subscription payment has been initiated", which is a
  payment claim about a collector that does not exist; the insertion branch is REMOVED
  rather than parked behind a flag, because a flag is a working fake collection one
  boolean away from returning. See the D07 bullet below. The PSP adapter will be designed FRESH per the non-custodial
  Pattern A when the counsel and PSP integration gates clear.
  REPO-CLEAN IS NOT HOST-CLEAN: the 2026-07-29 host reconnaissance
  (`REGULATORY_AUDIT.md` APPENDIX, finding H2) found the DPO / Flutterwave / Yo
  payment credentials sitting in a `root:root` mode-644 backup `.env` on the
  production box — written 2026-03-17 and still there at the recon, having
  survived the teardown that removed them from the tree. Mode 644 made it readable
  by any local account including `www-data`, the account the app itself runs as.
  Deleting a file does not revoke a credential — provider-side revocation is
  recorded there as OUTSTANDING
- A PAYMENT INTERFACE NEVER CLAIMS WORK NO PROVIDER PERFORMED (D07): ✅ the
  platform collects no money — the order-payment writer was deleted in the
  custodial teardown, no PSP integration replaced it, and
  `notifications_app.controllers.sms` holds the ONLY outbound HTTP call site in
  the tree — yet four surfaces still spoke as though it did. Each is corrected at
  the place that makes the claim, and NOTHING here builds, enables or simulates a
  collector.
  **THE SUBSCRIPTION COLLECTOR IS REFUSED AT THE SERVICE, NOT AT THE VIEW**
  (`finance_app/controllers/tx_subscription.py`). `initiate` answered "The
  subscription payment has been initiated. Please confirm payment when promted",
  booked a Pending `DinifyTransaction` and contacted nobody — the provider call
  was a COMMENT. It now answers **501 `subscription_collection_unavailable`** with
  ZERO effects: no transaction, no payment intent, no OTP or challenge, no SMS or
  email, no stored MSISDN, no amount or status write. **The refusal lives in the
  SERVICE because the endpoint is one caller** — `initiate` is reachable in-process
  by any caller (including one passing `user=None`, which used to persist
  `created_by=None`), so disabling a view or hiding a button would leave the fake
  collection fully available. **501, NOT 503** — RFC 9110 §15.6.2 is "does not
  support the functionality required" while §15.6.4 is a temporary overload, and a
  `Retry-After`, a retry timer or a poll would all suggest that waiting implements
  a collector. The ORDER of the gates is preserved: the endpoint's
  `can_manage_restaurant` 404 runs FIRST, and an unknown or malformed restaurant id
  still answers that same opaque 404 rather than 500ing or disclosing, so only a
  request that cleared both gates reaches the 501. **The insertion branch is
  REMOVED rather than parked behind an enable switch**, and
  `finance_app/subscription_capability.py` is the one leaf module both planes read
  so the portal cannot advertise a collector the server refuses — it is a
  DISCLOSURE, never a switch, and `initiate` deliberately does not branch on it
  (a test pins that while the constant reads False, `initiate` refuses).
  **THE RESTAURANT'S OWN BILLING READ STATES WHAT THE SERVER KNOWS** — an additive
  projection on `restaurant-setup/subscription-details/` carrying the collection
  capability and either the CANONICAL recorded terms or an explicit "none
  recorded". It reuses `commercial_app`'s own open-terms rule rather than
  reconstructing current terms from `flat_fee`, `preferred_subscription_method`,
  `subscription_validity` (which DEFAULTS TRUE and has no writer), a transaction
  count or a UI catalogue; it adds no second source of truth, no terms writer, no
  invoice, no entitlement inference and no read-time `get_or_create`; and it does
  NOT import or bypass the admin endpoint to expose its privileged fields. The
  EXISTING settings-module read gate, the foreign/unknown denials and the
  delegated exclusion are all preserved — read permission is not the collector's
  management permission, so it resolves through the existing resolvers rather than
  a new hardcoded owner/manager check.
  **A NEW TABLE NO LONGER ADVERTISES A PAY STEP** — `Table.qr_mode`'s default moved
  `order_pay` → `order_only` (migration `restaurants_app/0058`, an `AlterField`
  with NO `RunPython`). `order_pay` is RETAINED as a choice, stays inside
  `ORDERING_QR_MODES` and remains fully orderable, so **no existing row is rewritten
  and no venue's diners lose the ability to order**; operationally the two modes are
  IDENTICAL and `menu_only` is the one that blocks, so this changes what a new table
  CLAIMS and nothing about what it DOES. Pinned by
  `restaurants_app/tests_qr_mode_default.py`; reverting the default fails exactly
  its two REGRESSION cases while all four CONTROLs hold.
  **AND THE DASHBOARD DISCLOSES THAT PAYMENT IS NOT MEASURED** — `dashboard-v2`
  publishes `payment_tracking_enabled` beside its cards, governing the
  `revenue` card, the `payment_methods` card and `orders.breakdown.paid` — an
  empty list, a zero, and a whole headline figure, each indistinguishable from a
  real trading fact. It is the SAME v1
  constant, asserted equal by identity so the two responses cannot drift. See
  `BREAKING_CHANGES.md` §17 for the 501's cutover note — **FRONTEND FIRST**, the
  reverse of the usual rule, because backend first makes an older client send a
  REAL OTP (the billing dialog dispatched one BEFORE its POST) for a payment that
  then fails — and `D07_PAYMENT_CLAIM_CLOSURE.md` for the delivery record: the
  E1-E7 to changed-consumer mapping, the Stage A evidence corrections, the
  judgement calls (the CREATE pickers dropping `order_pay`, the receipt-copy
  extension, the Sales Method blank cell) and what was deliberately NOT run.
  **TWO OF ITS BROWSER CLAIMS WERE NOT SUPPORTED BY THE SCRIPT THAT MADE THEM,
  and §14 records the correction** (a frontend-only delta; no backend source
  moved, and NO ENDPOINT WAS RELAXED to rescue a harness). The collector probe in
  `e2e/billing-journey/billing.mjs` sent `restaurant` where
  `finance_app/endpoints/transactions.py` reads `restaurant_id`, so
  `can_manage_restaurant` refused it **404 at the authorization gate** and
  `SubscriptionPaymentTransaction.initiate()` never ran — while the only
  assertion made, "the live server REFUSES it", is satisfied by that 404,
  because `ErrorInterceptor` flattens an ordinary failure to a STRING with no
  status. The harness now sends the shape the retired dialog really sent and
  reads the SAME response off the wire before any interceptor, asserting an
  exact 501, an exact `subscription_collection_unavailable` and the exact
  request-specific sentence, with a 404 control for a missing and a foreign id
  and an ASSERTED (no longer manually observed) before/after subscription-row
  count. Separately, the PR-5 dashboard pairing was passing on MOCK data —
  `DashboardService.USE_MOCK_DATA` is still `true`, so no request reached this
  server at all — and now selects the real branch through a test-only runtime
  flip, observes the authorized `dashboard-v2` request and asserts
  `payment_tracking_enabled: false` in the response body. Measured: 56/56, with
  51/56 under the wrong-key mutation and 54/56 under the mock-branch one
- **AND `summarize_revenue` STATES NO TREND IT DID NOT COMPUTE (D07/G1).** It
  returned `'month_growth': 'up'` as a LITERAL, with the comparison that would
  have justified it commented out on the same line
  (`# if this_month > last_month else 'down'`), so it reported growth for every
  restaurant in every month — measured, for a restaurant with no orders at all:
  `{'total': 0, 'this_month': 0, 'month_growth': 'up'}`. A direction asserted
  from no comparison is the same class of claim as an empty card reading "no
  settled payments in this period".
  **THE CONSUMER SEARCH DECIDED THE REMEDY, and it is why the key is REMOVED
  rather than given an honest value.** `summarize_revenue` has NO production
  caller anywhere in this repository — it is on no urlconf, in no serializer and
  in no response, reached only from `orders_app.tests_launch_boundary` (whose own
  comment calls it "the dead-but-live all-time revenue helper") and
  `reports_app.tests_timezone_clocks`, and neither reads that key. The
  frontend's `month_growth` type declarations belong to `DinifyDashboardData`,
  the retired admin-plane shape, and name different fields entirely. So there is
  no wire contract to keep compatible. A trend that is genuinely wanted later
  gets built against a baseline that can be ABSENT — precisely what a bare
  direction string cannot express, and the lesson `PaymentMethodData.change_pct`
  was deleted for on the other side.
  **AND THE TWO FIGURES THAT REMAIN ARE DISCLOSED**: `total` and `this_month`
  both aggregate `payment_status='paid'`, so the helper now publishes the SAME
  `PAYMENT_TRACKING_ENABLED` constant v1 and v2 do, asserted BY IDENTITY rather
  than as a second literal. **Nothing is rebased, repriced or recomputed** — the
  paid filter stays, and a control pins that `SALE_STATUSES` does not appear in
  the function. Pinned by `reports_app/tests_summarize_revenue_claims.py` (6);
  **4 failed on the unmodified tree** and the 2 that passed are the controls.
  The frontend half of G1 (the four dashboard consumers) and G2 (the billing
  read states) are in the Frontend `CLAUDE.md`; `D07_PAYMENT_CLAIM_CLOSURE.md`
  §13 is the completion record, and §10 item 6 now records that the deep-link
  evidence originally cited was a QR-rotation METHOD test — which answers a
  different question — replaced by a real browser navigation in
  `e2e/billing-journey/`
- Reports module — rebuilt on the clean contract: ✅ Complete. All four
  restaurant reports (`api/v1/reports/restaurant/<name>/` →
  `RestaurantReportsEndpoint`, `{status, message, data}` envelope) are rebuilt on
  shared foundations in `reports_app/controllers/common/`: `sale_filters.py` (the
  canonical "what is a sale / what is revenue" — `SALE_STATUSES` {served, paid},
  revenue = `Sum('actual_cost')`, discount = `Sum('savings')`; Order-based) and
  `bucketing.py` (single grouped-query, EAT-aligned period bucketing — no
  per-period loop). Rebuild contract: RAW enum values (the frontend owns display
  formatting — NO backend `.title()`-casing), a stable 0-filled shape, and
  grouped queries (no per-bucket / per-row N+1).
  **BUCKET SERIES ARE DENSE (BUCKETS-ZEROFILL-00)** — `sales-trends` and BOTH
  `dashboard-v2` series (`revenue`, `orders`) return one row per bucket in the
  requested window, empty ones zeroed, never omitted. `bucketing.py` states ONE
  density policy for both of its paths: a grouped query can only return groups
  that have rows, so the CALLER enumerates the axis and fills — hour-of-day onto
  `range(24)` (a fixed domain), a period onto `period_boundaries(date_from,
  date_to, period)` (window-dependent, so the module derives it). `bucket_sales`
  itself takes NO window and its signature must stay that way — a caller may hand
  it an unbounded queryset. `BOUNDARY_PERIODS` spans BOTH bucketing vocabularies
  (`hour` AND `quarter`) precisely so `PERIOD_TRUNC` and `BUCKET_TRUNC` do NOT
  have to merge; adding `hour` to the axis did not add it to `PERIOD_TRUNC`, and
  the documented asymmetry between those two maps still stands. When filling
  `dashboard-v2`'s `revenue` series, WATCH THE BASIS: it is driven by PAID orders
  with refunds joined in, so the fill inserts only where that basis has no bucket,
  never overwrites a real row, and never widens the axis to `paid ∪ refunded` — a
  refund-only bucket surfaces its real refund, not a `0.00` contradicting
  `totals.refunds`. Rebuilt: Sales
  listing/trends (PR #165, `sales.py`), Transactions summary/listing
  (PR #166, `transactions.py`), Diners summary/listing (PR #167, `diners.py`),
  and Menu summary (PR #168, `menu.py`; the menu-summary date-range cap
  was later relaxed in PR #169) — each with its own `tests_*_report.py`.
  Sales additionally exposes `sales-hourly/` — an EAT-aware hour-of-day (0–23)
  distribution (PR #184, `bucket_sales_by_hour` via `ExtractHour(tzinfo=LOCAL_TZ)`,
  keyed by integer hour so deliberately NOT a `PERIOD_TRUNC` entry, zero-filled
  to a continuous 24-hour axis on the same revenue basis). Sales-trends now
  emits ISO/sortable `period` keys (`2024-03` for month, `2024-Q1` for quarter;
  day/year already ISO; a `week` key is the Monday boundary of the bucket in EAT
  as `YYYY-MM-DD`, NOT an ISO `2024-W10` string, which `parseISO()` cannot read)
  so the frontend can `parseISO()` every bucket (PR #185);
  `REPORTS_CONTRACT_AUDIT.md` at the repo root is the cross-repo Reports contract
  reconciliation / test plan for the eventual live-data flip.
  The LIVE slug set is exactly `dashboard`, `dashboard-v2`, `sales-listing`,
  `sales-trends`, `sales-hourly`, `menu-summary`, `transactions-summary`,
  `transactions-listing`, `diners-summary`, `diners-listing` — there is no
  `menu-listing`. Two slugs were RETIRED as dead surface in PR #247:
  `dashboard1` (C1, with `get_restaurant_dashboard_1` + `summarize_orders`) and
  `sales-summary` (C2, with `generate_restaurant_sales_summary`; its permission
  canaries and shared-invariant tests re-point onto `sales-listing`). Do not
  reintroduce either — `dashboard` / `dashboard-v2` / `summarize_revenue` are
  the live ones.
  **The `dashboard` slug now CONSUMES `sale_filters` (PR-H §1)** — it was the last
  Order-based restaurant report that did not. `num_sales` was `orders.count()`
  (every row: `initiated` drafts, cancellations, refunds) and was also the
  denominator for all three percentages. Now: `orders_placed`
  (`order_status != initiated`) is the SHARED denominator for the cancellation,
  refund and payment rates — one denominator is what makes the first two
  comparable and stops either exceeding 100% — `num_sales` filters
  `SALE_STATUSES`, and `sales_amount` is `revenue_sum()` (`Sum('actual_cost')`)
  over sales rather than `Sum('total_cost')` over `payment_status='paid'`, which
  nothing writes and which therefore made it permanently `null`. Two ADDITIVE keys:
  `orders_placed`, and `payment_tracking_enabled` — a module constant, `False`
  until the PSP write path lands, flagging that `paid_orders` is a placeholder and
  not a measurement (flip it in the same PR that lands PSP). **D07 published the SAME
  constant on `dashboard-v2` too**, where it governs three more figures — the
  `revenue` card, the `payment_methods` card and `orders.breakdown.paid` — which were
  silently zero rather than disclosed as unmeasured. **`revenue` is the one most
  easily missed**: `_build_revenue` aggregates `gross` and `discounts` over that same
  unwritten `payment_status='paid'` column, `net = gross - discounts - refunds` is
  derived from them, and `refunds` is NOT paid-gated — so a window holding one
  reports a NEGATIVE net against a zero gross, and `pricing_conventions` counts over
  the same empty set, which is why the D02/C mixed-pricing notice has never been able
  to render on a live payload. One constant, four consumers: the v1 and
  v2 responses are asserted EQUAL by identity, so they cannot drift. dashboard-v2's
  `_build_orders` base shared the draft-counting defect and was fixed identically,
  so `orders.total` now equals v1's `orders_placed` and `breakdown` sums to
  `total`. The diner / item / peak-hour figures DELIBERATELY still read the
  unfiltered queryset — rebasing those belongs with the Diners surface. No field
  was renamed or removed; see `BREAKING_CHANGES.md` §11 and note the headline
  "Sales" figure DROPS on deploy.
  `dashboard-v2` takes `bucket` ∈ {hour, day, week, month, year} (`BUCKET_TRUNC`)
  and `bucket` is REQUIRED — absent/empty/whitespace-only is a 400 alongside
  unknown values, because the endpoint caps neither the date range nor the bucket
  count and so has no defensible default granularity. That missing bucket-count
  cap is a KNOWN OPEN SEAM, scoped as `DASH-BUCKET-CAP-00` in
  `REPORTS_CONTRACT_AUDIT.md` §9/G4: it pre-dates the zero-fill, but the fill made
  `bucket=hour` over a long window a payload FLOOR rather than a worst case. Its legacy `period`
  selector and the server-computed previous-period comparison
  (`previous_totals` / `previous_total` / `previous_series` on the `revenue` and
  `orders` cards) were REMOVED once the frontend stopped using them — the latter
  was a second full aggregation over a second date window per card, so dropping
  it removed 7 queries per dashboard load. The response carries ONE window; the
  frontend issues its own second call for the comparison basis. Do not
  reintroduce either — see `BREAKING_CHANGES.md` §10.
  **The `dashboard-v2` REVENUE CARD DISCLOSES WHICH PRICING CONVENTION IT SUMMED**
  (D02/C). `gross` is `Sum('total_cost')` and `discounts` is `Sum('savings')`, and
  D02 changed what both COLUMNS MEAN: a CORRECTED order's `total_cost` includes paid
  modifier costs and its `savings` can never be negative, while a LEGACY one's
  excludes them and could be. A window spanning the deployment therefore reported
  three figures — `gross`, `discounts`, and the `net` derived from both — mixing two
  measurements, with nothing on the response saying so; the boundary was described
  ONLY in `BREAKING_CHANGES.md` §13, which an operator reading a dashboard never
  sees. The card now carries an additive
  `pricing_conventions: {mixed, legacy_orders, corrected_orders, notice}`.
  **NO ORDER IS REPRICED, REWRITTEN OR EXCLUDED** — both conventions stay in the
  totals, because dropping the legacy half would silently understate a real trading
  period, which is worse than a mixed figure that says it is mixed. The `notice` is
  present ONLY when the window actually straddles the boundary (one that appeared on
  every response would be ignored on the one that mattered), and it is a sentence
  about COMPARABILITY, never about correctness: the payable is unaffected. **IT COSTS
  NOTHING** — the counts are conditional aggregates folded into an aggregate that had
  to run anyway, so `_build_revenue` went from 5 queries to **4** (the two money sums
  were two separate round trips over the same queryset). That repin is DOWNWARD and
  carries its breakdown. Known adjacent surface, deliberately NOT given a notice here:
  `sales-trends` / `sales-hourly` emit a `discount` column over the same `savings`
  contract, but their headline `revenue` is `actual_cost` — the payable, which means
  the same thing under both conventions — so the mixing there is confined to a
  non-headline column and widening the change would have touched four more response
  shapes.
  Sales/Diners/Menu are Order-based and share `sale_filters`; Diners operates
  strictly on non-NULL-customer sale orders so anonymous-QR guests are never
  collapsed into a phantom repeat diner (guests are surfaced as a separate count,
  and the legacy `diners-trends` report was dropped). Transactions is
  `DinifyTransaction`-based, so its axis is `transaction_status` /
  `transaction_type` (statuses success/failed/pending/initiated; live types
  order_payment/subscription) — NOT the Order-based `sale_filters` /
  `SALE_STATUSES`; its listing serializer
  `SerializerGetRestaurantTransactionListing` emits raw enums + a single `amount`
  + `payment_mode`, and the controller uses `select_related('order')`. The
  Dashboard endpoint and the admin `reports_app/controllers/dinify/` path
  (incl. `SerializerGetDinifyTransactionListing`) are separate and untouched
- Dead-code / dead-endpoint audit: ✅ Program CLOSED — the closure record is
  `DEAD_CODE_CLOSURE.md` at the repo root (sequenced across PRs #244, #245, #247).
  It is the authority on what was retired and, as importantly, what was
  consciously KEPT. Most of its retirements are already reflected in the sections
  below (the `misc_public.py` directory endpoint, the manager-OTP profile path,
  the `orders` setup vocab, the `dashboard1` / `sales-summary` report slugs, and
  the authz closures in URL Structure). Read it before deleting a surface that
  merely looks unreferenced, and refresh it when the deliberate keeps change.
  Sibling records: `dinify_backend/tenancy/ASSURANCE.md` +
  `dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md` (tenant boundary — BOTH
  live under `dinify_backend/tenancy/`, not the repo root),
  `REGULATORY_AUDIT.md` (non-custodial posture — and see its APPENDIX below),
  `D07_PAYMENT_CLAIM_CLOSURE.md` (payment interfaces that claimed work no provider
  performed — the delivery record for the 501, the terms projection, the QR-mode
  default, the reporting disclosures and the copy corrections),
  `REPORTS_CONTRACT_AUDIT.md` (cross-repo Reports
  contract), `PHASE_0_5_CLOSURE.md` (the four-PR Phase 0.5 pre-launch remediation
  ladder — what it closed, what it deliberately left open, and the seams Phase 1
  inherits) and `BACKGROUND_TASKS.md` (management-command runbook)
- The audit record now covers the HOST, not just the tree: `REGULATORY_AUDIT.md`
  carries an APPENDIX of post-audit host findings (H1–H5, 2026-07-29). Everything
  above that appendix examined the REPOSITORY and was verified in the repository;
  the production host was never in scope. Its theme is that removing code does not
  remove what the host is still holding or still running. Three items are recorded
  as OUTSTANDING and are host-side, so nothing in this repo closes them: H2 (PSP
  credentials in a backup `.env` — provider revocation), H4 (MongoDB Atlas IP
  access list open to `0.0.0.0/0`), H5 (a real transactional-email provider on a
  Dinify-controlled domain; `EMAIL_HOST` was pointed at a domain that had lapsed
  to NXDOMAIN, a credential trap, mitigated on the box by `EMAIL_HOST=localhost`
  — an env change with no repository counterpart). H3 (OTP broadcast to
  contractor mailboxes, 2025-02 → 2026-02) has its code path closed but its
  impact assessment open. Do not restate any of these as remediated
- NOTHING SCHEDULED IS CURRENTLY RUNNING (discovered 2026-07-29, recorded in
  `BACKGROUND_TASKS.md`). This repo has no scheduler configuration — no Celery,
  no Beat, no crontab, no Procfile — and every management command is invoked
  externally, which was already documented. What the recon added is what that
  external scheduler actually WAS: a single root cron entry firing every minute
  since 2024-12-12, calling `process_transactions` — a command deleted in the
  custodial teardown — and failing on every run for ~5 months (594 log files,
  739 MB). Of the eleven wrapper scripts in `/home/scripts/`, nine call commands
  that no longer exist and the two that survive (`determine-customers`,
  `send_messages`) are NOT scheduled. So unsent notifications are not being
  dispatched and orders are not being matched to customers except by hand.
  Documented, not fixed — do not assume any background work runs on a schedule
- Login 500 regression: ✅ Resolved — not reproducible after the auth-stack work;
  login → refresh → logout verified working on UAT (closed June 2026)
- Django 5.2 LTS upgrade: ✅ Complete — Django 4.2.30 → 5.2.15 (PRs #123–#125).
  Forward-compat deps bumped: `asgiref` 3.11.1, `django-cors-headers` 4.9.0 (DRF
  3.17.1 / SimpleJWT 5.5.1 / psycopg 3.1.18 already supported 5.2). The
  deprecation surface was clean — no removed-in-5.x APIs in use, no new
  migrations generated, `USE_TZ` already explicit. App timezone code now uses
  stdlib `zoneinfo`; `pytz`/`numpy`/`pandas`/`tzdata` were removed from
  requirements entirely (no app imports remain — do not reintroduce `import pytz`).
  PATCH PINS MOVE WITHOUT A CONTEXT UPDATE — Dependabot has since carried
  `requirements.txt` to Django 5.2.16, `cryptography` 50.0.0 and
  `typing_extensions` 4.13.2. Read `requirements.txt` for the current pins rather
  than quoting the version in this bullet, which records the UPGRADE, not the pin

## Deployment Rules — CRITICAL
- Merging a PR to main deploys automatically, but NOT off the merge event: the
  deploy runs on `workflow_run` when **Backend CI** completes successfully on
  `main`, so a red CI never reaches the box. One concurrency group
  (`deploy-uat-backend`, `cancel-in-progress: false`) serialises runs
- TRANSPORT IS SSM, NOT SSH (PR #281). The job assumes a repo-scoped IAM role via
  **GitHub OIDC** and dispatches the deploy script through **SSM Send-Command** to
  `i-0eeb7c0c3a36d3667` — no inbound port 22, no stored credentials. The UAT box
  moved to an instance whose security group does not admit GitHub-hosted runners,
  which is why the old `appleboy/ssh-action` path died at connect timeout. Three
  consequences that bite when editing the script: it needs its `#!/bin/bash`
  shebang (AWS-RunShellScript otherwise runs dash, which cannot parse the `0027`
  gate's arithmetic); SSM executes as ROOT, so every touch of the ubuntu-owned
  tree or venv runs `sudo -u ubuntu` with the venv interpreter by absolute path
  (`$VENV_PY` — sudo resets PATH via secure_path, and root-owned files written
  into that tree break the NEXT deploy); and SSM keeps only the FIRST 24,000
  characters of output, so the `DEPLOY-SKIP:` / `DEPLOYED-HEAD:` markers must stay
   ABOVE the pip/migrate output and pip runs `-q`. The legacy `UAT_SSH_*` secrets
  were DELETED 2026-08-18 with the old-host teardown; no deploy, CI or audit
  workflow references `secrets.` — OIDC needs no stored credential. The one stored
  secret is `CLAUDE_CODE_OAUTH_TOKEN`, read only by the two Claude workflows
  (`claude.yml`, `@claude` mentions; `claude-code-review.yml`, an automatic review
  when a PR is opened or marked ready). It is an Anthropic credential and grants
  nothing in AWS. **Neither Claude workflow may hold `id-token: write`**: the action
  installs its own npm dependencies inside the job, `claude.yml`'s comment and
  issue events run from `refs/heads/main`, and the deploy role accepts this
  repository's OIDC tokens. The action is handed the job's `github.token` instead,
  so Claude posts as `github-actions[bot]` and a commit it pushes does not start
  CI on its own. The review plugin is vendored under `.claude/review-marketplace/`
  (README there), identical to Dinify-Frontend's and Dinify-Admin's copies
- THE DEPLOY IS PINNED TO ONE EXACT COMMIT (PR #283, post-incident 2026-08-08,
  when a deploy run reported success while the box stayed 39 hours behind on an
  older commit — a green deploy that did not deploy). The workflow injects the
  triggering CI run's head SHA into the script by placeholder substitution behind
  a quoted heredoc; the box fetches, `git checkout --detach`es exactly that SHA,
  ASSERTS `HEAD` equals the target, and prints `DEPLOYED-HEAD:` which the workflow
  reads back into the step summary — so the retained record is the box's own
  assertion, never the workflow's intent. The automatic path is FORWARD-ONLY: a
  queued older run prints `DEPLOY-SKIP:` and exits 0 rather than downgrading the
  box under a newer schema. Never reintroduce `git pull origin main` here — "pull
  whatever main is at execution time" is the defect this closed
- ROLLBACK / MANUAL REDEPLOY is `workflow_dispatch` with a full 40-hex `sha`
  input, and it is the ONE path allowed to move backwards. Its pre-flight refuses
  any SHA without a successful **Backend CI run on `main`** (branch-green is not
  enough), and it warns loudly that migrations already applied by newer code are
  NOT reversed — confirm schema compatibility (expand/contract) before using it.
  Event-derived values reach shell only through `env:` indirection and are
  re-validated (40-hex; literal `true`/`false`) immediately before substitution,
  because this script ultimately executes as root on the box
- The deploy DOES reinstall dependencies, so a `requirements.txt` change takes
  effect on the next deploy. The install runs after the checkout, before
  `migrate`; with `set -e` a failed install aborts before the Apache restart,
  leaving the live API up on the old workers. **It resolves `requirements.txt` on the box
  (unpinned transitives, whatever pip is there) and does NOT consume the D08 B2.5
  certified candidate or `release/python-lock.json`** — CI now validates an environment
  built from the lock, so the served venv and the validated one can differ in a
  transitive until promotion is connected. A direct-input change must also change the lock
  in the same PR, or CI refuses it (`stale_lock`). **Nor does it consume the D08 B2.6
  preflight** (`preflight.yml`). That workflow runs beside it, off the same "Backend CI"
  completion. A red preflight does not stop the deploy and cannot protect it. Connecting
  the two is B3, which is BUILT AND STAGED, NOT ACTIVE — see the next bullet
- **D08 B3 — IMMUTABLE INSTALLATION AND LOADED-PROCESS IDENTITY — IMPLEMENTED, REHEARSED,
  NOT ACTIVE** (`release/README.md` → "The installation"; `release/CUTOVER.md`). The
  installer (`release/installation.py`) admits a candidate by repeating the B2.6 receiving
  check on the host, builds it AS an unprivileged preparer into
  `<releaseRoot>/<commit>-<16 hex>/` (source, a venv created AT that path from the host's
  base Python and the retained wheels offline, wheelhouse, static, launcher files, receipt),
  reconciles and seals it root-owned, and never repairs, overwrites or prunes one. The
  transition (`release/transition.py`) takes a shared host lock, gates each plane's
  configuration AS the runtime identity, decides migrations against reviewed entries in
  `release/migration-decisions.json` (anything unreviewed, contracting or unknown STOPS),
  switches BOTH planes by rewriting one Apache include per plane that pins python-home /
  home / python-path to one release, verifies by asking the running workers, and restores on
  failure; `host resume` settles an interrupted operation by observing what serves, EXCEPT
  one whose migrations began and never recorded completion, which stays open until an
  operator states what the database holds (`--schema-established`). The trusted verifier
  must be traversable (`0711`) by the unprivileged identities. Two unauthenticated routes, `GET /uat/api/v1/release/` and `GET /api/admin/v1/release/`,
  report the identity a worker's launcher established ONCE at start (never re-read, so a
  changed file cannot relabel a running process); **under the legacy deploy they answer
  `unavailable` / `not_started_by_release_launcher`**, which is true and is what merging
  this actually changes on the live host. **Four states stay separate**: implementation
  tested (yes), runtime profile verified (NO — `release/profiles/uat-backend.json` is
  `unverified` with every host fact `null`, and the real path refuses it by name), cutover
  authorized (NO — every host, AWS, Apache and secret step in `CUTOVER.md` is OWNER),
  candidate serving (NO). The workflow, SSM script, ordering guard, marker reader and a
  read-only discovery collector are STAGED under `release/staged/`; nothing under
  `.github/workflows/` names them, and `release/tests_staged.py` fails the build if the
  staged workflow and `deploy-uat.yml` ever both exist. The cutover moves it in and deletes
  `deploy-uat.yml` in ONE reviewed change. The shared lock contract, and the Admin half
  (staged, not applied), is `release/HOST_LOCK_CONTRACT.md`. **Measured on the disposable
  rehearsal host**: a graceful reload reclaims the previous mod_wsgi daemons about 3 seconds
  after the signal regardless of `shutdown-timeout`, so a request longer than that is cut —
  still gentler than the legacy `systemctl restart apache2`
- The deploy is HEALTH-GATED (post-incident 2026-07-17, when a `.env` of mode
  600 `ubuntu:ubuntu` let CLI checks pass while mod_wsgi — running as
  `www-data` — died at settings import with `PermissionError`): BEFORE the
  Apache restart it verifies `.env` is readable by `www-data` with no
  group-write and no 'other' bits (`mode & 0027` must be 0 — `ubuntu:www-data`
  640 passes; 660/644/642 fail), probes that `www-data` can
  resolve required settings from `.env` (values never printed), re-runs
  `check --deploy` AS `www-data` from the project dir with the venv Python,
  and runs `apachectl configtest` — any failure aborts with the old workers
  still serving. AFTER the restart there are TWO probes, and neither substitutes
  for the other: an HTTP probe of the login route through local Apache must
  return exactly 405 (proves TLS/Apache/WSGI ROUTING — a boot-dead app turns the
  deploy red instead of green-and-down), and a probe of `/uat/api/v1/health/`
  must return 200 with `database == "connected"` (proves the RESTARTED daemon
  processes can reach PostgreSQL; `migrate` and `check --deploy` earlier prove
  only PRE-restart connectivity). The health body is parsed as JSON, never
  substring-matched, because that endpoint answers **200 with
  `status: degraded`** when the database is down — status code alone would pass
  a DB-less app. Both probes retry five times with `--resolve` pinning the public
  vhost to 127.0.0.1. Deployed runtime secrets (e.g.
  `DINER_CAP_KEY`) must be stored in the project `.env` — the contract the
  gates validate directly (`RepositoryEnv('.env')` as `www-data`) — readable
  by `www-data` through group-read, with no group-write and no permissions
  for other users (currently `ubuntu:www-data` mode 640); never in a shell
  profile
- HOST RUNTIME NOTE — the AWS CLI on the box is **v2, installed from AWS's
  official installer at `/usr/local/bin/aws`**, NOT from apt. Ubuntu 24.04 has no
  installation candidate for the `awscli` package even though `command-not-found`
  still advertises one, so `apt install awscli` fails and the suggestion is a dead
  end. Consequence: it is a hand-managed binary outside the package manager —
  `apt upgrade` never touches it, and it needs periodic MANUAL update
  (re-run AWS's installer). Do not "fix" its absence from apt
- NEVER suggest manual `git pull`, `migrate`, or Apache restart — the
  pipeline handles everything
- Each feature must be on its own branch → PR → merge
- Never stack work on unmerged branches

## Branch Selection — CRITICAL
- When the task text (the prompt provided for the task) names a specific
  branch, ALWAYS develop on and push to THAT branch — the branch named in the
  task text is authoritative and takes precedence over the session-designated
  branch
- Do NOT default to the session-designated branch (the auto-generated
  `claude/...` branch injected into the session/environment setup) when the
  task text names a different branch
- The session-designated branch is only the fallback for when the task text
  does not name a branch at all

## Branch Base — CRITICAL
- Before creating a feature branch, ALWAYS run `git fetch origin main` and
  branch from `origin/main` (e.g. `git checkout -b <new> origin/main`). NEVER
  branch from the local `main` ref — in a freshly-cloned Claude Code on the web
  container the local `main` can be STALE (behind the real `origin/main`),
  silently basing your work on outdated code
- The SessionStart hook (`.claude/hooks/session-start.sh`, registered in
  `.claude/settings.json`) auto-runs `git fetch origin main` at the start of
  every web session as a backstop — but still branch EXPLICITLY from the fetched
  `origin/main`, not local `main`
- If you discover mid-task that your base was stale, `git rebase origin/main`
  and re-verify (`./scripts/verify.sh`) before pushing

## URL Structure
- `api/v1/restaurant-setup/` → RestaurantSetupEndpoint (catch-all) +
  dedicated endpoints for: preset-tags, restaurant-tags (+ dedicated
  `restaurant-tags/reorder/` and `restaurant-tags/<tag_id>/usage-count/`, PR #245),
  upsell-config, upsell-config/items, reservations, waitlist,
  table-actions/<action>/, role-permissions. The dead `section-tables` write verb
  was removed (PR #213, BUG-P3-10) — a retired/unknown verb falls through to the
  generic unmapped handling; dining-area creation with tables goes through
  `create_dining_area(create_tables=True)`. Authz closures (PR #244): the ungated
  POST `restaurants` self-service create branch was REMOVED (DC-BE-014 — it made
  the caller owner after only a JWT decode); restaurant creation then flowed ONLY
  through the admin-gated `admin-register-restaurant` branch — which PR-A also
  REMOVED, along with the whole `create_restaurant.py` module, because its only
  gate was a `dinify_admin` role string. **There is still NO CUSTOMER-PLANE path that
  creates a restaurant, and there must never be one again.** The gap was closed on the
  ADMIN plane instead, natively, by Phase-1 Step 2D: `POST admin/v1/restaurants/`
  (elevated, CSRF-protected, audited — see "Admin Restaurant Creation").
  `PUT restaurant-setup/subscription-details/` was retired in the same PR (405; the
  settings-gated READ stays live). The last-active-owner guard moved onto the LIVE
  employee-deactivation path (PUT `employees` `{active:'false'}`), resolving the
  target through the server-scoped queryset and returning 409 — never 403, which
  force-logs-out the client (DC-BE-011). DELETE on `upsell-config/items/reorder/`
  now returns 405 instead of silently deleting an item (DC-BE-004)
- `api/v1/reports/restaurant/<report_name>/` → RestaurantReportsEndpoint
- `api/v1/release/` (customer) and `admin/v1/release/` (admin) → `ReleaseIdentityView`
  (`misc_app/endpoints/release_identity.py`, D08 B3) — the loaded-process release identity,
  unauthenticated, bounded, non-secret and `no-store`, like health. It is NOT health: it
  says WHICH installed release answered. A process no release launcher started (the legacy
  install) answers `unavailable`
- `api/v1/health/` → `misc_app/urls.py` → `HealthCheckView`
  (`misc_app/endpoints/health.py`) — `AllowAny` with `authentication_classes = []`,
  answering `{status, database, timestamp}` after a `SELECT 1`. It is DEPLOY-
  LOAD-BEARING (the UAT DB-connectivity gate parses it), so keep the three keys
  and their values stable. It deliberately answers **HTTP 200 even when the
  database is unreachable** (`status: degraded`, `database: unreachable`) — a
  consumer must read the body, never the status code. Distinct from the admin
  plane's own `admin/v1/` health route
- `api/v1/health/ready/` → `misc_app/endpoints/readiness.py` (D15 R1, customer plane
  only) — additive READINESS: `200 {status: ready, database: connected}` or
  `503 {status: not_ready, database: unreachable}`, exactly two keys, `no-store,
  private`, GET/HEAD only, unauthenticated, request data ignored. The `SELECT 1` runs
  in a HELPER PROCESS (`misc_app/readiness_probe.py`) on a new connection built from
  Django's own connection parameters, and is SIGKILLed and reaped inside one 2 s
  monotonic budget (admission through disposal) — the only bound that also covers
  DNS, a frozen server and a stalled result read. Per process: one probe at a time, a
  result served ≤ 2 s after it completed, non-owners wait ≤ 0.25 s; one fixed log line
  per change of state, never per caller. Non-PostgreSQL engines answer 503. It changes
  NOTHING above: `api/v1/health/` still answers 200 `degraded` and is still unbounded,
  admin health is still liveness, and no deploy step, staged release check or monitor
  reads this route yet. A request carrying `X-Delegation-Session` gets the same answer
  inside the same bound: the route is in `DelegatedAccessMiddleware.EXEMPT_ROUTES`, so
  the gate never evaluates it (no lookup, no 401/403, no audit row, no authority).
  Before that exemption, such a request against a frozen database got no answer at all
- `api/v1/orders/` → v1 orders (urls.py) — `submit` and `retire-quote` (both
  PUT) are live. **`retire-quote` is a SEPARATE ACTION, never a flag on
  `submit`** (D06): placing an order and establishing that it can no longer be
  placed are opposite decisions with opposite consequences, and which one a
  request made should be readable from the path rather than from a body — the
  same reasoning the admin plane applies to reissue vs cancel. Both share ONE
  authority resolution (the diner table session bound to the order's table, or a
  staff caller with the `tables` module) and both carry the verified capability
  to the protected boundary; what they do with the draft is where they differ,
  and that difference lives in the controller rather than in two copies of the
  authority code. The
  orphaned, unscoped `prepare`/`cancel`/`update-item` write actions were
  RETIRED (finding H3, PR #181) and any retired/unknown action now 404s
  (hardened dispatch, no fallthrough to 500). Superseded by `api/v1/kitchen/`,
  which gates every write
- `api/v2/orders/` → v2 orders (v2_urls.py) — separate file, don't confuse;
  only `initiate` (POST) is live. `add-items` (POST/DELETE) was retired
  (PR #181) and the AllowAny, unscoped `details/` GET was retired (finding C1,
  PR #182) — both 404 via the hardened dispatch. Its controller
  `handle_add_order_items` was DELETED (PR-H §3) — it had no production caller and
  was a dormant deadlock cycle: it acquired `OrderItem → Order`, the INVERSE of the
  create path's order, took no advisory lock and did no lifecycle re-check. Do not
  reintroduce it; a rebuilt add-items path must adopt the create path's lock order
  and route through the admission primitive. An `initiate`d order is a true
  DRAFT (`order_status='initiated'`) that does NOT occupy its table or reach the
  kitchen board; the table is claimed only at `submit` (PR #210) — that
  transition locks the table row and re-checks draft status + occupancy on the
  fresh row, so two diners submitting for the same table serialize (first claims,
  second gets a clean 400)
- `api/v1/orders/journey/` → the anonymous diner journey
  (`restaurants_app/endpoints/order_journey.py`): `table-scan/`, `show-menu/`,
  `order-details/`, `payment-details/`. **`order-details/` takes EITHER
  `?order=<uuid>` OR `?intent=<client_order_id>`** (D04/C) — ONE decision reached by
  two identifiers, deliberately not a second route: a dedicated one would have to
  join the diner capability allowlist in both repositories to say what this route
  already says. Both selectors are scoped to the session's restaurant AND table, so
  holding an intent key is not authority; naming both is a 400, naming neither keeps
  the existing 400, and every unresolvable value collapses to ONE non-disclosing 404
- `api/v1/kitchen/` → Kitchen endpoints (urls_kitchen.py) — separate file. The
  three ORDER COMMAND routes (`orders/<pk>/fulfilment-status/`,
  `orders/<pk>/priority/`, `orders/<pk>/cancel/`) each take an explicit command
  plus a REQUIRED `if_revision`, and are thin adapters over
  `kitchen_transition.execute`; the two feeds additively publish `order_status`,
  `fulfilment_revision` and an envelope `kitchen_protocol`. A FOURTH order route,
  `GET orders/<pk>/state/`, is the per-order OBSERVATION that settles an uncertain
  command (`kitchen_transition.read_state`) — it answers for an order that has
  left both feeds, which is exactly what a cancellation produces. None of the four
  is on the delegated `ALLOWED_ROUTES` allowlist and none may be added — see
  `delegation_scopes.py`, which records why for each
- `api/v1/support/` → support_app endpoints (`support_app/urls.py`):
  `issues/`, `issues/<uuid:issue_id>/` — separate app (`admin/issues/` retired)
- `api/v1/reviews/` → reviews_app endpoints (`reviews_app/urls.py`):
  `submit/`, `summary/`, `analytics/`, `<int:review_id>/resolution/`,
  `` (root) — separate app
- `api/v1/users/user-profile/` → `UserProfileEndpoint` (`users_app/endpoints/`) — ONE
  resource, two verbs: **GET** is the Step-2F.3 canonical authenticated profile
  bootstrap and **PUT** the self-service update. Both run on the default customer
  authentication stack; neither is on the delegated `ALLOWED_ROUTES` allowlist, which
  excludes this route by name because it acts on `request.user` and so has no
  restaurant dimension to scope. Do NOT add a `/session-bootstrap/`, `/me/` or
  `/owner-claim/profile/` alias — there is one user-profile resource
- `api/v1/users/owner-claim/challenge/` + `api/v1/users/owner-claim/redeem/` → the
  two halves of owner claim, Steps 2F.1 and 2F.2
  (`platform_admin_app/endpoints/owner_claim.py`, mounted from `users_app/urls.py`).
  Both are `authentication_classes = []` + `AllowAny`, and the raw claim token travels
  ONLY in the `X-Owner-Claim-Token` header. TWO EXPLICIT ROUTES, never
  `owner-claim/<str:action>/`: requesting a verification code and exercising a claim
  credential are different decisions with different consequences, and which one a
  request made should be readable from the path rather than from a body
- `api/v1/delegation/` → the CUSTOMER-plane half of delegated admin access
  (`platform_admin_app/urls_delegation.py`, served by the customer urlconf even
  though it lives in the admin app): `exchange/` (redeem a one-time code — NOT on
  the `ALLOWED_ROUTES` allowlist, since it is reached with a code, not a session),
  `session/`, `end/`. Minting / listing / revoking grants is the ADMIN plane's
  `admin/v1/delegations/`. A test asserts every `ALLOWED_ROUTES` entry resolves to
  a real route, so renaming one cannot silently strand it
- `admin/v1/` → platform_admin_app control plane (`platform_admin_app/urls.py`,
  mounted by `dinify_backend/urls_admin.py`; Apache strips the `/api` prefix).
  Explicit deny-by-default routes only — health, `auth/*`, `delegations/*`,
  `restaurants/` (the COLLECTION: **GET** is the Phase-1 Step-1 directory read,
  session-gated and unaudited; **POST** is the Step-2D creation write,
  elevation-gated, CSRF-protected and audited — ONE resource, two methods, two
  authority bars resolved per method by `get_permissions`; there is deliberately no
  `/restaurants/create/`) + `restaurants/<uuid:id>/` (the Step-1 detail READ — see
  "Admin Restaurant Reads" and "Admin Restaurant Creation" below), and
  `restaurants/<uuid:id>/transition/` (the ONLY writer of `Restaurant.status`,
  elevation-gated), the two Step-2E owner-invitation lifecycle writes
  `restaurants/<uuid:id>/owner-invitation/reissue/` +
  `.../owner-invitation/cancel/` (POST, elevation-gated, reason-required, audited —
  `reissue` NOT `resend`, because nothing is ever delivered; see "Admin
  Owner-Invitation Lifecycle"), the two Step-3D.2a commercial writes
  `restaurants/<uuid:id>/commercial/payment-timing/` +
  `restaurants/<uuid:id>/commercial/payment-collection-mode/` (POST, elevation-gated,
  reason-required — see "Admin Commercial Writes"), and the three Step-3D.2b
  subscription-terms writes `restaurants/<uuid:id>/commercial/subscription-terms/`
  + `.../subscription-terms/replace/` + `.../subscription-terms/end/` (POST, same
  contract — see "Admin Subscription-Terms Writes"). THREE ROUTES, NOT ONE WITH AN
  `action` SEGMENT: recording, superseding and closing are different decisions with
  different tokens and different histories. The two reads are session-gated
  but NOT elevation-gated, and are NOT audited — see that section for why both are
  deliberate

## Endpoint Pattern — CRITICAL
New resource types get their own dedicated endpoint file in
`restaurants_app/endpoints/`, NOT added to the RestaurantSetupEndpoint
catch-all. Examples already following this pattern:
- `reservations.py`, `waitlist.py`, `table_actions.py`, `preset_tags.py`,
  `restaurant_tags.py`, `upsell_config.py`, `manager_actions.py`,
  `order_journey.py`, `role_permissions.py`
(`misc_public.py` — the anonymous public-restaurant-directory endpoint — was
DELETED as dead surface (C4, PR #247); the QR-capability flow superseded it. Do
not reintroduce an anonymous directory read.)
Always register new endpoint files in `restaurants_app/urls.py` ABOVE
the catch-all `<str:config_detail>/` route.

## The Secretary Pattern — CRITICAL
- The `Secretary` class uses `EDIT_INFORMATION`
  (dinify_backend/configss/edit_information.py) to control which fields
  are editable via PUT requests
- Any new field that needs to be editable via PUT MUST be added to the
  appropriate list in EDIT_INFORMATION
- If omitted, Secretary silently ignores the field and returns
  "no changes detected"
- Current sections: `restaurants`, `restaurant_employee`, `menu_section`,
  `menu_item`, `table` (20+ fields), plus `EI_DINING_AREA` for dining areas,
  `EI_SECTION_GROUP` for menu section groups, and `EI_RESTAURANT_TAG` for
  restaurant tag catalog entries
- Secretary now honours absent-vs-None semantics: omitting a field leaves
  it untouched, sending `null` clears it. The legacy `clear_<field>`
  sentinels are deprecated — do not introduce new ones
- File fields (`STRINGIFY_LOG_FIELDS`, e.g. restaurant `cover_photo`/`logo`) are
  invisible to `determine_changes`, so `Secretary.update()`'s fallback is the
  only thing that can register a file edit — it keys on `key in self.data` (NOT
  "value is non-null"), so an explicit `null`-clear of a file field counts as a
  change and persists (HTTP 200), rather than collapsing to "no changes detected"
- TWO commercial keys are registered in `EDIT_INFORMATION['restaurants']` but are
  PLATFORM-owned: `flat_fee` (the subscription PRICE billed by
  `finance_app.tx_subscription`) and `preferred_subscription_method` (the BILLING
  METHOD; it used to be the ONLY gate in `tx_subscription.initiate` — `== 'per_order'`
  → refuse — but **D07 removed that branch**: every authorized request now gets the
  same 501 whatever the column says, because branching on it would have offered
  changing a plan as a way to enable machinery that does not exist. The strip below
  is UNCHANGED and still load-bearing for the Phase-1 admin writer). The restaurant-setup write path STRIPS BOTH keys from EVERY
  `restaurants` PUT payload AFTER `check_permission` and BEFORE the Secretary
  dispatch (`platform_only_fields` in `restaurant_setup.py`), so no principal on
  this plane can zero a subscription price or change the billing terms. `flat_fee`
  was closed first (PR #211; made unconditional by PR-A — it used to be stripped
  only for non-admins, leaving a `dinify_admin` role-holder able to write it);
  `preferred_subscription_method` was left behind by that PR, so an owner could set
  `monthly` and then `POST api/v1/finances/transactions/` (owner passes
  `can_manage_restaurant`) to have Dinify record a subscription charge against
  terms it never chose — closed by adding it to the same strip, with
  `tx_subscription` itself unchanged. This is a post-gate payload strip, NOT an
  EDIT_INFORMATION removal — both keys deliberately STAY in EDIT_INFORMATION so the
  Phase-1 admin-plane writer can still go through Secretary, and neither is
  `read_only` on `SerializerPutRestaurant` for the same reason. Delegated sessions
  never reach this at all: `restaurant-setup` is GET-only on `ALLOWED_ROUTES`, so
  the middleware refuses a delegated write before dispatch
- `status` is DIFFERENT and stricter: PR-5 REMOVED it from
  `EDIT_INFORMATION['restaurants']` entirely (and made it `read_only` on
  `SerializerPutRestaurant`), so NO principal writes it through Secretary — see
  the "Restaurant Lifecycle" section. `Table.status` was removed from
  `EDIT_INFORMATION['table']` in the same PR for its own reason: the dedicated
  `table-actions/update-status/` verb validates against `TABLE_STATUS_CHOICES` and
  keeps `is_active` in step with `out_of_service`, which the generic path did not.
  No `EDIT_INFORMATION` section exposes a `status` key any more
- `is_test` on a restaurant follows the SAME two-wall pattern as `status` and for the
  same reason: it is absent from `EDIT_INFORMATION['restaurants']` and absent from
  `SerializerPutRestaurant`'s field list, so no principal on the customer plane can
  set it. Do NOT add it to a generic tenant edit surface — see "Canonical Data Shapes"
- Check this file before adding any editable field — it may already be there

## Tenant Isolation / Role-Permission ENFORCEMENT — CRITICAL
- Portal gates ENFORCE per-module access (PR C): every restaurant-scoped read
  and write routes through the resolver primitives in
  `users_app/controllers/permissions_check.py`. The seeded owner/manager
  defaults hold every grid module, so this is behaviour-neutral for them — it
  only constrains non-owner/manager roles (kitchen, staff) and custom
  `RestaurantRolePermission` overrides.
  - `can_user_access_module(user, restaurant_id, module)` → single-record /
    single-restaurant gate (A's resolver — do NOT modify). Owner → all; otherwise
    the role grid. `support` is ungated (always True). Fail closed — a
    None/unknown restaurant denies.
  - `get_module_restaurant_ids(user, module)` → list-scoping counterpart:
    ALWAYS a set — `set()` deny-all, otherwise the restaurant ids whose grid grants
    `module` (active restaurant + active, non-deleted employment). `support` →
    every employed restaurant. The `None` = "unrestricted, callers must NOT scope"
    sentinel was REMOVED by PR-A: it was what turned a `dinify_admin` role string
    into `model.objects.all()` and an untouched list filter. Do not reintroduce it.
  - `get_employed_restaurant_ids(user)` → role-agnostic employed set (no
    restaurant-status filter); powers the ungated `support` module's scoping.
  - `get_any_restaurant_roles(user)` (login/profile portal-role payload) also
    filters `RestaurantEmployee` on `active=True` (PR #187), matching the module
    resolvers — a deactivated employment (`active=False`) no longer resolves
    portal roles on the next sign-in or profile fetch.
  - The legacy `get_readable_restaurant_ids` / `can_read_restaurant` /
    `READ_ROLES` (owner/manager-only) were DELETED — do not reintroduce them.
    `can_manage_restaurant` / `MANAGE_ROLES` REMAIN, but only for the
    manage-level elevation gates ABOVE module access (review resolution,
    kitchen goodwill-cancel) — these are intentionally NOT module-granular.
  - NO PLATFORM SHORT-CIRCUIT EXISTS (PR-A). `is_dinify_admin` /
    `is_dinify_superuser` / `dinify_roles` and the `DINIFY_ADMIN` /
    `DINIFY_ACCOUNT_MANAGER` constants are DELETED. `User.roles` is never read for
    platform authority anywhere on this plane; `account_type` is the sole
    plane discriminator and `roles` carries restaurant roles only. A platform
    administrator reaches tenant data ONLY through a delegation grant. A standing
    CI gate (`scripts/check_ambient_authority.py` +
    `dinify_backend/tenancy/ambient_authority.py`, TENANT-AUTH-00, empty allowlist)
    fails the build if any of it regrows by name.
  - The per-(restaurant, role) grids have an owner-only MANAGEMENT surface —
    `RolePermissionsEndpoint` (GET/PUT `restaurant-setup/role-permissions/`,
    `team`-gated) over the `RestaurantRolePermission` override model, seeded by
    `ensure_role_permissions` (backfill migration `0053`). It reuses the
    constants/defaults (`configs/role_defaults.DEFAULT_ROLE_MODULES`) but
    deliberately NOT the resolver function. The owner row is read-only/all-True;
    writes validate explicitly (NOT Secretary) and partial-merge over the
    effective grid — do NOT route this through EDIT_INFORMATION.
- The `RestaurantSetupEndpoint` catch-all maps each record/`config_detail` →
  module via `_RECORD_MODULE` (restaurants→settings, employees→team
  [owner-only, per Decision 1], menu*→menu, tables/diningareas→tables). The
  `orders` vocab was RETIRED from the catch-all (C3, PR #247 — it duplicated the
  reports surface), so `GET restaurant-setup/orders/` now falls through to the
  generic unmapped-resource 403. ONE map drives the write gate
  (`check_permission`), the GET
  list scoping (`scope_list_filter` + `LIST_RESTAURANT_PATH`), and the
  single-record detail read. The client `?restaurant=` can only narrow within
  the allowed set, never widen.
- Single-record / subscription-details (→settings) / tables-grouping (→tables)
  / detail branches gate on the resolved restaurant and return 404 (not 403) on
  cross-tenant access so existence is not confirmed. A nonexistent resource PK
  404s BEFORE the gate (never passed into the gate as None).
- Writes resolve the target restaurant SERVER-SIDE from the resource FK (by PK)
  via `_RESTAURANT_RESOLVERS`, then module-gate that resolved id — the spoof
  `{id: <victim record>, restaurant: <attacker own>}` cannot smuggle access.
- Portal module access requires a lifecycle state that GRANTS PORTAL ACCESS — the
  three resolvers filter `restaurant__status__in=portal_access_states()`
  (`onboarding` + `live`), never a literal. PR-5 WIDENED this: the filter used to
  be `['active']`, so an owner at a not-yet-approved restaurant was denied the
  portal entirely; `onboarding` now grants FULL staff access so the owner can build
  a menu and provision tables before going live. `suspended` / `offboarded` deny.
  See the "Restaurant Lifecycle" section below.
- Any NEW read/write branch must map its resource to a module and route through
  these primitives — an unmapped resource fails closed (403/404)

## Restaurant Lifecycle — CRITICAL

- `Restaurant.status` is a CONSTRAINED four-state commercial lifecycle (PR-5,
  migration `restaurants_app/0056_restaurant_lifecycle_states`):
  `onboarding` → `live` → `suspended` → `offboarded`. Constants +
  `RESTAURANT_STATUS_CHOICES` + `RESTAURANT_LIFECYCLE_STATES` live in
  `string_definitions.py`. The legacy free-text vocabulary
  (`pending`/`active`/`inactive`/`blocked`/`rejected`) and its five
  `RestaurantStatus_*` constants were REMOVED — do not reintroduce them
- `offboarded` is deliberately NOT `archived`: `users_app.BaseModel` already owns
  `deleted` (the technical soft-delete) plus a dormant `archived` boolean. This
  axis is COMMERCIAL state; `deleted` stays the soft-delete mechanism and is
  orthogonal (a soft-deleted `live` restaurant is still invisible to diners)
- ONE WRITER: `restaurants_app/controllers/lifecycle.py`
  (`transition_restaurant`). No other code path may assign `status`. Enforcement
  is two-layer — the field is ABSENT from `EDIT_INFORMATION['restaurants']`
  (Secretary builds its payload solely from those keys) and `read_only` on
  `SerializerPutRestaurant`. The legacy admin `changeApprovalStatus` PUT is
  RETIRED: a Dinify admin can no longer write `status` through restaurant-setup
  either. The platform-owned commercial strip in `restaurant_setup.py` REMAINS
  (`flat_fee` + `preferred_subscription_method`)
- The service takes `lock_admission_exclusive(restaurant.pk)` as the FIRST statement
  in its transaction — before the `Restaurant` row lock — so a transition excludes
  every in-flight order admission (Closure PR 2). **LOCK ORDER, and the advisory lock
  is a single new TOP level in all three:**
  `advisory(restaurant) SHARED → Table → Counter → Order → OrderItem` (order create),
  `advisory(restaurant) SHARED → Table → Order` (order submit),
  `advisory(restaurant) EXCLUSIVE → Restaurant → AdminAuditLog` (transition), and
  the SAME pair again for `manage.py mark_restaurant_test` (the `Restaurant.is_test`
  writer) — it repeats the transition's order exactly, so it joins an ordering
  already proven acyclic rather than adding a level. It needs the advisory lock
  because a `Restaurant` row lock does NOT exclude an admission — `admit` reads
  `status`/`is_test` with a plain `values_list().get()`, never a `select_for_update`,
  and under MVCC that read does not block on a row held FOR UPDATE. The advisory lock is the only thing the two transactions share — so ANY
  future writer of a restaurant field an admission reads must take it too.
  Take it FIRST or not at all — a transaction that takes a row lock and then reaches
  for the advisory lock reintroduces the cycle this ordering prevents.
  A further participant joined in PR-H §2: `Restaurant → INSERT Table`
  (table-number allocation, `restaurants_app/controllers/tables.py`). It takes NO
  advisory lock and row-locks no existing `Table`, so it cannot cycle against
  either order path (which row-lock `Table` but never `Restaurant`) or against the
  transition (which holds the advisory lock and then waits for `Restaurant`; this
  one never reaches for the advisory lock, so it can only block, never cycle).
  A SECOND participant of exactly that shape joined in Step 2E.1:
  `Restaurant → RestaurantEmployee` (membership mutation,
  `restaurants_app/controllers/employee_membership_lock.py`). Same reasoning, same
  conclusion — it takes the `Restaurant` row and never afterwards reaches for the
  advisory lock, so it can block the transition but cannot cycle against it
- The service enforces the matrix against the row read under `select_for_update`
  (so concurrent transitions serialize), requires a reason (≥10 chars, mirroring
  `platform_admin_app.delegation.MIN_REASON_LENGTH`), and writes an
  `AdminAuditLog` row IN THE SAME TRANSACTION — a failed audit unwinds the
  transition. A REFUSED transition is audited too
  (`admin.restaurant.transition_denied`) and then raises
  `LifecycleTransitionError`. Allowed: onboarding→live (readiness-gated),
  onboarding→offboarded, live↔suspended, live/suspended→offboarded.
  `offboarded → live` is NEVER allowed — restoration is re-onboarding. There is
  no self-transition
- ONE POLICY: `restaurants_app/controllers/lifecycle_policy.py` is the single
  source for what each state PERMITS — `CAPABILITY_MATRIX` is the spec table as
  data and every predicate reads from it. Readers call a named predicate
  (`grants_portal_access`, `allows_order_creation`, `allows_kitchen`,
  `allows_support`, `diner_menu_visibility`, `effective_delegated_scope`) or the
  derived set (`portal_access_states()`), NEVER `status == '<literal>'`. It is
  import-light on purpose (imported by `permissions_check`) — no models, no
  querysets. An unknown/legacy value fails CLOSED (denies everything, menu gone)
- THE LAUNCH BOUNDARY (PR-D): `onboarding` and `live` are NO LONGER identical. A
  seventh capability key `CAP_LIVE_TRADING` (onboarding **False**, live True,
  suspended/offboarded False) is the one cell that differs, and two predicates read
  it: `allows_diner_ordering` (= `CAP_ORDER_CREATE and CAP_LIVE_TRADING`) refuses the
  PUBLIC at a restaurant that has not gone live, and `orders_are_commercial` is the
  sole source of `Order.is_test`. `CAP_ORDER_CREATE` deliberately STAYS True at
  onboarding so the owner can place the end-to-end rehearsal order the Phase-1
  checklist requires. The diner gate lives in `ConOrder.initiate_order` beside the
  other three `created_by is None` gates and returns `MESSAGES['NOT_OPEN_YET']`; the
  diner MENU still renders during onboarding (only ordering is refused), so the owner
  can preview the real QR → menu experience
- Per-state behaviour: staff portal + kitchen + order-create are allowed for
  onboarding/live and blocked for suspended/offboarded; the diner menu is served
  for onboarding/live, answers a graceful **503** for `suspended` (the ONE state
  that does not collapse to the generic 404 — a diner at a printed QR is told the
  place is temporarily unavailable) and a flat 404 for `offboarded`; support is
  reachable in every state; a delegated admin session is CAPPED to `view` scope at
  an `offboarded` restaurant (`DelegationContext.scope` applies the ceiling;
  `granted_scope` exposes the as-minted value).
  KNOWN GAP (found in Closure PR 2 recon, not fixed there): a DELEGATED
  administrator still reads the kitchen board of a **suspended** restaurant —
  `permissions_check.py` short-circuits on delegation BEFORE the lifecycle-filtered
  resolver, and `suspended`'s delegated ceiling is `None`. Staff are blocked there
  but a delegation is not
- IN-FLIGHT ORDERS ARE **FROZEN, NOT DRAINED** (Closure PR 2, pinned in
  `lifecycle_policy.py` beside the matrix and tested). An order already accepted
  (`pending`, `preparing`) when the restaurant is suspended keeps its row untouched,
  but staff cannot SEE, ADVANCE or CANCEL it — `CAP_STAFF_PORTAL` and `CAP_KITCHEN`
  are both False, and kitchen authorisation resolves through the portal-access state
  set. Fulfilment resumes if the restaurant returns to `live`. `offboarded` behaves
  the same and additionally removes the restaurant from the diner surface. This is
  the deliberate reading of the matrix, not an enforcement accident — letting staff
  work the kitchen at `suspended` would mean suspension no longer suspends. TWO
  CONSEQUENCES, both real and both left for Phase 1: **the table is never freed**
  (cancel is what releases it, and cancel is 403, so a restaurant suspended
  mid-service comes back with those tables still occupied — a narrow cancel-only
  carve-out was considered and declined); and **diner-facing reads are inconsistent
  at `suspended`** — the menu answers 503 but `order-details`/`payment-details` still
  answer 200 and a review can still be submitted, none of those paths consulting
  lifecycle state. Both predate the admission work
- TWO PHASE-1 SEAMS, both named single-call functions with real call sites —
  `check_go_live_readiness` and `has_outstanding_receivables`. Do NOT inline either at
  a call site. **`check_go_live_readiness` FAILS CLOSED (PR-D)**: it returns
  not-ready with the single blocker `readiness_not_configured`
  (`lifecycle.BLOCKER_READINESS_NOT_CONFIGURED`), so `onboarding → live` is refused
  on EVERY path until Phase 1 wires the real checklist. It used to return ready
  unconditionally without reading its argument — a safety gate that always said yes.
  The one production restaurant is already `live`, so nothing existing is stranded —
  but as of Step 2D an ADMIN-CREATED restaurant IS: it starts `onboarding` and cannot
  reach `live` until Phase 1 wires the real checklist. That is the accepted, visible
  cost of a safety gate that fails closed, and it is the right way round — a tenant
  that cannot go live is recoverable, a tenant that went live unready is not. There
  is deliberately NO override — do not add one.
  `has_outstanding_receivables` still returns False (`SubscriptionInvoice` does not
  exist yet, and `DinifyTransaction` is never the receivable)
- Admin transition endpoint: `POST admin/v1/restaurants/<uuid:id>/transition/`
  (`platform_admin_app/endpoints/restaurants.py`), `AdminAPIView` +
  `IsRecentlyElevated` — body `{to_state, reason}`. It is deliberately ABSENT from
  the delegated `ALLOWED_ROUTES` allowlist, so a delegated session can never
  transition anything
- The go-live owner notification (`restaurant-activated`) moved from the deleted
  `Secretary.make_notification` hook into the lifecycle service, fired
  post-commit and best-effort. There is no `restaurant-rejected` counterpart —
  `rejected` is not a state in the new vocabulary

## Admin Restaurant Reads — Phase 1, Step 1

The admin portal's restaurant DIRECTORY and DETAIL reads. Two routes, both on the
admin plane, both `AdminAPIView` + `IsAuthenticated`:

```
GET admin/v1/restaurants/                  -> AdminRestaurantCollectionView
GET admin/v1/restaurants/<uuid:id>/        -> AdminRestaurantDetailView
```

The collection view also answers `POST` (Step 2D creation), under a STRICTLY HIGHER
bar resolved per method by `get_permissions` — elevated, CSRF-protected and audited.
Everything in this section describes the READS and is unchanged by that; see "Admin
Restaurant Creation" for the write. (The class was renamed from
`AdminRestaurantListView` because it is no longer only a list; the route name
`admin-restaurant-list` is unchanged.)

- THE VIEWS ARE THIN. Every projection lives in `platform_admin_app/restaurant_reads.py`,
  mirroring how the transition endpoint delegates to `restaurants_app.controllers.lifecycle`.
  Add a field there, not in the view. One projection is delegated onwards the same
  way: the DETAIL-only `onboarding` object comes from
  `platform_admin_app/onboarding_reads.py` (Step 2C) — see the Admin Onboarding
  Domain section
- NOT ELEVATION-GATED, deliberately. `IsRecentlyElevated` gates ACTIONS that change a
  tenant's world; requiring a second factor to LOOK at the directory would train the
  operator to elevate reflexively, which is exactly what devalues the step-up on the
  transition route. A valid admin session is the bar for reading
- NOT AUDITED, deliberately. `AdminAuditLog` is an append-only record of privileged
  DECISIONS, not an access log. Auditing ordinary GETs would bury the transition and
  delegation entries under directory page-views and manufacture "activity" that is
  really just someone scrolling. `AdminAPIView`'s "exactly one entry per unsafe
  request" convention is unchanged — these are safe requests
- Soft-deleted restaurants are EXCLUDED from the list and 404 on detail, matching the
  transition endpoint's existing treatment

### Three fields that are reported as unconfigured rather than inferred
Step 1 exposed truth that existed THEN. Each of these was easy to fake and is not.
**Two of the three have since been superseded by the canonical `commercial` object
(Step 3D.1, below) and survive only as compatibility** — read that section before
touching them:

- **Readiness** delegates to `lifecycle.check_go_live_readiness` — the ONE seam. It
  fails closed today with `readiness_not_configured`, so that is what the portal is
  told. Do NOT build a second checklist here; Step 3 fills the seam. Readiness is
  reported as `not_applicable` outside `onboarding`: "is it ready to go live" has no
  answer for a live or offboarded tenant, and zero blockers there would read as ready.
  UNCHANGED by 3D.1 — readable commercial state does not make a restaurant ready
- **Payment mode** (`payment_mode` + `payment_mode_configured`) — TRANSITIONAL
  COMPATIBILITY, frozen permanently unconfigured. It is NOT wired to
  `payment_collection_mode`: the old ambiguous "payment mode" label is a different
  contract from either Step-3B axis, and repointing it would silently change what the
  deployed frontend believes it is rendering. `require_order_prepayments` is still
  never consulted
- **Subscription** reports the LEGACY `Restaurant` columns under names that say so
  (`source: 'legacy_restaurant_fields'`, `legacy_validity_flag`, `legacy_expiry_at`).
  `SubscriptionInvoice` / `SubscriptionPayment` do not exist. **`has_outstanding_receivables`
  is NOT consulted** — it returns a cheerful `False` that means only "invoices do not
  exist", and wiring it belongs in the same change that makes an invoice capable of
  becoming overdue. **`has_commercial_subscription` STAYS `false` even for a
  restaurant with open `RestaurantSubscriptionTerms`** — the deployed Admin frontend
  renders that boolean as **Active**, which recorded terms do not prove

### The canonical `commercial` object — Phase 1, Step 3D.1
`platform_admin_app/commercial_reads.py` is where the Step-3B/3C commercial facts are
read. It exposes TWO functions and `restaurant_reads` uses both:
`annotate_commercial(queryset)` (the query architecture) and
`commercial_summary(restaurant)` (the projection). The object is present on the
directory ROW and the DETAIL response and is byte-identical in both — one helper over
one set of annotations applied once in `directory_queryset()`, so the two are
structurally incapable of disagreeing. Read-only PR: no migration, no writer, no HTTP
write verb, no new route; the reads stay session-gated, NOT elevation-gated and NOT
audited.

```
"commercial": {
    "payment_timing":          {"configured": bool, "value": "pay_first"|"pay_after"|null,
                                "set_at": ISO8601|null},
    "payment_collection_mode": {"configured": bool, "value": "offline"|"psp_online"|null,
                                "set_at": ISO8601|null},
    "subscription_terms":      {"configured": bool,
                                "current": null | {"id": UUID, "recurring_amount": "0.00",
                                                   "currency": "UGX",
                                                   "billing_interval": {"unit": ..., "count": ...},
                                                   "effective_from": ISO8601,
                                                   "recorded_at": ISO8601}}
}
```

- **THREE INDEPENDENT FACTS, never flattened.** Each axis carries its own
  `configured`, and there is deliberately NO `commercial_configured` boolean — every
  partial combination is a real state, and the readiness engine needs to know which
  of the three is missing to name a blocker
- **`configured` on an axis derives from the VALUE, not from the row existing.** A
  configuration row created by a write to one axis leaves the other NULL, and that
  restaurant is genuinely unconfigured on the second axis
- **`subscription_terms.configured` means AN OPEN TERMS ROW EXISTS — nothing more.**
  Not active, paid, valid, current standing, invoiced, collected or in good standing.
  `current` means "the terms record currently in force", not a billing verdict. Dinify
  has never collected a subscription payment through this system
- **"Open" is `ended_at IS NULL` and only that** — never `effective_from <= now`,
  never latest `recorded_at`/`effective_from`, never `subscription_validity`,
  `subscription_expiry_date` or `DinifyTransaction`. Safe to state that flatly because
  the partial unique index makes "at most one open row" a database fact and the Step-3C
  writers refuse future-dated terms, so there is no scheduled state to resolve
- **`recurring_amount` is a decimal STRING.** DRF's JSON encoder renders a bare
  `Decimal` as a float, which would emit `0.0` for `0.00` and lose the stored scale.
  `0.00` is a real, deliberate price (free pilot, waived period, test tenant) — NOT
  "free", "trial" or an absence, which is the absence of a row
- **THE RESPONSE IS THE CONCURRENCY TOKEN.** `subscription_terms.current.id` is the
  `expected_terms_id` Step 3C's replace/end writers require, and the two axis `value`s
  are their `expected_current`. The domain facts ARE the tokens — do NOT add a version
  counter, and do NOT bind anything to `service_configuration.updated_at`
- **NO INFERENCE, NO TRANSLATION, NO PSP.** Values are the exact persisted machine
  vocabulary. `offline` is a fully configured, first-class mode — never "unconfigured",
  never `cash`; `psp_online` carries no provider, merchant id or readiness verdict.
  Nothing is derived from `require_order_prepayments`, `Table.prepayment_required`,
  `flat_fee`, `preferred_subscription_method`, `subscription_validity`,
  `subscription_expiry_date`, order history, lifecycle or `is_test`. Where a legacy
  field disagrees, **the `commercial` object wins** and the legacy field is not consulted
- **NO ACTOR IDENTITY**, enforced by the SQL rather than the serializer: the projection
  annotates the four service-configuration columns it needs, so `*_set_by_id` and
  `recorded_by_id` never enter the SELECT list. WHO decided is `AdminAuditLog`'s question
- **`is_test` gets no special read semantics** and no restaurant is hard-coded
- **THE PROJECTION IS PURE** — no write, `get_or_create`, save, lock, transaction,
  audit row or legacy synchronisation. Reading an unconfigured restaurant creates no
  commercial rows; reading ended history opens nothing
- **The legacy `payment_mode` / `payment_mode_configured` / `subscription` keys are a
  TRANSITIONAL COMPATIBILITY CONTRACT.** They are kept byte-for-byte, may disagree with
  `commercial`, and no new consumer should read them. They are contracted deliberately
  once the Admin frontend has migrated — **the portal does NOT yet render `commercial`**

### Query architecture (the directory's bounded-query contract survives)
`annotate_commercial` adds TWO LEFT JOINs to `directory_queryset()`, so the page is
still retrieved in ONE query and the endpoint costs the SAME as before: measured at
**4 queries for the list (flat from `page_size=2` to `page_size=20`) and 8 for the
detail, identical on `origin/main` and on this branch**. Notes for anyone editing it:

- The service configuration is annotated field-by-field rather than `select_related`-ed
  — one uniform annotation contract (no path that could become a per-row lazy load) and
  a narrower SELECT, which is what makes "no actor identity" a property of the query
- Subscription terms are 0..N, so a NAIVE join is a real defect: ten historical rows
  would return one restaurant ten times, inflate the support aggregate, corrupt
  `.count()` and shuffle pagination. `FilteredRelation` puts `ended_at IS NULL` in the
  JOIN's ON clause so historical rows never enter the result, and
  `one_open_subscription_terms_per_restaurant` guarantees at most one survivor — **that
  index is what makes the join safe**
- Do NOT swap in a `Prefetch` (two queries for the page) or seven correlated subqueries

### ONE definition of attention
`needs_attention(restaurant)` (the per-row predicate) and `attention_filter()` (its
SQL mirror for `?attention=`) are both in `restaurant_reads.py`, and a ratchet test
asserts they agree across EVERY lifecycle state. A filter that disagrees with the
badge is how an operator's inbox silently drops work — do not add a second opinion.

### Query parameters and pagination
`?search=` (name/location, trimmed), `?status=` (a lifecycle state), `?attention=`
(strict boolean — `true/1/yes` / `false/0/no`), `?page=`, `?page_size=` (default 25,
max 100). Invalid values are a **400 with a field-keyed `errors` map reporting ALL
problems at once**, never a silent default: a filter that quietly ignores what it was
asked returns a plausible page answering a different question. Ordering is
`('name', 'id')` — the `id` tiebreak is what makes pagination deterministic when two
restaurants share a name.

**The query string is deny-by-default too.** `KNOWN_PARAMS` is the complete accepted
set and an unrecognised key is a 400 under `errors['__all__']` — `?stats=live` (a typo
for `status`) must never return a cheerful unfiltered 200, which is the worst version
of the silent-default failure because the operator believes they filtered. Adding a
filter means adding it to `KNOWN_PARAMS` as well as parsing it; a test asserts the two
stay in step, so a new parameter cannot ship silently rejected. **`page` is bounded by
`MAX_PAGE` (1,000,000)** for a mechanical reason, not a product one: `page` multiplies
with `page_size` into a SQL `OFFSET`, and an unbounded page number overflowed
PostgreSQL's `bigint` and raised `DataError` — a 500 from the one endpoint whose whole
contract is that a bad parameter is a 400.

### No N+1
`directory_queryset()` carries `select_related('owner')`, an aggregate
`open_issue_count`, and a correlated `Subquery` for `last_activity_at` (correlated
because `AdminAuditLog.restaurant_id` is a plain `UUIDField`, not an FK). A query-count
test asserts a large page costs the SAME number of queries as a small one — add a
per-row read and it fails.

### There is no human-readable restaurant reference
No `REST-0018`. The backend has no such column, and minting a sequential business key
inside a read endpoint would create a persistent identifier nothing else writes. The
`Restaurant` UUID is the identity.

## Admin Restaurant Creation — Phase 1, Step 2D

The authoritative primitive for a NEW canonical restaurant entering Dinify through the
platform Admin control plane. ONE route, on the collection resource that already
serves the directory:

```
GET  admin/v1/restaurants/    -> the directory read (Step 1). Session-gated, NOT
                                 elevation-gated, NOT audited. UNCHANGED.
POST admin/v1/restaurants/    -> creation. IsAuthenticated + IsRecentlyElevated +
                                 the existing Admin CSRF policy. Audited exactly once.
```

`AdminRestaurantListView` was renamed `AdminRestaurantCollectionView` (the route NAME
`admin-restaurant-list` is unchanged, so every reverser keeps working). The two methods
resolve DIFFERENT permission sets from one view via `get_permissions()` — a class-level
`permission_classes` cannot express that, and duplicating the GET onto a second view
would be two directories to keep in step. There is deliberately no
`/restaurants/create/`, `/restaurants/new/` or `/onboarding/create-restaurant/`: the
method carries the meaning, not the URL. Nothing was added to the customer plane —
there is still NO customer-plane path that creates a restaurant.

### The layering
- `platform_admin_app/onboarding_creation.py` — THE DOMAIN SERVICE
  (`create_admin_restaurant`). Owns the transaction, the owner lock, the collision
  rules, the invariant proof and the credential. Returns a frozen `CreationResult`
  (`restaurant`, `onboarding`, `owner`, `owner_created`, `invitation`, `claim_token`).
- `platform_admin_app/endpoints/restaurant_creation.py` — THE REQUEST CONTRACT: the
  strict input primitives, the discriminated owner serializer, the domain-code → HTTP
  status map, the audit `after_state` and the response body. Writes nothing.
- `platform_admin_app/endpoints/restaurants.py` — the collection view. HTTP
  translation, the outer transaction and the audit row.
- `platform_admin_app/endpoints/reasoned_request.py` — NEW, and a MOVE not a copy: the
  reason contract (`ReasonedRequestSerializer`, `MAX_REASON_LENGTH`, `audit_reason`,
  `audit_error_code`) and the guarded body parse (`read_request_body`) came out of
  `commercial_base`, which re-exports them so every existing import is unchanged. Both
  rules were real review findings; a second copy is how they come back.

### What one successful request creates, atomically
1. the owner `User` — **only** in `mode=new`;
2. the canonical `Restaurant`;
3. the owner's `RestaurantEmployee`;
4. `RestaurantOnboarding(source='admin_created', created_by=<actor>)`;
5. one unresolved `OwnerInvitation`;
6. exactly one `AdminAuditLog` entry.

All six or none. A failure at any stage unwinds every earlier one THROUGH THE
DATABASE, never through compensating deletes — and an EXISTING owner is only ever read
and locked, so there is nothing about it to undo.

### THE OWNER MODE IS EXPLICIT
```
{"restaurant": {"name": ..., "location": ..., "is_test": false},
 "owner": {"mode": "new", "first_name": ..., "last_name": ...,
           "phone_number": ..., "email": ...},          // email OPTIONAL
 "reason": "..."}

{"owner": {"mode": "existing", "user_id": "<exact User UUID>"}}
```
- **`mode` is a closed vocabulary and each mode REFUSES the other's fields** (400),
  read off the keys the caller actually SENT — a key that was sent is a claim they
  made, and dropping it as blank would confirm a belief that is wrong. Presence is
  captured in the nested serializer's `to_internal_value`, because a nested serializer
  has no `initial_data`.
- The service takes a DISCRIMINATED UNION (`NewOwner` | `ExistingOwner`), so the
  hybrid the endpoint refuses is *unrepresentable* one layer down.
- **A phone already in use is a 409 `owner_account_already_exists`, NEVER a silent
  reuse.** "That number exists, so that must be who you meant" is the quiet
  wrong-owner failure this contract exists to prevent: a restaurant attached to the
  wrong person looks exactly like one attached to the right person, and nobody finds
  out until they sign in. The conflict body carries the existing account's UUID (and
  nothing else — no name, phone or email) so the operator can look at it and
  deliberately request `mode=existing`.
- **A duplicate non-blank EMAIL is refused too (409 `owner_email_already_in_use`), and
  the refusal names no account.** Email is NOT identity here and is never used to
  select an owner — but `users_app.controllers.login` and
  `reset_password._resolve_user` both resolve an address through
  `users_app.controllers.email_lookup.get_user_by_email`, which asks
  `User.objects.get(email=...)` first, so a duplicate would break email login with a
  **500 for both users** (password reset has treated an exactly shared address as
  ineligible since D11 E-R1 — acknowledged, nothing issued — but that is a reset-only
  catch, not a fix to the duplicate). `update_user_profile` already refuses
  an email change for exactly this reason. Do not let this policy mutate into "email
  identifies the owner".
  **THAT HELPER TAKES AN ADDRESS IN ANY CAPITALS AND CHANGES NOTHING ELSE**: it tries
  the address as typed, and only when that names no account does it try the address
  lower-cased, accepting it only if exactly one account holds it. Both halves are
  load-bearing. A profile edit stores an email as typed, so lower-casing every lookup
  would send a `Diner@Example.com` owner to the account holding `diner@example.com`.
  And a fallback that `get()`s or `.first()`s an address several accounts share would
  crash or pick one of them silently (pinned in `users_app/tests_email_lookup_case.py`).
  `determine-customers` matches an order's email through the same helper, and it
  creates an account only from a usable phone, never from an email alone
  (`orders_app/tests_determine_customers.py`).

### A NEW OWNER HAS NO PASSWORD AND NO CUSTOMER ACCESS
`account_type=restaurant_user`, `username` = `phone_number` = the canonical
`256XXXXXXXXX` MSISDN (`normalise_msisdn`, Uganda-only), `roles=[]`, names
`.strip().title()` per the repo convention, email lower-cased or NULL, `country='UG'`
from the server, `set_unusable_password()` — and, since Step 2D.1,
**`customer_access_state=pending_initial_claim`** (see "Pre-Claim Customer Access"
below).

**THE UNUSABLE PASSWORD WAS NEVER THE INVARIANT**, and this file used to say
otherwise. Generic password reset exists precisely to replace an unusable password
and needed nothing but the owner's phone number to do it, so "nothing can
authenticate as this account until redemption" was an aspiration rather than an
enforced fact. Step 2D.1 makes it true by adding an identity-level gate; the unusable
password remains as defence in depth beside it, never as the gate.

**NO TEMPORARY PASSWORD ARCHITECTURE.** No generated password, no `self_register`, no
`create_employee`, no credential email or SMS, no OTP, and the invitation is never
marked consumed. The retired `admin_register_restaurant` did all of that; a test
AST-scans `onboarding_creation` and fails the build if `self_register`,
`create_employee`, `Notification`, `save_action`, `OtpManager`, `random`,
`make_password` or `get_random_string` reappear by name. `prompt_password_change` keeps
its model default and is NOT touched — it must never become the proof of claim.

### AN EXISTING OWNER IS NEVER MODIFIED
Resolved by exact UUID, `select_for_update(of=('self',))` and re-read INSIDE the
transaction — `of=('self',)` and no `select_related`, so it locks the `users` row and
nothing else (the PR-E lesson). Refused with 409 when the account is **inactive**
(`owner_account_inactive` — reactivating somebody's account is a separate decision with
its own actor and reason, and this operation was not asked to make it) or is
**`platform_staff`** (`owner_account_not_restaurant_user`; `services.guard_membership_creation`
refuses the membership independently). Unknown UUID is **409
`owner_account_not_found`, NOT 404** — the caller is an authenticated administrator who
named that UUID, and a 404 on this route would say the wrong thing was missing.
Nothing about the account is altered: not the name, email, phone, username, password,
roles, `prompt_password_change`, `is_active` or `customer_access_state`. A `User`
owning several restaurants is an ordinary supported case — and `customer_access_state`
is neither read nor written here, in either direction: creating another restaurant for
somebody must not rewrite their authentication state, and an established owner of
restaurant A named as owner of a new restaurant B keeps full customer access while B's
invitation is pending.

### RESTAURANT FACTS: THREE, AND DELIBERATELY NOT THE MODEL
`name`, `location`, `is_test`. A `ModelSerializer` would make forty-odd columns
candidate request fields and turn creation into an untyped edit API for a tenant that
does not exist yet. `status` starts `onboarding` (never from the request — lifecycle
remains the only writer afterwards), `country` is the server Phase-1 value, `owner` and
`created_by` are supplied by the service, and every other column keeps its model
default. `created_by` is ATTRIBUTION, not authority.

Name and location are whitespace-normalised (trimmed, internal runs collapsed) and
**not case-folded or title-cased** — the retired path title-cased names, which renders
"KFC" as "Kfc".

**`is_test` is REQUIRED and STRICT** — a `StrictBooleanField` refusing `1`, `"true"`,
`"yes"` and `null`, so the classification cannot be decided by a coercion table. It is
never inferred from the name, location, environment or actor. Creation is allowed to
set it directly because this IS the trusted platform-owned creation writer; do NOT
route a new restaurant through `mark_restaurant_test` afterwards — that command changes
an EXISTING tenant's classification, and using it here would emit a second audit event
for a fact the creation request already stated.

### OWNER AUTHORITY, AND THE INVARIANT PROOF
`Restaurant.owner` alone is a name on a row: the customer plane resolves permissions
from an active, non-deleted owner-role `RestaurantEmployee`. Exactly one is created,
with the canonical `RESTAURANT_OWNER` constant (never a hand-typed `'owner'`), and
`assert_owner_consistency(restaurant)` is then called INSIDE the transaction — so a
creation that failed the invariant could not commit. It validates and never repairs.

**ZERO `RestaurantRolePermission` ROWS ARE SEEDED**, and that is a decision rather than
an omission. `permissions_check._resolve_from_roles` short-circuits an owner to full
access and every other role falls back to `role_defaults.DEFAULT_ROLE_MODULES` when no
override row exists — `ensure_role_permissions`'s own docstring says the resolver is
correct without them. Seeding four rows that restate the coded defaults would create
state whose only future is to drift from them. (The retired creator seeded them; that
is not a reason.)

### THE CREDENTIAL
`secrets.token_urlsafe(48)` (~288 bits, the same standard as an admin session token, a
login challenge and a delegation code), hashed with `sessions.hash_token` — **only the
hash is persisted**. The raw token is returned ONCE, in the 201 body, in a response
stamped `Cache-Control: no-store, private`. It is never logged, never audited, never in
a cookie, never in a `Location` header, and **no claim URL is fabricated** — a token is
a credential; a URL is a product promise, and the customer-plane redemption route does
not exist.

`ADMIN_OWNER_INVITATION_TTL` (default 7 days, `getattr`-with-matching-default like
every other admin constant) is read once, and `issued_at` / `expires_at` come from ONE
captured `now` so the window is exactly the TTL. Expiry stays DERIVED
(`OwnerInvitation.is_expired`) — nothing here runs on a schedule.

**A LOST RESPONSE IS UNRECOVERABLE BY DESIGN.** If the server commits and the response
is lost, the platform holds a valid unresolved invitation and only its hash. The remedy
is REISSUE, which Step 2E built: it supersedes the unresolved invitation and mints a
fresh token in one transaction. Do NOT store recoverable plaintext, do NOT return
the stored hash as a credential, and do NOT invent an "exact retry returns the same
token" guarantee the schema cannot support.

**ISSUANCE IS NOT DELIVERY.** No SMS, no email, no notification, no delivery claim —
the schema has no delivery columns for exactly that reason. Operator-mediated handoff
is the first implementation; delivery lands additively later.

### THE SUCCESS PAYLOAD
```
201 {"status": 201, "message": "Restaurant created.", "data": {
  "restaurant": { ...the canonical Step-1 DETAIL projection... },
  "owner_account": {"id": "<uuid>", "created": true|false},
  "owner_invitation": {"id": ..., "issued_at": ..., "expires_at": ...,
                       "claim_token": "<RAW, ONCE>"}}}
```
`restaurant` is `restaurant_reads.serialize_detail` re-read from the database INSIDE
the same transaction — the same bytes `GET admin/v1/restaurants/<id>/` returns, never a
second "created restaurant" shape. So the onboarding words the client sees (`tracked`,
`admin_created`, `consistent`, `not_established`, `pending`) are DERIVED by the existing
Step-2C evidence rules from the rows just written; `onboarding_reads` was NOT modified
to produce them, and a test proves the two payloads match. `owner_account.created` is
stated by the operation, never re-derived — a brand-new account and a long-standing one
are indistinguishable a moment later.

### AUDIT — EXACTLY ONE ENTRY PER POST
`admin.restaurant.created`. ONE action for ONE decision however many rows moved, and
one action per OUTCOME too (`AdminAuditLog.result` carries success / failure / denied).
Covers: success, an unreadable body, a rejected payload, every domain conflict, and a
stale-elevation refusal (via a `permission_denied` override, since DRF rejects
permissions before the handler). NOT audited: an anonymous request, a CSRF failure
(both refused inside authentication, before any decision exists) and a `GET`.

- Success: `resource_id` / `restaurant_id` = the new tenant; **no `before_state`** (the
  resource did not exist — a row of nulls would imply a prior state); `after_state` =
  `restaurant_status`, `is_test`, `admin_onboarding_source`, `owner_user_id`,
  `owner_account_created`, `owner_invitation_id`, `owner_invitation_expires_at`.
  **No owner name, phone or email. No raw token and no token hash.**
- Failure / denied: no `resource_id`, no `restaurant_id`, no state blobs — nothing was
  created, and naming an id would put a row in the log the activity strip would then
  attribute to some restaurant.
- A rejected request records `reason` ONLY if the reason field itself validated, and
  then its NORMALIZED value (`ReasonedRequestSerializer.audit_reason`). An unreadable
  body records `reason=''` — a reason must never be invented from a body that would
  not parse. A denial records nothing from the body at all.

**DOMAIN MUTATION + AUDIT SHARE ONE OUTER `transaction.atomic()`** in the view; the
service's own atomic block nests as a savepoint. A failed audit rolls the entire
creation back (pinned by a fault injected at `AdminAuditLog.objects.create`, plus a
guard test proving the domain rows really existed at that moment), and a REFUSED
creation is still recorded because the domain exception unwinds only its savepoint.
The SERVICE therefore writes no audit row itself — deliberately unlike
`onboarding_adoption`, whose adapter is a shell command: here the auditable unit is THE
REQUEST, which also has to record denials and unreadable bodies the service never sees.

### STATUS MAP
- **400** — malformed request facts: missing fields, blank name/location, invalid
  `owner.mode`, cross-mode fields, invalid UUID syntax, a non-boolean `is_test`, an
  invalid phone or email, a missing or too-short reason. Field-keyed `errors`, NESTED
  the way DRF nests them (`{"owner": {"phone_number": [...]}}`) — including for a
  DOMAIN refusal such as `normalise_msisdn`'s, so one field never has two error
  shapes depending on which layer refused it.
- **409** — a well-formed request the platform's current state contradicts:
  `owner_account_already_exists`, `owner_email_already_in_use`,
  `owner_account_not_found`, `owner_account_inactive`,
  `owner_account_not_restaurant_user`, `restaurant_already_exists`. Carries `code`,
  and (for the two account conflicts) a `details` object of UUIDs only.
- **415** `unsupported_media_type` / **400** `malformed_body` — `request.data` parses
  ON ACCESS, and the two exceptions are distinct (`UnsupportedMediaType` is NOT a
  `ParseError` subclass). Both audited; different statuses, because the status is the
  caller's remedy.
- **403** stale/absent elevation, **401** anonymous.
- An UNMAPPED domain code is deliberately re-raised → **500** and a rollback, never
  relabelled a tidy client error.

### DUPLICATE PROTECTION AND CONCURRENCY
Two rules, one answer:
- **SAME OWNER** — `Restaurant.Meta.unique_together (name, location, owner)`, a
  database fact. The pre-check produces a sentence; the constraint behind a SAVEPOINT
  is what enforces it, and it counts SOFT-DELETED rows too (the index has no `deleted`
  predicate). An `IntegrityError` is re-checked before being called a duplicate — an
  unrelated one is re-raised.
- **ANY OWNER** — the same name at the same location, case-insensitively, among
  non-soft-deleted restaurants. The rule the retired creator applied, kept because it
  is the strongest truthful duplicate statement this repo has made and an accidental
  double submission is far likelier than two businesses sharing a name AND a location.

**Creation is not adoption**: a duplicate is REFUSED, never answered by handing back
the restaurant somebody else created.

Serialization points: an EXISTING owner's `User` row (locked); a NEW owner's
`phone_number` unique index (no lock needed — it is a database fact). LOCK ORDER:
`User → (INSERT Restaurant) → (INSERT RestaurantEmployee) → (INSERT
RestaurantOnboarding) → (INSERT OwnerInvitation) → AdminAuditLog`. It row-locks nothing
but the owner and never waits on a `Restaurant` row, so it cannot cycle against the
lifecycle transition (which holds `Restaurant` and waits on `User` only for the
`FOR KEY SHARE` its audit insert takes). **NO ADMISSION ADVISORY LOCK** — an order path
cannot read a restaurant that does not exist yet, and taking it would invert the
documented `advisory → Restaurant` order for nothing.

**TWO KNOWN, DELIBERATELY OPEN RACES**, both recorded rather than papered over;
`platform_admin_app/tests_restaurant_creation_concurrency.py` states them beside the
races it does close.

1. Two simultaneous creations naming the SAME restaurant under DIFFERENT owners can
   both pass the cross-owner read. Nothing in the schema forbids that pair, and closing
   it would need either a new global uniqueness index over live rows (whose behaviour
   against existing data is not obviously safe) or a lock domain broad enough to
   serialise unrelated creations. The same-owner case — the double-click an operator
   actually produces — IS closed.
2. Two simultaneous creations with DIFFERENT phones and the SAME owner email can both
   pass the email read. **`User.email` carries no unique constraint**, and under READ
   COMMITTED a `SELECT` takes no predicate lock, so the pre-check is best-effort — in
   contrast to the phone check, which the `phone_number` unique index makes race-free.
   THIS IS AN EXISTING REPOSITORY-WIDE SEAM, not one this surface introduced:
   `self_register` and `update_user_profile` carry the identical non-atomic
   `filter(email=...).exists()` check, so a duplicate can already arise from the
   customer plane. The real fix is a partial unique index on `User.email` (excluding
   NULL and `''`, of which there are many — `create_user` stores `''` for a missing
   address). That is a CONTRACT migration under the expand-only rule: it FAILS AT
   DEPLOY if the corpus already holds a duplicate, so it needs the production data
   inspected first and belongs in its own PR alongside a repair for whatever it finds.
   Do NOT close it with an advisory lock here — that would serialise this endpoint
   against itself while the two customer-plane writers went on writing around it, which
   reads as enforcement without being it.

### WHAT CREATION DOES NOT TOUCH
No `RestaurantServiceConfiguration`, `RestaurantSubscriptionTerms`, invoice, payment,
receivable, PSP row, tax decision or owner go-live approval; no `payment_timing` or
`payment_collection_mode`. No dining area, table, QR credential, menu section, menu
item, order, test order, support issue or delegation grant. No readiness change —
`check_go_live_readiness` still fails closed with `readiness_not_configured`. No
`RestaurantRolePermission` rows. No legacy action log
(`misc_app.controllers.save_action_log`), no `Notification`, no `Secretary`. A newborn
tenant being commercially and operationally EMPTY is correct: those absences are the
readiness blockers a later step will name.

### NOT BUILT BY THIS STEP
The Angular creation UI, owner-invitation REDEMPTION, the password/claim flow,
delivery, readiness, owner go-live approval, QR / menu / table setup, commercial
configuration and lifecycle controls. **Step 2 is not complete.** (Step 2D.1 later
added the pre-claim access gate — enforcement of the pending state, not the redemption
that clears it. Step 2E then added REISSUE and CANCEL; there is deliberately no
"resend", because nothing is delivered.)

### SPEC DEBT (reported, not resolved here)
The Admin MVP document's §6 still says "no admin-plane onboarding API in Phase 1"
while its own §15 sequences "Create restaurant shell + owner invitation", and this file
already records that the customer-plane creation path was removed *until Phase 1
rebuilds onboarding natively on `/api/admin/v1`*. The product evolved in favour of the
latter and this step implements it. The stale §6 wording is NOT a reason to put
creation back on the customer plane; reconcile the Admin spec when the creation UI
slice touches it.

## Pre-Claim Customer Access — Phase 1, Step 2D.1

The gate that makes Step 2D's central claim enforceable. `User.customer_access_state`
is an IDENTITY-level fact answering one question — *may this identity be admitted onto
the customer plane at all?* — and it is checked at every door that could hand out or
honour a customer session.

### THE BYPASS IT CLOSES
Step 2D creates a new owner with an unusable password and an `OwnerInvitation` as the
account-claim credential. Nothing enforced that. Generic password reset needed only
the owner's phone number:

```
initiate-reset-password(phone) -> OTP -> reset-password(phone, otp)
    -> set_password() -> RefreshToken.for_user() -> a customer session
```

leaving the platform asserting `owner_control: not_established` and `invitation:
pending` about an account already exercising owner authority over the restaurant.
**AN UNUSABLE PASSWORD WAS NEVER THE INVARIANT** — it is what password reset exists to
replace. Correct the older wording wherever it survives: the account is unusable
before claim because of this gate, not because of the password.

### THE VOCABULARY — TWO STATES, AND NO MORE
`established` | `pending_initial_claim` (`string_definitions.py`).

`established` is the ordinary state, the model default, and what every pre-existing
identity migrated to. It asserts nothing about verification — it means "subject to the
ordinary customer rules and nothing more". `pending_initial_claim` means the identity
was provisioned by Dinify and has not completed its first owner claim.

Do NOT add `suspended`, `disabled`, `expired`, `invited`, `cancelled` or `verified`:
`is_active` and `OwnerInvitation` already own those questions, and a second vocabulary
for them is two columns that can disagree.

### WHY NOT AN EXISTING FIELD
- `is_active` means ADMINISTRATIVELY DEACTIVATED and is read by Django and SimpleJWT
  throughout; reusing it would make every "account disabled" message and any future
  reactivation path lie. A pending owner stays `is_active=True`.
- `prompt_password_change` defaults `True` for every account ever created, so it
  distinguishes nothing — and a claim flow must never make it the proof of claim.
- `has_usable_password()` is the thing being protected, not the protection.
- `last_login` is null for plenty of legitimate accounts; a `UserOtp` row is transient.

### WHY INVITATION STATE CANNOT BE THE GLOBAL GATE
`OwnerInvitation` and `RestaurantOnboarding` are RESTAURANT-scoped and one `User` may
own several restaurants. An established owner of restaurant A who is named owner of a
new restaurant B holds a PENDING invitation for B; reading invitation state globally
would revoke their access to A — a live tenant losing its owner because Dinify created
a second one. **This is the principal false positive the design exists to avoid**, and
it is pinned end-to-end (creation, projection, real login, real OTP, real session).

### THE FOUR CONCEPTS STAY SEPARATE
| | question |
|---|---|
| `customer_access_state` | may this User authenticate on the customer plane? |
| `owner_relationship` | does `Restaurant.owner` agree with the owner membership? |
| `owner_control` | has Dinify obtained evidence that the current owner controls this restaurant? |
| `invitation.status` | what happened to that restaurant's claim credential? |

They correlate for a new owner and are NOT aliases. `onboarding_reads` was NOT taught
to read `customer_access_state`: owner control remains evidence-based (an invitation
consumed by the CURRENT owner) and nothing else.

### THE POLICY AND THE ONE MINT — `users_app/customer_access.py`
- `is_established(user)` / `is_refused(user)` — fail closed on `None`, `AnonymousUser`,
  a missing attribute or an out-of-vocabulary value. Deliberately narrow: it does NOT
  absorb `is_active`, `account_type`, roles, permissions, OTP or password state, which
  keep their own owners. There is no `can_authenticate()` mega-helper.
- `issue_customer_tokens(user)` — **the only production `RefreshToken.for_user` call**.
  Raises `CustomerAccessRefused` for a non-established identity. Callers still gate
  explicitly and answer with their own surface's generic refusal; the exception is the
  backstop, so a forgotten gate is a loud 500 rather than a quiet token.

### THE SIX DOORS, ALL FAIL-CLOSED
| door | where | refusal |
|---|---|---|
| login | `login.py`, beside the `platform_staff` check | generic `WRONG_PASSWORD` |
| password reset | `reset_password._resolve_user` — guards BOTH stages at once | the uniform E-R1 acknowledgement (initiation) / `Invalid OTP.` (completion) |
| customer-auth OTP issuance | `OtpManager.make_otp` | `False` (delivery failure) |
| login-OTP mint sink | `OtpManager.verify_otp` | the shared `invalid` dict |
| token presentation | `CustomerJWTAuthentication.get_user` | SimpleJWT `user_inactive` |
| token refresh | `GatedTokenRefreshView` | SimpleJWT `InvalidToken` |

The login gate sits ABOVE the `last_login` write, the mint, the success action log and
the role traversal that leads to OTP issuance. The reset gate is in the shared
RESOLVER, not in `initiate_password_reset`, because a caller can invoke stage two
directly and an OTP may already exist. **Password reset must never be "fixed" by
consuming the `OwnerInvitation`** — it never sees the claim credential, so it cannot
know the right person is on the other end.

OTP is gated NARROWLY, by purpose (`CUSTOMER_AUTH_OTP_PURPOSES` = `login`,
`reset-password`) and never by user: the future redemption may want a factor of its
own, and the one identity it needs to reach is precisely a pending one.

Presentation and refresh are gated even though no pending identity should be able to
MINT one, because the invariant worth having is *a pre-claim identity cannot EXERCISE
customer authority* — not *today's known mint paths will not hand it one*. That is the
same argument that produced `CustomerJWTAuthentication` for `account_type`.

### NO NEW ACCOUNT-STATE ORACLE
Every refusal reuses the surface's EXISTING generic answer, so an anonymous caller who
knows a phone number cannot distinguish a pending owner from a wrong password, an
unknown account, a deactivated one or platform staff. Logs say `refused: customer
access not established` and carry no credential, token, OTP or owner PII.

### NOT AUDITED
Anonymous customer login/reset refusals are customer authentication attempts, not
platform-admin decisions, so they write no `AdminAuditLog` row. The Step-2D creation
audit already records that the account was provisioned and deliberately does NOT carry
`customer_access_state` — one action per request, describing the creation decision.

### MIGRATION AND ROLLBACK
`users_app/0014_customer_access_state`: one `AddField` plus the vocabulary
`AddConstraint`. **NO `RunPython`** and no inference from invitations, onboarding rows,
password state, login history or memberships — the only truthful rule is *every
identity that predates this gate is `established`*, which the field default applies to
the whole corpus in one statement.

**ONE CLASS THE DEFAULT CANNOT COVER, and it is a deploy-time check rather than a code
change.** Step 2D shipped the `mode=new` creator before this gate existed (live on UAT
from 2026-08-25), so an owner created in the window between the two deploys migrates to
`established` while still holding an unresolved invitation. It is NOT backfilled,
because the obvious rule is wrong in the dangerous direction: the invitation is minted
unconditionally, so a `mode=existing` owner holds one too, and demoting them would lock
a live tenant out of its own restaurant — the exact multi-tenant false positive this
design exists to prevent. Nothing in the schema distinguishes the two modes; only
`AdminAuditLog.after_state['owner_account_created']` does. The exposure is therefore
ENUMERATED before deploying, not guessed:

```sql
SELECT after_state->>'owner_user_id', created_at FROM admin_audit_log
WHERE action = 'admin.restaurant.created' AND result = 'success'
  AND after_state->>'owner_account_created' = 'true';
```

Empty is the expected answer. Non-empty means setting exactly those users pending as a
deliberate operator action against a named list.

It carries **`db_default` as well as `default`**, and that is load-bearing rather than
decoration. Django manages defaults in Python: `AddField` adds the column with a
default and immediately DROPS it, so a NOT NULL column ends up with no database
default. Fine for reads; not for writes — and `users` is a table old code INSERTs into
(`self_register`, `determine-customers`). Under the expand-only rule a rollback lands
OLD CODE ON NEW SCHEMA, and those inserts would hit a NOT NULL violation. `db_default`
keeps a real database default so they succeed and land on `established`. A test asserts
it by INSERTing through raw SQL without naming the column.

### NO CUSTOMER-PLANE WRITE SURFACE
`customer_access_state` is protected exactly as `account_type` is — by ABSENCE. It is
not in `SerGetUserProfile.fields` (the only `ModelSerializer` over `User`), there is no
`user` section in `EDIT_INFORMATION` at all, and `self_update_user_profile` takes named
keyword arguments. Pinned by tests including an end-to-end profile `PUT` that tries to
smuggle it.

### THE FUTURE REDEMPTION CONTRACT — DOCUMENTED, NOT BUILT
Initial-owner redemption for a NEW owner must, in ONE transaction:

1. consume the exact claim invitation as the CURRENT owner; **and**
2. move `customer_access_state`: `pending_initial_claim` -> `established`;

plus whatever credential establishment is then designed. It must never produce
*invitation consumed but access still pending*, nor *access established but invitation
not consumed*.

**There is deliberately NO supported writer of that transition yet.** In particular
there is no public `establish_customer_access(user)` service — a caller could invoke it
without claim evidence — and a test asserts its absence. Invitation EXPIRY changes
nothing about the identity: no sweeper, no signal, no clock-driven `User` mutation; a
future reissue supersedes and mints afresh.

### STRUCTURAL RATCHET
`users_app/tests_customer_access_gate.py` AST-scans every production module and fails
if any of them calls `RefreshToken.for_user` outside `users_app/customer_access.py`.
The bypass this step closes was ONE innocent-looking mint in a flow nobody thought of
as authentication; auditing the three that existed fixes today, and the scan is what
fixes tomorrow.

### NOT BUILT / STILL OPEN
Redemption, delivery, the Admin creation UI, readiness and owner go-live approval
remain deferred, and **Step 2 is still incomplete**. (Reissue and cancel landed in
Step 2E; neither touches `customer_access_state` in either direction, and both are
pinned to prove it.) The two known
races recorded under "Admin Restaurant Creation" — non-atomic `User.email` uniqueness
and cross-owner same-name+location duplication — are UNCHANGED and still open; neither
is touched here.

## Admin Commercial Writes — Phase 1, Step 3D.2a

The FIRST supported HTTP path for changing canonical commercial configuration. Two
routes, one per axis, in `platform_admin_app/endpoints/commercial.py`:

```
POST admin/v1/restaurants/<uuid>/commercial/payment-timing/
POST admin/v1/restaurants/<uuid>/commercial/payment-collection-mode/
```

Body for both: `{"value": ..., "expected_current": ..., "reason": ...}`. No migration,
no model change. **Subscription-terms writes are deliberately NOT here** — they are
their own three routes with their own serializers, tokens and audit actions; see
"Admin Subscription-Terms Writes" below. The control-plane mechanics both adapters
share (the reason field, the `request.data` parse guard, the domain→HTTP status map,
the canonical re-read, the `permission_denied` audit override) live in
`platform_admin_app/endpoints/commercial_base.py` so the two surfaces are
structurally incapable of disagreeing about them.

- **TWO ROUTES, NEVER ONE WITH A FIELD PARAMETER.** Payment timing is a SERVICE-MODEL
  fact and collection mode a CUSTODY fact — different consequences and plausibly
  different future write authority. A `commercial/<str:field>/` route would make "what
  did this operator change?" a question about a URL segment, and would let one grant
  reach both. The same reasoning keeps `service_configuration._set_axis` private behind
  two named public writers
- **BOTH ARE ELEVATION-GATED** (`IsAuthenticated` + `IsRecentlyElevated`) and require a
  substantive `reason` — `MIN_REASON_LENGTH` imported from
  `platform_admin_app.delegation`, never respelled. Nothing in the order/kitchen path
  reads payment timing yet and that is NOT a reason to gate it lightly: the decision is
  consequential when it is RECORDED, because the enforcement built later is built
  against whatever the configuration then says
- **CSRF is the existing admin policy**, enforced by `AdminSessionAuthentication` — not
  disabled, not exempted, not reimplemented per route. Tests use
  `Client(enforce_csrf_checks=True)`; the DEFAULT test client sets
  `_dont_enforce_csrf_checks` and would prove nothing
- **`expected_current` IS REQUIRED, AND MISSING ≠ EXPLICIT NULL.** Explicit `null`
  asserts "nobody has configured this yet" — the one assertion that succeeds against a
  fresh restaurant. Omission asserts nothing, so it is a 400; treating it as null would
  hand a forgetful client that claim by accident and defeat the concurrency check it was
  meant to exercise. Enforced by `required=True` + `allow_null=True` on the field, never
  by `request.data.get(...)`
- **THE ENDPOINT IS AN ADAPTER, NOT A SECOND WRITER.** It calls
  `commercial_app.service_configuration.set_payment_timing` /
  `set_payment_collection_mode` and assigns no model field; a test asserts the binding
  by identity and an AST scan fails the build if `save`/`create`/`update_or_create`
  appears in the module. Locking, the soft-delete re-check, actor resolution, vocabulary
  validation, optimistic concurrency, the same-state no-op and attribution stamping all
  stay in Step 3C
- **ACTOR IS `request.user`.** There is no `actor_id` / `set_by` request field and there
  must never be one
- **STATUS MAPPING:** changed success and same-state no-op both `200`;
  `stale_service_configuration` → **`409`** with `{"code": "stale_service_configuration"}`
  and a fixed sentence (never the domain's own message, which names internals); malformed
  body → `400` with field-keyed `errors`; missing or soft-deleted restaurant → `404`;
  stale/absent elevation → `403`. A conflict is NOT a 400 — the body was fine, the world
  moved. An unmapped domain code is deliberately re-raised rather than relabelled, so an
  internal condition surfaces as a 500 and rolls back
- **SAME-STATE RETRY IS A 200 WITH `changed=false`**, attribution untouched, even when
  `expected_current` is stale — Step 3C's rule, reached through HTTP unchanged
- **ONE AUDIT ROW PER AUTHENTICATED UNSAFE DECISION**, under
  `admin.restaurant.payment_timing_set` / `admin.restaurant.payment_collection_mode_set`.
  ONE ACTION PER ENDPOINT, not per outcome — `AdminAuditLog.result` carries that axis, and
  a `*_changed`/`*_no_op`/`*_failed` trio would make "how often did anyone try?"
  unanswerable without knowing every spelling. Covers changed success, **no-op success**,
  invalid body, an **unparseable body**, stale conflict and elevation denial (via a
  `permission_denied` override, since DRF rejects permissions before the handler). NOT
  audited: anonymous requests, CSRF failures (both refused inside authentication, before
  any administrative decision) and a missing/soft-deleted target (the transition
  endpoint's existing convention — nothing was denied and no tenant was touched).
  **`request.data` PARSES ON ACCESS**, so that access is guarded: unguarded, DRF answers an
  unreadable body with its own bare `{"detail": ...}` — a different shape from every other
  error here — and the request never reaches `self.audit`, so an elevated administrator's
  unsafe request would be missing from the log purely because it was unreadable.
  **TWO DISTINCT EXCEPTIONS reach that guard and catching only the first is the easy
  mistake**: malformed JSON raises `ParseError` (→ **400**, `malformed_body`), while a
  `Content-Type` with no parser raises `UnsupportedMediaType` — NOT a subclass of it — and
  bypassed the guard entirely until Step 3D.2b (→ **415**, `unsupported_media_type`). Both
  are audited; they keep different statuses because the status IS the caller's remedy — 400
  says the body was wrong, 415 says send JSON, and folding one into the other deletes the
  clue. An EMPTY body reaches neither branch (DRF invokes a parser only when there is
  content), so empty `text/plain` is an ordinary validation failure
- **A REJECTED REQUEST RECORDS `reason` ONLY IF THE REASON FIELD ITSELF VALIDATED**, and
  then its NORMALIZED value — corrected in Step 3D.2b. The audit used to carry the RAW
  `request.data['reason']`, so a refused request wrote an untrimmed, over-long or
  entirely non-string value into the log's `reason` column: the one field an operator
  reads to learn why a change was attempted, filled with something the endpoint had
  just refused. It cannot be read off `serializer.validated_data` either — DRF empties
  that after ANY field fails — so `commercial_base.audit_reason()` re-runs the reason
  field's own validation against `initial_data`, and returns `''` unless
  `'reason' not in serializer.errors` proves it passed. So a valid `'   ...   '`
  survives a sibling field's failure as its trimmed form, and a blank, short or
  numeric reason is recorded as absent rather than as itself
- **`before_state` / `after_state` carry ONLY that axis**, e.g.
  `{"payment_timing": "pay_first"}`. Equal on a no-op — the request happened, nothing
  moved. A conflict records the real current value from the error's `actual_current` and
  **no after_state**; nothing was applied, so none may be invented
- **DOMAIN MUTATION + AUDIT SHARE ONE OUTER `transaction.atomic()`.** The Step-3C writer's
  own atomic block nests as a savepoint. **A failed audit rolls the mutation back** — pinned
  by a test that injects the fault at `AdminAuditLog.objects.create`, i.e. after the writer
  genuinely mutated the row, plus a guard test proving the writer really ran. The same
  structure lets a REFUSED mutation still be recorded: the domain exception unwinds only its
  savepoint, so the failure audit written afterwards commits
- **THE SUCCESS RESPONSE RETURNS THE CANONICAL STEP-3D.1 `commercial` OBJECT**, re-read from
  the database inside the same transaction via `commercial_reads` — never assembled from the
  request, never a second write-path projection. So a write hands the client the exact
  `expected_current` token its next edit needs, byte-identical to what a GET would return.
  The transitional `payment_mode` / `payment_mode_configured` / `subscription` keys are
  deliberately ABSENT from the write response: they exist for the deployed frontend's GET
  contract, and a new surface must not recruit consumers for them
- **NOTHING ELSE MOVES.** No readiness change (`check_go_live_readiness` still fails closed
  with `readiness_not_configured`; `needs_attention` / `attention_filter` untouched); no
  owner-approval effect and nothing bound to `service_configuration.updated_at`; no legacy
  synchronisation in either direction (`require_order_prepayments`,
  `Table.prepayment_required`, `preferred_subscription_method`, `flat_fee`,
  `subscription_validity`, `subscription_expiry_date`, `DinifyTransaction.payment_mode`);
  no subscription-terms row created or changed; `is_test` gets no special write semantics
  and no new lifecycle prohibition is invented
- **`psp_online` PERFORMS EXACTLY ONE CONFIGURATION MUTATION** — no provider call, merchant
  record, merchant id, payment initiation, transaction or webhook row. `offline` is accepted
  as unremarkably as the other: a permanent, first-class mode, never a fallback
- **NO ADMISSION ADVISORY LOCK.** Deliberate and load-bearing: the lifecycle transition takes
  `advisory → Restaurant row`, and taking it AFTER the row lock here would invert that order.
  Nothing in the order path reads these values yet. When payment timing is eventually enforced
  in order admission, changing it while live will need a deliberate lock-design update — not
  a lock quietly added here first
- **GET COST IS UNCHANGED**: 4 queries for the list (flat across page size) and 8 for the
  detail, measured on this branch; no read module was touched. A POST costs 12–13 (auth,
  target resolve, row lock, actor, config read, the write, audit, canonical re-read)
- **The Admin frontend does NOT expose these controls yet** — Step 3E migrates the portal to
  `commercial`, adds the editing UI, sends `expected_current`, collects the reason and handles
  the 409

## Admin Subscription-Terms Writes — Phase 1, Step 3D.2b

The Admin HTTP surface over the three Step-3C terms operations, in
`platform_admin_app/endpoints/subscription_terms.py`:

```
POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/          (record)
POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/replace/
POST admin/v1/restaurants/<uuid>/commercial/subscription-terms/end/
```

No migration, no model change, no schema change and no new domain rule — every
mutation is `commercial_app.subscription_terms` called unchanged. With this, all five
Step-3C mutations are reachable over HTTP and nothing else is.

- **WHAT THESE ROWS ARE.** The recurring SOFTWARE-SUBSCRIPTION terms Dinify has
  RECORDED for a restaurant — restaurant → Dinify money, entirely separate from
  diner → restaurant payments. They are **not** an invoice, a payment, paid status,
  entitlement, good standing, PSP state or owner agreement; recording terms charges
  nobody and proves nothing about the owner's consent. "Open" is `ended_at IS NULL`
  and nothing more. Do not let a future field smuggle a billing verdict in here
- **THREE EXPLICIT ROUTES, NEVER ONE WITH AN `action`.** Recording first terms,
  superseding the open ones and closing them are materially different decisions with
  different preconditions, different concurrency tokens and different histories left
  behind. A `subscription-terms/<str:action>/` route would make "what did this
  operator do?" a question about a path segment, and one serializer with
  conditionally-required fields would make the accepted body a question about a value
  inside it. Same reasoning as the two service-configuration routes
- **`expected_terms_id` IS ON REPLACE AND END, AND DELIBERATELY ABSENT FROM RECORD.**
  Record means "this restaurant has no open terms" — an assertion about ABSENCE, which
  no row id can name; the domain enforces it and answers
  `subscription_terms_already_open`. A client that sends one on record is not honoured
  and not humoured: the field is simply not on that contract
- **THREE STRICT INPUT PRIMITIVES, each closing a specific silent coercion.**
  `StrictDecimalStringField` refuses a JSON number, because the read emits
  `recurring_amount` as a decimal string and a float in the middle of that round trip
  is exactly what loses `0.00` vs `0.0` (a DRF `CharField` would NOT do — it coerces
  the number to its string form and bypasses the contract with the very input it
  exists to refuse). `AwareDateTimeField` requires an explicit offset, because DRF's
  `DateTimeField` makes a naive value aware using the CURRENT timezone — silently
  converting an omission into an assumption, and midnight EAT vs midnight UTC is three
  hours of "which terms were in force". `StrictUUIDStringField` refuses a JSON number
  for the token, because DRF's `UUIDField` evaluates `uuid.UUID(int=42)` and produces
  a well-formed UUID no row has carried — the request then misses in the domain and
  comes back **409 "terms changed since they were loaded"** when nothing changed and
  the body was simply wrong. **A conflict is the one error here that means the world
  moved; it must never be manufactured by coercion**
- **STATUS MAPPING.** Changed success and every same-state no-op are `200`;
  `subscription_terms_already_open` / `stale_subscription_terms` /
  `no_open_subscription_terms` / `subscription_terms_not_found` are `409`; a malformed
  or unparseable body is `400` with field-keyed `errors`; an unreadable `Content-Type` is
  `415` (`unsupported_media_type`, audited — see the parse-guard bullet in the 3D.2a
  section, which both surfaces share); a missing or soft-deleted
  restaurant is a silent `404`; stale or absent elevation is `403`.
  **`subscription_terms_not_found` is a 409, NOT a 404** — this route's target is the
  RESTAURANT, which exists; the terms id is a concurrency assertion about it, and
  answering 404 would tell the operator their restaurant was gone. An unmapped domain
  code is re-raised rather than relabelled, so an internal condition surfaces as a 500
  and rolls back
- **THE AUDIT RULE THAT IS EASIEST TO GET WRONG.** Each endpoint's before/after states
  describe THE CANONICAL CURRENT CONFIGURATION as this request found it and left it —
  never a historical transition replayed. `replace_subscription_terms` returns
  `previous_terms` on an exact retry, as the evidence that the replacement already
  happened; using that row as this request's `before_state` would write the old → new
  transition into the log **a second time, as though it had occurred twice**. It did
  not: the request moved nothing. So a replace no-op audits `before == after ==` the
  current open terms, an end no-op audits `null → null`, and a record no-op audits
  equal current states rather than claiming the terms were created again
- **ONE AUDIT ACTION PER ENDPOINT, not per outcome** —
  `admin.restaurant.subscription_terms_recorded` / `_replaced` / `_ended`, with
  `AdminAuditLog.result` carrying that axis. Covers changed success, no-op success,
  invalid body, unparseable body, every domain conflict and elevation denial. NOT
  audited: anonymous requests, CSRF failures and a missing/soft-deleted target — the
  same three exclusions the service-configuration routes make, for the same reasons
- **THE AUDIT SNAPSHOT IS NARROW.** `terms_snapshot()` carries the id and the immutable
  commercial facts (`recurring_amount` as a string, `currency`, `billing_interval`,
  `effective_from`) and nothing else. Deliberately absent: `recorded_by` / `recorded_at`
  (the audit row already says who and when), `ended_at` (a terminal stamp is not a
  commercial fact, and on a replacement's outgoing row it would make the before-state
  describe the closure rather than the terms), and every word this domain does not have
  — no status, active, paid, valid, good standing, invoice, PSP or transaction. A
  failure's before-state is THIS restaurant's actual open terms, read by the endpoint —
  never reconstructed from `exc.details`, which can name a row belonging to another
  tenant
- **DOMAIN MUTATION + AUDIT SHARE ONE OUTER `transaction.atomic()`**, exactly as in
  3D.2a: the Step-3C writer's own atomic block nests as a savepoint, a failed audit
  rolls the mutation back (pinned by a fault injected at `AdminAuditLog.objects.create`
  for all three operations), and a refused mutation is still recorded because the domain
  exception unwinds only its savepoint
- **THE SUCCESS RESPONSE RETURNS THE CANONICAL STEP-3D.1 `commercial` OBJECT**, re-read
  from the database inside the same transaction — so a write hands back the exact
  `expected_terms_id` its next edit needs, byte-identical to a GET. The transitional
  `payment_mode` / `payment_mode_configured` / `subscription` keys are ABSENT from the
  write response, as on the other two routes
- **THE ENDPOINTS ARE ADAPTERS, NOT SECOND WRITERS.** The `Restaurant` lock, the
  monotonic timeline rule, open-row selection, the continuous close-then-insert
  boundary, the exact-retry proofs, the future-dating refusal and every no-op rule stay
  in `commercial_app`. AST scans over BOTH the terms module and `commercial_base` fail
  the build on `save` / `create` / `update_or_create` / `get_or_create` /
  `bulk_create` / `delete`, so moving a write one module down does not slip past
- **NOTHING ELSE MOVES.** No invoice, payment, receivable or PSP row exists or is
  created; no readiness change (`check_go_live_readiness` still fails closed with
  `readiness_not_configured`); no lifecycle gate (a tenant may need correction while
  onboarding, live or suspended) and no lifecycle write; the two service-configuration
  axes and their attribution are untouched and no configuration row is created by a
  terms write; no legacy synchronisation in either direction (`flat_fee`,
  `preferred_subscription_method`, `subscription_validity`, `subscription_expiry_date`,
  `DinifyTransaction`); a terms write never auto-creates a replacement; historical rows
  are retained, never deleted; and `is_test` gets no special write semantics
- **NO ADMISSION ADVISORY LOCK**, for the same load-bearing reason as 3D.2a — the
  lifecycle transition takes `advisory → Restaurant row`, so acquiring it after the row
  lock here would invert that order, and nothing in the order path reads terms
- **The Admin frontend does NOT expose these controls yet** — Step 3E adds the UI, sends
  the tokens, collects the reason and handles the 409

## Admin Onboarding Domain — Phase 1, Steps 2A + 2B + 2C + 2D

Four faces of one domain, all in `platform_admin_app/`: the SCHEMA and its validator
(Step 2A — two models in `models.py` plus `onboarding.py`, migration
`platform_admin_app/0009`), the ADOPTION writer (Step 2B — `onboarding_adoption.py`
and its management command, no migration), the READ projection (Step 2C —
`onboarding_reads.py`, no migration), the CREATION writer (Step 2D —
`onboarding_creation.py` plus `POST admin/v1/restaurants/`, no migration; see "Admin
Restaurant Creation"), and the CREDENTIAL LIFECYCLE (Step 2E —
`onboarding_invitations.py` plus two `owner-invitation/` routes, no migration; see
"Admin Owner-Invitation Lifecycle").

TWO PROVENANCE WRITERS, ONE PER PROVENANCE, and neither can write the other's: adoption
records a PRE-EXISTING tenant as `legacy_adopted` and refuses to convert
`admin_created`; creation makes a NEW tenant as `admin_created` and never adopts
anything. **There is still NO attestation writer and NO REDEMPTION** — Step 2E resolves
invitations by SUPERSEDING and CANCELLING them, and nothing writes `consumed_at`, so
`owner_control` still has no path to `invitation_redeemed`. **No restaurant is adopted
or created automatically**: there is no backfill, no signal and no `get_or_create`, so
absence still means "not yet represented in the Admin onboarding domain".

### RestaurantOnboarding — provenance, not a second restaurant
The durable record of HOW one canonical `Restaurant` entered the Admin onboarding
domain. Baba House already owns its owner, membership, menu, tables, QR state,
orders and lifecycle; this row holds ONLY facts with no canonical home. Admin
manages the canonical `Restaurant` regardless of how it entered Dinify.

- `source` ∈ {`admin_created`, `legacy_adopted`}, **no default** and a
  `CheckConstraint`, not merely `choices=` — a row that cannot say how the tenant
  arrived must fail, never silently become one provenance or the other
- `admin_created` requires `created_by` (the platform-staff actor who created it —
  NOT `Restaurant.owner`) and forbids the adoption and attestation fields;
  `legacy_adopted` requires `adopted_at` + `adopted_by` and forbids `created_by`.
  Both shapes are named database constraints
- `owner_control_attested_at` / `_user` / `_by` (legacy only) are a **TRIPLE that
  moves together** — *"at this time (`_at`) this administrator (`_by`) attested that
  this user (`_user`) genuinely controls this restaurant"* — **NOT** "the owner
  claimed the account at this historical timestamp". We do not know that for a
  legacy tenant, and a fabricated claim timestamp is indistinguishable from an
  observed one afterwards. Never infer it from `last_login` /
  `prompt_password_change` / an OTP row / `is_active` / the owner FK or membership
  existing. ALL THREE NULL is a legitimate state
- **The attestation NAMES ITS SUBJECT (`_user`) rather than reading it off
  `Restaurant.owner`.** An attestation certifies ONE person's control; leaving the
  subject implicit would mean reassigning the owner silently re-points the evidence
  and the replacement inherits control nobody vouched for. Making invalidation the
  duty of every future owner-write path is not enforceable (a cross-table rule
  cannot be a `CheckConstraint`, and this domain adds no signals), so the binding is
  stored: a reader compares `owner_control_attested_user_id` against the CURRENT
  `restaurant.owner_id` and a stale attestation stops counting on its own. It is
  **NOT a second owner of record** — it is a historical snapshot that must NEVER be
  kept in step with the FK; drifting apart is the signal
- **CLAIM STATE IS DERIVED, never stored** — a consumed `OwnerInvitation`, or the
  attestation triple, or neither. No `claimed` / `claim_status` / `owner_claimed_at`
- **Owner go-live approval is NOT here and is Step 3's** — its reset semantics are
  not frozen. Do not add `go_live_approved_*` or a `GoLiveApproval` model
- **ABSENCE IS MEANINGFUL, and there was NO backfill.** No signal, no
  `get_or_create`, no `RunPython`: creating a `Restaurant` through any existing path
  still yields zero onboarding rows. Absence means "not yet represented in the Admin
  onboarding domain", which is the truthful state of every restaurant today —
  **Baba House was NOT adopted and was not mutated**; it is the next PR's first
  explicit legacy-adoption case

### OwnerInvitation — a credential record
One single-use attempt to have ONE `User` confirm control of the owner relationship
for ONE `RestaurantOnboarding`. Only `token_hash` (SHA-256 hex) is stored — the raw
token is never persisted, matching `AdminSession` / `AdminLoginChallenge` /
`DelegationGrant`. The restaurant is DERIVED (invitation → onboarding → restaurant);
a second FK would be a second value able to drift. No identity snapshot.

- **AT MOST ONE UNRESOLVED INVITATION PER ONBOARDING**, a partial `UniqueConstraint`
  over `onboarding` where `consumed_at`/`cancelled_at`/`superseded_at` are all NULL.
  The predicate deliberately does NOT consult the clock (a partial-index predicate
  must be immutable), so an **expired-but-unsuperseded row still occupies the slot**
  — which is what makes Step 2E's reissue supersede step load-bearing, exactly as
  `challenges.create_challenge` consumes before it inserts. Reissuing out of an
  EXPIRED head is the case that proves it: the row reads `expired` but is still
  unresolved, so without the supersede the replacement insert would violate the index.
  As of Step 2E rows are MINTED by creation and reissue, SUPERSEDED by reissue and
  CANCELLED by cancel; **nothing consumes one** — that is Step 2F
- **Expired is DERIVED** (`expires_at <= now`, the `is_expired` property), never a
  stored `status='expired'`: nothing in this repo runs on a schedule to maintain one
- The three terminal stamps are mutually exclusive (three named constraints);
  `cancelled_at`/`cancelled_by` move together; `expires_at > issued_at`
- **NO DELIVERY MODEL** — no channel / state / `delivered_at` / provider ids and no
  `OwnerInvitationDelivery`. Today's transactional delivery is not reliable enough
  to freeze a contract around; the first implementation can hand the claim link over
  operator-mediated, and delivery lands additively later

### The owner-FK / owner-membership invariant
`platform_admin_app/onboarding.py::assert_owner_consistency(restaurant)`. Dinify
answers "who owns this?" twice — `Restaurant.owner` (owner of record) and an active
owner-role `RestaurantEmployee` (owner authority, which is what the customer plane
resolves permissions from) — and nothing keeps them in step. CONSISTENT means
exactly ONE `active=True`, `deleted=False` membership at that exact restaurant
carries `RESTAURANT_OWNER`, **and** its user is `restaurant.owner`. It returns that
membership; otherwise it raises `OwnerConsistencyError` with
`missing_owner_membership` / `multiple_owner_memberships` (checked BEFORE the
identity match — ambiguity is not agreement) / `owner_membership_mismatch`. Details
carry UUIDs only, never an owner's email or phone.

Inactive and soft-deleted rows do not count; `manager` is not `owner`; `User.roles`
can never satisfy it (that is the ambient authority Phase 0.5 removed). Account
ELIGIBILITY is a separate question — a deactivated owner account is still
structurally consistent, and later services decide eligibility.

**IT IS VALIDATION-ONLY AND REPAIRS NOTHING** — it never reassigns the owner,
deactivates a duplicate membership, creates a missing one or seeds a
`RestaurantRolePermission`. It also opens **no transaction, takes no
`select_for_update` and acquires no advisory lock**, deliberately: hiding lock
acquisition inside something that reads like a harmless assertion is how
lock-ordering cycles arrive by accident. Its answer is a snapshot, so a MUTATING
caller must establish its own transaction and locking discipline first and call this
inside it.

### adopt_existing_restaurant — the legacy adoption writer (Step 2B)
`platform_admin_app/onboarding_adoption.py`. The domain service; the future Admin
HTTP endpoint calls it unchanged, and `manage.py adopt_restaurant_onboarding` is a
thin operator adapter that adds NO policy of its own.

    adopt_existing_restaurant(*, restaurant_id, actor, reason) -> AdoptionResult

- ADOPTION MEANS ONE THING: *this pre-existing canonical Restaurant is now
  represented in the Admin onboarding domain*. It does NOT mean Dinify created the
  tenant, does NOT mean anyone vouched for the owner's control, and does NOT mean the
  owner was ever invited. The attestation triple stays NULL and no `OwnerInvitation`
  is created — both would be fabricated evidence indistinguishable from the real
  thing afterwards, which is the exact failure `legacy_adopted` provenance exists to
  avoid
- TARGETING is ONE immutable UUID: no name, no fuzzy match, no `.first()`, no bulk
  mode. Lifecycle state is NOT a blocker (a legacy tenant may be `onboarding`,
  `live`, `suspended` or `offboarded`) and adoption never changes it
- THE SERVICE RE-VALIDATES THE ACTOR against the database row even though the command
  already resolved it — the service writes the audit row, so the service is where the
  attribution has to be true. Reason bar is `lifecycle.MIN_REASON_LENGTH` (10),
  imported, never re-spelled
- LOCK ORDER: `Restaurant (select_for_update) → RestaurantOnboarding →
  AdminAuditLog`, all in ONE `transaction.atomic()`. The `Restaurant` row is the
  SERIALIZATION POINT — the onboarding row does not exist yet, so it is the only
  thing two concurrent adopters share; the loser reads the winner's committed row and
  reports an idempotent no-op instead of surfacing the OneToOne `IntegrityError`.
  It takes **NO admission advisory lock**, no table-allocation lock and no QR lock,
  deliberately: adoption writes nothing an order path reads, and enrolling it in
  those lock domains would only add cycles for a future transaction to hit
- IDEMPOTENCY IS ABOUT HISTORY. A rerun returns `already_adopted` with the ORIGINAL
  `adopted_at` / `adopted_by` and NO second audit row. It also does NOT re-check
  owner consistency for an already-adopted restaurant — ownership drifting later does
  not make the past adoption untrue; current consistency is Step 2C/3's question
- IT NEVER REPAIRS OWNERSHIP. A NEW adoption calls `assert_owner_consistency` under
  the lock and, on any of the three codes, refuses and leaves the drift in place
- `admin_created` IS NEVER CONVERTED to `legacy_adopted`:
  `AdoptionError('onboarding_source_conflict')`, no mutation, no audit row
- Domain errors: `invalid_restaurant_id`, `restaurant_not_found`, `invalid_actor`,
  `invalid_reason`, `onboarding_source_conflict`. `OwnerConsistencyError` keeps its
  own type and codes rather than being flattened into `AdoptionError`
- Audit: `admin.restaurant.onboarding_adopted`, `RESULT_SUCCESS`,
  `resource_type='Restaurant'`, resource/restaurant id = the tenant UUID, the trimmed
  reason, and state blobs carrying ONLY `{'admin_onboarding_source': None}` →
  `{'admin_onboarding_source': 'legacy_adopted'}`. No owner name, phone or email, no
  tenant detail, no token material. Refusals are NOT audited (matching
  `mark_restaurant_test`: the commonest refusal has no resolvable actor to attribute)

### onboarding_summary — the read projection (Step 2C)
`platform_admin_app/onboarding_reads.py`. Called by
`restaurant_reads.serialize_detail` exactly as it calls
`lifecycle.check_go_live_readiness` — a thin delegation to whoever owns the question.
It lives beside the domain's other faces because what it computes is ONBOARDING
semantics (what counts as evidence of owner control), not directory presentation. Step
2D consumes it unchanged: the four words a freshly created restaurant reads back
(`tracked` / `admin_created` / `consistent` / `not_established`, invitation `pending`)
are DERIVED by these rules from the rows the creation writer produced — the projection
was not taught to say them.

```
"onboarding": {
    "tracked": bool,
    "source": "legacy_adopted" | "admin_created" | null,
    "recorded_at": ISO8601 | null,
    "owner_relationship": {"status": "unavailable" | "consistent" |
                                     "missing_owner_membership" |
                                     "multiple_owner_memberships" |
                                     "owner_membership_mismatch"},
    "owner_control":      {"status": "unavailable" | "not_established" | "attested" |
                                     "invitation_redeemed" | "stale_attestation",
                           "evidence": "legacy_attestation" |
                                       "invitation_redeemed" | null,
                           "evidence_at": ISO8601 | null},
    "invitation":         {"status": "unavailable" | "not_applicable" |
                                     "not_issued" | "pending" | "expired" |
                                     "consumed" | "cancelled" | "superseded",
                           "id": UUID | null,
                           "issued_at": ISO8601 | null,
                           "expires_at": ISO8601 | null}
}
```

- UNTRACKED IS NOT A NEGATIVE VERDICT. Every axis reads `unavailable` — an untracked
  restaurant is not "inconsistent" and its owner control is not "not established";
  those questions have simply not been asked of it
- `source` is the PERSISTED CANONICAL value, never translated into display prose. The
  portal renders `legacy_adopted` as "Pre-existing restaurant"; the API carries the
  vocabulary the writer, the audit log and the `CheckConstraint` all already use. An
  unrecognised source (unreachable behind
  `restaurant_onboarding_source_vocabulary`) fails CLOSED — the raw value passes
  through so the anomaly is visible, and NO provenance-specific evidence rule is
  applied, so it can never be silently treated as legacy
- QUERY COST: ONE query for an untracked restaurant (the lookup that decides it); a
  tracked one adds exactly the owner-consistency read; `admin_created` adds at most
  three `LIMIT 1` invitation lookups, short-circuiting on the first hit. All constant
  in the size of the tenant's history, pinned by query-count tests
- Baba House is NOT hard-coded anywhere — no UUID or name special case. After deploy
  it will read `tracked: true` / `legacy_adopted` / `consistent` /
  `not_established` / `not_applicable` purely from its data
- **THE INVITATION AXIS CARRIES SAFE CONCURRENCY METADATA (Step 2E).** `id`,
  `issued_at` and `expires_at` sit beside `status` so an operator can NAME the exact
  invitation they reviewed when they reissue or cancel it — `id` IS the
  `expected_invitation_id` those two routes require. `issued_at`/`expires_at` are what
  make `pending` and `expired` legible: "expires in two days" and "expired last month"
  are different operational situations and the status word alone cannot tell them
  apart. **Still NEVER** `token_hash`, a raw token, a claim URL, delivery state, or
  password/OTP state — an invitation id is an opaque handle, a token is a credential,
  and the two must not become interchangeable because they sit in the same object.
  The three keys are present and null for `not_issued` / `not_applicable` /
  `unavailable`, so a client never has to branch on the status word to know which keys
  exist. Query cost is UNCHANGED — the projection already held the row when it decided
  the status
- **ONE DEFINITION OF THE CURRENT INVITATION (Step 2E).** `select_head_invitation` is
  that definition, and the Step-2E writers call it rather than deciding for
  themselves — a writer with its own opinion would disagree with this projection in
  exactly the case that matters (an operator reads a screen showing invitation A,
  clicks Cancel, and the server cancels something else). **The head is the ACTIONABLE
  credential**: the single unresolved row (`expired` or `pending` against the clock),
  else an invitation consumed BY THE CURRENT OWNER, else the latest resolved row, else
  `not_issued`
- **THE UNRESOLVED ROW COMES FIRST, AND THE ORDER CHANGED IN STEP 2E.** Through Step
  2C the current owner's consumed row came first, and both axes were answered from
  that one short-circuiting chain. That HID A LIVE CREDENTIAL in a state reissue makes
  reachable: owner A consumes an invitation, ownership moves to B, a credential is
  issued to B, ownership moves back to A. A's consumed row then answered "what is this
  onboarding's invitation?", so the read published a RESOLVED id — cancellation refused
  that id (already resolved) and refused B's (stale), and reissue refused outright
  because A's control was established. B's live claim credential was **impossible to
  revoke through the API**. No deployed behaviour changed with the fix: nothing writes
  `consumed_at` until redemption lands in Step 2F, so a consumed row and an unresolved
  row cannot coexist on a live system yet
- **OWNER CONTROL IS ITS OWN LOOKUP** (`HeadInvitation.control_evidence`), computed
  independently of the head. The two questions — *what is outstanding?* and *has the
  current owner claimed?* — are genuinely independent, and answering both from one
  chain is what produced the hidden credential. So `invitation: pending` alongside
  `owner_control: invitation_redeemed` is a legitimate, non-contradictory pair, just
  as `invitation: consumed` alongside `not_established` already was. COST: two `LIMIT
  1` queries when anything is outstanding or the owner has claimed, three when
  neither — one more than the old chain spent on a settled claimed restaurant, and
  that extra query IS the fix, because the only way to know nothing is outstanding is
  to ask

## Admin Owner-Invitation Lifecycle — Phase 1, Step 2E

The ADMINISTRATIVE CREDENTIAL LIFECYCLE that sits between Step 2D's initial issuance
and Step 2F's redemption. Two routes, two domain operations, NO MIGRATION — the Step-2A
schema already carried `superseded_at`, `cancelled_at`/`cancelled_by` and the
one-unresolved index:

```
POST admin/v1/restaurants/<uuid>/owner-invitation/reissue/
POST admin/v1/restaurants/<uuid>/owner-invitation/cancel/
```

Body for both: `{"expected_invitation_id": "<UUID>", "reason": "..."}`.

- **`reissue`, NOT `resend`.** The name is load-bearing and appears in the route, the
  audit action and the module. This system performs NO DELIVERY of any kind — no email,
  no SMS, no notification, no delivery column on the schema, no provider — so a verb
  promising a delivery event would be a promise the platform cannot keep, made in the
  URL where an operator is most likely to believe it. What happens is ROTATION: the old
  credential dies, a new raw token is handed to the authenticated elevated operator
- **TWO EXPLICIT ROUTES, never one with an `action` segment.** Rotating a live
  credential and terminating one are opposite decisions — one hands out authority, the
  other withdraws it — and the route should tell a reviewer which a request made
  without them reading a body. Same reasoning as the three subscription-terms routes
- **BOTH ARE ELEVATION-GATED** (`IsAuthenticated` + `IsRecentlyElevated`), CSRF-protected
  by the existing `AdminSessionAuthentication` policy, and require a substantive
  `reason` (`MIN_REASON_LENGTH`, imported from `lifecycle`, never respelled). Anonymous
  → 401 unaudited; CSRF failure → 403 unaudited (both refused inside authentication,
  before any decision exists); stale elevation → 403 with exactly one denial audit

### The layering
- `platform_admin_app/onboarding_invitations.py` — THE DOMAIN. Owns the credential
  policy, the transaction, the locks, the head selection, the concurrency token, owner
  binding, the supersede-before-insert step and the TTL. It writes NO `AdminAuditLog`
  row and inspects no session — `_resolve_actor` answers *whose decision this was*,
  never *were they allowed to*
- `platform_admin_app/endpoints/owner_invitation.py` — THE ADAPTER. Authority, the
  request contract, the audit row, HTTP translation and the no-store headers. An AST
  test fails the build if `save`/`update`/`create`/`delete` appears in it

### THE CREDENTIAL POLICY IS IN ONE PLACE
`mint_owner_invitation` is THE ONLY place an owner claim credential is generated —
`secrets.token_urlsafe(48)`, `sessions.hash_token`, `ADMIN_OWNER_INVITATION_TTL`, one
captured `now`. **Step 2D's creation service now calls it too**, so an initial
credential and a reissued one are indistinguishable in entropy, hashing and window; the
constants and `owner_invitation_ttl` MOVED here and are re-exported from
`onboarding_creation` so every existing import site is unchanged. A test asserts the
binding BY IDENTITY, so a second copy cannot pass by producing similar output. The
primitive deliberately does NOT supersede anything — freeing the slot is the caller's
job under the caller's lock, and hiding a destructive step inside something called
"mint" would make half a rotation invisible at the call site.

### WHAT `expected_invitation_id` ASSERTS — STATED EXACTLY
**IDENTITY, NOT STATUS.** It asserts *the invitation I reviewed is still the one this
onboarding presents as its head*. It does NOT assert the invitation is still in the
state the operator saw.

That has a concrete consequence, and it is deliberate: if another operator CANCELS A
while a reissue naming A is in flight, A remains the head (nothing unresolved exists,
and A is the latest resolved row), so the reissue proceeds and mints B. That is the
same outcome the operator would reach by reloading — seeing `cancelled`, id A — and
deliberately clicking Reissue, which is a REQUIRED workflow.

What the token DOES prevent is the failure it exists for: once a reissue has moved the
head from A to B, a request naming A is stale and refused, so an old Cancel click can
never terminate a credential the operator has never seen.

**Identity is not enough on its own.** Matching the id does not mean "any transition is
now fine" — each operation applies its OWN preconditions to the state found UNDER THE
LOCK, never to a pre-lock read. It is REQUIRED with no default: an omitted token is a
400, never "act on whatever is current".

### LOCK ORDER
```
Restaurant -> RestaurantOnboarding -> head OwnerInvitation   (domain, one atomic block)
                                   -> AdminAuditLog          (adapter, outer transaction)
```
A tail extension of the documented global order — `onboarding_adoption` already
establishes `Restaurant -> RestaurantOnboarding`, and `OwnerInvitation` is a table
nothing else locks — so it cannot cycle against anything that exists.
`select_for_update(of=('self',))` throughout, so a future `select_related` cannot
silently widen the lock (the PR-E lesson). **NO ADMISSION ADVISORY LOCK**: the order
path reads no invitation fact, and taking that lock AFTER the `Restaurant` row would
invert the lifecycle transition's `advisory -> Restaurant` order.

`one_unresolved_owner_invitation_per_onboarding` is the FINAL DATABASE BACKSTOP, not
the concurrency user experience — the loser of a race gets `stale_owner_invitation`,
not an `IntegrityError` surfacing as a 500.

### REISSUE — the state matrix
| head | what happens |
|---|---|
| **pending** | superseded; new pending minted |
| **expired** | superseded (still unresolved, still holds the slot); new pending minted. **No persisted "expired" stamp or status is ever written** |
| **cancelled** | left cancelled and untouched; new pending minted beside it. There is no "uncancel" |
| **consumed by a PREVIOUS owner** | not mutated, evidence not transferred; new pending minted for the CURRENT owner |
| **consumed by the CURRENT owner** | **REFUSED** — `owner_control_already_established` |

The refusal asks *has the CURRENT owner's control been established?*, never *has any
invitation in history ever been consumed?* — read from the head selector's
`establishes_current_owner_control`, the same evidence rule the read publishes, and
never from a raw `consumed_at`.

- **IT ALWAYS BINDS TO THE CURRENT CANONICAL OWNER** — `Restaurant.owner`, re-read
  under the lock. Never the previous invitation's `invited_user` (which may name a
  former owner), never `onboarding.created_by`, and never a request field: **the
  request carries no owner identity and the service takes no owner argument at all**
- **IT REQUIRES OWNER CONSISTENCY.** `assert_owner_consistency` must pass before
  anything is minted, because an invitation instructs one specific person to take
  control and issuing one while the two answers to "who owns this?" disagree would hand
  authority to whichever answer was read. It validates and NEVER repairs — a drifted
  tenant is a 409 carrying the canonical code and is left exactly as it was
- The owner must also be ACTIVE and a `restaurant_user`; neither is repaired

### CANCEL — the state matrix
| head | what happens |
|---|---|
| **pending** | `cancelled_at` + `cancelled_by` stamped. `changed=true` |
| **expired** | same — an expired invitation is unresolved and still holds the slot |
| **already cancelled** | EXACT RETRY: `changed=false`, timestamp and actor UNMOVED |
| **consumed** or **superseded** | **REFUSED** — `owner_invitation_already_resolved` |

It writes those two columns and nothing else: no delete (history is evidence), no
`superseded_at`/`consumed_at` (each says something different and untrue about how the
credential ended), no touch of `expires_at` or `token_hash`, and **no replacement** —
that is reissue's job, and conflating them would make "cancel" mean "rotate".

**THE EXACT RETRY IS SAFE HERE AND IMPOSSIBLE ON REISSUE**, because cancellation returns
no credential. It is only available while nothing has moved: once a reissue has made a
new invitation the head, the old id is stale and refused.

### CANCELLATION DELIBERATELY DOES NOT REQUIRE OWNER CONSISTENCY
**The asymmetry is the design.** Reissue MINTS authority and needs a sound owner target.
Cancellation REMOVES authority, and a drifted tenant with a live claim credential
outstanding is precisely when an administrator most needs to be able to kill it — making
revocation wait for the ownership mess to be resolved would leave the credential live
for as long as the mess took to fix. It still infers nothing and repairs nothing.

### THE CREDENTIAL, AND THE LOST RESPONSE
Only the SHA-256 hash is persisted. The raw token is returned ONCE, in the reissue 200
body, in its own `owner_invitation` object (never merged into `onboarding`, so no future
change to the canonical projection can start carrying it), under **`Cache-Control:
no-store, private` + `Pragma: no-cache` + `Expires: 0`**, all three pinned. It never
enters an audit row, a log, an exception, a cookie, a `Location` header or a query
string, and **no claim URL is fabricated** — the customer-plane redemption route does
not exist.

**A LOST RESPONSE IS RECOVERED BY ROTATING AGAIN, NEVER BY RECOVERING PLAINTEXT.** The
sequence is pinned end to end: reissue commits → response lost → the retry naming the
old id is a **409 `stale_owner_invitation`** → the client reloads and the canonical read
shows the credential it never saw → it deliberately reissues THAT one, superseding the
unknown credential and minting a known one. Do NOT store recoverable plaintext, return
the hash as a credential, or invent an "identical retry returns the same token"
guarantee the schema cannot support. **This is why reissue is rotation and not
"resend".**

### TTL AND EXPIRY
Reissue uses the SAME `ADMIN_OWNER_INVITATION_TTL` as creation, from ONE captured `now`
(`issued_at = now`, `expires_at = now + TTL`). A reissued credential gets a FRESH
window and never inherits the previous expiry; cancellation never changes expiry.
**Expiry stays DERIVED** (`expires_at <= now`) — no `status` column, no `expired_at`, no
sweeper, no signal, and nothing in this repo runs on a schedule.

### NEITHER OPERATION TOUCHES CUSTOMER ACCESS
`User.customer_access_state` above all: a restaurant-scoped credential is not an
identity claim in either direction. A `pending_initial_claim` owner stays PENDING (they
simply have no claimable credential until Admin reissues); an `established` owner stays
ESTABLISHED (their access to their OTHER restaurants has nothing to do with this one).
Both cases are pinned, and an AST scan fails the build if either module assigns the
field. Also untouched: the owner's password, `is_active`, `prompt_password_change`,
`last_login`; `Restaurant.owner` and the owner membership; the onboarding row's
provenance and attestation triple; and every historical invitation's stamps, expiry and
token hash.

**Redemption remains the ONLY supported writer of `pending_initial_claim → established`,
and it still does not exist.** Issuing another credential is not evidence of anything:
`owner_control` stays `not_established` after a reissue.

### AUDIT
Two actions, `admin.restaurant.owner_invitation_reissued` and
`admin.restaurant.owner_invitation_cancelled`. **ONE ACTION PER ENDPOINT, not per
outcome** — `AdminAuditLog.result` carries success/failure/denied, so there is no
`*_failed` / `*_no_op` / `*_denied` sibling. Covers changed success, the cancel no-op,
an invalid body, an unreadable body (400) and an unsupported media type (415), every
domain conflict, and elevation denial via a `permission_denied` override. NOT audited:
anonymous requests, CSRF failures, and a missing/soft-deleted target.

`before_state` / `after_state` carry ONE narrow snapshot each under `owner_invitation`
— exactly `{status, id, issued_at, expires_at}`, asserted as an exact key set. **Never**
the raw token, the `token_hash`, the owner's id, name, phone or email, or any whole-object
serialization. A reissue's before-state is the head AS THE REQUEST FOUND IT, captured
under the lock before the stamp was written — so a rotation out of `expired` records
`expired`, which the stamped row can no longer tell anybody. A cancel EXACT RETRY audits
`before == after == cancelled`: the request happened, nothing moved. A failure's
before-state is this restaurant's real head, read by the endpoint — never reconstructed
from `exc.details`, which can name a row the caller has no business being handed.

**DOMAIN MUTATION + AUDIT SHARE ONE OUTER `transaction.atomic()`** in the adapter; the
service's own block nests as a savepoint. A failed audit rolls the rotation or the
cancellation back — pinned by a fault injected at `AdminAuditLog.objects.create`, plus
guard tests proving the domain had genuinely written by that point. A REFUSED mutation
is still recorded, because the domain exception unwinds only its savepoint.

### STATUS MAP
- **400** — malformed request facts: a missing, null, blank, non-string, numeric or
  malformed `expected_invitation_id`; a missing, short, blank or over-long reason;
  malformed JSON. A numeric token is a 400 and **never a 409** — a conflict means the
  world moved and must not be manufactured by a coercion table
- **415** `unsupported_media_type` (audited; distinct from 400 because the status is
  the caller's remedy)
- **404** — missing or soft-deleted restaurant, silent and unaudited. The TARGET is
  resolved BEFORE the body is read, so these routes cannot become an existence oracle
- **409** — `onboarding_not_tracked`, `owner_invitation_not_applicable` (legacy
  adoption — provenance is never converted), `stale_owner_invitation`,
  `owner_invitation_not_issued`, `owner_control_already_established`,
  `owner_account_not_found` / `_inactive` / `_not_restaurant_user`,
  `owner_invitation_already_resolved`, and the three owner-consistency codes. Conflict
  bodies carry a fixed sentence plus the code and **no `details`** — naming the current
  invitation id would invite a blind retry instead of a reload
- An UNMAPPED domain code is deliberately re-raised → 500 and a rollback

### READ/WRITE AGREEMENT
For every supported state — pending, expired, cancelled, reissued-pending, and a
historical consumed row under a NEW current owner — the id the detail read publishes is
exactly the id the write endpoints accept, and a write's canonical `onboarding` object
equals the next GET's byte for byte. There is no second interpretation of "the current
invitation".

### NOT BUILT BY THIS STEP
Owner-invitation REDEMPTION (Step 2F), the claim/password flow, delivery of any kind,
the Angular Admin UI, readiness and owner go-live approval. **Step 2 is still
incomplete.** The two known races recorded under "Admin Restaurant Creation" —
non-atomic `User.email` uniqueness and cross-owner same-name+location duplication — are
UNCHANGED and still open; neither is touched here.

## Owner Claim — Phase 1, Step 2F.1 (challenge only)

The FIRST HALF of owner-invitation redemption. One route, on the CUSTOMER plane:

```
POST /api/v1/users/owner-claim/challenge/
X-Owner-Claim-Token: <raw token>
-> 200 {"status":200,"message":"Verification code sent.",
        "data":{"credential_setup_required": true|false}}
```

`platform_admin_app/owner_claim.py` (the domain) + `endpoints/owner_claim.py` (the
adapter), MOUNTED from `users_app/urls.py`. **NO MIGRATION** — `OwnerInvitation`,
`UserOtp` and `customer_access_state` already hold every fact this needs.

### CLAIM IS TWO-FACTOR, AND THAT IS WHY REDEMPTION IS SPLIT
The high-entropy `OwnerInvitation` token proves POSSESSION of the credential Dinify
issued; an OTP to the invited identity's canonical phone proves CURRENT CONTROL of
that identity. **The token alone must never establish customer access** — it is a
bearer credential handed over out of band (operator-mediated today), so anyone who
intercepted it would otherwise become the owner. Splitting the flow means the second
factor is delivered before anything irreversible happens.

### WHAT THIS STEP DOES NOT DO
It does not consume the invitation, move `customer_access_state`, set a password, mint
a customer token, create a session, or establish owner control. **Step 2F.2 does all
of that atomically and is NOT BUILT.**

### THE ROUTE IS ON THE CUSTOMER PLANE, DELIBERATELY
The owner is claiming THEIR OWN restaurant identity; an `AdminSession` has no
authority in it. The admin plane ISSUES the credential (2D/2E) and the owner REDEEMS
it — putting redemption on `/api/admin/v1` would mean the platform could complete an
owner's claim on their behalf, which is the exact fact `owner_control` exists to
record honestly. The domain still lives in `platform_admin_app`, where every other
line of invitation state is; the same arrangement `delegated_exchange` uses. **Do not
move `OwnerInvitation` into `users_app`.** The route is EXPLICIT — never
`owner-claim/<str:action>/`; redemption will get its own named route.

### HEADER-ONLY TOKEN TRANSPORT
`X-Owner-Claim-Token` and nowhere else — not a query parameter, not the path, not the
JSON or form body, not a cookie. One canonical extractor
(`claim_token_from_request`), mirroring the delegation code and the diner capability
channel. A credential in a URL lands in access logs, `Referer` headers and history;
one in a body is captured by ordinary request logging. It is never logged, echoed,
returned, or placed in an exception.

`x-owner-claim-token` is in `settings.CORS_ALLOW_HEADERS` beside the diner and
delegation credentials, and it has to be: a custom header absent from that allowlist
is stripped by the browser at preflight, so the feature fails ONLY in-browser while
every endpoint test and every curl call keeps passing. Permitting a request HEADER is
not a widening of CORS — `CORS_ALLOWED_ORIGINS` is untouched. Pinned by a real OPTIONS
preflight in `dinify_backend/tests_cors_preflight.py` (CORS-HEADER-00), which asserts
against the production allowlist rather than restating the setting.

### NO AMBIENT AUTHENTICATION
`authentication_classes = []`, not merely `AllowAny`. A customer JWT, a delegated
session and an admin cookie all have ZERO influence on which invitation or which owner
is resolved; `request.user` is never consulted. The invited identity comes from the
STORED invitation.

### ELIGIBILITY — ALL OF IT, OR ONE REFUSAL
invitation exists · `onboarding.source == admin_created` · restaurant exists and is
not soft-deleted · invitation UNRESOLVED · not EXPIRED · it IS the head
(`onboarding_reads.select_head_invitation`, the ONE definition of "the current
invitation") · `invited_user == Restaurant.owner` NOW · `assert_owner_consistency`
passes · invited user exists, is active, is `restaurant_user`, and its stored phone is
already CANONICAL. Legacy-adopted tenants never enter this flow.

**CANONICAL, not merely non-blank.** `make_otp(user=...)` with no `msisdn` argument
canonicalises only a msisdn it was PASSED and then falls back to `user.phone_number`
VERBATIM as the SMS destination, so whatever is stored is what the gateway is handed.
A non-canonical value is reachable: the `users_app/0008` backfill deliberately SKIPS
invalid / unsupported / diverged / colliding rows, and `mode=existing` attaches such an
account without modifying it. The check compares `normalise_msisdn(stored)` against the
STORED value — parsing alone is not enough, since `+256772000000` parses and would still
be sent with the `+`. Without it the code goes to a malformed destination, or delivery
fails and this endpoint answers 500 where every other unclaimable state answers 400.

The token is hashed with `platform_admin_app.sessions.hash_token` — **the same
primitive the invitation was minted with**; there is no second implementation.

### THIS IS A PREFLIGHT, NOT THE CLAIM BOUNDARY
**The eligibility preflight takes NO lock and opens NO transaction.** The challenge
grants no durable authority, so a snapshot is enough for it. If the invitation is reissued, cancelled,
expires or ownership drifts a millisecond later, the only consequence is an OTP that
Step 2F.2 will refuse to honour — harmless.

Holding the `Restaurant` row across delivery would not be. PR #306 measured the cost:
a lifecycle transition waiting on that row holds the EXCLUSIVE admission advisory lock
while it waits, and every diner order at the restaurant queues behind it. **A harmless
stale OTP is always preferable to external I/O under the ownership serialization
lock.**

**THE ISSUANCE THAT FOLLOWS IS ONE SHORT TRANSACTION (D11 B2-C), AND IT ENDS BEFORE
TRANSPORT.** `make_otp` opens `atomic(durable=True)`, takes the owner's `users` row
`FOR KEY SHARE` FIRST, then deletes the challenge it replaces, inserts the new one and
its `pending` ledger row, and COMMITS — only then is the sender entered. The key share
comes first because the challenge's foreign key to `users` is deferred: without it the
`users` row would be reached only at COMMIT, after the old challenge was locked, which
is the reverse of redemption's `users` then `user_otps` order and deadlocks. It takes
no `Restaurant` row and never the ownership/membership barrier.

Pinned structurally, not by timing (`platform_admin_app/tests_owner_claim.py`):
AT THE SENDER no transaction is open and no transaction id is held (`pg_locks`); the
ONLY locking read in the whole call is that single `FOR KEY SHARE` on `users`, inside
the issuance transaction; the claim preflight before it runs in autocommit with no
lock; finalization runs after COMMIT; and the membership barrier is never acquired.
This replaced the older oracle "no `FOR UPDATE` in any query", which asserted the
absence of a lock the issuance now correctly takes.

### THE `owner-claim` OTP PURPOSE
Exactly that spelling. **NOT in `CUSTOMER_AUTH_OTP_PURPOSES`** (`login`,
`reset-password`) — those are the two flows that can end in a customer session or
password, and a `pending_initial_claim` identity is refused them. This purpose must
REACH a pending identity: that is the one identity it exists for, as
`otp_manager`'s own docstring anticipated. `login`/`reset-password` gating is
unchanged, and a verified owner-claim code mints nothing — `verify_otp` mints only
for `purpose == 'login'`. It is EVIDENCE Step 2F.2 will consume.

### CROSS-PURPOSE OTP SEMANTICS AS RECORDED AT 2F.1 — SUPERSEDED BY D11 E-R2
**Historical.** Since D11 E-R2 the replacement DELETE is scoped to
`(user, msisdn, purpose)`, `verify-otp` binds `login`, and generic resend refuses
`owner-claim`; see that bullet under Current Implementation Status. The record below is
kept because it explains why. At 2F.1:
`make_otp` deleted prior challenges with
`UserOtp.objects.filter(user=user, msisdn=msisdn).delete()` — **purpose-blind**. Both
generic callers (`login.py:202`, `reset_password.py:43`) pass NO `msisdn`, so their
rows store `msisdn=NULL` and that filter matches them.

**CONSEQUENCE, in both directions:** an owner-claim challenge destroys a live login or
reset code for that identity, and a login attempt destroys a live owner-claim
challenge. NOT FIXED HERE: the complete fix is purpose-scoped deletion, which changes
what a login OTP does to a reset OTP — two shipped authentication flows, with their own
blast radius. A half-measure making only `owner-claim` polite would leave the likelier
direction (a login wiping a claim challenge) open while looking closed. Pinned by tests
in both directions so the behaviour is visible and any future change is deliberate.

**`verify_otp` DOES NOT PURPOSE-BIND.** It selects the most recent live challenge for
the identity and reads `purpose` OFF THE ROW; it takes no expected purpose. So "the
code was correct" does not today mean "the code was an owner-claim code". Harmless for
a surface that only ISSUES — but **Step 2F.2 must either extend `verify_otp` with an
explicit expected-purpose filter or check `UserOtp.purpose` itself under its own
lock.** This is the single most important thing 2F.2 inherits.

### ONE PUBLIC FAILURE
Unknown token, expired, cancelled, superseded, consumed, legacy tenant, deleted
restaurant, owner moved, ownership drifted, inactive account, wrong account type,
missing phone — all render `400 {"status":400,"message":"This owner claim is invalid
or no longer available."}`, exactly two keys. Anything else is an oracle: an anonymous
caller who could tell "wrong token" from "right token, wrong tenant state" learns which
guess was closest and learns facts about a restaurant they have no relationship with.
A short reason CODE goes to the server log; never to a response. `ClaimRefused` carries
no `details` at all, and `ClaimPreflight` carries no token and no hash.

### SUCCESS CARRIES ONE BOOLEAN
`credential_setup_required` is `customer_access_state == pending_initial_claim` and
**nothing else** — never inferred from password usability, `prompt_password_change`,
`last_login` or the invitation's age. It is safe to give because the caller already
holds the claim credential for this invitation. No owner PII, no invitation id, no
restaurant identity, no token, no session. Responses (success, refusal and throttle
alike) are `no-store, private` + `Pragma: no-cache` + `Expires: 0`; no cookie, no
`Location`, no fabricated claim URL.

### DELIVERY FAILURE FAILS CLOSED
A falsy `make_otp` is a 500 ("We couldn't send your verification code. Please try
again."), exactly as login and password reset do. The invitation, the access state and
the password are all untouched.

### THROTTLE
`owner_claim_challenge`, default `5/min` per IP (`THROTTLE_OWNER_CLAIM_CHALLENGE`).
**Not the security boundary** — the token is ~288 bits and the OTP verifier has its own
attempt cap — but each attempt can cost an SMS. Keyed on the client IP, never on the
token: DRF throttle cache keys surface in diagnostics. NOTE for anyone writing throttle
tests: DRF binds `SimpleRateThrottle.THROTTLE_RATES` as a CLASS attribute at import, so
`override_settings(REST_FRAMEWORK=...)` does NOT reach it and a test written the obvious
way silently exercises the real rate. Patch the attribute (`claim_rate()` in the suite).

### STEP 2F.2 HAS LANDED — see "Owner Claim Redemption" below
The contract this section used to describe as future work is implemented. Two things
stated here changed as a consequence, and both are load-bearing rather than cosmetic:

- **the challenge now passes the canonical destination to `make_otp` explicitly**, so
  `UserOtp.msisdn` records where the code actually went. Without a stored destination,
  redemption could not tell a factor delivered to the CURRENT phone from one delivered
  to a number the account no longer has;
- **the challenge refuses an invitation whose claim budget is spent**
  (`verification_locked`), which is what makes that budget survive OTP re-issuance.

`verify_otp` now takes optional `expected_purpose` / `expected_msisdn`, so the gap
recorded above — "verify_otp does not purpose-bind" — is closed.

## Owner Claim Redemption — Phase 1, Step 2F.2

The AUTHORITY TRANSACTION. One customer-plane route turns the two claim credentials
into durable owner-control evidence:

```
POST /api/v1/users/owner-claim/redeem/
X-Owner-Claim-Token: <raw token>
{"otp": "1234", "new_password": "<chosen>"}     # password only when required
-> 200 {"status":200,"message":"Owner claim completed.",
        "data":{"token":..., "refresh":..., "restaurant_id":"<uuid>"}}
```

`platform_admin_app/owner_claim_redemption.py` (the domain) + the same
`endpoints/owner_claim.py` adapter as the challenge, mounted from `users_app/urls.py`.
**MIGRATION: `platform_admin_app/0010_owner_invitation_claim_attempts`** — one
`AddField` plus its bounding `AddConstraint`, additive, no `RunPython`, no backfill.

### TWO TRANSITIONS, AND THE SECOND IS THE FALSE POSITIVE TO AVOID
A **BRAND-NEW OWNER** (`customer_access_state == pending_initial_claim`) completes
their first claim, and FOUR FACTS MOVE TOGETHER OR NONE DO: the invitation is consumed,
the chosen password is persisted, `pending_initial_claim -> established`, and
`prompt_password_change` is cleared. A customer session is then minted. There must never
be a committed state saying *invitation consumed but access still pending*, *access
established but invitation not consumed*, *access established but the password is still
unusable*, or *a new password persisted while the invitation stays pending*.

An **ESTABLISHED OWNER** claiming an ADDITIONAL restaurant is a different operation that
shares a route. `OwnerInvitation` and `RestaurantOnboarding` are RESTAURANT-scoped and
one `User` may own several restaurants, so redemption consumes THIS restaurant's
credential and mints a session, and touches the identity's password,
`prompt_password_change`, `customer_access_state`, email, phone, `account_type`,
`is_active` and `roles` NOT AT ALL. **An established account with an unusable password
stays an established account with an unusable password** — repairing it during a
restaurant claim would reinterpret a restaurant-scoped fact as global account
onboarding, which is the exact mistake `users_app/customer_access.py` exists to prevent.
Their access to their OTHER restaurants is likewise untouched.

### THE REQUEST CONTRACT
`otp` is always required and is a STRING — never parsed as an integer (a leading zero is
significant) and never `.strip()`ped. `new_password` is required exactly when the
identity is pending and **REFUSED otherwise**, rather than ignored: silently dropping it
would leave a caller believing they had changed their password. It is not `.strip()`ped
either — whitespace can be part of a password. Owner id, restaurant id, invitation id,
phone, email, `customer_access_state`, `roles` and `account_type` are NOT accepted from
the body; they are authoritative server facts.

### PASSWORD POLICY, AND WHY THE HASHING HAPPENS BEFORE THE TRANSACTION
`django.contrib.auth.password_validation.validate_password(new_password, user=<owner>)`
— the CONFIGURED validators are the policy and there is no second one. Validation runs
only AFTER the claim token has resolved to a real eligible pending claim, so a random
token cannot become an oracle by submitting a weak password and reading the response.
A password-validation failure spends nothing: no OTP consumed, no OTP attempt, no
invitation attempt, no state change.

**THE HASH IS COMPUTED OUTSIDE THE TRANSACTION AND ASSIGNED INSIDE IT.** PBKDF2 at
Django 5.2's iteration count is ~258ms of CPU, and the transaction holds the
`Restaurant` row — which is simultaneously the PR #306 membership barrier and a row the
lifecycle transition waits on WHILE HOLDING the exclusive order-admission advisory lock.
A quarter-second of hashing under it would stall every diner order at that restaurant
for no benefit; hashing needs no tenant serialization. Assigning a pre-computed hash
skips `AbstractBaseUser.save`'s `password_changed` dispatch, which is safe ONLY while no
configured validator implements that hook — a test asserts none does, so adding one that
does fails the build rather than silently bypassing it.

### THE OTP IS BOUND THREE WAYS
`verify_otp` gained optional `expected_purpose` and `expected_msisdn`, which **NARROW
the locked query** rather than filtering after it — so a challenge issued for something
else is never selected and is left completely untouched (not consumed, attempt counter
unmoved). Redemption passes `expected_purpose='owner-claim'` and the LOCKED owner's
canonical phone. Both default to `None` and change nothing for a caller that passes
neither — `self_register` and `create_employee` today — which is pinned by exercising
the primitive rather than by reading its signature. Password reset binds
`expected_purpose='reset-password'` with no destination (D11 E-R1), and the generic
`verify-otp` endpoint binds `expected_purpose='login'` with no destination (D11 E-R2);
each is pinned by its own structural test in `tests_otp_binding.py`.

Purpose binding is load-bearing under `ENV=dev`, where every code is `1234`: the digits
cannot distinguish a login code from a claim code, so the row must.

**DESTINATION BINDING** is what makes "the OTP proves CURRENT control" true. The
challenge now stores the delivery destination (`UserOtp.msisdn`), so a code delivered to
a phone the account no longer has cannot be spent. Note there is **NO production writer
that mutates an existing `User.phone_number`** — `self_update_user_profile` refuses a
change with a 400 and every other site is a CREATE — so the race is pinned with a direct
`UPDATE`, which is the only way a phone can move today. The binding compares against the
`users` row the transaction LOCKS, so it holds whatever a future writer turns out to be.

**A STALE DESTINATION COSTS NO ATTEMPT BUDGET.** It is its own refusal
(`otp_destination_stale`), decided before verification: the claimant did not cause the
phone change and five such attempts must not lock a good credential. The condition is
narrow — *something is outstanding for this identity, and none of it went where it
should go now* — because `make_otp` deletes by `(user, msisdn, purpose)` (by
`(user, msisdn)` before D11 E-R2), so a fresh challenge to a new number leaves the old
row live beside it. It is not an oracle (the response is
identical either way) and not probeable (issuing a challenge always binds to the CURRENT
canonical phone).

### THE INVITATION-LEVEL ATTEMPT BUDGET
`OwnerInvitation.claim_failed_attempts`, capped by
`OWNER_CLAIM_MAX_FAILED_ATTEMPTS = 5`. **`UserOtp.attempts` was not sufficient**:
`make_otp` deletes the old row and inserts a fresh one with `attempts=0`, so
re-requesting the second factor resets that budget — fine for login UX, and wrong for
turning a stolen bearer token into restaurant authority (five guesses, request another
code, five more, and a four-digit space is walked). This counter lives on the CREDENTIAL.
Nothing else about an attempt is stored: no submitted code, no IP, no timestamp.

**`verification_locked` IS DERIVED, exactly as expiry is** — there is no `claim_locked_at`,
no status column and no sweeper, because nothing in this repository runs on a schedule.
An exhausted invitation is STILL UNRESOLVED: it holds the per-onboarding slot, it is
reissuable and it is cancellable; it simply cannot be challenged or redeemed.
`onboarding_reads` publishes it with EXPLICIT precedence — `verification_locked` beats
`expired` beats `pending` — because both are unclaimable and both are remedied by a
reissue, but only one of them says somebody sat there guessing; reporting `expired`
would file a security event as a clock problem. The numeric count is NOT exposed: the
status is enough for the UI, and a count is progress reporting for an attacker.
**The challenge route refuses an exhausted invitation and sends no OTP**, under the same
uniform 400 — that is what makes the budget survive OTP re-issuance. **Reissue resets it
by MINTING A NEW INVITATION, never by mutating history**: the locked row is superseded
with its count intact and the replacement starts at zero.

### THE AUTHORITATIVE TRANSACTION
```
Restaurant -> RestaurantOnboarding -> head OwnerInvitation -> owner User -> UserOtp
```
A tail extension of the documented global order, all `select_for_update(of=('self',))`
where a join could otherwise widen the lock. `Restaurant -> RestaurantOnboarding ->
OwnerInvitation` is what `onboarding_invitations` already takes; `UserOtp` is otherwise
locked only by `make_otp`'s replacement delete, inside its short issuance transaction,
which takes `User` `FOR KEY SHARE` BEFORE that delete so the two cannot deadlock (D11
B2-C); `User` is taken AFTER `Restaurant`, the same direction as
the lifecycle transition. The one service that locks `User` FIRST — `onboarding_creation`
— goes on to INSERT a `Restaurant` and never waits on an existing one, so it cannot close
a cycle. **NO ADMISSION ADVISORY LOCK**: the order path reads no invitation, OTP or
customer-access fact, and taking it after the row would invert `advisory -> Restaurant`.

A pre-lock hash lookup discovers WHICH restaurant to lock and grants no authority —
every fact, the token's identity included, is re-queried under the lock. Redemption asks
the canonical `select_head_invitation` rather than `objects.get(token_hash=…)`, which is
what makes a superseded or cancelled credential genuinely dead rather than merely
expected to be.

**THE DECISION CLOCK IS ALSO READ UNDER THE LOCK.** `now` is captured AFTER the
`Restaurant` row is held, not before reaching for it — `select_for_update` BLOCKS while a
competing redemption, reissue or cancel holds that row, so a `now` read before it is
stale by the whole wait. Two things would follow: an invitation whose `expires_at` fell
inside the wait would still compare as live and could be redeemed, and `consumed_at`
would be stamped earlier than the claim actually happened, putting
`owner_control.evidence_at` before the event it is evidence of. The clock is a fact like
any other, and authoritative facts are read once the serialization point is held.

**IT CONSUMES THE PR #306 BARRIER RATHER THAN REBUILDING IT.** Taking the `Restaurant`
row is what stops a customer-plane role removal, deactivation, soft-delete, insert or
REACTIVATION committing between `assert_owner_consistency` and the consume it guards.
One measured consequence, stated rather than discovered later: while redemption runs,
any INSERT with a foreign key to the owner's `users` row waits (RI takes `FOR KEY SHARE`
on the parent) — realistically that owner logging in concurrently. The wait is bounded
by a transaction that performs no I/O at all.

### TWO DIFFERENT TRANSACTION SEMANTICS, ON PURPOSE
A **WRONG OTP** is a failed security attempt and its evidence must SURVIVE, so the
service RETURNS a result rather than raising and the transaction COMMITS both counters.
Raising out of `transaction.atomic()` would let an attacker erase their own attempt
count by definition.

A **FAILURE AFTER A CORRECT OTP** is the opposite: the invitation save, the identity
save and the token mint all roll back EVERYTHING, the OTP's consumption included — so a
legitimate claimant does not lose a valid second factor to a server-side database error
and can retry with the same code. Fault injection at each stage proves it, with guard
assertions showing the earlier stages really had run.

### THE SESSION, AND WHAT THE RESPONSE CARRIES
`customer_access.issue_customer_tokens` — the ONE sanctioned mint, enforced by the
repo-wide AST scan. For a pending owner it is reached only after the transition is both
applied in memory and persisted. `RefreshToken.for_user` INSERTs an `OutstandingToken`,
so minting is a real database write INSIDE the transaction: a persistence failure there
unwinds the whole redemption rather than leaving a consumed invitation with no session.

The 200 carries `token`, `refresh` and `restaurant_id` and nothing else — no claim
token, no token hash, no OTP, no password, no invitation id, no owner PII, no admin
actor, no `require_otp` (the claim-specific second factor has just been consumed) and no
profile. Every response — success, refusal, password error and throttle alike — is
`no-store, private` + `Pragma: no-cache` + `Expires: 0`, with no cookie, no `Location`
and no fabricated claim URL.

### ONE PUBLIC REFUSAL
Unknown token, expired, cancelled, superseded, consumed/replayed, verification locked,
owner changed, ownership drifted, inactive or wrong-type account, stale phone, no
matching OTP, wrong OTP and wrong purpose all render
`400 {"status":400,"message":"This owner claim or verification code is invalid or no
longer available."}` — exactly two keys. Password-policy errors are the narrow exception,
because a claimant holding a valid pending claim needs actionable guidance, and they are
reachable only behind a resolved eligible claim. Internal codes go to the log, never to a
response, and no PII enters an exception.

### REPLAY AND THE LOST SUCCESS RESPONSE
After success the token is DEAD: the same token, code and password render the generic
refusal, the consume timestamp does not move, no second session is minted and the
password is not rewritten. **A lost 200 does NOT roll authority back** — the durable
claim succeeded, and no plaintext customer JWT is stored for replay. Recovery is
ORDINARY LOGIN: a new owner now has established access and the password they chose; an
established owner already had credentials.

### OWNER CONTROL MOVES TRUTHFULLY, AND NOTHING ELSE IS WRITTEN
Immediately after commit, `onboarding_summary` derives `owner_relationship: consistent`,
`owner_control: invitation_redeemed` with `evidence_at` equal to the exact `consumed_at`,
and `invitation: consumed`. **THE CONSUMED INVITATION IS THE EVIDENCE** — no `claimed`
boolean, no `owner_control` column, no approval row.

**NO `AdminAuditLog` ROW, and that is deliberate.** That log records PLATFORM-STAFF
decisions and its actor is a platform staff member; a row naming a restaurant owner as
the actor of an admin action would corrupt what the log means and drop a
tenant-initiated event into the operator's activity strip. A future unified Activity
surface may project the consumed invitation separately.

**NO EXTERNAL I/O AT ALL** — no SMS, email, `Notification` (MongoDB), legacy
`save_action` or network call — which is why redemption establishes the credential
itself instead of reusing `change_password` or `reset_password`, both of which perform
synchronous MongoDB I/O. Patching each channel to explode leaves redemption succeeding.
No lifecycle change (the restaurant stays `onboarding`), no readiness change
(`check_go_live_readiness` still fails closed with `readiness_not_configured`), no
commercial row, no membership or owner-of-record write, no attestation.

### STRUCTURAL RATCHETS
`OWNER-CLAIM-REDEEM-SAFE-00` AST-scans both modules: no forbidden assignment (identity
facts, ownership, membership, other invitation stamps, provenance, lifecycle,
classification, commercial), no direct `for_user`, no credential in a logging argument,
no admission advisory lock, no membership writer, no result object carrying credential
material. The Step-2F.1 token-source scan was NARROWED rather than dropped — redemption
legitimately reads a body, so the rule now says what it means: every literal key read
out of a mapping must be in `BODY_KEYS` (`otp`, `new_password`), and the token still has
exactly one entry point.

**The customer-access writer ratchet now sanctions EXACTLY TWO production modules** —
`onboarding_creation` (writes `pending_initial_claim`) and `owner_claim_redemption`
(writes `established`) — as an inventory rather than a count, so a third writer is a
deliberate edit. There is still no public `establish_customer_access(user)` helper.

### OTP REPLACEMENT SEMANTICS — CHANGED AT 2F.2, SCOPED TO THE PURPOSE BY D11 E-R2
Passing an explicit `msisdn` moves the owner-claim row out of the `msisdn IS NULL`
bucket that `login` and `reset-password` share, so the cross-purpose collision recorded
under Step 2F.1 changed in BOTH directions and improved in both: an owner-claim
challenge no longer destroys a live login or reset code, and a login or reset attempt no
longer destroys a live owner-claim challenge (the load-bearing direction — otherwise
requesting a login code would kill the claim challenge mid-flow). **The delete itself
stayed purpose-blind at 2F.2**: `login` and `reset-password` shared the NULL bucket and
replaced one another until D11 E-R2 scoped the delete to `(user, msisdn, purpose)` —
see that bullet under Current Implementation Status.

One interaction survived 2F.2: two live rows for one identity could coexist, and the
purpose-BLIND generic verify endpoint picked the most recent, so a newer owner-claim
challenge shadowed an older login code there. (Coexistence already occurred on
`origin/main` via `resend_otp`, which has always passed `msisdn=user.phone_number`.)
Since D11 E-R2 that endpoint binds `login`, so it no longer selects, charges or spends a
claim, reset or null-purpose row. Redemption is immune — it binds purpose AND
destination — and since D11 E-R1 so is password-reset completion, which binds its
purpose.

### NOT BUILT BY THIS STEP
Invitation delivery of any kind, the Admin creation UI, the restaurant-portal claim UI,
readiness and owner go-live approval. **Step 2 is still incomplete.** The known races
recorded under "Admin Restaurant Creation" — non-atomic `User.email` uniqueness and
cross-owner same-name+location duplication — are UNCHANGED and still open.

The deliberate absence of a profile from the 200 above is not a gap the client fills
itself: Step 2F.3 added `GET api/v1/users/user-profile/` as the canonical bootstrap
read, and the token this response returns is what authenticates it. See the next
section.

## Customer Profile Bootstrap — Phase 1, Step 2F.3

`GET /api/v1/users/user-profile/` — the canonical authenticated customer profile read,
and the half of the redemption handoff that lives outside the claim flow.

```
GET /api/v1/users/user-profile/
Authorization: Bearer <customer access token>
-> 200 {"status":200,"message":"Profile retrieved.",
        "data":{"profile": { ...SerGetUserProfile... }}}
```

**NO MIGRATION**, no new model, no new serializer, no session or bootstrap concept, no
profile cache, no second membership projection. `User` + JWT + `SerGetUserProfile`
already held every fact.

### WHY IT EXISTS
Redemption returns `token + refresh + restaurant_id` and no profile, which is correct —
a claim transaction is not a profile endpoint. But the restaurant portal persists a
principal containing a profile and its route guard reads `profile.restaurant_roles`, so
a session alone cannot bootstrap it. **The client must not close that gap itself.**
Deriving `restaurant_roles` from the single `restaurant_id` would be a guess about
tenant authority: an owner may hold several memberships, the one just claimed carries a
resolved permissions map no client can compute, and a fabricated partial profile would
put a second, wrong source of truth in front of the real one.

**Nor can the claimant simply log in again.** An owner membership sets `require_otp` in
`users_app.controllers.login`, so ordinary login would demand a SECOND verification code
moments after the claim transaction consumed its own — asking a user to prove themselves
twice for one act. **No extra OTP is required merely to bootstrap after a successful
owner claim.**

### IT EXTENDS THE EXISTING RESOURCE
There is ONE user-profile resource and one route. `PUT` is its self-update; `GET` is its
read side. There is deliberately no `/session-bootstrap/`, `/me/`, `/owner-claim/profile/`
or `/auth/profile/` — a claim-specific profile endpoint would be a second profile
contract, which is the drift this step exists to prevent.

### AUTHORITY IS THE EXISTING CUSTOMER STACK, AND NOTHING IS RESTATED
`CustomerJWTAuthentication` + `IsAuthenticated` from `settings.REST_FRAMEWORK`. The view
adds **no** `authentication_classes = []`, no `AllowAny`, no admin-session authority, no
delegated-owner authority and no claim-token authority. That is what makes the three
refusals true without a second copy of any gate: platform staff on a customer token, a
`pending_initial_claim` identity and a deactivated user are all refused inside
`get_user` before a handler runs. **Do NOT weaken `CustomerJWTAuthentication` to make a
bootstrap work** — the correct ordering is redemption establishes the identity, THEN
mints, THEN the profile reads; before redemption even a fabricated token stays refused.

A delegated administrator reaches neither verb: `users/user-profile/` is deliberately
absent from `platform_admin_app.configs.delegation_scopes.ALLOWED_ROUTES` (it acts on
`request.user`, i.e. on the administrator's OWN records), and the allowlist is keyed on
`(route, method)`, so adding `GET` did not widen it.

### THE CANONICAL SERIALIZER, AND WHY THAT IS THE POINT
`SerGetUserProfile(request.user)` with **no** `restaurant_roles` context, so
`get_restaurant_roles` delegates to `get_any_restaurant_roles` — the same call `login`
makes and feeds in through context. Login and bootstrap therefore cannot disagree about
which tenants a principal holds or what it may do there, which is the architectural
reason for the endpoint: owner claim and ordinary login must bootstrap the SAME frontend
principal. A regression pins their two profiles equal for the same database state, on
both the token branch and the OTP branch.

**The membership resolver is authoritative.** Nothing infers membership from the
`restaurant_id` redemption returned — that is CONTEXT for the UI. Eligibility follows
the existing resolver semantics unchanged: active, non-deleted memberships at
restaurants in `portal_access_states()` (`onboarding` + `live`). Step 2F.3 widened
nothing.

### WHAT THE RESPONSE MAY NOT CARRY
No access or refresh token, no `require_otp`, no `account_type`, no
`customer_access_state`, no invitation state, no claim token or hash, no OTP, no
password, no Admin state. This is PROFILE BOOTSTRAP, not another authentication
operation. `prompt_password_change` is already a field of the canonical profile and does
not get a second source of truth.

### NO SIDE EFFECTS
No `last_login` stamp, no token mint or rotation, no current-restaurant write, no action
log, no `AdminAuditLog` row, no OTP consumed, no `OwnerInvitation` or
`customer_access_state` touched, no profile field repaired. Pinned generally rather than
table by table: a test asserts every statement the request issues is a `SELECT`, so a
write to a table nobody thought of fails too.

### NO-STORE, ON THE VIEW
`Cache-Control: no-store, private` + `Pragma: no-cache` + `Expires: 0`, plus
`Vary: Authorization`, set in `finalize_response` so no branch can forget them. No
cookie. **PUT's response is stamped by the same override, which is deliberate and
additive** — it answers with the same canonical profile — and changes no status code,
body, request contract or error semantics; it is the only way PUT differs from before.

### QUERY BUDGET
`get_any_restaurant_roles` reads the memberships in ONE query and every override row
across those restaurants in a second, so the cost does not grow with membership count.
A flatness test compares one membership against twelve and asserts the counts are
EQUAL; a second proves seeded `RestaurantRolePermission` rows add no per-restaurant
query. The absolute number is incidental and deliberately not asserted.

### NOT BUILT BY THIS STEP
No frontend of any kind. Invitation delivery, the Admin creation UI, the
restaurant-portal claim UI, readiness and owner go-live approval remain unbuilt, and
**Step 2 customer-facing and Admin UI is still incomplete.** Step 2F.2's authority
semantics are untouched — this step changed no claim, invitation, OTP or
customer-access rule.

## Owner-Membership Serialization — Phase 1, Step 2E.1

The repository-wide remediation of PR #305's Codex **P1**, which was correct. Not a
new feature: one small primitive plus five call sites, no migration, no schema change,
no new rule about what anybody may do.

### THE FINDING, and why it was valid
> When a tenant-plane `PUT .../employees` changes the current owner's `roles` or
> `active` state concurrently, that path locks only the `RestaurantEmployee` row and
> does not participate in this service's `Restaurant` lock. It can therefore commit
> immediately after `assert_owner_consistency()` reads a valid membership but before
> the invitation is inserted, causing this endpoint to return a live claim credential
> for an owner relationship that is already inconsistent.

`assert_owner_consistency` is a VALIDATOR and says so: *"the answer is a snapshot,
valid for the instant it was read… a MUTATING caller must therefore establish its own
transaction and locking discipline FIRST and call this INSIDE it."* The onboarding
writers always did their half — `onboarding_adoption` (Step 2B) and
`onboarding_invitations` (Step 2E) both take the `Restaurant` row first and assert
inside that transaction. The other half was missing: `Secretary.update()`/`delete()`
take `select_for_update()` on the TARGET row only, and `build_scoped_instance_queryset`
filters on a plain `restaurant_id__in` column with no join, so there was nothing to
widen the lock onto the parent. **The exposure predates Step 2E** — adoption has shipped
with the same window since Step 2B.

### WHY LOCKING THE MEMBERSHIP ROWS WOULD HAVE BEEN A PARTIAL FIX
Under READ COMMITTED a `SELECT … FOR UPDATE` takes no predicate lock, so locking the
rows the assertion READ cannot stop a row it could not have read from appearing.
**REACTIVATION is that case and it is reachable today**: a soft-deleted owner membership
is invisible to the assertion (`deleted=False`), and `create_employee_from_existing_user`
revives it with an UPDATE. `unique_together (user, restaurant)` does not help — a second
owner is a different user.

### THE `Restaurant` ROW IS THE SERIALIZATION POINT
Every membership belongs to exactly one restaurant; an INSERT has no membership row to
lock yet; and the onboarding writers already take that row, so the customer plane joins
an ordering already proven rather than inventing a second one.

`restaurants_app/controllers/employee_membership_lock.py` —
`lock_restaurant_for_membership_mutation(restaurant_id)`. It asserts it is inside a
transaction (a `select_for_update` in autocommit is released by the statement that took
it, and every test would still pass), takes `select_for_update(of=('self',))` with no
`select_related` (PR-E's lesson: an over-broad lock is how the delegation ABBA cycle
happened), and does NOTHING else — no authorization, no membership read, no mutation,
no advisory lock. It returns `None` for an unresolvable target rather than raising, so
it cannot invent a refusal in endpoints that already have their own not-found posture.

**The generic `Secretary` was deliberately NOT broadened.** It serves menu items,
tables, dining areas, sections and employees alike; changing the lock order for every
resource because employees need a parent barrier would be a far larger lock-domain
change than the defect warrants.

### MEASURED POSTGRESQL BEHAVIOUR — half of this was already true by accident
While one transaction holds the `restaurants` row `FOR UPDATE` (PostgreSQL 16,
measured, not assumed):

| concurrent statement on `restaurant_employees` | blocked? | why |
|---|---|---|
| INSERT referencing that restaurant | **YES** | RI takes `FOR KEY SHARE` on the parent |
| UPDATE that CHANGES the FK to it | **YES** | same RI re-check |
| UPDATE that leaves the FK alone | **NO** | keys unchanged → the RI trigger never fires |
| DELETE of the child row | **NO** | no parent RI check |

So the membership INSERT was already serialized — **incidentally**, by one database's
RI triggers rather than by anything this repository states. Everything the finding
names (`roles`, `active`) and everything adjacent (soft-delete, reactivation) was not.
The barrier makes it explicit for all of them. `restaurants_app/tests_table_allocation_lock.py`
records the same trap from the other side: a test written there to prove table-allocation
locking PASSED against the buggy code because `bulk_create`'s FK lock stalled the
allocator anyway. Never read "it blocked" as "the barrier works" without a negative control.

### LOCK ORDER
`Restaurant → RestaurantEmployee`, never the inverse. A tail extension of the documented
global order, in the same shape as table-number allocation (`Restaurant → INSERT Table`,
PR-H §2): it takes the `Restaurant` row and never afterwards reaches for the admission
advisory lock, so it can BLOCK the lifecycle transition but cannot cycle against it.
**NO ADMISSION ADVISORY LOCK** — membership management does not participate in order
admission, and taking that lock after the row would invert `advisory → Restaurant`.

**THE ONE INVERSION IN THE TREE, analysed and unreachable.** `Secretary.delete()` runs
`ConVacuumDeletedRecords().vacuum()` INLINE inside its transaction, and `VACUUM_MODELS`
leads with `Restaurant` — so in principle a membership delete could hold one
`restaurants` row and then UPDATE others, which is `RestaurantEmployee → Restaurant`.
It cannot happen: **no production path soft-deletes a `Restaurant`** (the `delete()`
dispatch dict has no `restaurants` key), so that sweep's queryset is empty by
construction. The sweep's child models are safe on their own terms — an UPDATE that
leaves the FK unchanged takes no lock on the parent row (measured above). If a
restaurant soft-delete is ever added, this becomes live and needs re-analysis.

### THE PRODUCTION MEMBERSHIP WRITERS, and what each does now
| writer | shape | change |
|---|---|---|
| `POST restaurant-setup/create-employee/` → `controllers/create_employee.py` | new `User` + INSERT via Secretary | barrier taken as the FIRST statement of the transaction it already opened |
| `POST restaurant-setup/employees/` (existing-user shortcut) → `controllers/employees/create_employee.py` | REACTIVATION (UPDATE) | had **no transaction at all**; now one transaction, barrier first, membership re-read under its own row lock |
| `POST restaurant-setup/employees/` (generic) | INSERT via Secretary | barrier around `Secretary.create()` |
| `PUT restaurant-setup/employees/` | UPDATE `roles`/`active` | own `_update_employee` branch: barrier → last-owner guard → `Secretary.update()`, all in one transaction |
| `DELETE restaurant-setup/employees/` | soft-delete | own `_delete_employee` branch: barrier → `Secretary.delete()` |
| `platform_admin_app/onboarding_creation.py` | INSERT | **NO CHANGE, deliberately** — see below |

**Step 2D creation needs no barrier and does not get one.** It creates the owner
membership for a `Restaurant` INSERTed moments earlier in the same uncommitted
transaction. No concurrent writer can see that parent row, so there is nothing to
serialize against; locking a row this transaction just created would be ceremony, and
it would add a second `Restaurant` acquisition to a service whose lock order
(`User → INSERT Restaurant → …`) is deliberately documented.

**The parent is resolved SERVER-SIDE on update and delete**, from the membership's own
FK via `_RESTAURANT_RESOLVERS` — never from a client-supplied `restaurant`. That is the
same resolution `check_permission` already authorized against, and `restaurant` is
`read_only` on `SerializerPutRestaurantEmployee`, so the parent cannot shift under the
lock. A vanished membership takes the helper's `None` path and falls through to
Secretary's existing scoped lookup and its non-enumerating 404. **No authority changed**:
the same callers may mutate membership as before, they simply participate in the barrier.

### THE LAST-OWNER GUARD IS NOW INSIDE THE LOCK
It was `check` (outside any lock) → `write` (inside Secretary's). Both halves now sit in
the same transaction under the same `Restaurant` row, so the check is authoritative for
the write it guards.

**REPORTED, NOT FIXED (a pre-existing integrity gap, not a concurrency one):** that guard
covers `PUT {active:'false'}` ONLY. Removing `RESTAURANT_OWNER` from `roles`, or
soft-deleting the membership outright, leaves a restaurant with no owner authority and is
not refused. Closing it means deciding owner-reassignment semantics, which is a product
decision and not a concurrency PR's to make.

**ALSO REPORTED, NOT FIXED:** the generic `POST restaurant-setup/employees/` branch is
broken on `origin/main` — `restaurant` is `read_only` and no `server_values` supplies it,
so any user without a soft-deleted membership to revive gets an `IntegrityError` (NOT NULL
on `restaurant_id`) surfacing as a 500. Only the reactivation shortcut and
`create-employee` actually work. Untouched here; fixing it means deciding whether that
branch should exist.

### THE PROOFS
`platform_admin_app/tests_owner_membership_concurrency.py` (13 tests, `TransactionTestCase`,
PostgreSQL only). Blocking is proved POSITIVELY — the customer connection sets
`lock_timeout` and PostgreSQL raises by name — never by watching a thread fail to finish.
The admin thread parks at the REAL seam (`assert_owner_consistency` runs, THEN the thread
stops), so the customer write is attempted in exactly the window the finding names.
Synchronisation is `threading.Event`; no `sleep` decides anything; every wait has a
timeout so a deadlock fails loudly.

Both orderings are pinned for reissue (holds the barrier → the mutation cannot commit
inside the decision; mutation commits first → reissue refuses with the canonical
consistency conflict and mints nothing), for role removal, deactivation, soft-delete,
INSERT and REACTIVATION, and for legacy adoption.

**NEGATIVE CONTROL.** Neutralising the barrier in the production source (dropping the
`select_for_update`) makes **5 of the 7** positive proofs fail. The two that survive are
exactly the predicted ones: the INSERT (still blocked by referential integrity — which is
why it is documented as not being evidence) and a sequential Case-B test that does not
race at all. The suite also carries `BarrierRemovedTests`, which patch the barrier out at
EVERY import site and assert the bad interleaving reproduces — a live claim credential
minted for a restaurant with no owner authority, and a second owner reactivated inside
the decision.

### THE RATCHET
`restaurants_app/tests_membership_serialization.py` (MEMBERSHIP-LOCK-00). An AST scan
over production modules: the set that can write a membership must equal a stated
inventory, each entry must either reference the barrier by name or carry a recorded
exemption, and the inventory must not name a module that no longer writes. It detects
manager-level mutations, `RestaurantEmployee(...)`, use of the write serializer, and
direct assignment to `roles`/`active`/`deleted` — verified to fire on both an innocent
`objects.create` helper and a direct-instance revive. Test and fixture writes are out of
scope; a ratchet that fired on those would be noise.

### STEP 2F MAY RELY ON THIS — it is still unbuilt
Owner-invitation redemption will need `Restaurant` lock → owner consistency → consume the
invitation → establish customer access, in one transaction. The customer-plane membership
side of that transaction is now safe by construction, PROVIDED redemption takes the same
parent lock first. Nothing about redemption is implemented here.

**Cancellation semantics are unchanged.** Step 2E's cancel deliberately does NOT require
owner consistency and still does not. Because cancel already locks the `Restaurant`, a
simultaneous membership mutation may now wait briefly for it — that is serialization, not
a new precondition, and cancellation remains available on a drifted tenant.

## Commercial & Service Configuration Domain — Phase 1, Steps 3B + 3C
(+ the 3D.1 read and the 3D.2a/3D.2b Admin adapters, which live elsewhere)

`commercial_app` — a first-party BUSINESS-DOMAIN app holding the restaurant-level
commercial facts that had no authoritative home. Step 3B built the SCHEMA (two models,
their constraints, the initial migration); Step 3C added the INTERNAL DOMAIN WRITERS
that are now the only supported way to mutate it. Step 3D.1 added a READ projection —
which lives in `platform_admin_app`, NOT here, because it is an Admin-plane response
shape rather than a domain rule (see "The canonical `commercial` object" under Admin
Restaurant Reads). `commercial_app` itself still has **no serializer, no endpoint, no
management command and no HTTP surface of any kind**; nothing writes it over HTTP, and
absence of a row still means "not yet recorded".

**It is deliberately NOT in `platform_admin_app`.** That app is a CONTROL PLANE /
operator surface; these are business-domain facts that readiness (`restaurants_app`)
and other non-Admin code will read. It is equally not in `finance_app`, which holds
payment/transaction RECORDS — commercial terms are not transactions.

### FOUR CONCEPTS THAT ARE ROUTINELY CONFUSED
Read this before adding a field. The tree still contains remnants of two earlier,
incomplete payment designs and it is easy to reverse-engineer the wrong architecture
from them.

| Concept | Level | Vocabulary | Home |
|---|---|---|---|
| **Payment timing** | restaurant | `pay_first` \| `pay_after` | `RestaurantServiceConfiguration.payment_timing` |
| **Payment collection mode** | restaurant | `offline` \| `psp_online` | `RestaurantServiceConfiguration.payment_collection_mode` |
| **Payment method / tender** | **transaction** | `cash` \| `momo` \| `card` | `finance_app.DinifyTransaction.payment_mode` — UNCHANGED |
| **Dinify subscription** | restaurant → Dinify | recurring software fee | `RestaurantSubscriptionTerms` |

- **Payment timing is a SERVICE-MODEL fact**: must settlement be recorded before the
  kitchen may fire the order (counter café, QSR, nightlife), or does the order fire
  immediately and the tab settle at the end (full-service dining)? It NEVER
  determines custody, whether Dinify initiates anything, whether payment is digital,
  which tender the diner used, or which provider is involved
- **Payment collection mode is a CUSTODY fact**: does Dinify initiate the diner
  payment at all? `offline` = it does not; the restaurant collects the money itself
  (cash, its own MTN/Airtel merchant till, its own card terminal, another external
  mechanism) and Dinify may RECORD the settlement without ever executing it.
  `psp_online` = Dinify initiates through a licensed provider on the restaurant's
  behalf; the RESTAURANT remains merchant of record and funds settle directly to it.
  Dinify never holds, controls, pools or disburses diner money in either mode. It
  never determines timing or tender
- **`offline` IS A PERMANENT, FIRST-CLASS COMMERCIAL MODE** — not degraded, not a
  fallback, not temporary, not pre-launch-only, not test-only. PSP-backed collection
  AUGMENTS Dinify later and is not a prerequisite for anyone to launch: **the first
  commercial restaurant must be able to go live in `offline`**
- **THE TWO AXES ARE INDEPENDENT.** All four timing × collection combinations are
  legitimate and there is deliberately NO cross-constraint coupling them. A test
  pins all four
- **Dinify's revenue model is a recurring SOFTWARE SUBSCRIPTION** — never commission,
  a surcharge, a percentage of GMV, a per-order fee, or anything netted from diner
  settlements. Restaurant → Dinify money is ordinary first-party revenue and must
  never be confused with diner → restaurant money

### RestaurantServiceConfiguration
OneToOne with `Restaurant` (PROTECT), UUID pk, plain `models.Model` (NOT
`users_app.BaseModel` — `deleted`/`archived`/`vacuumed` assert rows get hidden, which
is wrong for commercial configuration). Both axes are **nullable with NO DEFAULT**:
"not configured" must stay distinguishable from a decision, which is exactly what
`subscription_validity` (`default=True`, only writer deleted) can no longer do.
Defaulting the mode to `offline` would be the same mistake in a new table — VALID and
CHOSEN are different facts.

Each axis carries its OWN attribution pair (`*_set_at` / `*_set_by`, User FK PROTECT)
answering *who last established the current value, and when* — not owner approval,
and not a replacement for change history, which stays in `AdminAuditLog`. Two
separate pairs rather than one generic `updated_by`, because the axes may end up with
DIFFERENT write authority. No "must be platform staff" rule is encoded at the model
layer; the future domain writer owns authorization.

**There is NO tender field and there must never be one** — a restaurant on `offline`
may take cash from one diner and mobile money from the next.

### RestaurantSubscriptionTerms
FK to `Restaurant` (PROTECT), UUID pk, **0..N with at most ONE OPEN row**
(`one_open_subscription_terms_per_restaurant`, a partial unique index over
`ended_at IS NULL`). Terms change by CLOSING the open row and INSERTING a replacement,
never by editing an amount in place — a future invoice or owner approval references
the exact row it was raised or given under. Like
`one_unresolved_owner_invitation_per_onboarding`, the predicate consults no clock (a
partial-index predicate must be immutable), which is what makes the future writer's
close-then-insert step load-bearing.

Fields: `recurring_amount` (DecimalField 12,2), `currency` (CharField(3), **no
default**, DB-constrained to three uppercase ASCII letters — WHICH currencies are
supported is writer policy, not a database fact), `billing_interval_unit` ∈
{day, week, month, year} + `billing_interval_count` ≥ 1, `effective_from` (required,
never fabricated from `Restaurant.time_created`), `ended_at` (nullable), and
`recorded_at` / `recorded_by`.

- **It is called TERMS, not Agreement.** A platform administrator recording terms does
  not prove the restaurant's OWNER agreed to them; `Agreement` would assert consent
  this platform has never observed. Evidence of the current owner's consent comes from
  the future owner go-live approval. For the same reason there are no `agreed_at` /
  `agreed_by` / `signed_at` columns — only `recorded_*`
- **It is NOT** payment received, an invoice, an invoice paid, good standing, a
  successful transaction, a trial, merchant readiness or any diner payment state.
  There is no `status` / `active` / `valid` / `paid` / `good_standing` / `expired`
  column: each would need a maintainer and **nothing in this repo runs on a schedule**.
  "Open" is DERIVED — `ended_at IS NULL`
- **`recurring_amount = 0` is legal and meaningful** (a test tenant rehearsing the real
  path, a free pilot, a waived period) and is a DIFFERENT fact from "no terms exist",
  which is the absence of a row. That is why there is no separate `chargeable` boolean
- **No plan catalogue, no `commission_rate`, no `per_order_rate`, no surcharge** — and
  `per_order` is deliberately absent from the interval vocabulary, since reviving it
  would restate the per-order-commission framing the non-custodial posture keeps out
- **No FK to `Order` or `DinifyTransaction`** — that is exactly how commercial TERMS
  would quietly become a payment-state model. A test pins it

### NO PSP STATE, NO TAX-OBLIGATION FIELD, NO OWNER APPROVAL
No provider name, merchant id, provider account, merchant status or webhook state
appears anywhere, and `psp_online` is provider-agnostic. **There is no PSP integration
in this repository**, so there is no provider-authoritative state to project; a local
`ready` flag nobody writes would read `not_configured` forever or be an operator
asserting a fact only the provider can know. PSP merchant state arrives WITH the first
integration; until then readiness can derive `not_applicable` (offline) vs
"required but unavailable" (psp_online) from the collection mode alone.

No `tax_obligation` field was added — the repository has no authoritative basis for
deciding which Ugandan restaurants must supply which identifier. The tenant-owned
`vat_registered` / `vat_rate` / `tin` are unchanged.

**Future owner go-live approval will bind to the EXACT facts it approved** — the
current `owner_id`, the exact `RestaurantSubscriptionTerms.id`, the exact
`payment_timing` and the exact `payment_collection_mode`. It must **NOT** bind to
`RestaurantServiceConfiguration.updated_at`: a generic row timestamp would let an
unrelated future edit silently invalidate a valid approval, and would equally fail to
invalidate one if a value changed without the timestamp moving.

### NO BACKFILL, NO AUTOMATIC ROWS
Migration `commercial_app/0001_initial` creates two tables and **nothing else** — no
`RunPython`, no `RunSQL`, no touch of any existing table, zero runtime rows. Nothing
is derived from `require_order_prepayments` (ZERO runtime readers — a stored intention
nothing enforces cannot establish a service model), `Table.prepayment_required`,
`preferred_subscription_method`, `flat_fee`, `subscription_validity`,
`subscription_expiry_date` or `DinifyTransaction.payment_mode`. Those legacy fields
survive unchanged for compatibility/history — expand first, contract later.

There is **no signal, no `post_save`, no `get_or_create` on a read path and no default
row**: a newly created restaurant gets ZERO commercial rows until a future onboarding
service deliberately records them, and **`is_test` is not a schema shortcut** (a test
tenant rehearses the same path; zero-priced terms are legitimate only once recorded).
Tests pin all of this, including that no signal receivers are registered.

### The domain writers (Step 3C) — the ONLY supported mutation paths
`commercial_app/service_configuration.py`, `commercial_app/subscription_terms.py`,
with `errors.py` (one `CommercialMutationError` carrying a stable `.code`) and
`mutation_context.py` (`lock_restaurant`, `resolve_actor`, `parse_uuid`).

    set_payment_timing(restaurant_id, value, actor, expected_current)
    set_payment_collection_mode(restaurant_id, value, actor, expected_current)
    record_subscription_terms(...)      first terms; NEVER supersedes
    replace_subscription_terms(...)     close + insert, atomically
    end_subscription_terms(restaurant_id, expected_terms_id, ended_at)

- **THE `Restaurant` ROW IS THE SERIALIZATION POINT for every commercial mutation.**
  Each service opens ONE `transaction.atomic()`, `select_for_update()`s the restaurant
  by exact UUID, re-checks `deleted` on the LOCKED row, then touches its own child
  tables. The commercial rows may not exist yet, so the tenant row is the only thing
  two concurrent operators are guaranteed to share. LOCK ORDER:
  `Restaurant → RestaurantServiceConfiguration` / `Restaurant →
  RestaurantSubscriptionTerms` — a tail extension of the documented global order that
  cannot cycle. **It must NEVER reach for the admission advisory lock afterwards**:
  the lifecycle transition takes that lock FIRST and the row second, so acquiring it
  after the row would invert that order. It is not needed at all today — nothing in
  the order path reads payment timing or collection mode. WHEN timing is eventually
  enforced in the order/kitchen path, changing it while live WILL need the barrier
- **OPTIMISTIC CONCURRENCY.** Both configuration writers take `expected_current`
  (no default — `None` is a real assertion, so a forgotten argument must not make it
  by accident) and compare it against the value read UNDER THE LOCK; a mismatch is
  `stale_service_configuration`. Terms writers use exact identity instead:
  `expected_terms_id` names the row the caller believes is open
- **SAME-STATE RETRY IS A NO-OP, checked BEFORE staleness.** If the stored value
  already equals the request, the call succeeds having written nothing — not even
  `set_at`/`set_by` — even when `expected_current` is stale. A lost HTTP response
  followed by an identical retry must not become a conflict, and must not rewrite the
  attribution of a decision somebody else made. `record_subscription_terms` and
  `end_subscription_terms` have the same property; `replace_subscription_terms`
  no-ops only on a PROOF that identifies the exact completed replacement (named row
  ended precisely at the requested instant, open row carrying exactly the requested
  facts from that instant) — never on "some open row has this amount". An END retry
  additionally requires that NOTHING HAS OPENED SINCE: if fresh terms were recorded
  after the end, replying "already done" would report success for a postcondition
  (no open terms) that no longer holds and would slip past the `expected_terms_id`
  guard, so it is a `stale_subscription_terms` conflict instead
- **TERMS ARE NEVER EDITED IN PLACE.** History is create / close+insert / close;
  `ended_at` is the only intended terminal mutation. A future invoice is raised UNDER
  a specific row and a future approval is given FOR one, both by `id` — if the numbers
  could move underneath them neither reference would mean anything. The DATABASE does
  not enforce this (no trigger, no signal): **the service is the discipline**
- **FUTURE-DATED TERMS ARE REFUSED** (`future_effective_terms_not_supported`, for both
  `effective_from` and `ended_at`). The open-row invariant is `ended_at IS NULL`, a
  predicate that consults no clock, so a scheduled row would need a sweeper — and
  nothing in this repo runs on a schedule. Backdating is fully supported. A
  replacement's boundary is CONTINUOUS: the outgoing row's `ended_at` is set to
  exactly the successor's `effective_from`, so there is no gap and no overlap
- **THE TIMELINE IS MONOTONIC.** `record_subscription_terms` after an earlier set was
  ended refuses an `effective_from` before that closure — otherwise terms effective
  1 July and ended 1 August could be followed by terms effective 15 July, and "which
  terms were in force on 20 July?" would have two answers. The check reads the LATEST
  `ended_at` across the whole history, not just the most recent row. Back-to-back
  (`effective_from == latest ended_at`) is legitimate and accepted
- **MALFORMED INPUT NEVER ESCAPES AS A PYTHON OR DATABASE EXCEPTION.** Two bounds
  exist for that reason alone and both were found by review: `recurring_amount` is
  magnitude-checked BEFORE it is quantized (`Decimal('1e100').quantize(...)` raises
  `InvalidOperation`), and `billing_interval_count` is capped at 2^31-1 because
  `PositiveIntegerField` is a 32-bit `integer` on PostgreSQL and a larger value
  raised `DataError` at the INSERT. Both would have surfaced through a future
  adapter as a 500 instead of a named refusal — the same class of defect
  `restaurant_reads.MAX_PAGE` exists to prevent
- **NO CLEAR/UNCONFIGURE OPERATION YET**, and `set_*(value=None)` is refused rather
  than overloaded. NULL is legitimate BEFORE a decision; deliberately removing a
  recorded consequential decision raises readiness and owner-approval semantics that
  are not frozen
- **NO AUTHORIZATION AND NO AUDIT LIVE HERE.** The services never inspect
  `account_type`, an `AdminSession`, elevation, CSRF or a request, and never write
  `AdminAuditLog`; `resolve_actor` answers *who did this*, never *were they allowed
  to*. A test AST-scans the package and fails if any non-test module imports
  `platform_admin_app`. The Step-3D.2a and 3D.2b Admin adapters each wrap **domain
  mutation + audit in ONE OUTER transaction** so a failed audit rolls the mutation
  back — which only works because the domain does not own the audit write; the
  internal atomic blocks nest as savepoints inside it
- **Lifecycle state does NOT gate these writes** (a tenant may need correction while
  onboarding, live or suspended), and the 3D.2a/3D.2b adapters add no gate of their
  own — an `offboarded` tenant's commercial facts stay correctable, since offboarding
  is when they most often need a closing correction. Soft-DELETED is the one refusal,
  and it comes from `lock_restaurant` on the locked row, surfacing as a silent 404. `check_go_live_readiness` is UNCHANGED and
  still returns `readiness_not_configured` — writable commercial facts do not make a
  restaurant ready. No owner-approval row is created, cleared or marked stale (none
  exists), and **nothing is bound to `configuration.updated_at`**
- **NO LEGACY SYNCHRONISATION**, in either direction: the writers never write or read
  `require_order_prepayments`, `Table.prepayment_required`,
  `preferred_subscription_method`, `flat_fee`, `subscription_validity`,
  `subscription_expiry_date` or `DinifyTransaction.payment_mode`. Tests pin that
  representative legacy fields are byte-identical after successful mutations

### Tenancy
No serializer — the Step 3D.1 read projection builds plain dicts and adds none either
— so the TENANT-STRUCT-00 machinery (which reasons about DRF `ModelSerializer`
relations) discovers nothing, `baseline.txt` is unchanged and the
ratchet reports no additions — the cleanest possible outcome, since there is no
writable relation to classify. Tests prove no serializer targets either model, that
neither is reachable through the `restaurant-setup` catch-all, that a `restaurants`
PUT cannot smuggle the fields, and that no delegated route mentions the domain.


## Deletion & Referential Integrity — CRITICAL
- Deletion-integrity rules live on the MODEL as `deletion_blockers()` (returns
  a human-readable reason, or `None` if deletable), NOT in the generic
  Secretary, so the rule survives the endpoint/controller substrate
- The restaurant-setup DELETE endpoint calls `deletion_blockers()` before the
  soft-delete and returns HTTP 409 when blocked — never 403 (a 403
  force-logs-out the client)
- Tables-domain deletion model (3 legs):
  - `Order.table` is `on_delete=PROTECT` (migration `0031`) — order/financial
    history is never silently destroyed by a cascade
  - `DiningArea.deletion_blockers()` — cannot delete an area that still
    contains any non-deleted table (move/remove the tables first)
  - `Table.has_unsettled_orders()` / `Table.deletion_blockers()` — cannot
    delete a table with a live order. Terminal = `payment_status == 'paid'`
    OR `order_status in {cancelled, refunded}`; everything else (incl.
    served-but-unpaid) is live. Payment-aware on purpose — distinct from the
    fulfilment-based occupancy helper `any_present_ongoing_order`
- The `vacuum_deleted_records` dining-area→table soft-cascade was removed (dead
  code now that empty-area deletion is enforced) — do not reintroduce it
- Menu-domain deletion rules now also live on the models via `deletion_blockers()`
  (enforced by `restaurants_app/controllers/menu_relationships.py`): a `MenuItem`
  referenced as an extra cannot be soft-deleted or demoted (`is_extra` True→False)
  while an active same-restaurant parent still lists it in `extras_applicable`;
  `MenuSection` / `SectionGroup` expose `deletion_blockers()` too
- ORDER-HISTORY LEG (D12 B2, migration `orders_app/0042`): `OrderItem.item` is
  `on_delete=PROTECT`. An ORM delete of a `MenuItem` that any order line names,
  directly or through `SectionGroup`/`MenuSection` CASCADE, raises
  `ProtectedError` while the collector is still collecting, so nothing is written.
  Lines flagged `deleted=True` still protect (the collector reads the base
  manager), and a mixed `QuerySet.delete()` is all-or-nothing. It is enforced by
  Django, not the database: the constraint stays `NO ACTION DEFERRABLE INITIALLY
  DEFERRED`. Under CASCADE such a delete removed the lines while the order and its
  `OrderAcceptance` survived, moved a draft's `quote_ref` (its submit then answered
  `quote_ref_stale`) and orphaned extras into main dishes. OUT OF SCOPE AND
  UNCHANGED: deleting an `Order` (its lines go with it, `order` is CASCADE) or a
  line directly (`parent_item` is SET_NULL), raw SQL, and code rolled back to
  CASCADE, which brings the old behaviour back and restores nothing it deleted.
  The supported HTTP delete is a soft delete and never reaches the collector, so
  no response changed. Do not "fix" a `ProtectedError` by reverting to CASCADE or
  by deleting the lines. Pinned by `orders_app/tests_order_line_retention.py`

## Monetary Fields — CRITICAL
- ALL monetary/financial fields must use `DecimalField`, never `FloatField`
- Never use `Decimal(float)` conversions or `int()` truncation in
  payment or financial logic
- A committed static guard (`scripts/check_money_fields.py`) fails CI if any
  `models.py` declares a monetary `FloatField`. It scans `models.py` files
  only — migrations are never scanned (historical money FloatFields there are
  immutable) — and matches whole underscore-tokens against monetary terms
  (`MONEY_TOKENS`, unchanged). **Since D08 B2.3 it reads declarations with `ast`,
  not a line regex**, which on `d4aacbd` missed three of four real spellings: a
  parenthesised multiline assignment, an annotated assignment (`fee: float =
  models.FloatField()`) and a same-module `from … import FloatField as X` alias. Still
  NOT followed, deliberately: assignment aliases (`F = models.FloatField`), subclasses,
  helper-built fields and classes merely named like the field. An unparseable or
  unreadable `models.py`, an unlistable directory and an empty scope are INCOMPLETE
  (exit 2), never clean

## MongoDB — Rules
- MongoDB Atlas is currently unreachable from EC2
- Affects only action logs and archiving — not core functionality
- `archive_record` and `save_action_log` have try/except wrappers — do not remove
- Do not add any new hard dependencies on MongoDB
- Exception: `mark_as_read` on notifications MUST remain synchronous
  because the endpoint needs its return value

## SMS / Yo Uganda
- VERIFIED 2026-07-20 from the EC2 box: `smgw1.yo.co.ug` resolves and connects —
  the earlier "DNS resolution issues on the production server" note was stale
  and is retired
- The gateway reports outcomes INSIDE HTTP 200 bodies (urlencoded):
  `ybs_autocreate_status=OK` is the ONLY success signal, and per-destination
  states arrive as `<msisdn>:<STATE>` in `ybs_autocreate_message`. **HTTP 200 is
  NOT success.** The ONE consolidated sender —
  `notifications_app/controllers/sms.py::send_sms` (params-dict request, 10s
  default timeout, one retry on transport errors only, body parsed via
  `parse_qs`, real bool on every path) — owns this contract; do not hand-roll
  gateway calls elsewhere. `Messenger.send_sms` is a thin delegate; the old
  duplicate `payment_integrations_app.YoIntegration` was DELETED
- SMS dispatch is threaded (`threading.Thread(daemon=True)`) EXCEPT where the
  return value is needed: `make_otp` in ENV test/prod sends SYNCHRONOUSLY with a
  tight 3s timeout BY DESIGN so callers can fail closed (the verification
  checklist's "except where return value needed" carve-out). Never add a
  synchronous send with the default 10s timeout to a request path
- ENV='dev' on the server is a DELIBERATE pre-launch state (hardcoded OTP
  `1234`, no SMS egress), revisited at launch. To verify gateway credentials
  WITHOUT changing ENV, run `manage.py send_test_sms --to <msisdn>` (or set
  `TEST_SMS_RECIPIENT`) — it bypasses the ENV gate on purpose and prints the
  raw gateway response

## Phone Numbers / MSISDN — CRITICAL
- The canonical STORED/COMPARED form of `User.phone_number` and `User.username`
  is `256XXXXXXXXX` — 12 digits, NO leading `+`. Display formatting (`+256 …`) is
  a frontend concern; the backend never stores or compares it
- `normalise_msisdn()` in `misc_app/controllers/msisdn.py` is the SINGLE source
  of truth for canonicalisation (Uganda-only; strips `+`/spaces/hyphens then
  branches on the remaining digits; idempotent; raises `InvalidMsisdn` /
  `UnsupportedCountry` — both subclasses of `MsisdnError`/`ValueError` — and
  NEVER returns `None` or a partial string). Its error messages never include the
  raw number, so they are safe to log or surface
- Apply it at EVERY write/compare site. It is already wired into `self_register`
  (registration / staff invite / admin onboarding), `self_update_user_profile`
  (before Secretary — the manager-OTP `update_user_profile` sibling has since been
  deleted), payment `initiate()` intake, and OTP `make_otp`/`verify_otp` (so
  create/verify compare canonical-to-canonical). Do NOT reintroduce the deleted
  `clean_msisdn.py` / `internationalise_msisdn` helper or hand-roll ad-hoc phone
  formatting
- `mask_msisdn()` (length-based, defensive) is the helper for logging phone-ish
  values without leaking them; `plan_msisdn_backfill()` is the pure, collision-safe
  planner behind migration `users_app/0008_backfill_canonical_msisdn` (idempotent,
  re-runnable; buckets rows into writes/invalid/unsupported/diverged/collision,
  masked before→after summary gated on `MSISDN_BACKFILL_DEBUG`, off by default)

## Development Config
- `ENV=dev` must always be retained — hardcodes OTP to `1234` and skips
  SMS sending, essential for local development
- Never delete or disable this config

## Import Path Convention
- The correct config directory is `dinify_backend/configss/` (double-s)
- Referenced in 20+ imports — never rename or restructure this directory

## Key Model Defaults (not bugs)
- `first_time_menu_approval_decision` defaults to `'approve'`
- `first_time_menu_approval` defaults to `True`
- Do not revert these

## Canonical Data Shapes — CRITICAL
- `MenuItem.tags` is the dietary-tag field. The `allergens`→`tags` rewire is
  now fully complete — post-0046 dropped the legacy `_legacy_tags` column. Do
  not reintroduce a separate allergens path. Menu items additionally reference
  the restaurant tag catalog via `tag_ids`
- `MenuItem.discount_details` uses the canonical post-0042 shape.
  `MenuItem.is_discount_active()` (timezone-aware, EAT) + `effective_base_price()`
  are the SINGLE source of truth for whether a discount is live now and the
  per-unit base price — both read purely from `discount_details`, NOT the stored
  `discounted_price` column. The diner menu serializer
  (`is_discount_active`/`current_price`/`discount_percentage`) and the order
  charge path (`ConOrder.determine_effective_unit_price`) gate on the SAME
  predicate so the displayed and charged prices agree — do NOT gate discount
  logic on `running_discount` alone (an expired / out-of-window / wrong-day /
  zero-value discount must charge `primary_price`; `recurring_days=[]` means
  "every day"). Use `get_discount_percentage` (returns positive magnitude) — do
  not invert the sign in callers. A menu-item PUT with an inverted date window
  (`end_date` < `start_date`) is rejected 400 by `SerializerPutMenuItem`
  (end-date stays inclusive; `end_date == start_date` is a valid one-day window).
  The per-extra `discounted` flag persisted on an order item is likewise derived
  from `is_discount_active()`, NOT the raw `running_discount` column (PR #214,
  BUG-P3-4) — a configured-but-not-live extra discount charges full price and
  reads `discounted=False`, matching the parent-item path
- `Restaurant.branding_configuration` uses the four-key shape (post-0041).
  Do not regress to the legacy nested shape
- `MenuItem.listing_position` and `MenuSection.listing_position` are
  authoritative for ordering; reorder writes go through `ConMenuItem`
  / the section-reorder path, never ad-hoc updates
- `Restaurant.is_test` (migration `restaurants_app/0057`, indexed, default False) is
  PLATFORM-OWNED metadata marking a tenant that is not a real commercial customer —
  a demo, a fixture, an internal rehearsal account. It is **deliberately absent from
  `EDIT_INFORMATION['restaurants']` AND from `SerializerPutRestaurant`'s field list**,
  exactly like `status`: two independent walls, so no restaurant user can set it and
  no generic tenant edit surface exposes it. There is NO customer-supplied request
  field for it and it must never gain one. Migration 0057 is additive with **NO
  backfill** — in particular no name-based heuristic; whether an existing restaurant
  is a test tenant is an explicit operator decision, not something inferred from its
  name. That operator decision is made through the audited
  `manage.py mark_restaurant_test` command — the ONLY writer of the flag (see
  Existing Management Commands); there is still no admin-plane write endpoint and no
  Admin UI. Three consequences: it surfaces on the admin directory/detail reads; it
  feeds `Order.is_test` below — which FLAGS a test restaurant's orders and limits
  nothing there: **a test restaurant can do everything a live restaurant can**; and
  Dinify's OWN portfolio and financial figures (Admin spec §11/§16 — the Home
  portfolio summary, metrics and receivables, none built yet) are meant to leave it
  out. That last one is about Dinify's numbers, never the restaurant's, and it is why
  a real customer must never be classified TEST by mistake
- `Order.is_test` (migration `orders_app/0035`, indexed) FLAGS an order as a test
  order. The order is always operationally real, and it is commercially invisible
  only when it is a PRACTICE order (the rule below). It is **SERVER-DERIVED, NEVER
  CLIENT-SUPPLIED**, in `_create_order`, and it is now TRUE under either of two
  independent conditions:
  1. **TENANT** — `verdict.restaurant_is_test`: the restaurant is flagged a test
     tenant, so every order it takes is flagged, in any lifecycle state
  2. **LIFECYCLE** — `not orders_are_commercial(verdict.status)`: the classic
     PRE-GO-LIVE REHEARSAL case, an order placed while still `onboarding`
  BOTH values come from the `AdmissionVerdict`, which reads `status` and `is_test` in
  ONE query under the shared advisory lock (`order_admission.admit`) — never from the
  caller's possibly-stale `Restaurant` instance. That is the same authoritative-moment
  rule the lifecycle half already followed, extended to the tenant flag; reading the
  flag off an earlier instance would reintroduce exactly the drift the lock was taken
  to prevent. **No extra `Restaurant` row lock was added** — the flag rides the
  existing `values_list`, so the order path's pinned query counts are unchanged.
  There is no request field for either input and there must never be one.
  **THE FLAG IS A LABEL, AND AT A TEST RESTAURANT IT LIMITS NOTHING
  (TEST-RESTAURANT-PARITY-00).** A test restaurant exists so somebody can check that
  everything a live restaurant does actually works, so its orders — every one flagged
  `is_test` by the TENANT half above — count in its own reports and dashboards, can be
  reviewed and are matched to customers exactly like a live restaurant's. Until this
  change every consumer filtered `is_test=False`, so none of that was true: a test
  restaurant's orders could not be reviewed ("This order is not eligible for review.")
  and were missing from its sales/diners/menu reports, both dashboards, the
  transactions report and customer matching. **What IS left out is a PRACTICE ORDER**
  — a test order at a restaurant that is NOT a test restaurant: a pre-go-live
  rehearsal (the LIFECYCLE half), or an order from a restaurant's time as a test
  restaurant before it was switched to real. A practice order is operationally real —
  it occupies its table, reaches the kitchen board and is served/cancelled normally —
  but is excluded from `sale_filters.sale_orders()` (the chokepoint that
  sales/diners/menu inherit), both dashboards, `summarize_revenue`, the transactions
  report (via `Q(order__isnull=True) | counted_orders_q('order__')`, so order-less
  subscription rows survive), and `determine-customers` (which MINTS REAL USERS); and
  it cannot be reviewed (`submit_review` refuses it, because review analytics
  aggregate on the denormalised `Review.restaurant` and would never see an order
  filter). **THE RULE LIVES IN ONE PLACE**, `orders_app/controllers/test_orders.py`:
  `counted_orders_q(prefix)` for querysets — built in the POSITIVE form, joining the
  order's restaurant inside the same statement so it adds NO query to any pinned
  count — and `is_practice_order(order)` for one order in hand (select the restaurant
  with it). Every consumer asks through it, and **a bare `is_test=False` filter
  anywhere else is a defect**: it switches a test restaurant off.
  `orders_app/tests_test_restaurant_parity.py` fails the build on one (an AST scan of
  filter/exclude/`Q` calls, with a self-test proving it fires) and compares a test
  and a real live restaurant consumer by consumer; against the pre-change consumers
  12 of its 19 fail. It reads the restaurant's CURRENT classification, so switching a
  test restaurant to real takes the orders it took as a test restaurant out of its
  figures (they become practice orders) and switching back returns them — pinned.
  **KNOWN EDGE**, stated rather than hidden: review READS aggregate on
  `Review.restaurant` and never consult the order's flag, so reviews left on a test
  restaurant's orders stay visible if it is later switched to real. The
  DELIBERATE inclusions are dashboard-v2's `_build_kds` and the OCCUPANCY queryset
  inside `_build_tables` — live floor state, which must agree with the kitchen board,
  and which counts practice orders too; note `_build_tables` is split, so its
  median-visit / turns / avg-ticket metrics DO apply the rule (history and money). `has_completed_test_order`
  (`orders_app/controllers/test_orders.py`) is the queryable fact Phase-1's readiness
  checklist consumes. Accepted and documented: a rehearsal order consumes a real
  `RestaurantDailyOrderCounter` ticket number
- `Order.order_remarks` was REMOVED (migration
  `0032_remove_order_order_remarks`, dormant field) along with the dead
  `item_note` emit from the orders API — do not reintroduce either
- The legacy inline-review fields on `Order`/`OrderItem` (`rating`, `review`,
  `block_review`, `block_review_reason`, `review_blocked_by`) were REMOVED
  (orders_app migration `0034`). `reviews_app.Review` is now the system of
  record for order reviews — do not reintroduce inline review columns
- `OrderItem.selected_modifiers` is CANONICALIZED before it is compared, priced,
  snapshotted or persisted. `ConOrder.normalize_selected_modifiers` (via
  `normalize_order_items`, called in-transaction inside `_create_order` after the
  table lock and before the daily counter) validates a diner's
  `{group_id: [choice_id]}` against the ordered item's OWN `options`, de-dupes
  choices, orders groups/choices by menu-definition order, omits empty groups, and
  enforces group min/max on the unique set. That ONE canonical value drives
  `find_existing_order_item` line-merge (which keys on an order-/duplicate-independent
  signature, so it also tolerates legacy pre-canonical rows),
  `determine_effective_unit_price`, `construct_option_items`/`modifiers_snapshot` and
  persistence — so `{"g":["c","c"]}` and `{"g":["c"]}` are the same order line and never
  persist duplicates. No data migration was needed (new writes are canonical; comparison
  tolerates legacy duplicates). Do NOT persist or compare raw client `selected_modifiers`

## Key Serializer Notes
- **MEDIA LEAVES THE API AS A PATH, NEVER AN ABSOLUTE URL** (MEDIA-PATH-00). Every
  client renders `environment.apiUrl + image`, and the deployed `apiUrl` carries the
  Apache mount (`…/uat`), which is where the media alias lives (`/uat/media/`).
  Stock DRF file fields return `request.build_absolute_uri(url)` as soon as a
  `request` is in the serializer context. When `Secretary._read_context` started
  passing one (the QR-disclosure fix, backend #326, 2026-09-21), every image on the
  portal's `restaurant-setup` list reads became absolute: menu item `image`, section
  `section_banner_image`, restaurant `logo`/`cover_photo`. The client then built
  `…/uathttps://…/media/…`. The absolute URL is wrong even on its own:
  `MEDIA_URL = '/media/'` has a leading slash, which Django never prefixes with
  `SCRIPT_NAME`, so it names `/media/…` at the host root, where no alias exists
  (measured on the live host: `/media/` answers 404, `/uat/media/` 403). The diner
  menu, upsells and the single-record detail read were unaffected, because they
  pass no request. **THE RULE LIVES ON THE FIELD**:
  `misc_app/serializers/fields.py` has `MediaPathImageField` /
  `MediaPathFileField` and `MediaPathFieldsMixin`, which goes BEFORE
  `ModelSerializer` in the bases and maps model file/image columns onto them. Every
  image-bearing serializer carries it, the archival `SerArc*` ones included. A
  field declared explicitly (e.g. `UpsellItemSerializer.item_image`) must use the
  media-path class itself. **Do NOT "fix" a future breakage by taking the request
  out of the context**: the QR credential policy fails closed without it.
  `restaurants_app/tests_media_paths.py` drives the real reads under
  `SCRIPT_NAME='/uat'` over HTTPS, and GUARDS every project serializer through the
  tenancy discovery, so a new serializer that renders an absolute media URL fails
  the build
- **A HISTORICAL ORDER LINE STATES THE NAME IT WAS BOUGHT UNDER, AND SAYS WHEN THAT
  NAME IS MISSING** (D12, reader). `historical_name(row)` in
  `orders_app/controllers/orders/serializers.py` is THE reading, and every
  diner-facing name site goes through it. The top-level `orders_app/serializers.py`
  imports it rather than restating it. It returns `item_name_snapshot` VERBATIM and a
  provenance of `"snapshot"` (non-empty) or `"missing"` (`""`), emitted as an additive
  sibling at all six sites:
  - order-details `items[].item.name` / `items[].extra_items[].name` → `name_provenance`;
  - every `serialize_order_item_details` row (initiate, including a D04 replay) and its
    nested `extras[]` → `item_name_provenance`;
  - `quote[].item_name` / `quote[].extras[].item_name` → `item_name_provenance`.

  **NEVER READ `MenuItem.name` FOR A HISTORICAL LINE**, and never fall back to it: the
  record keeps changing after the purchase (renamed, or rewritten to
  `<name>_autodelN` by `Secretary.delete()`'s inline vacuum), and the quote's old
  `snapshot or live` fallback presented today's name as history.
  **A BLANK IS REPORTED, NEVER FILLED**: `""` stays `""`, a string, never `null`.
  It is the column's own value and the value the kitchen feed already emits, and the
  Frontend's kitchen wire validator rejects a `null` name by dropping the WHOLE feed.
  Nothing is trimmed or suffix-stripped either: a saved name containing `_autodel` is
  what the diner saw. **Provenance describes the NAME FIELD ONLY**, never money,
  options or allergens.
  **A BLANK PROVES NOTHING ABOUT WHY**: it does not mean the row predates
  `orders_app/0028` (which added the column with no backfill), and it does not mean
  the order has no intent key.
  **DO NOT BACKFILL SNAPSHOTS FROM THE CATALOGUE**. The snapshot columns are inside
  `order_quote`'s fingerprint, so rewriting a draft's saved name moves its `quote_ref`
  and its next submit answers `quote_ref_stale`. The live catalogue is also exactly
  the wrong source, and no trustworthy archive of historical names has been
  identified (which is not proof none exists).
  What is NOT a historical-name site, and deliberately stays live: `items[].item.id`
  and `is_special`, and the menu-performance report, which is a CURRENT-menu report
  by contract. The kitchen feeds already read the snapshot and are unchanged.
  **Consumer effect:** a blank quote name no longer pairs with a basket that knows
  the name (`quote-equivalence.ts`), so the diner gets the itemised review with an
  empty row name instead of the plain prompt. Nothing in the Frontend displays the
  provenance yet.
  **D12 STAYS OPEN, and these remain unfixed:**
  - Hard deletion of a purchased `MenuItem` is now refused by the ORM
    (`OrderItem.item` is PROTECT since B2, #360). Deleting an order or a line
    directly, raw SQL, and a code rollback to CASCADE are still outside it.
  - The kitchen shows `""` and `allergen_tags: []` for a blank legacy row. An unknown
    allergen list is NOT "no allergens".
  - There is no description snapshot.
  Pinned by `orders_app/tests_order_history_names.py` (22 tests over real
  endpoints, 14 of which fail on `b027e84`). Eight source mutations are each caught:
  a live name at any one of the six sites, the restored quote fallback, and a false
  `snapshot` provenance. Its detail-read query pin is 8, down from 10, because the
  extras no longer load their `MenuItem` for a live name. A live-name read in
  `get_extra_items` puts it back to 10, and the mutation run shows exactly that.
- `SerializerPublicGetMenuItem` includes `section` and `in_stock` —
  added deliberately for the diner menu. Do not remove them. It also emits
  read-only `is_discount_active` (bool) and `current_price` (effective base
  price, string) alongside `discount_percentage`, all gated on
  `is_discount_active()` (see Canonical Data Shapes)
- `SerializerPublicGetTableDetails` (diner QR table-scan) hand-builds its
  restaurant dict and now passes `socials` through raw, beside
  `branding_configuration` — do not drop it (no migration/EDIT_INFORMATION
  needed; `socials` is already a Secretary-editable `JSONField`). Its
  `get_current_order` delegates to `ConOrder.any_present_ongoing_order` — the
  occupancy gate the kitchen board and order-create path already share. A table
  is occupied iff it has a SUBMITTED order: `order_status != 'initiated'` (an
  `initiated` order is an unconfirmed draft that does NOT occupy — PR #210), not
  deleted, not cancelled, and `fulfilment_status != 'served'` — so a served order
  FREES the table for diner checkout instead of blocking forever on the stale
  payment axis (PR #186; diner payment is unwired, so `payment_status` never
  leaves `'pending'`). Do NOT regress `get_current_order` to the
  `order_status`/`payment_status` axis

## Existing Management Commands
- `optimize_images` in `restaurants_app/management/commands/` — resizes
  uploaded MenuItem images to 800px max. Do not recreate it
- `check_item_data` in `restaurants_app/management/commands/` — debugging
  helper that inspects MenuItem fields and can clean stray empty allergens
  entries (`--clean-allergens`)
- `reoptimise_menu_images` in `restaurants_app/management/commands/` —
  re-runs image optimisation across existing MenuItem images. Do not recreate it
- `create_platform_admin` in `platform_admin_app/management/commands/` — creates a
  platform-staff account for the admin control plane: prompts for the password
  INTERACTIVELY (never argv), enrols TOTP, prints the `otpauth://` URI + ASCII QR +
  ten one-time recovery codes. Refuses a duplicate username/email, a phone-number
  username, or a missing `ADMIN_SECRET_ENCRYPTION_KEY`. Requires a TTY
- `reset_platform_admin_totp` in `platform_admin_app/management/commands/` — the
  documented break-glass path: re-provisions the TOTP secret + recovery codes for an
  existing admin and revokes all of its sessions. Does NOT change the password, and
  never DECRYPTS (it re-provisions from scratch), so it survives key loss — but it
  does ENCRYPT the new secret, so a valid `ADMIN_SECRET_ENCRYPTION_KEY` must be
  installed BEFORE it runs. Accepts `--username` + `--noinput`. The four-step
  key-loss sequence (recovery-code sign-in → install a new key → re-provision →
  re-enrol) is in `BACKGROUND_TASKS.md` and is covered end to end by
  `platform_admin_app/tests_second_factor.py::BreakGlassSequenceTests`
- `mark_restaurant_test` in `platform_admin_app/management/commands/` — the ONLY
  writer of `Restaurant.is_test`. Sets or clears it for exactly ONE restaurant named
  by UUID (never a name, no bulk mode), attributed to an active `platform_staff`
  `--actor` and a `--reason` (same 10-char bar as a lifecycle transition), with the
  write and its `admin.restaurant.test_classification_changed` audit row in ONE
  transaction that takes `lock_admission_exclusive` FIRST and then the `Restaurant`
  row lock — the transition's exact lock order, and load-bearing: the row lock alone
  does not exclude an in-flight order admission, which reads `is_test` unlocked. Bidirectional and idempotent — a same-value
  rerun writes nothing and audits nothing. It does NOT rewrite history: existing
  `Order.is_test` rows are untouched, since classification governs what FUTURE orders
  derive at admission. **Classifying a restaurant TEST limits nothing** — a test
  restaurant can do everything a live one can, and its orders are merely flagged;
  what the classification changes is the FLAG on future orders, whether
  already-flagged orders count follows the restaurant's CURRENT classification
  (switching one back to real takes its test orders out of its figures — see the
  practice-order rule under `Order.is_test`), and Dinify's own portfolio and
  financial figures (Admin spec §11/§16, not built) are meant to leave a test
  restaurant out. There is still NO admin-plane write endpoint and no Admin UI
  for the flag, and no restaurant has been classified with the command yet
- `adopt_restaurant_onboarding` in `platform_admin_app/management/commands/` — the
  only writer of `legacy_adopted` onboarding provenance (the `admin_created` one is
  `POST admin/v1/restaurants/`). Represents exactly ONE pre-existing
  restaurant named by UUID as `RestaurantOnboarding(source='legacy_adopted')`,
  attributed to an active `platform_staff` `--actor` and a `--reason` (the same
  10-char bar as a lifecycle transition), with the row and its
  `admin.restaurant.onboarding_adopted` audit entry in ONE transaction that takes
  `select_for_update()` on the `Restaurant` FIRST — the serialization point — and NO
  admission advisory lock. A thin adapter: every rule lives in
  `platform_admin_app/onboarding_adoption.py`, which the future Admin endpoint calls
  unchanged. Idempotent (a rerun preserves the original `adopted_at`/`adopted_by` and
  writes no second audit row) and refuses an `admin_created` conflict. It creates no
  `OwnerInvitation`, attests no owner control, sends no email/SMS and never modifies
  the restaurant. There is no admin-plane endpoint and no Admin UI for adoption, and
  no restaurant has been adopted with the command yet
- `check_order_input_compatibility` in `orders_app/management/commands/` — the D01
  READ-ONLY preflight. Answers two separate questions: will `orders_app/0036` apply
  (any persisted `OrderItem.quantity < 0`, across ALL rows — soft-deleted, archived
  and vacuumed included, because a CHECK constraint covers them too), and will the
  catalogue still be orderable (stored modifier definitions the new normaliser
  refuses, plus requirements that cannot be met within the request ceilings). It
  reuses `inspect_modifier_definition` — the SAME pure function checkout uses — so
  it cannot hold a different opinion; definitions are inspected in Python rather
  than by a JSON expression in SQL for exactly that reason, and a
  `max(array_length)` aggregate would answer a question nobody asked. Two
  unsatisfiability classes are easy to miss and are reported explicitly: a
  REQUIRED group or choice whose stored id **no request can name** (the inspector's
  `usable_identifier` is deliberately broader than the request contract — an id
  only has to work as an id there — so an OPTIONAL group keyed by the integer `1`
  stays perfectly orderable and must NOT be flagged, while a REQUIRED one can never
  be submitted), and the **COMBINED** modifier + extras minimum against the single
  whole-request `MAX_SELECTION_ENTRIES_PER_REQUEST` ceiling — 32 groups requiring 64
  choices each sits exactly at it, so one required extra beside them puts the only
  satisfying request one entry over while each axis passes its own check. The
  identifier contract is PASSED IN (`is_submittable_identifier`, asserted by
  identity) rather than restated, so the preflight and the validator cannot drift. It separates
  **blockers** from **compatibility concerns** from **informational capacity**, says
  whether an affected item is orderable or a draft, streams rows in chunks so memory
  does not grow with the catalogue, and reports counts plus bounded samples of ids
  and stable reason codes — never catalogue JSON, order contents or personal data.
  Exit codes: 0 clean, 1 blocker, 2 concerns only, 3 INSPECTION INCOMPLETE (never
  reported as clean). **INCOMPLETE COVERS A PARTIAL PASS, NOT ONLY A FAILED ONE, AND
  IT DOMINATES 1 AND 2** — the extras axis is skipped past `MAX_TRACKED_UNPRICEABLE`,
  and reporting that run as `2` would be the more dangerous of the two available lies:
  a concern list reads as something an operator can work through to the end, when part
  of the pass that produced it never ran. A definite blocker from section 1 is still
  printed in full and named in the exit reason (`_exit` takes a contextual one for
  exactly that case), because one code has to be chosen and neither finding may be
  lost to the choice. Until the Codex review of PR #315 the skip was a stderr NOTE
  only, so such a run could print `CLEAN` and exit 0. It has NO fix/repair mode and performs no save, audit write,
  notification or provider call, and it runs against the PRE-migration schema so it
  can inform the deploy decision rather than only confirm it. A preflight is a
  point-in-time observation, not a substitute for the constraint. Invocation:
  `python manage.py check_order_input_compatibility [--sample-size N] [--chunk-size N]`.
  **IT NOW INSPECTS MONEY TOO (D02/C).** The structural pass says in terms that it
  "validates NO monetary configuration", so before this it could report a catalogue
  CLEAN while D02 refused items in it at checkout. It reuses `resolve_price`,
  `parse_money` and — the one that matters most — `modifier_adjustment`, all asserted
  BY IDENTITY, and reads an adjustment exactly as `con_orders` does
  (`choice.get('additionalCost', 0)`, **no `or 0`**): an `or 0` here was quietly more
  permissive than the thing the command exists to predict, calling a catalogue clean
  that checkout then refuses on a stored `None` / `''` / `[]` / `{}` / `False`.
  FIVE NAMED DISTINCTIONS, kept apart on purpose: an unpriceable LIVE item is a
  CONCERN while an unpriceable DRAFT one is INFORMATIONAL; a required group with
  **FEWER PRICEABLE CHOICES THAN ITS OWN MINIMUM** is a CONCERN (no variant of the dish
  is orderable) while an unreadable choice beside ENOUGH readable ones is INFORMATIONAL
  (reporting it would tell an operator to take a working dish down). **THE SHORTFALL IS
  MEASURED AGAINST THE REQUIREMENT, NEVER AGAINST ZERO** — `readable == 0` answered only
  the requires-ONE case, so a group requiring TWO with one readable choice beside one
  unreadable one was filed as informational even though every request meeting the
  minimum must name the unreadable choice and be refused (found by the Codex review of
  PR #315). The structural pass does not cover it: `MIN_EXCEEDS_DEFINED_CHOICES` fires
  only when the minimum exceeds the DEFINED choices, and the monetary check stays scoped
  to groups that actually hold an unreadable choice so it never double-reports a
  structural defect. And a possible NEGATIVE combination is a
  worst-case bound that IGNORES group maxima, so it is informational and says which
  selections refuse is decidable only at checkout. A required EXTRAS minimum that
  cannot be met from the priceable extras is its own concern, resolved by a SECOND
  streaming pass over `has_extras` parents rather than a per-item lookup, off a
  BOUNDED id set that reports itself incomplete past the cap. ONE observation time is
  captured for the whole scan — a discount window is time-dependent, and reading the
  clock per item would describe no single moment. Still read-only, still bounded,
  still no repair mode, and the report still carries no catalogue JSON
- `export_checkout_limits_contract` in `orders_app/management/commands/` — writes or
  checks `orders_app/contracts/checkout_limits.contract.json`, the source-authoritative
  export of the D01 request ceilings, derived from `order_input`'s own constants.
  `--check` exits non-zero when the committed file is stale; `--write` regenerates it;
  with neither it reports the path, the digest and the state and changes nothing. It
  is a CONVENIENCE, not the gate — the gate is
  `orders_app/tests_order_input.py::CrossRepositoryCeilingContractTests`, which asserts
  the file against the live constants unconditionally, so a ceiling changed without
  regenerating fails CI whether or not anyone runs this. It reads and writes exactly
  one file in this repository, contacts nothing, touches no database and makes no
  claim about any other repository's copy. The client repository selects this backend
  by commit through a peer receipt read from the committed file, so changing a ceiling
  also needs the client's copy and a new receipt approved there — an ordered, manual
  sequence nothing on this side enforces
- `export_published_capabilities` in `orders_app/management/commands/` — the same
  shape for the four capability levels this server publishes (`checkout_protocol`,
  `quote_protocol`, `kitchen_protocol`, `quote_policy_version`): writes or checks
  `orders_app/contracts/published_capabilities.contract.json`, derived from the very
  constants the wire emits. A convenience, not the gate — the gate is
  `orders_app/tests_published_capabilities.py`, which asserts the file against the
  live constants unconditionally. It exists because Dinify-Frontend's release gate
  compared a client's required levels against integers TYPED INTO ITS OWN POLICY;
  its peer-receipt producer now reads this file at an exact selected commit instead.
  It is a statement about SOURCE at a revision, never about what is deployed: there
  is still no runtime identity (B3), and the frontend gate refuses on that by name
- `unlock_platform_admin` in `platform_admin_app/management/commands/` — clears
  `failed_attempts`/`locked_until` for a platform-staff account under a row lock and
  audits `ADMIN_AUTH_LOCKOUT_CLEARED`. Does NOT touch the password, TOTP secret or
  recovery codes, and needs no encryption key. The narrow tool: do NOT reach for
  `reset_platform_admin_totp` to undo a lockout — it destroys the authenticator and all
  ten recovery codes. See the nuisance-lockout section of `BACKGROUND_TASKS.md`
- `prune_otp_accounting` in `users_app/management/commands/` — deletes D11 B2-C OTP
  ledger rows that are strictly older than the fixed seven days, any state, in bounded
  oldest-first batches. `--batch-size` 1–1000 (default 500), `--max-batches` 1–10000
  (default 100), and NO retention override; nothing schedules it. It prints one line per
  batch and a total per table, and every count printed has COMMITTED (it refuses to run
  inside a caller's transaction). It says `complete` only after a short batch AND a
  fresh bounded check found nothing eligible, and says so only for that moment; a short
  batch alone is not proof, because a concurrent cleanup can take the rows it selected.
  Otherwise it says `batch limit reached: eligible rows may remain`. On any failure it
  exits 1: the confirmed counts are printed first, the failed statement's outcome is
  called UNKNOWN, and the message carries a fixed category. The one failure with no
  statement to report on is opening the connection for its initial transaction-state
  check: that prints zero deletions and says no cleanup statement was attempted. No
  database text, key, id, timestamp or SQL is printed, and the exception is not chained
- Five more exist and are equally not-to-be-recreated: `vacuum_deleted_records` +
  `vacuum_configuration` (`misc_app`), `send_messages` + `send_test_sms`
  (`notifications_app`, the latter being the ENV-bypassing SMS credential probe
  described under SMS / Yo Uganda), and `determine-customers` (`orders_app`).
  `BACKGROUND_TASKS.md` is the full runbook — what each touches and where the
  operational gaps are

## Database
- `CONN_MAX_AGE: 600` for persistent DB connections — do not remove
- All migrations must be generated and included in PRs when models change
- MIGRATIONS MUST BE EXPAND-ONLY — CRITICAL. Rollback moves CODE backwards, never
  SCHEMA: the `workflow_dispatch` path re-deploys an older commit, but the deploy
  script has no `migrate --backwards` step at all (it runs a bare forward
  `manage.py migrate`), and its backward branch prints
  `WARNING: migrations already applied by newer code are NOT reversed` precisely
  because nothing reverses them. So a rollback lands OLD CODE ON NEW SCHEMA, and
  the schema is what has to tolerate it:
  - Every schema change must be backward-compatible with the immediately
    preceding deployed commit. If it isn't, application rollback cannot save you
    — the only remaining recovery is a hand-written forward fix under incident
    conditions
  - EXPAND FIRST, CONTRACT LATER, in separate PRs: add nullable columns, add new
    tables, dual-write — deploy — backfill — and only then drop, rename or add
    `NOT NULL`, once the expanded state is live and proven
  - NEVER rename or drop a column in the same PR that stops reading it. The old
    code is still one rollback away from selecting it
  - This is not theoretical: the 2026-08-10 deliberate backwards deploy from
    `f8acc50` to `010fd8dd` succeeded *because no migration sat between them*.
    That property is what this rule preserves — see the ROLLBACK / MANUAL
    REDEPLOY bullet under "Deployment Rules — CRITICAL"
- A TEST THAT REWINDS MIGRATIONS RESTORES THE WHOLE PROJECT GRAPH (#353). Rewinding one
  app also unapplies every migration in other apps that depends on it, and a
  `TransactionTestCase`'s DDL is not rolled back, so a tearDown that restores only its
  own app leaves the rest unapplied for every later test (the 0055 sanitize test did
  exactly that and broke an owner-claim race test ordered after it). Restore
  `executor.loader.graph.leaf_nodes()` with NO app argument.
  `dinify_backend/tests_migration_test_isolation.py` enforces it: it finds test modules
  the way Django's default runner does (`test*.py` from the repository root, recursing
  only into packages), requires every one that drives `MigrationExecutor` to be listed,
  checks each listed tearDown restores the whole graph, and runs each listed class to
  prove nothing is left unapplied
- Latest migration: `restaurants_app/migrations/0058_table_qr_mode_order_only_default.py`
  (0058 is MODEL-STATE ONLY — one `AlterField` moving `Table.qr_mode`'s default
  `order_pay` → `order_only`, with NO `RunPython`, no row rewrite and no backfill:
  every existing table keeps its stored value, `order_pay` stays a legal choice and
  stays orderable, so a rollback is a TRUE INVERSE that restores the old default for
  FUTURE rows and changes no existing one — see the D07 bullet;
  0054 adds `Table.qr_version`; 0055 data-repairs MenuItem extras — see the
  "Write-time menu relationship integrity" bullet; 0056 constrains
  `Restaurant.status` and fail-closed-maps the legacy vocabulary — see
  "Restaurant Lifecycle"; 0057 adds the platform-owned `Restaurant.is_test` flag,
  additive with NO backfill — see "Canonical Data Shapes"),
  `orders_app/migrations/0042_alter_orderitem_item.py` (0034 removed the
  inline review fields; 0035 adds the launch-boundary `Order.is_test` flag; 0036
  adds the D01 `quantity >= 0` CHECK constraint, additive and reversible with NO
  `RunPython`; 0037 adds `Order.pricing_version`, one `AddField` carrying BOTH
  `default` and `db_default` so an INSERT from rolled-back code stays valid, plus an
  index — the index build is proportional to the table's row count, which this
  repository cannot observe, so do not describe the deploy as instantaneous; the
  migration documents the `AddIndexConcurrently` alternative — see the D01/D02 bullets
  in Current Implementation Status; 0038 adds the D04 `Order.request_fingerprint`,
  NULLABLE with no `db_default` — sufficient here, unlike `users_app/0014`, because
  old code INSERTs without naming the column and a nullable column accepts that —
  and with NO backfill and none possible: a pre-D04 order has no record of the
  request that created it, so NULL means exactly "predates D04". Its `db_index=True`
  is NOT free; the index build is proportional to the row count of `orders`, which
  this repository cannot observe; 0039 creates the D04/C `OrderAcceptance` table and
  NOTHING else — no existing table touched, no `RunPython`, no backfill and none
  possible, since an order accepted before it left no record of when or against which
  quote. `CreateModel` is the safest shape under the expand-only rule: old code
  neither reads nor writes the table, and its index is built on an empty one;
  0040 adds the D05 `Order.fulfilment_revision`, ONE additive `AddField` carrying
  BOTH `default=0` and `db_default=0` — load-bearing, because a rollback lands
  OLD CODE on the NEW schema and old code INSERTs orders without naming the
  column. `ADD COLUMN ... DEFAULT 0 NOT NULL` does not rewrite the table on
  PostgreSQL 11+, but it still takes a brief ACCESS EXCLUSIVE lock to update the
  catalogue and therefore WAITS behind any open transaction on `orders`; that
  wait is a property of the deployment, which this repository cannot observe, so
  do not describe it as instantaneous. Deliberately NO index — nothing filters or
  orders by it; 0041 creates the D06 `OrderQuoteClosure` table and NOTHING else
  — no existing table touched, no `RunPython`, no backfill and none possible,
  since a draft predating it was never closed under this protocol and an absent
  row means "unknown, or not closed", never "safe to replace". `CreateModel` is
  the safest shape for the SCHEMA under the expand-only rule — old code neither
  reads nor writes the table and its index is built on an empty one — but **the
  BEHAVIOURAL direction needs an operational decision rather than a revert**:
  old code does not consult closures, so while it runs, a draft this build has
  permanently closed could be accepted by it. Prefer a forward fix; hold a
  rollback across this change; 0042 (D12 B2) is one generated `AlterField` making
  `OrderItem.item` PROTECT. It changes Django's model state only: `sqlmigrate`
  prints a no-op both ways, the constraint stays NO ACTION DEFERRABLE INITIALLY
  DEFERRED, and no row is touched. A code rollback past it is schema-compatible
  but brings CASCADE back),
  `finance_app/migrations/0028_remove_dinifytransaction_tip_amount.py`,
  `reviews_app/migrations/0003_review_tags.py`,
  `users_app/migrations/0015_otp_accounting.py` (0010 adds
  `User.account_type`; 0011 flips existing platform-role holders to
  `platform_staff`; 0012 makes `phone_number` unique — see the "Platform-admin
  identity layer" bullet; 0013 blacklists outstanding platform-staff refresh
  tokens and strips platform-only roles from `restaurant_user` rows, data-only and
  idempotent — see "Tenant Isolation / Role-Permission ENFORCEMENT"; 0014 adds the
  Step-2D.1 `customer_access_state` gate, one `AddField` plus its vocabulary
  `AddConstraint`, NO `RunPython` — see "Pre-Claim Customer Access"; 0015 follows
  0014 and creates the D11 B2-C OTP ledger, two `CreateModel`s and nothing else — no
  existing table touched, no `RunPython`, no backfill. A rollback leaves the tables and
  rows in place and stops collection and automatic cleanup),
  `platform_admin_app/migrations/0009_restaurantonboarding_ownerinvitation_and_more.py`
  (0001 identity, 0002 `AdminSession`, 0003 `AdminAuditLog`, 0004 TOTP replay counter,
  0005 `DelegationGrant`, 0006 `DelegatedSession`, 0007 the break-glass
  `recovery_only` challenge flag, 0008 the one-live-challenge partial unique index
  preceded by an idempotent duplicate-consuming data repair — see "Platform-admin
  control plane"; 0009 creates the onboarding domain, two new tables with their
  constraints and NOTHING else — no `RunPython`, no backfill, no existing table
  touched — see "Admin Onboarding Domain"),
  `misc_app/migrations/0004_drop_service_tickets.py`,
  `commercial_app/migrations/0001_initial.py` (the Step-3B commercial domain: two
  new tables with their constraints and one index, NO `RunPython`/`RunSQL`, no
  existing table touched, zero rows created — see "Commercial & Service
  Configuration Domain")

## CI — `.github/workflows/ci.yml`
- Runs on push to `main` and on PRs to `main`
- Runs a SINGLE-LEG Python matrix in `ci.yml`, pinned to **`3.12.3`** — the live
  EC2 interpreter. The interpreter migration is DONE: the box was rebuilt on
  Ubuntu 24.04 / Python 3.12.3 and cut over 2026-08, and the temporary `3.10.12`
  leg was deleted once it no longer matched anything (PR-B), as the dual-leg
  comment it carried had instructed:
  - The pin exists to REPRODUCE PROD, so a version-specific issue can't pass CI
    and then fail at the live WSGI import. That is a feature-and-behaviour
    mirror, not a byte-identical one: `actions/setup-python` installs an upstream
    CPython while the box runs Ubuntu's package with security patches backported
    under the same version string. The old `3.10.12` pin had exactly the same
    property — this is not a new gap
  - THE PIN AND THE BOX MOVE TOGETHER, in the same PR as the venv rebuild.
    Bumping the pin alone means CI tests an interpreter that serves no traffic;
    rebuilding the venv alone means prod runs an interpreter nothing tested.
    Note the deploy pipeline CANNOT rebuild the venv or `mod_wsgi` — that is
    hands-on host work, which is why the two can drift if nobody couples them
  - `.github/workflows/audit.yml` pins the SAME value independently, and so does
    `dependency_audit/policy.json → target.python` (D08 B2.1: the audit refuses any
    other interpreter as not the validation target). All three must be changed
    together — `dependency_audit/tests_workflow.py` fails if they differ — and they
    are the only places the interpreter version is asserted (no `setup.py` /
    `pyproject.toml` / `tox.ini` exists, and `requirements.txt` declares no
    `requires-python`). Dependency bumps must satisfy `requires-python <= 3.12`
  - The matrix job is keyed `suite`, so its leg reports as `suite (3.12.3)`.
    Branch protection on `main` requires a check literally named `test`, which a
    matrix job can NEVER produce (it always suffixes the leg value — collapsing
    to one leg did not change that, since the suffix comes from HAVING a matrix,
    not from having several) — so the `test` job is an AGGREGATOR: `needs:
    [suite]`, `if: always()`, failing unless every leg succeeded. It is the
    single required check, and that is what lets the matrix change — add, remove
    or re-pin a leg, as deleting `3.10.12` at cutover did — with NO repo-settings
    change. Do NOT rename the `test` job, drop the aggregator, give it
    `continue-on-error`, or flatten `suite` out of the matrix construct; `if:
    always()` is load-bearing, because a SKIPPED required check never reports a
    failure and would block merges on a pending check instead of failing loudly
  - Note: Django 5.2.x is the LAST series supporting Python 3.10/3.11, so the
    3.12 move is also what unblocks a future Django 6.0 upgrade
- Spins up a real Postgres 15, runs `django check`,
  `makemigrations --check --dry-run`, then the money-field guard
  (`scripts/check_money_fields.py`) against `dinify_backend.test_settings`
- Then the ambient-authority gate (`scripts/check_ambient_authority.py`,
  TENANT-AUTH-00): no customer-plane production module may reference the retired
  role-based admin predicates/constants, hard-code a platform-only role string, or
  select users by one through a `roles__*` ORM lookup. Unlike the ratchet below
  this is a flat zero-tolerance check with an EMPTY allowlist, and it is PERMANENT
  — there is no future state in which reintroducing the mechanism is acceptable.
  Scope + the four documented exclusions live in
  `dinify_backend/tenancy/ambient_authority.py`
- Also runs the tenant-relation ratchet (`scripts/check_tenant_relation_ratchet.py`,
  TENANT-STRUCT-00): the `dinify_backend/tenancy/baseline.txt` of not-yet-classified
  writable serializer relations may only SHRINK (PR base or, on push,
  `github.event.before` — not the branch tip). It proves conscious CLASSIFICATION,
  not tenant isolation; the baseline count is NOT a vulnerability count. See
  `dinify_backend/tenancy/ASSURANCE.md` for the exact assurance boundary and
  `non_fk_tenant_inventory.py` for tenant refs outside DRF relations
- **THE THREE GUARDS ABOVE ARE QUALIFIED, AND AN INCOMPLETE SCAN IS NEVER CLEAN (D08
  B2.3).** Each CLI runs its own `--self-test` INSIDE its default invocation, before
  the scan, so editing the `ci.yml` line cannot bypass it; the commands and step names
  are unchanged. Shared exit contract: **0 complete and clean · 1 violation · 2
  INCOMPLETE · 3 self-test failed**. What was reproduced on `d4aacbd` and is now closed:
  the money guard reported `OK — scanned 0` for an empty or unlistable scope and OK for
  an unparseable `models.py`; the ambient gate returned no violations for an
  unparseable module "for `django check` to report" — but `django check` never
  imported 46 of the 244 modules it scanned (`wsgi_admin.py`, `settings_admin.py`,
  `urls_admin.py`, `lifecycle.py`, management commands …), so a `wsgi_admin.py` that
  reintroduced `is_dinify_admin` and failed to parse passed BOTH; and the ratchet
  (a) compared a push with ITSELF when `GITHUB_EVENT_BEFORE` was not plumbed (it fell
  back to `main`, which on a push is the pushed commit), (b) read a MOVED baseline
  (file and `BASELINE_PATH` moved together, an entry added) as "bootstrap" and passed
  in CI, and (c) crashed with a traceback when the baseline file was deleted. Now, in
  CI, every state in which nothing was compared exits 2; a genuine bootstrap (the base
  predates `scripts/check_tenant_relation_ratchet.py`) and a local run that cannot
  compare still exit 0 but print `NO COMPARISON PERFORMED`; a missing baseline is
  INCOMPLETE everywhere and ZERO debt is an empty file. The ratchet's CI refusal for an
  unreadable base moved from exit 1 to 2 (still a failure; the oracle in
  `tests_relation_classification.py` was corrected, not relaxed), and so did the
  ambient test that pinned the silent syntax-error skip. `scripts/tests_guards.py`
  (30 tests, offline, ~4 s, its own `Guard qualification tests` step before the long
  suite) drives the REAL CLIs against disposable trees and real git repositories with a
  LOCAL bare origin, executes the committed step text under the runner's shell, and runs
  `verify.sh` itself with every non-guard step stubbed green. `scripts/` became a
  package only so that suite is discoverable; every guard still runs as a plain script
- Then runs the full Django test suite — a missing migration or a model
  change without a generated migration will fail CI
- **AND THEN THE DEPENDENCY AUDIT, inside the same `suite` leg (D08 B2.1 —
  `dependency_audit/README.md`), so the `test` aggregator cannot go green without it.**
  `python -m dependency_audit snapshot` runs directly after the environment is installed
  and records THIS interpreter's installed inventory (`pip inspect`, per-package RECORD
  digests — 27 packages on main, including the `cffi` / `pycparser` transitives and
  `pip`; since D08 B2.5 that environment is the certified one built from the lock, see the
  next bullet); the offline evaluator matrix runs with
  the other gates; and `self-test && audit` runs LAST, scanning exactly that inventory as
  exact pins (`pip-audit --no-deps --disable-pip --strict`) plus the scanner's own venv
  (29 hash-pinned packages, `--require-hashes --only-binary=:all: --isolated`). The
  policy is shared with Frontend and Admin (`conformance.json` byte-identical, digest
  pinned in each suite): four outcomes, `within_policy` 0 / `exceptions_only` 0 /
  `blocking` 1 / `incomplete` 2. **pip-audit reports no severity**, so a Python finding
  on a runtime package blocks and one on `pip` is incomplete — never read as low. A
  failed scan is never clean: measured, PyPI unreachable makes pip-audit exit 1 with EMPTY
  stdout (the same status as "vulnerabilities found"), so the status is only accepted when
  the body agrees. `policy.json → records` is empty — nothing is pre-approved. Evidence is
  uploaded as `dependency-audit-<python>-<run>-<attempt>`, pass or fail. It audits the CI
  environment, NOT the live UAT venv, which the deploy re-installs independently
- **THE `suite` LEG NOW CERTIFIES A RETAINED RELEASE CANDIDATE, AND `test` REQUIRES AN
  INDEPENDENT OFFLINE RECONSTRUCTION OF IT (D08 B2.5 — `release/README.md`).** Nothing is
  installed from an index any more: `release/python-lock.json` is the reviewed,
  target-specific (CPython 3.12.3 / x86_64 / glibc 2.39, `runs-on: ubuntu-24.04`),
  hash-locked closure of `requirements.txt` — 26 application wheels plus pip 26.2.1 as a
  SEPARATELY labelled bootstrap installer — and `requirements.txt` stays byte-unchanged as
  the direct-input contract. The leg runs `lock check` (a changed `requirements.txt` with
  the old lock is `stale_lock`, even a comment edit; certification never regenerates a
  lock — `python -B -m release lock generate` is a reviewed local change), `observe` (every
  tracked file's bytes and exec bit equal HEAD's blobs — catching `skip-worktree` edits
  `git status` hides — and nothing untracked), `acquire` (NETWORK, the lock's exact
  `files.pythonhosted.org` addresses, admitted on size + sha256 only), `install`
  (OFFLINE: `venv --without-pip`, the pinned pip installs itself from its own wheel, then
  `--isolated --no-index --require-hashes --no-deps --only-binary=:all:` under a scrubbed
  env, `PIP_CONFIG_FILE=/dev/null` and a dead proxy, then RECONCILED: installed set ==
  lock, `Requires-Dist` closure == the application packages, every wheel's RECORD against
  its own bytes and every installed file against the wheel, no unowned files, `pip
  check`), and puts that venv first on `PATH`, so the snapshot, every guard, the suites
  and the audit run IN it. **`pip install --upgrade pip` and `pip install -r
  requirements.txt` are gone from `ci.yml`, and the setup-python pip cache with them.**
  After everything passes, `package` (no `if:`; re-checks `toJSON(steps)` against
  `release/candidate.py::REQUIRED_STEPS`, which a test holds equal to the leg's step ids)
  re-observes, re-reconciles, re-decides the audit from its raw output, exports `git
  archive` of the commit and requires it to hash to the tree, and writes `record.json`
  (`dinify.backend.candidate/1`, no self-digest) beside `source.tar`, `wheelhouse/` and
  the nine audit files. Only a push to main is PROMOTABLE (`backend-candidate-<run>-<attempt>`);
  a PR yields `backend-candidate-nonpromotable-…`; promotable means eligible to be
  CONSIDERED later and authorizes nothing. The new `reconstruct` job (`contents: read`, no
  credential) checks out its OWN consumer, receives the candidate as data and refuses it
  unless identity, the tree RECOMPUTED FROM THE ARCHIVE against its own git, the lock,
  every wheel and the re-decided audit all hold — before anything runs — then rebuilds
  offline, requires the same portable environment digest, and starts both planes with
  disposable settings. `test` needs `[suite, reconstruct]`; a skipped reconstruction is
  not success. **The Django runner discovers `test*.py`, so the two heavy release suites
  are `release/qualify_*.py`** and run in the "Release-candidate tests" step, not twice.
  **`deploy-uat.yml` IS UNTOUCHED AND CONSUMES NONE OF THIS** — it still runs `pip install
  -q -r requirements.txt` on the box, so the live venv has NOT acquired the candidate's
  exact-package guarantee (a test pins that statement). Measured on the way: pip's
  `--isolated` still reads `PIP_CONFIG_FILE` and the global/site `pip.conf`; the B2.1
  scanner install has that exposure to the global file and is recorded, not changed
- **`test_settings.py` sets `PASSWORD_HASHERS` to `MD5PasswordHasher`, and that is
  DELIBERATE — do not "fix" it.** Django 5.2's default `pbkdf2_sha256` runs 1,000,000
  iterations (~258ms per hash) and the suite builds fixtures per test method (159 `setUp`
  vs 2 `setUpTestData` across ~1,817 tests), so the default cost was paid hundreds of times
  over for no test value: nothing in the repo asserts on the hash algorithm, prefix or
  iteration count, and there are no pre-hashed fixtures. Measured on `users_app` alone
  (146 tests): 344.7s → 5.6s. **TEST SETTINGS ONLY — `settings.py` declares no
  `PASSWORD_HASHERS` and keeps the Django default; never add this there.** It will look
  like a finding to a security sweep, which is exactly why it is written down here
- Both `django test` invocations (the tenant-isolation gate and the full suite) pass
  `--timing`, in `ci.yml` and `verify.sh` alike, so the remaining runtime stays
  attributable. The two files mirror each other exactly — change them together
- `scripts/verify.sh` is the committed source of truth that runs the same
  checks locally in the same order; the `/dinify-check` command defers to it.
  Run it and paste the output before raising a PR
- A separate `.github/workflows/deploy-uat.yml` deploys to the UAT host — NOT on
  push, but on `workflow_run` when this "Backend CI" workflow completes
  successfully on `main`, dispatched over SSM/OIDC and pinned to the exact SHA
  that CI certified (see Deployment Rules above; never pull/migrate/restart
  manually). Two coupling notes: renaming the `Backend CI` workflow silently
  breaks the deploy trigger, which matches on that literal name, and the
  `workflow_dispatch` rollback pre-flight queries `ci.yml`'s runs by path
- A scheduled `.github/workflows/audit.yml` is a weekly (Mondays 06:30 UTC) + manual
  `workflow_dispatch` RE-SCAN of main with the SAME evaluator and policy — the enforcing
  audit is inside `suite`; this exists because advisories are published between merges.
  It is not a required check, and it authorizes nothing: deploy-uat.yml triggers on the
  workflow named "Backend CI" and its manual path re-verifies `ci.yml` runs by path. A
  failure (blocking, or a scan that could not complete) fires GitHub's
  scheduled-workflow notification. It used to run `pip-audit -r requirements.txt` with an
  unpinned scanner — an independent resolution, not the validated inventory
- **A NON-DEPLOYING PREFLIGHT RE-ASKS THE ADVISORY QUESTION OVER ONE RETAINED CANDIDATE
  (D08 B2.6 — `release/README.md` → "The preflight").** `.github/workflows/preflight.yml`
  runs after a successful "Backend CI" push to main (`workflow_run`), or on
  `workflow_dispatch` with an exact `sha` and an optional run and attempt. It runs three
  commands of `release/preflight.py`:
  - `facts` (READ TOKEN, no scanner): the run, that attempt's jobs, the artifact listing,
    the commit, main and the trusted trees from the API; the zips BY ID, unopened.
  - `assess` (NO TOKEN): select, admit each zip by the listing's digest, B2.5
    consumer-verify, bind the reconstruction, query advisories now, decide.
  - `verify` (NO TOKEN), a separate job: the receiving side.

  **CERTIFICATION IS READ FROM GITHUB, NEVER FROM THE CANDIDATE.** The candidate uploads
  before `reconstruct` and `test` run, so `promotable: true` proves nothing. The preflight
  requires:
  - the workflow by id AND path `ci.yml`, a completed successful push to main for the
    exact commit, and ancestry;
  - `suite (3.12.3)`, `reconstruct` and `test` all successful IN THE SAME ATTEMPT. A
    partial re-run carries earlier attempts' jobs and is `certification_mixed_attempt`
    (re-run ALL jobs). `REQUIRED_JOBS` is held equal to `ci.yml`'s names by a test;
  - a complete artifact listing with exactly one candidate and one reconstruction of that
    attempt, unexpired;
  - the reconstruction report bound to that exact record. Manual runs with more than one
    green run for the commit are `certification_ambiguous`.

  **THE QUERY IS OVER THE RETAINED INVENTORY.** It uses the certification snapshot's
  `name==version` set, bound to `record.environment.auditInventorySha256`, with
  `--no-deps --disable-pip --strict`. Nothing is installed, resolved or built, and no
  candidate code runs. Its other rules:
  - the scanner is installed now from the TRUSTED hash-pinned requirements, with
    `PIP_CONFIG_FILE=/dev/null`, a fresh cache per graph, and no runner tokens or
    step-output files;
  - **THE TRUSTED POLICY DECIDES**, never the candidate's. A certification-time exception
    does not survive, and the original audit is kept as labelled history;
  - the window is 24 hours from the evaluation's START, cut short by any applied record;
  - a verifier or policy that main has moved past is refused, and needs a NEW evaluation;
  - there is no clock or scanner override in the CLI.

  The result is `dinify.backend.preflight/1`, and every one says
  **`deploymentAuthorized: false`**. `verify` re-derives every fact, reproduces the
  decision from the raw output under its own trusted policy, bounds the times by GitHub's
  own run start and upload time (2-minute skew), and refuses anything within 30 minutes of
  the deadline. It is the contract for B3. **Nothing consumes it yet, and `deploy-uat.yml`
  is unchanged — the preflight cannot protect the live deploy.** Backend CI becomes
  stricter only through the added tests (`release/tests_preflight.py`, and
  `release/qualify_preflight.py` at ~90 s, both picked up by the existing release-tests
  patterns). `release/testing.py::bundled_pip` also gained a fallback to
  `/usr/share/python-wheels`, the Debian/Ubuntu ensurepip location, so the qualification
  suites run on a system Python. CI's setup-python build already bundles pip.

  **MEASURED, with the synthetic parts named.** The real public-API facts for main's run
  `36261585225`/1 select cleanly, but the artifact's BYTES were unreachable here (egress
  refuses GitHub's artifact blob host). So a genuine local candidate of the same commit
  was built:
  - every suite-leg step was run for real on setup-python's CPython 3.12.3, and 4,883
    Django tests passed;
  - its environment digest `191df5a2…80213` is identical to the real run's.

  Then real `assess` ran (real hash-pinned scanner, real PyPI queries, 27 + 29 packages,
  within policy, ~17 s), and real `verify` received the result. Only the GitHub
  provenance around it was synthetic. `release/README.md` → "Measured at delivery" has
  the identities and controls.

## Verification
Before raising any PR, run `./scripts/verify.sh` (mirrors CI) and confirm:
1. Confirm migrations are generated for any model changes (CI enforces this)
2. If this PR adds a migration, confirm it is expand-only and backward-compatible
   with the deployed commit (see ## Database)
3. Confirm new editable fields are added to EDIT_INFORMATION
4. Confirm no synchronous SMS calls introduced (except where return value needed)
5. Confirm no new hard MongoDB dependencies
6. Confirm monetary fields use DecimalField
7. If adding a new endpoint, confirm it's registered in urls.py above the catch-all
8. Confirm no reintroduction of `clear_<field>` sentinels in PUT payloads
9. Confirm the dependency audit passes (`verify.sh` runs it last; it needs network and
   the Python 3.12.3 target, and refuses — fails — otherwise)
10. If `requirements.txt` changed, regenerate and REVIEW `release/python-lock.json` in
   the same PR (`python -B -m release lock generate`, on the 3.12.3 target) —
   `verify.sh`'s first step and CI's first release step refuse a stale lock
