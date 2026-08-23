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
- Order-path READ BUDGET: ✅ (PR-H §4) — the per-line cost inside `_create_order`'s
  transaction is **4 queries** (the chokepoint's restaurant-scoped `MenuItem` guard,
  the merge lookup, the allergen-tag read, the INSERT); a 4-line order runs 35
  queries, a 1-line order 23. It was 9/line, 54 and 27. Pinned by
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
- Legacy restaurant adoption WRITER: ✅ (Phase 1, Step 2B) — the first and only thing
  that writes the onboarding domain. `platform_admin_app/onboarding_adoption.py`
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
  gate was a `dinify_admin` role string. **There is currently NO API path that
  creates a restaurant**; this is a knowingly accepted gap until Phase 1 builds
  onboarding natively on `/api/admin/v1`. Do not re-add one on the customer plane.
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
  `restaurants/` + `restaurants/<uuid:id>/` (the Phase-1 Step-1 directory and
  detail READS — see "Admin Restaurant Reads" below), and
  `restaurants/<uuid:id>/transition/` (the ONLY writer of `Restaurant.status`,
  elevation-gated). The two reads are session-gated but NOT elevation-gated, and
  are NOT audited — see that section for why both are deliberate

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
  one never reaches for the advisory lock, so it can only block, never cycle)
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
  Nothing is stranded: no API path creates a restaurant, and the one production
  restaurant is already `live`. There is deliberately NO override — do not add one.
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
GET admin/v1/restaurants/                  -> AdminRestaurantListView
GET admin/v1/restaurants/<uuid:id>/        -> AdminRestaurantDetailView
```

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
Step 1 exposes truth that exists TODAY. Each of these was easy to fake and is not:

- **Readiness** delegates to `lifecycle.check_go_live_readiness` — the ONE seam. It
  fails closed today with `readiness_not_configured`, so that is what the portal is
  told. Do NOT build a second checklist here; Step 3 fills the seam. Readiness is
  reported as `not_applicable` outside `onboarding`: "is it ready to go live" has no
  answer for a live or offboarded tenant, and zero blockers there would read as ready
- **Payment mode** has NO authoritative persisted field. `require_order_prepayments`
  is a diner-checkout toggle, NOT the spec's commercial `cash_only`/PSP mode — do not
  infer one from the other. Emitted as `payment_mode: null` +
  `payment_mode_configured: false`
- **Subscription** reports the LEGACY `Restaurant` columns under names that say so
  (`source: 'legacy_restaurant_fields'`, `legacy_validity_flag`, `legacy_expiry_at`).
  `RestaurantSubscription` / `SubscriptionInvoice` / `SubscriptionPayment` do not
  exist. **`has_outstanding_receivables` is NOT consulted** — it returns a cheerful
  `False` that means only "invoices do not exist", and wiring it belongs in the same
  change that makes an invoice capable of becoming overdue

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

## Admin Onboarding Domain — Phase 1, Steps 2A + 2B + 2C

Three faces of one domain, all in `platform_admin_app/`: the SCHEMA and its validator
(Step 2A — two models in `models.py` plus `onboarding.py`, migration
`platform_admin_app/0009`), ONE writer (Step 2B — `onboarding_adoption.py` and its
management command, no migration), and the READ projection (Step 2C —
`onboarding_reads.py`, no migration). **There is still NO endpoint that writes, NO
invitation service and NO attestation writer.** **No restaurant is adopted
automatically**: there is no backfill, no signal and no `get_or_create`, so absence
still means "not yet represented in the Admin onboarding domain".

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
  — which is what makes the future reissue path's supersede step load-bearing,
  exactly as `challenges.create_challenge` consumes before it inserts
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
It lives beside the domain's other two faces because what it computes is ONBOARDING
semantics (what counts as evidence of owner control), not directory presentation.

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
                                     "consumed" | "cancelled" | "superseded"}
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
  ONLY writer of the Admin onboarding domain. Represents exactly ONE pre-existing
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
  `orders_app/migrations/0035_order_is_test.py` (0034 removed the inline review
  fields; 0035 adds the launch-boundary `Order.is_test` flag),
  `finance_app/migrations/0028_remove_dinifytransaction_tip_amount.py`,
  `reviews_app/migrations/0003_review_tags.py`,
  `users_app/migrations/0013_close_ambient_admin_authority.py` (0010 adds
  `User.account_type`; 0011 flips existing platform-role holders to
  `platform_staff`; 0012 makes `phone_number` unique — see the "Platform-admin
  identity layer" bullet; 0013 blacklists outstanding platform-staff refresh
  tokens and strips platform-only roles from `restaurant_user` rows, data-only and
  idempotent — see "Tenant Isolation / Role-Permission ENFORCEMENT"),
  `platform_admin_app/migrations/0009_restaurantonboarding_ownerinvitation_and_more.py`
  (0001 identity, 0002 `AdminSession`, 0003 `AdminAuditLog`, 0004 TOTP replay counter,
  0005 `DelegationGrant`, 0006 `DelegatedSession`, 0007 the break-glass
  `recovery_only` challenge flag, 0008 the one-live-challenge partial unique index
  preceded by an idempotent duplicate-consuming data repair — see "Platform-admin
  control plane"; 0009 creates the onboarding domain, two new tables with their
  constraints and NOTHING else — no `RunPython`, no backfill, no existing table
  touched — see "Admin Onboarding Domain"),
  `misc_app/migrations/0004_drop_service_tickets.py`

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
