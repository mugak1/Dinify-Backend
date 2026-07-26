# Dinify Backend — Claude Code Context

## Project Overview
Dinify is a QR-code-based digital ordering and restaurant management platform
built for Uganda and mobile-money-first markets. Django/DRF backend on AWS EC2
with PostgreSQL on AWS RDS.

## Tech Stack
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
  Do NOT create a new dashboard endpoint — it already exists
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
  `dinify_backend/tenancy/tests_ambient_authority.py` keep it that way
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
  - `AdminLoginChallenge` + `AdminSession` — two-step login as separate models, so
    "has a session" never stops meaning "fully authenticated": `auth/login/` checks
    the password and mints a short-lived challenge in its own `__Host-` cookie, and
    only `auth/verify/` (TOTP or recovery code) mints the opaque cookie session.
    Every login failure returns one byte-identical body (the password check runs
    against a dummy hash for unknown users so timing does not leak existence); the
    real reason goes to the audit log. TOTP NEVER consults `ENV` — it shares no code
    path with the restaurant OTP flow's `ENV=dev` `1234` shortcut. `auth/elevate/`
    marks a session recently-elevated for the destructive routes
  - `AdminAuditLog` — append-only (an update raises `AppendOnlyViolation`),
    recording actor / session / action / resource / restaurant / delegation /
    reason / before+after state / result / request id
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
  Pattern A when the counsel and PSP integration gates clear
- Reports module — rebuilt on the clean contract: ✅ Complete. All four
  restaurant reports (`api/v1/reports/restaurant/<name>/` →
  `RestaurantReportsEndpoint`, `{status, message, data}` envelope) are rebuilt on
  shared foundations in `reports_app/controllers/common/`: `sale_filters.py` (the
  canonical "what is a sale / what is revenue" — `SALE_STATUSES` {served, paid},
  revenue = `Sum('actual_cost')`, discount = `Sum('savings')`; Order-based) and
  `bucketing.py` (single grouped-query, EAT-aligned period bucketing — no
  per-period loop). Rebuild contract: RAW enum values (the frontend owns display
  formatting — NO backend `.title()`-casing), a stable 0-filled shape, and
  grouped queries (no per-bucket / per-row N+1). Rebuilt: Sales
  listing/trends (PR #165, `sales.py`), Transactions summary/listing
  (PR #166, `transactions.py`), Diners summary/listing (PR #167, `diners.py`),
  and Menu summary (PR #168, `menu.py`; the menu-summary date-range cap
  was later relaxed in PR #169) — each with its own `tests_*_report.py`.
  Sales additionally exposes `sales-hourly/` — an EAT-aware hour-of-day (0–23)
  distribution (PR #184, `bucket_sales_by_hour` via `ExtractHour(tzinfo=LOCAL_TZ)`,
  keyed by integer hour so deliberately NOT a `PERIOD_TRUNC` entry, zero-filled
  to a continuous 24-hour axis on the same revenue basis). Sales-trends now
  emits ISO/sortable `period` keys (`2024-03` for month, `2024-Q1` for quarter;
  day/year already ISO) so the frontend can `parseISO()` every bucket (PR #185);
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
  `TENANT_ISOLATION_CLOSURE.md` (tenant boundary), `REGULATORY_AUDIT.md`
  (non-custodial posture), `REPORTS_CONTRACT_AUDIT.md` (cross-repo Reports
  contract) and `BACKGROUND_TASKS.md` (management-command runbook — note there is
  NO scheduler configuration in this repo; every command is invoked externally)
- Login 500 regression: ✅ Resolved — not reproducible after the auth-stack work;
  login → refresh → logout verified working on UAT (closed June 2026)
- Django 5.2 LTS upgrade: ✅ Complete — Django 4.2.30 → 5.2.15 (PRs #123–#125).
  Forward-compat deps bumped: `asgiref` 3.11.1, `django-cors-headers` 4.9.0 (DRF
  3.17.1 / SimpleJWT 5.5.1 / psycopg 3.1.18 already supported 5.2). The
  deprecation surface was clean — no removed-in-5.x APIs in use, no new
  migrations generated, `USE_TZ` already explicit. App timezone code now uses
  stdlib `zoneinfo`; `pytz`/`numpy`/`pandas`/`tzdata` were removed from
  requirements entirely (no app imports remain — do not reintroduce `import pytz`)

## Deployment Rules — CRITICAL
- Merging a PR to main automatically triggers GitHub Actions to pull code,
  install dependencies (`pip install -r requirements.txt`), run migrations,
  and restart Apache
- The deploy DOES reinstall dependencies, so a `requirements.txt` change takes
  effect on the next deploy. The install runs after `git pull`, before
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
  still serving. AFTER the restart an HTTP probe of the login route through
  local Apache must return exactly 405, so a boot-dead app turns the deploy
  red instead of green-and-down. Deployed runtime secrets (e.g.
  `DINER_CAP_KEY`) must be stored in the project `.env` — the contract the
  gates validate directly (`RepositoryEnv('.env')` as `www-data`) — readable
  by `www-data` through group-read, with no group-write and no permissions
  for other users (currently `ubuntu:www-data` mode 640); never in a shell
  profile
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
- `api/v1/orders/` → v1 orders (urls.py) — only `submit` (PUT) is live; the
  orphaned, unscoped `prepare`/`cancel`/`update-item` write actions were
  RETIRED (finding H3, PR #181) and any retired/unknown action now 404s
  (hardened dispatch, no fallthrough to 500). Superseded by `api/v1/kitchen/`,
  which gates every write
- `api/v2/orders/` → v2 orders (v2_urls.py) — separate file, don't confuse;
  only `initiate` (POST) is live. `add-items` (POST/DELETE) was retired
  (PR #181) and the AllowAny, unscoped `details/` GET was retired (finding C1,
  PR #182) — both 404 via the hardened dispatch. An `initiate`d order is a true
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
  Explicit deny-by-default routes only — health, `auth/*`, `delegations/*`, and
  `restaurants/<uuid:id>/transition/` (the ONLY writer of `Restaurant.status`,
  elevation-gated)

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
- `flat_fee` (the Dinify subscription price billed by
  `finance_app.tx_subscription`) is registered in `EDIT_INFORMATION['restaurants']`
  but is PLATFORM-owned — the restaurant-setup write path STRIPS the key from
  EVERY `restaurants` PUT payload AFTER `check_permission` and BEFORE the Secretary
  dispatch (PR #211; made unconditional by PR-A), so no principal on this plane can
  zero a subscription price. It used to be stripped only for non-admins, which left
  a `dinify_admin` role-holder able to write it. This is a post-gate payload strip,
  NOT an EDIT_INFORMATION removal — the key deliberately STAYS in EDIT_INFORMATION
  so the Phase-1 admin-plane writer can still go through Secretary
- `status` is DIFFERENT and stricter: PR-5 REMOVED it from
  `EDIT_INFORMATION['restaurants']` entirely (and made it `read_only` on
  `SerializerPutRestaurant`), so NO principal writes it through Secretary — see
  the "Restaurant Lifecycle" section. `Table.status` was removed from
  `EDIT_INFORMATION['table']` in the same PR for its own reason: the dedicated
  `table-actions/update-status/` verb validates against `TABLE_STATUS_CHOICES` and
  keeps `is_active` in step with `out_of_service`, which the generic path did not.
  No `EDIT_INFORMATION` section exposes a `status` key any more
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
  either. The `flat_fee` non-admin strip in `restaurant_setup.py` REMAINS
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
- Per-state behaviour: staff portal + kitchen + order-create are allowed for
  onboarding/live and blocked for suspended/offboarded; the diner menu is served
  for onboarding/live, answers a graceful **503** for `suspended` (the ONE state
  that does not collapse to the generic 404 — a diner at a printed QR is told the
  place is temporarily unavailable) and a flat 404 for `offboarded`; support is
  reachable in every state; a delegated admin session is CAPPED to `view` scope at
  an `offboarded` restaurant (`DelegationContext.scope` applies the ceiling;
  `granted_scope` exposes the as-minted value)
- TWO PHASE-1 SEAMS, both named single-call functions with real call sites —
  `check_go_live_readiness` (returns ready today; the hard blockers need
  Phase-1 models) and `has_outstanding_receivables` (returns False today;
  `SubscriptionInvoice` does not exist yet, and `DinifyTransaction` is never the
  receivable). Do NOT inline either at a call site
- Admin transition endpoint: `POST admin/v1/restaurants/<uuid:id>/transition/`
  (`platform_admin_app/endpoints/restaurants.py`), `AdminAPIView` +
  `IsRecentlyElevated` — body `{to_state, reason}`. It is deliberately ABSENT from
  the delegated `ALLOWED_ROUTES` allowlist, so a delegated session can never
  transition anything
- The go-live owner notification (`restaurant-activated`) moved from the deleted
  `Secretary.make_notification` hook into the lifecycle service, fired
  post-commit and best-effort. There is no `restaurant-rejected` counterpart —
  `rejected` is not a state in the new vocabulary

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
  existing admin and revokes all of its sessions. Does NOT change the password
- Five more exist and are equally not-to-be-recreated: `vacuum_deleted_records` +
  `vacuum_configuration` (`misc_app`), `send_messages` + `send_test_sms`
  (`notifications_app`, the latter being the ENV-bypassing SMS credential probe
  described under SMS / Yo Uganda), and `determine-customers` (`orders_app`).
  `BACKGROUND_TASKS.md` is the full runbook — what each touches and where the
  operational gaps are

## Database
- `CONN_MAX_AGE: 600` for persistent DB connections — do not remove
- All migrations must be generated and included in PRs when models change
- Latest migration: `restaurants_app/migrations/0056_restaurant_lifecycle_states.py`
  (0054 adds `Table.qr_version`; 0055 data-repairs MenuItem extras — see the
  "Write-time menu relationship integrity" bullet; 0056 constrains
  `Restaurant.status` and fail-closed-maps the legacy vocabulary — see
  "Restaurant Lifecycle"),
  `orders_app/migrations/0034_remove_order_block_review_and_more.py`,
  `finance_app/migrations/0028_remove_dinifytransaction_tip_amount.py`,
  `reviews_app/migrations/0003_review_tags.py`,
  `users_app/migrations/0013_close_ambient_admin_authority.py` (0010 adds
  `User.account_type`; 0011 flips existing platform-role holders to
  `platform_staff`; 0012 makes `phone_number` unique — see the "Platform-admin
  identity layer" bullet; 0013 blacklists outstanding platform-staff refresh
  tokens and strips platform-only roles from `restaurant_user` rows, data-only and
  idempotent — see "Tenant Isolation / Role-Permission ENFORCEMENT"),
  `platform_admin_app/migrations/0006_delegatedsession.py` (0001 identity,
  0002 `AdminSession`, 0003 `AdminAuditLog`, 0004 TOTP replay counter,
  0005 `DelegationGrant`, 0006 `DelegatedSession`),
  `misc_app/migrations/0004_drop_service_tickets.py`

## CI — `.github/workflows/ci.yml`
- Runs on push to `main`, `develop`, `claude/**` and on PRs to `main`/`develop`
- Runs on **Python 3.10.12**, pinned in `ci.yml` to match the prod EC2 runtime
  (the UAT venv is `python3.10`) — keep CI and prod on the same interpreter; any
  dependency bump must satisfy `requires-python <= 3.10`. Note: Django 5.2.x is
  the LAST series supporting Python 3.10/3.11 — a future Django 6.0 upgrade
  requires bumping the prod interpreter first
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
- `scripts/verify.sh` is the committed source of truth that runs the same
  checks locally in the same order; the `/dinify-check` command defers to it.
  Run it and paste the output before raising a PR
- A separate `.github/workflows/deploy-uat.yml` deploys to the UAT host on
  push to `main` (pull, migrate, restart Apache) — reinforces the deployment
  rule above: never pull/migrate/restart manually
- A scheduled `.github/workflows/audit.yml` runs a weekly `pip-audit` sweep
  (Mondays 06:30 UTC) + manual `workflow_dispatch` — NOT triggered on
  PRs/pushes, so it never becomes a blocking PR check; a failure (advisories
  found) fires GitHub's scheduled-workflow notification

## Verification
Before raising any PR, run `./scripts/verify.sh` (mirrors CI) and confirm:
1. Confirm migrations are generated for any model changes (CI enforces this)
2. Confirm new editable fields are added to EDIT_INFORMATION
3. Confirm no synchronous SMS calls introduced (except where return value needed)
4. Confirm no new hard MongoDB dependencies
5. Confirm monetary fields use DecimalField
6. If adding a new endpoint, confirm it's registered in urls.py above the catch-all
7. Confirm no reintroduction of `clear_<field>` sentinels in PUT payloads
