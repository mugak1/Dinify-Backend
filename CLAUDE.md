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
  writes only the fulfilment axis
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
  control, and claims nothing about them
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
- Order-path READ BUDGET: ✅ (PR-H §4, tightened by D02) — the per-line cost inside
  `_create_order`'s transaction is **1 query** (the INSERT, and nothing else); a
  4-line order runs **22** and a 1-line order **19**. The ladder, measured on one
  fixture across every pass: 4-line 54 → 35 (D01) → 23 (D02) → 22 (the allergen read
  folded into the snapshot); 1-line 27 → 23 → 20 → 19; per-line 9 → 4 → 1. **The most
  recent repin went DOWN, and it went down BECAUSE of the coherence fix** — folding
  two statements into one is what removed both the half-observed snapshot and the
  query. Pinned by
  `orders_app/tests_order_path_queries.py` with EXACT counts plus a flatness case,
  so a re-introduced N+1 fails CI. What made the difference: `add_order_item` takes
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
  regenerate-qr response. Both tokens
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
    `X-Delegation-Session` / `X-Delegation-Code` headers
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
  else. **It is a PREFLIGHT: no lock, no transaction, nothing durable written** — the
  invitation, the access state and the password are all untouched, and `owner_control`
  stays `not_established`. Step 2F.2 (the atomic consume) HAS SINCE LANDED — see the
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
  `summary/` + `analytics/` (owner/manager analytics),
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
  `test_settings.py`. The subscription flow is record-only —
  `tx_subscription.initiate()` writes a Pending `DinifyTransaction` and stops (no
  provider call). The PSP adapter will be designed FRESH per the non-custodial
  Pattern A when the counsel and PSP integration gates clear.
  REPO-CLEAN IS NOT HOST-CLEAN: the 2026-07-29 host reconnaissance
  (`REGULATORY_AUDIT.md` APPENDIX, finding H2) found the DPO / Flutterwave / Yo
  payment credentials sitting in a `root:root` mode-644 backup `.env` on the
  production box — written 2026-03-17 and still there at the recon, having
  survived the teardown that removed them from the tree. Mode 644 made it readable
  by any local account including `www-data`, the account the app itself runs as.
  Deleting a file does not revoke a credential — provider-side revocation is
  recorded there as OUTSTANDING
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
  not a measurement (flip it in the same PR that lands PSP). dashboard-v2's
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
  were DELETED 2026-08-18 with the old-host teardown; no workflow references
  `secrets.` at all — OIDC needs no stored credential
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
  leaving the live API up on the old workers
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
- `api/v1/health/` → `misc_app/urls.py` → `HealthCheckView`
  (`misc_app/endpoints/health.py`) — `AllowAny` with `authentication_classes = []`,
  answering `{status, database, timestamp}` after a `SELECT 1`. It is DEPLOY-
  LOAD-BEARING (the UAT DB-connectivity gate parses it), so keep the three keys
  and their values stable. It deliberately answers **HTTP 200 even when the
  database is unreachable** (`status: degraded`, `database: unreachable`) — a
  consumer must read the body, never the status code. Distinct from the admin
  plane's own `admin/v1/` health route
- `api/v1/orders/` → v1 orders (urls.py) — only `submit` (PUT) is live; the
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
- `api/v1/kitchen/` → Kitchen endpoints (urls_kitchen.py) — separate file
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
  METHOD, and the ONLY gate in `tx_subscription.initiate` — `== 'per_order'` →
  refuse). The restaurant-setup write path STRIPS BOTH keys from EVERY
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
  `reset_password._resolve_user` both call `User.objects.get(email=...)`, so a
  duplicate would break email login AND password reset with a **500 for both users**.
  `update_user_profile` already refuses an email change for exactly this reason. Do
  not let this policy mutate into "email identifies the owner".

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
| password reset | `reset_password._resolve_user` — guards BOTH stages at once | generic `NO_PHONE_NUMBER` |
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
**It takes NO lock and opens NO transaction.** The challenge grants no durable
authority, so a snapshot is enough for it. If the invitation is reissued, cancelled,
expires or ownership drifts a millisecond later, the only consequence is an OTP that
Step 2F.2 will refuse to honour — harmless.

Holding the `Restaurant` row across delivery would not be. PR #306 measured the cost:
a lifecycle transition waiting on that row holds the EXCLUSIVE admission advisory lock
while it waits, and every diner order at the restaurant queues behind it. **A harmless
stale OTP is always preferable to external I/O under the ownership serialization
lock.** Pinned structurally, not by timing: `no transaction is open when the OTP is
sent` (and no open transaction means no held row lock — a `select_for_update` in
autocommit is released by the statement that took it), no `FOR UPDATE` in any query,
and the membership barrier is never acquired.

### THE `owner-claim` OTP PURPOSE
Exactly that spelling. **NOT in `CUSTOMER_AUTH_OTP_PURPOSES`** (`login`,
`reset-password`) — those are the two flows that can end in a customer session or
password, and a `pending_initial_claim` identity is refused them. This purpose must
REACH a pending identity: that is the one identity it exists for, as
`otp_manager`'s own docstring anticipated. `login`/`reset-password` gating is
unchanged, and a verified owner-claim code mints nothing — `verify_otp` mints only
for `purpose == 'login'`. It is EVIDENCE Step 2F.2 will consume.

### CURRENT CROSS-PURPOSE OTP SEMANTICS — RECORDED, NOT CHANGED
`make_otp` deletes prior challenges with
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
canonical phone. Both default to `None` and change nothing for the four pre-existing
callers (`self_register`, `reset_password`, the `verify-otp` endpoint,
`create_employee`), which is pinned by exercising the primitive rather than by reading
its signature.

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
should go now* — because `make_otp` deletes by `(user, msisdn)`, so a fresh challenge to
a new number leaves the old row live beside it. It is not an oracle (the response is
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
OwnerInvitation` is what `onboarding_invitations` already takes; `UserOtp` is locked by
nothing except `verify_otp`; `User` is taken AFTER `Restaurant`, the same direction as
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

### CURRENT OTP REPLACEMENT SEMANTICS — CHANGED, AND STATED HONESTLY
Passing an explicit `msisdn` moves the owner-claim row out of the `msisdn IS NULL`
bucket that `login` and `reset-password` share, so the cross-purpose collision recorded
under Step 2F.1 changed in BOTH directions and improved in both: an owner-claim
challenge no longer destroys a live login or reset code, and a login or reset attempt no
longer destroys a live owner-claim challenge (the load-bearing direction — otherwise
requesting a login code would kill the claim challenge mid-flow). **The purpose-blind
delete itself is UNCHANGED**: `login` and `reset-password` still share the NULL bucket
and still replace one another. This is not the broad purpose-scoped migration.

One interaction survives and is not a regression: two live rows for one identity can now
coexist, and a purpose-BLIND `verify_otp` still picks the most recent, so a newer
owner-claim challenge shadows an older login code at the generic verify endpoint. That
is the same user-visible outcome as before (the row used to be deleted outright), and
coexistence already occurred on `origin/main` via `resend_otp`, which has always passed
`msisdn=user.phone_number`. Redemption is immune — it binds purpose AND destination.

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

## Monetary Fields — CRITICAL
- ALL monetary/financial fields must use `DecimalField`, never `FloatField`
- Never use `Decimal(float)` conversions or `int()` truncation in
  payment or financial logic
- A committed static guard (`scripts/check_money_fields.py`) fails CI if any
  `models.py` declares a monetary `FloatField`. It scans `models.py` files
  only — migrations are never scanned (historical money FloatFields there are
  immutable) — and matches whole underscore-tokens against monetary terms

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
  Admin UI. Two consequences: it surfaces on the admin directory/detail reads, and it
  feeds `Order.is_test` below
- `Order.is_test` (migration `orders_app/0035`, indexed) marks an order that is
  operationally real but commercially invisible. It is **SERVER-DERIVED, NEVER
  CLIENT-SUPPLIED**, in `_create_order`, and it is now TRUE under either of two
  independent conditions:
  1. **TENANT** — `verdict.restaurant_is_test`: the restaurant is flagged a test
     tenant, so it never produces commerce in any lifecycle state
  2. **LIFECYCLE** — `not orders_are_commercial(verdict.status)`: the classic
     PRE-GO-LIVE REHEARSAL case, an order placed while still `onboarding`
  BOTH values come from the `AdmissionVerdict`, which reads `status` and `is_test` in
  ONE query under the shared advisory lock (`order_admission.admit`) — never from the
  caller's possibly-stale `Restaurant` instance. That is the same authoritative-moment
  rule the lifecycle half already followed, extended to the tenant flag; reading the
  flag off an earlier instance would reintroduce exactly the drift the lock was taken
  to prevent. **No extra `Restaurant` row lock was added** — the flag rides the
  existing `values_list`, so the order path's pinned query counts are unchanged.
  There is no request field for either input and there must never be one. The governing rule: **a test order is
  operationally real and commercially invisible** — it occupies its table, reaches
  the kitchen board and is served/cancelled normally, but is excluded from
  `sale_filters.sale_orders()` (the chokepoint that sales/diners/menu inherit), both
  dashboards, `summarize_revenue`, the transactions report (via
  `Q(order__isnull=True) | Q(order__is_test=False)`, so order-less subscription rows
  survive), and `determine-customers` (which MINTS REAL USERS); and it cannot be
  reviewed (`submit_review` refuses it, because review analytics aggregate on the
  denormalised `Review.restaurant` and would never see an `is_test` filter). The
  DELIBERATE inclusions are dashboard-v2's `_build_kds` and the OCCUPANCY queryset
  inside `_build_tables` — live floor state, which must agree with the kitchen board;
  note `_build_tables` is split, so its median-visit / turns / avg-ticket metrics DO
  filter `is_test=False` (history and money). `has_completed_test_order`
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
  derive at admission. There is still NO admin-plane write endpoint and no Admin UI
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
- `unlock_platform_admin` in `platform_admin_app/management/commands/` — clears
  `failed_attempts`/`locked_until` for a platform-staff account under a row lock and
  audits `ADMIN_AUTH_LOCKOUT_CLEARED`. Does NOT touch the password, TOTP secret or
  recovery codes, and needs no encryption key. The narrow tool: do NOT reach for
  `reset_platform_admin_totp` to undo a lockout — it destroys the authenticator and all
  ten recovery codes. See the nuisance-lockout section of `BACKGROUND_TASKS.md`
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
- Latest migration: `restaurants_app/migrations/0057_restaurant_is_test.py`
  (0054 adds `Table.qr_version`; 0055 data-repairs MenuItem extras — see the
  "Write-time menu relationship integrity" bullet; 0056 constrains
  `Restaurant.status` and fail-closed-maps the legacy vocabulary — see
  "Restaurant Lifecycle"; 0057 adds the platform-owned `Restaurant.is_test` flag,
  additive with NO backfill — see "Canonical Data Shapes"),
  `orders_app/migrations/0037_order_pricing_version.py` (0034 removed the
  inline review fields; 0035 adds the launch-boundary `Order.is_test` flag; 0036
  adds the D01 `quantity >= 0` CHECK constraint, additive and reversible with NO
  `RunPython`; 0037 adds `Order.pricing_version`, one `AddField` carrying BOTH
  `default` and `db_default` so an INSERT from rolled-back code stays valid, plus an
  index — the index build is proportional to the table's row count, which this
  repository cannot observe, so do not describe the deploy as instantaneous; the
  migration documents the `AddIndexConcurrently` alternative — see the D01/D02 bullets
  in Current Implementation Status),
  `finance_app/migrations/0028_remove_dinifytransaction_tip_amount.py`,
  `reviews_app/migrations/0003_review_tags.py`,
  `users_app/migrations/0014_customer_access_state.py` (0010 adds
  `User.account_type`; 0011 flips existing platform-role holders to
  `platform_staff`; 0012 makes `phone_number` unique — see the "Platform-admin
  identity layer" bullet; 0013 blacklists outstanding platform-staff refresh
  tokens and strips platform-only roles from `restaurant_user` rows, data-only and
  idempotent — see "Tenant Isolation / Role-Permission ENFORCEMENT"; 0014 adds the
  Step-2D.1 `customer_access_state` gate, one `AddField` plus its vocabulary
  `AddConstraint`, NO `RunPython` — see "Pre-Claim Customer Access"),
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
  - `.github/workflows/audit.yml` pins the SAME value independently. Both files
    must be changed together; they are the only two places the interpreter
    version is asserted (no `setup.py` / `pyproject.toml` / `tox.ini` exists, and
    `requirements.txt` declares no `requires-python`). Dependency bumps must
    satisfy `requires-python <= 3.12`
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
- Then runs the full Django test suite — a missing migration or a model
  change without a generated migration will fail CI
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
- A scheduled `.github/workflows/audit.yml` runs a weekly `pip-audit` sweep
  (Mondays 06:30 UTC) + manual `workflow_dispatch` — NOT triggered on
  PRs/pushes, so it never becomes a blocking PR check; a failure (advisories
  found) fires GitHub's scheduled-workflow notification

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
