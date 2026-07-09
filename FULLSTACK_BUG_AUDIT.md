# Fullstack Bug Audit — Dinify Backend + Frontend

**Audit type:** Read-only cross-repo bug audit (diagnosis only — no code changed).
**Date:** 2026-07-09
**Audited commits (origin/main):**
- `mugak1/Dinify-Backend` @ `9593df5c9d0c26caebc253ad075e0c896b175a33`
- `mugak1/Dinify-Frontend` @ `cebf6ed39176543c84f70732655c3fb291936f8f`

**Scope.** Three surfaces plus the shared code they depend on: the Django/DRF backend; the
Restaurant Portal frontend (`src/app/restaurant-mgt`); and the Diner app (`src/app/diner-app`).
**Primary emphasis:** the cross-repo seams where backend and frontend work in tandem, and the
Diner↔Portal linkage (order pipeline, money integrity, menu/availability sync, QR resolution,
order tracking, reviews, parked payments).

**Method.** Every finding below was located in code and then independently re-verified against the
actual source on `origin/main` in the relevant repo (fresh-eyes adversarial refutation, then a
manual re-open of the cited `path:line`). Findings that could not be confirmed are under
*Needs verification*. Issues that pre-existing docs already mark fixed were confirmed against code
before being treated as fixed. Every citation is `repo path:line`; seam findings cite **both** sides.

> This file is committed identically to the root of both repos, mirroring `REPORTS_CONTRACT_AUDIT.md`.

---

## Executive summary

| Severity | Count |
|---|---|
| **P0** — cross-tenant write / auth bypass / money-integrity loss | **1** |
| **P1** — priv-esc, PII exposure, IDOR, shared-flow break, guaranteed crash on common path | **7** |
| **P2** — user-reachable 500s, correctness with workaround, race, leaks | **9** |
| **P3** — hygiene, perf, type-safety, latent/deferred | **10** |
| **Total confirmed** | **27** |
| Cleared / assessed-not-a-bug (documented) | 8 |
| Needs verification | 2 |

*(27 distinct confirmed findings after de-duplicating overlaps that the fan-out surfaced from more
than one area — e.g. the same AllowAny `.get()`→500 sites and the same "ordering-availability not
enforced" gap were reported by several finders and are consolidated here.)*

### Top risks

1. **P0 — `subscription-details` PUT is completely ungated (cross-tenant write).** Any authenticated
   account (including a self-registered diner) can overwrite **any** restaurant's subscription
   validity/expiry. `restaurants_app/endpoints/restaurant_setup.py:958` short-circuits to a
   controller that does zero authorization. *(BUG-P0-1)*
2. **P1 — Diner order-initiate performs no restaurant scoping (cross-tenant injection + table lock).**
   A crafted AllowAny `orders/initiate` body can attach another tenant's menu items to an order and
   permanently mark a foreign restaurant's table "occupied", blocking legitimate diners. *(BUG-P1-1, SEAM)*
3. **P1 — Served orders never become "sales".** The kitchen advances only `fulfilment_status`, but
   every Order-based report and the dashboard gate on `order_status ∈ {served, paid}` — a status no
   live path ever sets. The instant the reports mock flag flips, the entire live analytics product
   reads **zero revenue**. *(BUG-P1-2, SEAM)*
4. **P1 — Two unauthenticated PII leaks and one full-PII oracle.** `MiscPublicEndpoint` returns every
   active restaurant owner's email+phone to anonymous callers and dumps every tenant's tables;
   `UserLookupEndpoint` returns any user's full identity to any authenticated account. *(BUG-P1-3/4/5)*
5. **P1 — Password-reset OTP is brute-forceable** (4-digit, no per-account lockout, not invalidated on
   wrong guess) and a correct guess mints a JWT → targeted account takeover. *(BUG-P1-7)*

### Seam findings (called out separately)

The Diner↔Portal seam is where most of the highest-impact issues live. **Good news first:** order
pricing is fully **server-authoritative** — the diner sends only ids + quantities + modifier/extra
ids, and the backend recomputes every figure, so displayed==charged and client price tampering is
inert (see *Cleared*). The **seam defects** are: cross-tenant order injection (BUG-P1-1);
reports/kitchen status-axis disjoint (BUG-P1-2); ordering-availability (`accepting_orders` /
`qr_mode` / non-`active` status) not enforced on order placement (BUG-P2-1); the abandoned-order
phantom-ticket trap (BUG-P2-2); and the `PaymentMode` enum vocab mismatch (BUG-P3-8, known/deferred).
All are detailed in **§ Cross-repo seam & Diner↔Portal findings**.

---

## P0 — Critical

### BUG-P0-1 · `subscription-details` PUT is ungated: any authenticated user rewrites any restaurant's subscription
- **Repo/where:** backend `restaurants_app/endpoints/restaurant_setup.py:958-959` → `restaurants_app/controllers/subscriptions.py:30-36`
- **Category:** broken-access-control / cross-tenant write · **Severity:** P0
- **Description.** In `RestaurantSetupEndpoint.put()`, `config_detail == 'subscription-details'` returns
  `RestaurantSubscription().update(request)` **before** the generic `check_permission` gate at line 990.
  A code comment at lines 953-957 claims these branches "have their own scoped permission checks inside
  their controllers" — but `RestaurantSubscription.update()` performs **no authorization at all**: it
  reads the client-supplied `request.data['restaurant']`, loads that `Restaurant`, overwrites
  `subscription_validity` and `subscription_expiry_date` from client values, and saves. The endpoint
  class declares no `permission_classes`, so it inherits only `IsAuthenticated`. (Note the asymmetry:
  the **GET** subscription-details branch *is* gated on `can_user_access_module(..., MODULE_SETTINGS)`.)
- **Impact / repro.** `PUT /api/v1/restaurant-setup/subscription-details/` with
  `{restaurant: <ANY id>, subscription_validity: ..., subscription_expiry_date: <far future>}` from any
  logged-in account grants any restaurant an unlimited subscription or expires a competitor's. A
  cross-tenant write to another tenant's `Restaurant` row with no authorization. (Blast radius is
  currently latent — nothing else in live code gates functionality on these two fields — but the
  unauthorized cross-tenant write itself is P0.)
- **Fix direction.** Gate the PUT branch before dispatch, mirroring the GET path's
  `can_user_access_module(..., MODULE_SETTINGS)` (404 on cross-tenant), or restrict subscription writes
  to dinify-admin; resolve the restaurant server-side instead of trusting `request.data['restaurant']`.

---

## P1 — High

### BUG-P1-1 · Diner order-initiate does no restaurant scoping on the table or menu items (cross-tenant injection + un-remediable table lock) · SEAM
- **Backend:** `orders_app/controllers/con_orders.py:488` (`Table.objects.get(pk=table_id)`, unscoped);
  `con_orders.py:314/28/55/221` (`MenuItem.objects.get(pk=...)`, unscoped);
  `orders_app/controllers/services/create_order.py:109-131` (`Order.objects.create` binds table with no
  `table.restaurant == restaurant` check); AllowAny entrypoint `orders_app/endpoints/orders.py:56`.
- **Frontend (context):** `src/app/diner-app/basket/basket-body/basket-body.component.ts:280-303`
  (POST v2 `orders/initiate` with body-supplied `restaurant`/`table`/`items`).
- **Category:** IDOR / tenant-isolation · **Severity:** P1
- **Description.** `ConOrder.initiate_order` validates only `restaurant.status`; the table and every
  menu item are resolved by bare PK with no `section__restaurant == restaurant` filter, and
  `SerializerPutOrderItem` is `fields='__all__'` with no `validate()`. Table and menu UUIDs are public
  via the QR diner flow.
- **Impact / repro.** A crafted AllowAny body `{restaurant: A, table: <B's table>, items:[{item: <B's item>}]}`
  creates `Order(restaurant=A, table=B's-table)` carrying restaurant B's menu items (polluting A's KDS
  and Order-based reports with items not on A's menu). Worse: `any_present_ongoing_order(table)` queries
  by table only, so the injected order makes **B's** table read "occupied" — and because the phantom
  order lives under **A**, it is invisible to B's restaurant-scoped kitchen/order views, so B cannot
  cancel it. An unauthenticated attacker can enumerate a victim's tables and lock out all legitimate
  diner ordering. (Prices stay server-authoritative, so this is an integrity/tenancy gap, not a price bug.)
- **Fix direction.** Enforce `table.restaurant_id == restaurant.id` in `_create_order`/`initiate_order`
  and scope every `MenuItem` lookup by `section__restaurant=restaurant` (rejecting foreign items) before
  creating the order.

### BUG-P1-2 · Served orders never become "sales": reports/dashboard gate `order_status`, kitchen writes only `fulfilment_status` · SEAM
- **Backend:** `reports_app/controllers/common/sale_filters.py:31,76-81` (`SALE_STATUSES = [served, paid]`,
  `sale_orders` filters `order_status__in`) vs `orders_app/endpoints_kitchen.py:206-218` (serve writes
  only `fulfilment_status`, `update_fields` deliberately excludes `order_status`);
  `reports_app/controllers/restaurant/dashboard.py:154-157` (closed_orders requires `order_status='served'`
  AND `payment_status='paid'`); the sole `order_status='served'` writer `manage_order.py:120` has **zero
  live callers** (v1 update-item retired).
- **Frontend (context):** `src/app/restaurant-mgt/reports/services/reports.service.ts` (`USE_MOCK_DATA=true`),
  DashboardService core metrics (`USE_MOCK_DATA=true`) — the mock masks it until flip.
- **Category:** correctness / seam contract · **Severity:** P1
- **Description.** The order lifecycle has two disjoint status axes. Live `order_status` writes only ever
  reach `{initiated, pending, cancelled}`. The kitchen "serve" action writes only `fulfilment_status='served'`
  and never touches `order_status`. No live path sets `order_status='served'` or `'paid'`. But the canonical
  sale definition filters `order_status ∈ {served, paid}`. The intersection with the reachable set is empty.
- **Impact / repro.** A fully served, eaten order keeps `order_status='pending'` forever and matches
  **neither** "served" nor "paid". Every Order-based report (Sales summary/listing/trends/hourly, Diners,
  Menu) and the dashboard revenue/closed-order metrics return **zero for all real data** — a restaurant
  that served 500 orders shows 0 sales, 0 revenue. Guaranteed (not edge-conditional), no workaround, whole
  live reporting product. Latent today only because reporting is mock-gated; it becomes the real output the
  instant the mock flags flip. This is the concrete reason the documented "flip-time gate" exists.
- **Fix direction.** Either promote `order_status` to `'served'` on the kitchen serve action, or re-point
  the Order-based sale/closed filters onto `fulfilment_status='served'` (+ payment `'paid'`) so the sale
  predicate intersects the reachable status set.

### BUG-P1-3 · `UserLookupEndpoint` exposes any user's full PII to any authenticated user, unscoped and unthrottled
- **Backend:** `users_app/endpoints/user_lookup.py:11-46` (view — no `permission_classes`, no
  `get_throttles`); response body `:28-38`; global default `dinify_backend/settings.py:215-217` +
  empty throttles `:221`; route `users_app/urls.py:11` → `api/v1/users/user-lookup/?contact=`.
- **Category:** PII exposure · **Severity:** P1
- **Description.** `UserLookupEndpoint` declares no `permission_classes`, so it inherits `IsAuthenticated`
  only. `GET …/user-lookup/?contact=<email-or-phone>` returns another user's `id, first_name, last_name,
  phone_number, email` for an exact email or phone match, with no restaurant/self/owner scoping and no
  throttle. (Distinct from the documented `MsisdnLookupEndpoint`, which is AllowAny but returns only a
  boolean — see BUG-P3-1.)
- **Impact / repro.** Any low-privilege account (kitchen/staff, or a self-registered diner) resolves a
  known email/phone into a full cross-tenant identity — doxxing, targeted phishing, phone→name/email
  correlation on a MoMo-first user base; no rate limit blunts scripted harvesting against a wordlist.
- **Fix direction.** Add an explicit permission gate (self/owner/manager or onboarding-role scope) and a
  throttle; do not rely on the `IsAuthenticated` global default; trim returned PII to what the caller needs.

### BUG-P1-4 · `MiscPublicEndpoint` exposes every active restaurant owner's email + phone to anonymous callers
- **Backend:** `restaurants_app/endpoints/misc_public.py:20` (AllowAny), `:36-40` (`status='active'` only →
  `SerializerPublicGetRestaurant`); `restaurants_app/serializers.py:58-65` (`get_owner` returns
  `email` + `phone`); route `restaurants_app/urls.py:36` + `dinify_backend/urls.py:26`.
- **Category:** PII exposure · **Severity:** P1
- **Description.** `GET /api/v1/restaurant-setup/misc-public/restaurants/` is AllowAny, filters only
  `status='active'`, paginates over every active restaurant, and serializes `get_owner()` which returns
  the owner's `id, first_name, last_name, email, phone` with no redaction.
- **Impact / repro.** Unauthenticated bulk harvest of full name + email + phone of the owner of every
  active restaurant on the platform (walk the pages).
- **Fix direction.** Drop `email`/`phone` (ideally the whole owner block) from `get_owner()`; keep owner
  PII behind an authenticated tenant-scoped serializer and confine the public endpoint to display fields.

### BUG-P1-5 · `MiscPublicEndpoint` tables listing is unscoped — anonymous callers dump every tenant's tables
- **Backend:** `restaurants_app/endpoints/misc_public.py:33` (filter built only from `define_filter_params`,
  no restaurant scope, no `deleted=False`); `misc_app/controllers/secretary.py:277` (`Table.objects.filter(**{})`
  when no params); `restaurants_app/serializers.py:457-459` (`SerializerPublicGetTable`, `fields='__all__'`).
- **Category:** cross-tenant read (broken object-level authz) · **Severity:** P1
- **Description.** `GET …/misc-public/tables/` (AllowAny) applies no restaurant scope and no soft-delete
  filter. With no query params, `define_filter_params` returns `{}` and the queryset is unbounded, returning
  every table of every restaurant — all columns (restaurant FK, floor-plan geometry, capacities, QR flags,
  status, `created_by`) including soft-deleted rows.
- **Impact / repro.** Platform-wide cross-tenant enumeration of every restaurant's full table inventory and
  floor plan, unauthenticated. (Non-PII business records, hence P1 not P0.)
- **Fix direction.** Require an explicit single-`restaurant` scope (or drop the public `tables` list branch —
  diners use the single-table `details` path) and always apply `deleted=False` on public list queries.

### BUG-P1-6 · `admin-register-restaurant` POST has no admin gate — any user provisions accounts/restaurants + triggers credential dispatch
- **Backend:** `restaurants_app/endpoints/restaurant_setup.py:440-458` (branch, no `check_permission`/
  `is_dinify_admin`); `restaurants_app/controllers/create_restaurant.py:113,117` (the unimplemented
  `# TODO check that the user is an admin`), `:165-171` (`self_register(..., send_credentials=True,
  skip_otp=True)`); default perms `dinify_backend/settings.py:215-217`.
- **Category:** broken function-level access control (OWASP API5) · **Severity:** P1
- **Description.** The admin-only `admin-register-restaurant` branch performs no authorization; the
  controller literally opens with the unimplemented `# TODO check that the user is an admin`. It creates a
  **new** `User` (via `self_register`, dispatching username+password over SMS/email) for a caller-supplied
  phone/email, then a `Restaurant`, wiring that user as owner. The frontend restricts the button to the
  Platform Admin UI, but that is not a server control.
- **Impact / repro.** Any authenticated user (including a diner) creates arbitrary user accounts for
  attacker-chosen contacts, triggers credential SMS/email to them (spam/abuse + provisioning vector), and
  registers arbitrary restaurants (DB pollution).
- **Fix direction.** Implement the TODO: `if not is_dinify_admin(request.user): return 403` at the top of
  the branch / inside `admin_register_restaurant`.

### BUG-P1-7 · Password-reset OTP is brute-forceable → targeted account takeover
- **Backend:** `users_app/controllers/otp_manager.py:35` (4-digit `random.randint`, non-crypto RNG),
  `:94-120` (verify filters + invalid early-return, **no** attempt counter / rotation), `:141` (deletes
  only the matched row); `users_app/models.py:122-124` (5-min expiry);
  `users_app/controllers/reset_password.py:84-126` (valid OTP → fresh JWT + `temp_password`);
  only limiter is per-IP `AnonRateThrottle` (`users_app/throttles.py:18-19`, `dinify_backend/settings.py:225`).
- **Category:** auth weakness · **Severity:** P1
- **Description.** OTPs are 4 digits (9000-value space) with a 5-minute window. A wrong guess is not
  counted, does not rotate/invalidate the live OTP, and there is no per-account lockout — the only limiter
  is per-IP (5/min), defeated by distributed IPs. `reset_password` returns a fresh access+refresh JWT plus
  `temp_password` on a valid OTP, so a correct guess **is** full account takeover.
- **Impact / repro.** Attacker triggers `initiate-reset-password` for a victim username, then brute-forces
  the 9000-value space across ~100–180 proxied IPs within 5 minutes (~25–50% success/cycle, repeatable) to
  obtain a victim JWT and reset the password. Highest value against owner/manager accounts.
- **Fix direction.** Add an account/OTP-scoped failed-attempt counter that invalidates the live OTP after
  3–5 wrong guesses with a short account-level cooldown (independent of IP); widen to 6 digits generated via
  `secrets` rather than `random`.

---

## P2 — Medium

### BUG-P2-1 · Ordering availability (`accepting_orders`, `qr_mode`, non-`active` status) is never enforced on diner order placement · SEAM
- **Backend:** `orders_app/controllers/con_orders.py:448` (initiate gate rejects only
  `restaurant.status in ['blocked']`); `orders_app/controllers/services/create_order.py:70-139` (does not
  read `accepting_orders` or `qr_mode`); `restaurants_app/serializers.py:492` (`qr_mode` emitted) but
  `:526-555` (`accepting_orders` **absent** from the scan restaurant dict); `restaurants_app/models.py:757-773`
  (`is_available_for_scan` checks table lifecycle only).
- **Frontend:** `src/app/restaurant-mgt/settings/availability/availability.component.html:24` ("When off,
  your restaurant is paused and won't take new orders"); `src/app/diner-app/basket/basket-body/basket-body.component.ts:280`
  (`placeOrder` posts unconditionally); `src/app/_models/app.models.ts:255` (`TableScan` omits `qr_mode`);
  `src/app/restaurant-mgt/tables/utils/qr-print-sheet.ts:34,152` encodes `?mode=` but nothing in
  `src/app/diner-app` ever reads it.
- **Category:** correctness / seam · **Severity:** P2
- **Description.** The operator's `accepting_orders` toggle and per-table `qr_mode` (`menu_only`/`order_only`)
  are silently no-ops end-to-end: the diner app never reads `?mode=` and `TableScan` doesn't declare
  `qr_mode`; the backend initiate path gates only on `status == 'blocked'` (so `pending`/`inactive`/`rejected`
  restaurants also order) and never reads `accepting_orders` (which isn't even serialized to the diner).
- **Impact / repro.** A restaurant that toggles ordering off — or a `menu_only` display/lobby table, or a
  not-yet-activated / deactivated / rejected restaurant — still accepts and creates real diner orders that
  hit the KDS. The portal control and the diner flow are decoupled on a core shared flow.
- **Fix direction.** Enforce server-side in `initiate_order`/`_create_order`: reject (400) when
  `accepting_orders` is False, `qr_mode == 'menu_only'`, or `status != 'active'`; secondarily surface
  `qr_mode` on `TableScan` and `accepting_orders` in the scan payload so the diner UI can hide the CTA. The
  server gate is load-bearing.

### BUG-P2-2 · Backing out at the sold-out sheet abandons an already-created live order — phantom KDS ticket + self-locked table · SEAM
- **Backend:** `orders_app/controllers/services/create_order.py:94-131` (initiate is not a dry-run —
  `Order.objects.create` with `fulfilment_status='new'`, `order_status='initiated'`);
  `orders_app/endpoints_kitchen.py:93-100` (active feed lists Initiated, not-served, not-cancelled orders);
  `orders_app/controllers/con_orders.py:111-119` (occupancy gate counts Initiated orders).
- **Frontend:** `src/app/diner-app/basket/basket-body/basket-body.component.ts:411` (`cancelPartialOrder`
  merely hides the sheet — **no cancel API call**) and `:280-347` (order created before diner confirms the
  trimmed list).
- **Category:** correctness / diner↔kitchen · **Severity:** P2
- **Description.** `orders/initiate` creates a live `Order` before the diner has confirmed. That row both
  marks the table occupied (`any_present_ongoing_order` only excludes cancelled + `served`) and appears
  immediately on the kitchen board. When initiate reports unavailable items, the diner is shown the sold-out
  sheet; backing out (`cancelPartialOrder`) makes no cancel call, and there is no diner-facing cancel (the v1
  `cancel` action was retired).
- **Impact / repro.** The kitchen prepares an order the diner never confirmed; the diner's own table is now
  blocked (editing the basket resets `client_order_id`, so re-checkout 400s on the ongoing-order gate).
  Recovery needs staff to cancel the ghost ticket on the KDS. Same trap on any tab-close between initiate and
  submit.
- **Fix direction.** Stop counting un-submitted `Initiated` orders as occupancy/KDS work (exclude
  `order_status='initiated'` from `any_present_ongoing_order` and the active-kitchen queryset), or give the
  diner a real discard path that releases the initiated row.

### BUG-P2-3 · Family of unguarded lookups / missing else-branches → unauthenticated or user-reachable HTTP 500s
- **Category:** robustness / user-reachable 500 · **Severity:** P2
- **Description.** Seven live sites do a bare `.objects.get()` (or an unguarded branch) on client-supplied
  input with no try/except and no id validation, so a missing/malformed/unknown value raises
  `DoesNotExist`/`ValidationError`/`KeyError`/`UnboundLocalError`/`AttributeError` and returns a 500 instead
  of a clean 4xx. Most are on **AllowAny** endpoints. Uniform fix: validate the id / guard the branch and map
  to 400/404.

  | # | Site | Trigger | Auth |
  |---|---|---|---|
  | a | `users_app/controllers/otp_manager.py:91,94-113` (verify-otp) | `{"otp":"1234"}` (no `user`) → UnboundLocalError; `{"user":id}` (no `otp`) → AttributeError | AllowAny |
  | b | `users_app/controllers/otp_manager.py:169-204` (resend-otp, `identification='msisdn'` — the default) | `msisdn` branch is a no-op leaving `user=None` → `user.id`/`user.phone_number` deref | AllowAny |
  | c | `orders_app/endpoints/orders.py:37` (v1 `orders/submit`) | missing/malformed/unknown `order` id | AllowAny |
  | d | `restaurants_app/controllers/handle_diner_journey.py:126,143-145` (journey `order-details`/`payment-details`) | malformed/nonexistent order or transaction UUID | AllowAny |
  | e | `finance_app/endpoints/order_payments.py:31` (`initiate-order-payment`) | missing/malformed/unknown `order` id | AllowAny |
  | f | `misc_app/controllers/define_filter_params.py:110-113` | any unknown query param with value length ≥2 (e.g. `?zz=abc`) → KeyError; unknown model → TypeError | AllowAny via `misc-public`; also auth `restaurant-setup` list |
  | g | `restaurants_app/endpoints/misc_public.py:30-31` (`misc-public/details/`) | calls undefined `self.get_detail` → AttributeError | AllowAny |
- **Impact / repro.** Any anonymous caller can force a 500 on core public flows (OTP verify/resend, order
  submit, order/payment tracking, payment initiate, public listing) with a trivial malformed request — noisy
  error-spam, stack-trace leakage if `DEBUG` is ever on, and clean-404 cases surfacing as server errors.
  (`define_filter_params` is proven-known: `support_app/endpoints/admin_issues.py:33-53` already pre-filters
  keys against exactly this "KeyError-on-unknown-param" vector, but `misc_public`/`restaurant_setup` do not.)
- **Fix direction.** Validate/parse ids and wrap each lookup (try/except → 400 on malformed, 404 on
  `DoesNotExist`); in `verify_otp` default `otps = UserOtp.objects.none()` and 400 on missing otp/identifier;
  implement or remove the `msisdn` resend branch and the `misc-public/details` branch; guard
  `define_filter_params` against unknown keys/models (or pre-filter keys as `admin_issues.py` does).

### BUG-P2-4 · Table double-seating race: ongoing-order gate takes no row lock, no unique constraint on active-order-per-table
- **Backend:** `orders_app/controllers/services/create_order.py:83-104` (atomic block; unlocked gate at `:94`);
  `orders_app/controllers/con_orders.py:111-119` (plain SELECT, no `select_for_update`);
  `orders_app/models.py:113-127` (constraints only on `client_order_id` and `(restaurant, order_date, order_number)`).
- **Category:** race / concurrency · **Severity:** P2
- **Description.** `_create_order` is `atomic()`, but the seating gate is an unlocked SELECT and `Order` has
  no DB uniqueness forbidding two active orders per table. Under READ COMMITTED, two concurrent `initiate`
  calls for the same table both read "no ongoing order" and both INSERT. `client_order_id` is documented
  "absent today", so it does not protect this path.
- **Impact / repro.** Two diners scanning the same QR at once (or a double-tap) each create a live order on
  the same table; the table shows two ongoing orders and the kitchen gets duplicate tickets.
- **Fix direction.** `Table.objects.select_for_update().get(pk=...)` at the top of `_create_order` before the
  gate so concurrent initiates serialize on the table; or add a partial unique index for one
  non-served/non-cancelled order per table.

### BUG-P2-5 · `add_order_item` merge path can raise `MultipleObjectsReturned` → 500 on repeated item+modifier variants
- **Backend:** `orders_app/controllers/con_orders.py:128-171` (`determine_existing_order_item` inspects only
  `existing_item[0]`), `:284-292` (`update_item_quantity` re-fetches with an unfiltered
  `OrderItem.objects.get(order__id=..., item=..., deleted=False)` — no modifier/extra filter); live loop
  `orders_app/controllers/services/create_order.py:134-135`.
- **Category:** correctness / 500 · **Severity:** P2
- **Description.** A single order can legitimately hold two+ rows of the same menu item distinguished by
  modifiers (e.g. `[X+modA, X+modB]`). A later entry that matches the first row (`[X+modA, X+modB, X+modA]`)
  makes the merge path re-`.get()` on `item=` alone, which matches both rows → `MultipleObjectsReturned` →
  aborts the atomic `_create_order` → 500 for the whole initiate.
- **Impact / repro.** A diner items array with the same item in multiple modifier variants plus a repeat 500s
  the entire order-initiate, losing the order (conditional but certain given the payload).
- **Fix direction.** Have `update_item_quantity` operate on the exact row already matched (pass the pk), or
  make the match/merge fully modifier+extra-aware with `.filter(...).first()`, or de-dupe incoming items by
  `(item, modifiers, extras)` before the add loop.

### BUG-P2-6 · `self_update_user_profile` approval branch is dead code (feature broken) + `V2UserProfileEndpoint.get` returns `None` → 500
- **Backend:** `users_app/controllers/update_user_profile.py:62-66` (unconditional `return`; `:68-129`
  unreachable); `users_app/endpoints/user_profile.py:36-43` (`get` has no return for
  `intention != 'pending-approvals'`).
- **Category:** correctness / dead code / 500 · **Severity:** P2
- **Description.** For a privileged user (dinify admin or any `RestaurantEmployee`), `self_update_user_profile`
  returns "Kindly refer to your manager…" unconditionally, so the block that would build `profile_changes` and
  queue the approval is unreachable — the self-service profile-update-approval feature never records anything.
  Separately, `V2UserProfileEndpoint.get` only returns for `intention == 'pending-approvals'`; any other
  `intention` falls off the end → Django "view returned None" → 500.
- **Impact / repro.** Employees/admins editing their own name/email/phone via the v1 endpoint get a bare
  refusal and nothing is queued; the v2 GET 500s for any `intention` other than `pending-approvals`. (Low
  security impact; the working admin/owner edit path is the v2 `update-profile` PUT.)
- **Fix direction.** Remove/condition the premature `return` and decide the real intent (given MongoDB is
  unreachable, simplest is to let a self-edit persist); add an explicit else-return (400) to the v2 GET.

### BUG-P2-7 · AllowAny `initiate-order-payment` also allows anonymous transaction-record creation with a client-controlled split amount
- **Backend:** `finance_app/endpoints/order_payments.py:16` (AllowAny), `:31` (unguarded lookup — see BUG-P2-3e);
  `finance_app/controllers/tx_order_payment.py:53,68-73` (split `transaction_amount` from client `amount`,
  only bounded `< order.actual_cost`), `:134-149` (record created with `created_by=None`), `:151` (aggregator
  call is a no-op stub today).
- **Category:** abuse surface / latent money-integrity · **Severity:** P2
- **Description.** Beyond the 500 (BUG-P2-3e), the endpoint creates a `DinifyTransaction` for any order UUID
  the caller holds, with no ownership check; for `payment_form='split'` the amount is client-supplied.
- **Impact / repro.** An anonymous caller can spam transaction records against any order UUID, polluting the
  `DinifyTransaction`-based Transactions report. The client-controlled split amount is a latent
  money-integrity risk that becomes live when payment collection is re-wired at relaunch.
- **Fix direction.** Gate the endpoint to an authenticated diner tied to the order (ownership check) before
  creating any `DinifyTransaction`; bound the split amount server-side against `actual_cost` minus
  already-collected.

### BUG-P2-8 · `MiscPublicEndpoint` `details` path calls an undefined method → unauthenticated 500
*(Consolidated into BUG-P2-3g — same class, same fix. Retained here as a pointer for the `misc-public` audit line.)*

---

## P3 — Low / hygiene / latent

### BUG-P3-1 · `MsisdnLookupEndpoint` is an unauthenticated, unthrottled account-existence enumeration oracle
- **Backend:** `users_app/endpoints/user_lookup.py:49-74` (AllowAny, returns `{found}`), route
  `users_app/urls.py:12`; no throttle (`DEFAULT_THROTTLE_CLASSES=[]`, and it is a standalone APIView not
  covered by the auth-action throttles).
- **Severity:** P3 · **Description/impact.** Anyone can probe whether a phone number has a Dinify account,
  unlimited — useful for targeted phishing/SIM-swap lists on a MoMo-first base and as a precursor to the
  reset-OTP brute force (BUG-P1-7). Existence-only (no PII). *(This is the item flagged in
  `BREAKING_CHANGES.md` / `README.md` — still live.)* It also compares the raw `msisdn` param against the
  canonical stored `256…` form without `normalise_msisdn`, so a `+256`/`0`-prefixed probe silently returns
  `found:false`.
- **Fix direction.** Attach a throttle (`AnonRateThrottle`/`ScopedRateThrottle`) and normalise the incoming
  msisdn before lookup.

### BUG-P3-2 · `Restaurant.status` is Secretary-editable via `EDIT_INFORMATION` (latent self-status mass-assignment)
- **Backend:** `dinify_backend/configss/edit_information.py:7` (`{'key':'status'}` in `restaurants`); write
  path `restaurants_app/endpoints/restaurant_setup.py:990,1075`.
- **Severity:** P3 · **Description/impact.** A `MODULE_SETTINGS` user can PUT `restaurant-setup/restaurants/`
  and change their restaurant's lifecycle status. Currently limited because the resolver requires
  `restaurant__status='active'` (`users_app/controllers/permissions_check.py`), so a non-active restaurant's
  owner cannot reach the settings gate to self-activate — but `status` being client-writable is a latent
  privilege boundary that becomes a self-activation the moment that active-only invariant changes.
  (Ownership `owner` FK is correctly **not** in the list.)
- **Fix direction.** Remove `status` from `EDIT_INFORMATION['restaurants']` and route lifecycle changes
  through a dinify-admin-only branch with explicit validation.

### BUG-P3-3 · Naive `datetime.now()` for report "today" default and dashboard this-month metrics (EAT drift)
- **Backend:** `reports_app/endpoints/restaurant_reports.py:48`; `reports_app/controllers/restaurant/dashboard.py:127-128,147-148,195-196`
  (naive) — contrast `dashboard.py:424,502` (`timezone.now()`); settings `TIME_ZONE='Africa/Nairobi'`, `USE_TZ=True`.
- **Severity:** P3 · **Description/impact.** `datetime.now()` is naive/system-clock (UTC on prod), so near
  local midnight / month-end the default "today" range and the "this month" buckets drift up to ~3h into the
  wrong day/month. Mostly masked (the four rebuilt reports validate explicit dates from the FE, and
  dashboard-v2 uses `timezone.now()`).
- **Fix direction.** Use `timezone.localdate()`/`timezone.localtime(...)` at these sites, matching the
  `_build_tables`/`_build_kds` pattern already used in the same file.

### BUG-P3-4 · Extra order-items set the `discounted` flag from raw `running_discount`, not the live `is_discount_active()` predicate
- **Backend:** `orders_app/controllers/con_orders.py:253` (extras use `extra_item.running_discount`) vs
  `:362-364` (parent items use `menu_item.is_discount_active()`); surfaced by `serialize_order_item_details`.
- **Severity:** P3 · **Description/impact.** The **charged** extra price is correct (from
  `effective_base_price` via `determine_effective_unit_price`), but the persisted/serialized `discounted`
  flag can read `true` on an extra with a lapsed/out-of-window discount that is charged full price →
  misleading discount badge on extras. No money impact.
- **Fix direction.** Set `discounted = extra_item.is_discount_active()` in `process_item_extras`.

### BUG-P3-5 · Basket auto-commits without ever showing the server-recomputed total (a lapsed discount silently changes the recorded amount)
- **Backend (context):** `orders_app/controllers/con_orders.py:174-211` (`determine_effective_unit_price`
  recomputes on the live EAT predicate).
- **Frontend:** `src/app/diner-app/basket/basket-body/basket-body.component.ts:303-319` (when
  `unavailableCount===0`, `submitOrder()` fires immediately and `order_details.actual_cost` is never shown);
  snapshot frozen at `src/app/diner-app/menu-item-detail/menu-item-detail.component.ts:525-534`.
- **Severity:** P3 · **Description/impact.** Prices are server-authoritative (good), but the basket shows
  client snapshots taken at add-to-basket time; if a time-windowed discount lapses (or a price is edited)
  between add and checkout, the server records a higher `actual_cost` than the basket displayed, with no
  diner-visible confirmation. Limited today (in-app diner payment is unwired; the diner discovers the delta
  only on the physical bill; no platform money loss).
- **Fix direction.** In the `unavailableCount===0` branch, compare `actual_cost` to the client basket total
  and surface a one-tap confirm of the server total when they diverge, instead of auto-committing.

### BUG-P3-6 · `error.interceptor` leaves requests queued during a *failed* token refresh hung (masked by the full-page logout)
- **Frontend:** `src/app/_helpers/error.interceptor.ts:96-126`.
- **Severity:** P3 · **Description/impact.** On the refresh-failure path, `handle401` calls `logout()` +
  `throwError` for the leading request but never pushes onto `refreshTokenSubject`, so requests queued in the
  `filter(token => token !== null)` waiting branch never emit/complete/error. Masked today because `logout()`
  does a full-page redirect that unloads the hung subscriptions; becomes a real hang if `logout` is ever
  changed to a soft/SPA navigation (or `hardRedirect` is stubbed, e.g. in tests). Success path is correct.
- **Fix direction.** On both failure branches, terminate the waiting subscribers (push a sentinel/error
  through `refreshTokenSubject`, or complete + re-seed a fresh subject) before `logout()`.

### BUG-P3-7 · `app.models.ts` declares `Pagination` and `Series` twice with divergent shapes (declaration-merge type-lie)
- **Frontend:** `src/app/_models/app.models.ts:15-22,730-733,811-814,897-905`.
- **Severity:** P3 · **Description/impact.** `interface Pagination` is declared twice (one with
  `records_per_page`, one adding `paginated`/`page_size`); TS declaration-merges them into a union no real
  payload satisfies, papering over the `records_per_page` vs `page_size` key disagreement that
  `ApiService.loadAllPages` already works around defensively. `Series` is likewise duplicated. Type-safety
  debt at the pagination API boundary.
- **Fix direction.** Collapse each to one canonical declaration (settle on `page_size`; make legacy keys
  optional) and delete the duplicate blocks.

### BUG-P3-8 · `PaymentMode` enum vocabulary mismatch (FE `MTN MoMo`/`Airtel MoMo`/`Cash` vs BE `momo`/`card`/`cash`) · SEAM (known/deferred)
- **Backend:** `reports_app/serializers.py:39` + `finance_app/serializers.py` (raw lowercase `payment_mode`).
- **Frontend:** `src/app/restaurant-mgt/reports/models/reports.models.ts:73` (union) +
  `src/app/restaurant-mgt/reports/services/reports-adapter.ts:70,113` (pass-through cast, `Cash` fallback only).
- **Severity:** P3 · **Description/impact.** On flip, the Sales/Transactions "Method" column shows raw
  `momo`/`card`/`cash` rather than the intended labels, and any FE comparison against the `MTN MoMo` literals
  never matches (backend can't even distinguish MTN vs Airtel — stores only `momo`). Latent (reports
  mock-gated); degrades gracefully as plain text. **Already documented in CLAUDE.md as a KNOWN GAP** deferred
  to its own change — recorded here for seam completeness.
- **Fix direction.** Map backend tokens → display labels in the adapter on flip; widen the union to the raw
  vocab; the MTN-vs-Airtel split needs a product/backend decision.

### BUG-P3-9 · Sales report "Refunds"/"Net revenue" are computed from mock data regardless of `USE_MOCK_DATA` (flip-time landmine)
- **Frontend:** `src/app/restaurant-mgt/reports/sales/sales-report.component.ts:263-265` (`mockSalesRefunds`
  called outside the `USE_MOCK_DATA` gate); consumed by `sales-hero.component.ts:90-92`
  (`net = revenue − refunds`).
- **Severity:** P3 · **Verdict:** PLAUSIBLE (see *Needs verification* note). **Description/impact.** Unlike
  every other Sales data path, `refunds`/`prevRefunds` are always pulled from the in-memory mock generator
  and subtracted from revenue to produce the headline "Net revenue". The backend has no refund source
  (custodial refunds were removed), so the correct value is 0. Today the module is fully mock and
  `mockSalesRefunds` is deterministic (internally consistent), and it's a UI-labelled placeholder; the risk
  is that the documented flip-time gate is framed around endpoint-shape re-verification, and this is a local
  mock generator (not an endpoint), so it could slip through a shape-only check.
- **Fix direction.** Gate `refunds`/`prevRefunds` behind `USE_MOCK_DATA` (default 0 in the real branch) or
  hide the Refunds row (and drop it from `net`) until a backend refund source exists.

### BUG-P3-10 · `section-tables` POST is admin-only and unused by the frontend (dead endpoint verb)
- **Backend:** `restaurants_app/endpoints/restaurant_setup.py` (`section-tables` absent from
  `_RESTAURANT_RESOLVERS`/`_RECORD_MODULE`; only a dinify-admin bypass reaches `create_tables_in_section`).
- **Severity:** P3 · **Description/impact.** For non-admins the branch cleanly 403s (fail-closed RBAC working
  as designed); the underlying `create_tables_in_section` is still live via `create_dining_area(create_tables=True)`.
  Net residue: one endpoint verb is admin-only and has zero frontend callers — hygiene only.
- **Fix direction.** Remove the unused verb or document it as admin-only; no functional change needed.

---

## Cross-repo seam & Diner↔Portal findings

This section indexes the findings that span both repos or govern the Diner↔Portal linkage. Each is
detailed above; here is the seam view of what corrupts what.

| ID | Seam | One-line |
|---|---|---|
| **BUG-P1-1** | Diner order-initiate → Portal Kitchen/Reports | No server-side restaurant scoping on table/menu → cross-tenant item injection + un-remediable foreign-table lock. |
| **BUG-P1-2** | Portal Kitchen ↔ Portal Reports/Dashboard | Kitchen writes `fulfilment_status`; reports gate `order_status ∈ {served,paid}` — disjoint, so live reports read **zero**. |
| **BUG-P2-1** | Portal Settings/Tables ↔ Diner order placement | `accepting_orders` / `qr_mode` / non-`active` status not enforced server-side; diner app never reads `?mode=`. |
| **BUG-P2-2** | Diner basket ↔ Portal Kitchen | Abandoning the sold-out sheet leaves a live `Initiated` order → phantom KDS ticket + self-locked table. |
| **BUG-P2-3d/e** | Diner order/payment tracking ↔ backend | AllowAny `journey/order-details`, `journey/payment-details`, `initiate-order-payment` 500 on any bad id. |
| **BUG-P2-7** | Diner payments (parked) ↔ Transactions report | Anonymous `DinifyTransaction` creation with client split amount → report pollution + latent money risk at relaunch. |
| **BUG-P3-8** | Backend serializers ↔ FE Reports adapter | `PaymentMode` vocab mismatch (known/deferred). |

**Seam integrity — what is correct (verified, not a bug):**
- **Money integrity holds.** The public menu serializer's `current_price`/`is_discount_active`/
  `discount_percentage` and the order charge path (`ConOrder.determine_effective_unit_price`) gate on the
  **same** `is_discount_active()` EAT predicate; the diner POSTs only ids + quantities + modifier/extra ids
  (never prices), and the backend recomputes every figure. **Displayed == charged.** See *Cleared* §.
- **Enum vocabularies align** for `fulfilment_status` (`new/preparing/ready/served`, identical both sides)
  and the kitchen PUT verb (FE `postPatch(...,'put')` ↔ BE `def put`). `order_status`/`payment_status`/
  `SALE_STATUSES` values match `dinify_backend/configss/string_definitions.py`. *(One minor vocab drift:
  the FE `PaymentStatus` union in `reports.models.ts` includes `'refunded'`, but backend `payment_status`
  has only `paid/failed/pending` — `refunded` lives on the `order_status` axis. Cosmetic; listed under
  Needs verification.)*
- **Availability sync distinction is correct** where wired: `available=False` items are filtered out of the
  diner menu entirely; `in_stock=False` items still show with a "Sold out" badge; the kitchen 86 panel and
  the menu module write the **same** `in_stock` column. Dietary pills build off `tags`. *(The gap is
  ordering-availability, BUG-P2-1 — a different axis.)*
- **Reviews seam is correct:** one-per-order (409), unknown `ReviewTag` keys are **dropped** (not 400'd),
  `is_public` seeded from the threshold but owner-overridable — all match on both sides.

---

## Per-area coverage checklist

**Backend**
- users_app — reviewed · 4 findings (BUG-P1-3, BUG-P1-7, BUG-P2-6, BUG-P3-1) + parts of BUG-P2-3 (a,b)
- restaurants_app (setup catch-all + dedicated endpoints + Secretary/EDIT_INFORMATION + role-permissions) —
  reviewed · 4 findings (BUG-P0-1, BUG-P1-4, BUG-P1-5, BUG-P1-6, BUG-P3-2, BUG-P3-10) + BUG-P2-3f/g;
  role-permissions PUT atomicity checked — OK (`select_for_update`)
- orders_app (pipeline + kitchen) — reviewed · 5 findings (BUG-P1-1, BUG-P2-1, BUG-P2-2, BUG-P2-4, BUG-P2-5,
  BUG-P3-4) + BUG-P2-3c; kitchen module gates verified present on all 8 views (**no finding** — see Cleared)
- reports_app — reviewed · 2 findings (BUG-P1-2, BUG-P3-3); N+1 checked — none (grouped queries confirmed)
- reviews_app — reviewed · none (one-per-order / tag-drop / is_public all correct)
- support_app — reviewed · none (ungated scoping via `get_employed_restaurant_ids` is intentional; it is the
  reference implementation for the `define_filter_params` key pre-filter that misc_public lacks)
- notifications_app — reviewed · none (`mark_as_read` correctly synchronous)
- finance_app — reviewed · 2 findings (BUG-P2-7 + BUG-P2-3e)
- crm_app — reviewed · none (legacy/superseded; no live diner/portal seam)
- misc_app — reviewed · 1 finding (BUG-P2-3f, `define_filter_params`); paginator/secretary/decode_auth_token — OK
- payment_integrations_app — reviewed · none (only `yo_integrations.py` survives; custodial code deleted)

**Restaurant Portal (restaurant-mgt)**
- dashboard — reviewed · none new (mock-data status is documented/intended — see Cleared; the *consequence*
  is BUG-P1-2)
- menu — reviewed · none (real-wired; `available`/`in_stock`/discount surfaces correct)
- tables — reviewed · none live (Service View parked/hidden — see Cleared)
- reviews — reviewed · none (real-wired, adapter correct)
- reports — reviewed · 1 finding (BUG-P3-9) + BUG-P3-8; mock-gated (see *Needs verification* for B2)
- settings — reviewed · 1 finding (BUG-P2-1 FE half); services real-wired
- team — reviewed · none (owner-row lock read from `editable` flag; role picker aligned to backend roles)
- billing — reviewed · none (subscription-only; the *backend* subscription write is BUG-P0-1)
- support — reviewed · none

**Diner app (diner-app)**
- QR entry / table-scan — reviewed · part of BUG-P2-1 (status not enforced at scan)
- menu browse — reviewed · none (price/tag/stock surfaces correct)
- cart — reviewed · none (client totals are display-only; charged amount is server-side)
- order placement — reviewed · BUG-P1-1, BUG-P2-2, BUG-P2-3c
- order tracking — reviewed · BUG-P2-3d (diner sees only a binary `ongoing`; no fulfilment vocabulary
  surfaced — a UX gap, not a defect)
- payment — reviewed · BUG-P2-3e, BUG-P2-7 (screen orphaned/unwired)
- review submission — reviewed · none (gated on real order id; correct)
- BUG-P3-5 (auto-commit without server total) spans cart→placement

**Shared frontend**
- authentication service / token storage — reviewed · localStorage tokens assessed (Cleared/deferred)
- guards — reviewed · none (`AuthGuard` enforces roles; `permissionGuard` module keys match backend set)
- HTTP interceptors — reviewed · BUG-P3-6 (failed-refresh queue)
- models — reviewed · BUG-P3-7 (duplicate interfaces)
- phone-input component — reviewed · none (displays national-only, emits canonical `256XXXXXXXXX`; no double-`+256`)

---

## Needs verification

1. **REPORTS_CONTRACT_AUDIT B2 — sales-trends "this year" preset → `category=monthly` → backend 731-day cap → 400.**
   Documented open in `REPORTS_CONTRACT_AUDIT.md` (B2) and acknowledged in the frontend CLAUDE.md
   (`ReportGranularity` can't express `annual`). Not independently re-confirmed in this pass (the related
   *listing* 31-day cap was refuted — the FE clamps those; the trends path is separate).
   **Exact check:** with a >731-day custom range (or the "This year" preset spanning a leap boundary), does
   `ReportsService.getSalesAggregate` send `category=monthly` (vs `tf.category`), and does
   `reports_app/controllers/restaurant/sales.py` reject `>731` days with 400 on the `sales-trends` slug?
   If both, it's a live latent 400 on that preset after the mock flip.
2. **FE `PaymentStatus` union includes `'refunded'` but backend `payment_status` emits only `paid/failed/pending`.**
   `src/app/restaurant-mgt/reports/models/reports.models.ts` vs
   `dinify_backend/configss/string_definitions.py:43-45`. `refunded` is an `order_status` value, not a
   `payment_status` one. **Exact check:** confirm no FE surface switches on `payment_status === 'refunded'`
   expecting the backend to emit it (if it does, that branch is dead). Cosmetic either way.

---

## Documented issues: confirmed fixed vs still live

**Confirmed FIXED (verified against code):**
- `BREAKING_CHANGES.md` "19 monetary `FloatField`s" → **FIXED.** The only `FloatField`s in any `models.py`
  are `Table.floor_x/y/width/height` (floor-plan geometry, not money); `scripts/check_money_fields.py` is clean.
- `REPORTS_CONTRACT_AUDIT.md` B1 (non-ISO `sales-trends` `period` → FE `parseISO()` throws) → **FIXED** in
  `reports_app/controllers/restaurant/sales.py` (ISO/sortable keys `YYYY-MM`, `YYYY-Qn`, `YYYY`).
- `SECURITY_AUDIT_REPORT.md` F02 (RBAC guard commented out) → **FIXED** (`_helpers/auth.guard.ts` enforces roles).
- `SECURITY_AUDIT_REPORT.md` F05 (no token refresh) → **FIXED** (reactive 401→refresh→retry in `error.interceptor.ts`).
- `SECURITY_AUDIT_REPORT.md` F08/F10 and the diner/portal Orders + Payments findings → **moot** (those files
  deleted: `diner-app/orders/`, `restaurant-mgt/orders/`, `restaurant-mgt/payments/`).
- `REGULATORY_AUDIT.md` custodial money-flow (DinifyAccount/disbursement/refund-payout/tips/OVA) → **removed**
  (teardown PRs #149–#162); that doc is now historical.

**Still LIVE (confirmed against code):**
- `BREAKING_CHANGES.md`/`README.md` — `MsisdnLookupEndpoint` AllowAny enumeration → **live** (BUG-P3-1).
- `BREAKING_CHANGES.md` — `OrderPaymentsEndpoint` AllowAny → **live** (BUG-P2-7 + BUG-P2-3e).
- `SECURITY_AUDIT_REPORT.md` F04 — JWT + profile in `localStorage` → **live**, but documented-deferred
  (httpOnly-cookie migration needs backend coordination) — see *Cleared*.
- `SECURITY_AUDIT_REPORT.md` F03 — `SafePipe` sanitization bypass → **downgraded**: only the `'style'` branch
  bypasses (the `'html'` branch sanitizes), and there are **zero** `safe:'style'` use-sites — see *Cleared*.

**Note:** the task listed `DEAD_CODE_AUDIT.md` as a backend input; **no such file exists** in
`mugak1/Dinify-Backend` (the root audit docs are `BREAKING_CHANGES.md`, `REGULATORY_AUDIT.md`,
`REPORTS_CONTRACT_AUDIT.md`).

---

## Cleared / assessed — not a bug (with reasoning)

These were investigated and are **not** defects; recorded so they aren't re-raised.
- **Order pricing is server-authoritative (CLEARED).** `determine_effective_unit_price`
  (`con_orders.py:174-211`) uses `menu_item.effective_base_price()` and resolves modifier surcharges from the
  server-stored `options` blob; `add_order_item` derives all costs server-side; the client sends no prices.
  Displayed == charged; tampering is inert.
- **Kitchen views are all module-gated; `available`/`in_stock` handling is intentional (CLEARED).** All 8
  kitchen views gate on `can_user_access_module(..., MODULE_KITCHEN)`; the goodwill-cancel escalation adds
  `can_manage_restaurant`; transitions are one-step-forward with explicit recalls; writes are `update_fields`-scoped.
- **Reports per-order listing 31-day cap (CLEARED).** The backend caps listing ranges at 31 days, but the FE
  clamps/skips (`sales-report.component.ts` only calls the listing when ≤31 days; Transactions/Diners wrap in
  `recentWindow()` clamping to 31 days) — the predicted post-flip 400s do not occur.
- **"Production" serving mock Dashboard/Reports data (assessed — tracked risk, not a shipping defect).**
  `dashboard.service.ts` `USE_MOCK_DATA=true` and `reports.service.ts` `USE_MOCK_DATA=true` do ship, and
  `deploy-prod.yml` builds `--configuration=uat`, but this is explicitly documented-as-intended (both CLAUDE.md
  and an inline `deploy-prod.yml` rationale): `dinify-prod.web.app` is a pre-launch shell pointed at a
  UAT/test backend, "no real restaurant with orders exists yet," with a defined flip-time gate. No real user
  views fabricated analytics today. **Track:** add a "sample data" banner or flip before real GA — and note
  BUG-P1-2/BUG-P3-9 are the concrete correctness issues that must be resolved *before* that flip.
- **JWT in `localStorage` (assessed — documented deferred).** Real storage surface, but no active injection
  sink found and it is the documented httpOnly-cookie deferral (needs backend coordination). Architectural
  tradeoff, not a code defect.
- **`SafePipe` `'style'` bypass (assessed — latent, zero use-sites).** Only the `'style'` branch calls
  `bypassSecurityTrustStyle`; a repo-wide search finds no `safe:'style'` binding (the one live `safe:` use is
  `safe:'html'`, which sanitizes). Dead footgun with an explicit warning docstring — hygiene, not a live hole.
- **`lucide-angular` in diner order-complete (assessed — documented exception).** `diner-app.module.ts:78-81`
  documents this as an owner-approved exception to the inline-SVG rule.
- **Tables Service View reads target a nonexistent `api/v1/tables/*` mount (assessed — parked/dead path).**
  The reads sit behind `USE_MOCK_SERVICE=true` and the Service View is force-hidden (`activeView='setup'`);
  CLAUDE.md flags these paths must be wired before the flag flips. (Minor: a live request would 404 at the
  URLconf, not fall through to the `restaurant-setup` catch-all.)

---

*End of audit. Diagnosis only — no fixes were applied. Each finding lists a fix **direction**, not an
implementation; every fix is a separate scoped PR.*
