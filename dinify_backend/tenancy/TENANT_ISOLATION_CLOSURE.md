# Tenant-Isolation Closure — Backend (TENANT-ISO-PR6A)

> **This is an engineering closure record, not a certification and not a
> penetration test.** It documents what the tenant-isolation regression suite
> proves, at which commits, under which threat model — and, explicitly, what it
> does **not** prove.

## Narrow closure statement

At the audited commits below, the core Dinify **restaurant** and **dine-in
ordering** paths have **no known critical or high-severity tenant-isolation
defect under the tested threat model**. The key authorization boundaries are
exercised by adversarial regression tests in both repositories and run in CI.

We do **not** claim the whole application is perfectly secure, that every future
feature is automatically tenant-safe, that all OWASP categories were audited,
that this suite replaces a penetration test, that read/archival relation-baseline
entries are vulnerabilities, that mocked Mongo proves live Mongo behaviour, or
that public restaurant/menu media is confidential.

## 1–2. Audited commits

| Repo | Commit (audit base) |
|---|---|
| mugak1/Dinify-Backend | `fa3b94d0dd07134e4ff8f65cc4bc1dde0c213753` |
| mugak1/Dinify-Frontend | `662ee05708e18cd04726cc88a033b2f7e058e90c` |

The frontend counterpart is `docs/TENANT_ISOLATION_CLOSURE.md` in
mugak1/Dinify-Frontend (PR6B). The two PRs are linked; neither auto-merges; no
runtime cross-repo import or pinned clone exists.

## 3. Threat actors exercised

Unauthenticated caller (no diner credential); caller holding only a raw
restaurant/table/order/transaction UUID; caller holding a valid QR credential;
caller holding a valid short-lived table session; caller with a tampered
credential/session; caller with an expired session; caller whose
credential/session was invalidated by QR rotation; staff of restaurant A only;
staff of both A and B; staff with module access at A but not B; restaurant owner;
Dinify platform administrator; client submitting foreign nested IDs in an
otherwise-authorized request; client submitting forbidden audit/lifecycle/
privilege/QR fields; client replaying an idempotency key across restaurants;
client attempting rapid duplicate QR rotations.

Fixtures use **two** independent restaurants with independent tables, sections,
groups, menu items, extras, employees, orders and transactions
(`ClosureFixtureBase`).

## 4. Surfaces audited (boundary matrix)

| Surface | Auth channel | Tenant resolution | Disclosure posture |
|---|---|---|---|
| Table scan (`orders/journey/table-scan/`) | `X-Diner-Credential` header only | signed QR credential → (restaurant, table, generation) | 400 malformed / 404 unknown·foreign·stale (non-disclosing) |
| Order initiate (`api/v2/orders/initiate/`) | `X-Diner-Session` header (or staff JWT) | session-derived restaurant+table; body ids may only MATCH | 400 |
| Order submit / details, payment details, review submit | `X-Diner-Session` header | scoped to the session's table (restaurant_id, table_id) | 404 foreign/unknown/malformed |
| Public menu (`orders/journey/show-menu/`) | none (AllowAny) | `?restaurant=` resolved fail-closed | 400 missing / 404 unknown·deleted·non-active |
| Public directory (`restaurant-setup/misc-public/restaurants/`) | none | `status='active'`, `deleted=False` forced | soft-deleted never surfaces; table directory retired (404) |
| RestaurantSetup CRUD (`restaurant-setup/<record>/`) | staff JWT | server-resolved restaurant from resource FK; `_RECORD_MODULE` gate | 403 write-deny / 404 cross-tenant detail / 409 delete-blocked |
| `restaurant-setup/details/` | staff JWT | FK-chain resolve + module gate (allowlisted models) | 404 cross-tenant·unknown·unmapped |
| QR rotation (`table-actions/regenerate-qr/`) | staff JWT | table's restaurant, `MODULE_TABLES` gate | 401 unauth / 403 cross-tenant·no-module / 404 unknown |
| Subscription (`finances/transactions/`) | staff JWT | `restaurant_id` body authorized via `can_manage_restaurant` | 404 non-manager·cross-tenant |
| Self profile (`users/user-profile/`) | staff JWT | acts only on `request.user`; whitelisted fields only | privilege fields ignored |

## 5. Security invariants (and the test that proves each)

All closure tests live in `dinify_backend/tenancy/tests_tenant_isolation_closure.py`
(tagged `@tag('tenant_closure')`) unless noted. The gate runs this module
alongside the deep suites it builds on.

| # | Invariant | Proving test |
|---|---|---|
| I1 | A raw table UUID (query or body) mints no session; credential/session are header-only and not interchangeable; tampered/expired rejected; unknown/foreign are non-disclosing; an invalid session never falls back to staff JWT; capability responses are `no-store`; the cap key/session never reach logs | `AnonymousEntryClosureTests`; depth in `restaurants_app.tests_diner_capability` |
| I2 | QR rotation revokes the old credential AND live sessions, mints a working new credential, leaves other tables untouched, increments the generation once, is `MODULE_TABLES`-gated, cannot be driven cross-tenant, and never leaks the cap key; ordinary table PUT cannot forge `qr_version`/`qr_regenerated_at` | `QrRotationClosureTests`; `restaurants_app.tests_write_surface_tenancy` |
| I3 | A session for A cannot initiate/submit/read-order/read-payment/review restaurant B via body ids; foreign/unknown/malformed are indistinguishable 404; rejections make no state change; the staff-only `source` flag cannot be leveraged from the anonymous channel | `DinerResourceScopingClosureTests` |
| I4 | Pending/deleted restaurant serves no anonymous menu (paused-but-active still shows menu); hidden section/group/item cannot be ordered; foreign & non-allowlisted extras rejected; staff bypass keeps tenant integrity; unpublish blocks new checkout in-transaction; sold-out lines use zero-and-flag reconciliation (never hard-rejected) | `MenuPublicationCheckoutClosureTests`; read side in `restaurants_app.tests_menu_publication_boundary` |
| I5 | Modifier group/choice IDs are validated against the ordered item's own options, cost is recomputed server-side, min/max is re-enforced **in-transaction** (PR6A A0), and duplicate choices are de-duped so they neither trip limits nor double-charge; snapshots derive only from the validated parent | `ModifierIntegrityClosureTests` |
| I6 | Staff A can read/write only A; cross-tenant list is scoped out, detail is 404, update-by-id and spoofed-`restaurant` body are denied, delete is denied, foreign nested FK is rejected; module-scoped staff cannot write outside its module; admin scope is unrestricted | `StaffTenantIsolationClosureTests`; relation depth in `restaurants_app.tests_write_surface_tenancy` |
| I7 | Bulk-created tables/section-groups are bound server-side to the already-gated restaurant/section; cross-tenant bulk create is denied and atomic | `BulkCreationTenantClosureTests` |
| I8 | User privilege fields (`is_staff`/`is_superuser`/`password`/`groups`/`permissions`) are never mass-assignable; restaurant `owner`/audit fields and platform `status`/`flat_fee` never take effect from a non-admin | `MassAssignmentClosureTests`; `restaurants_app.tests_write_surface_tenancy` |
| I9 | Soft-deleted restaurants stay hidden regardless of `?deleted`; the anonymous table directory is retired; public directory / scan / menu leak no owner PII or internal audit/lifecycle fields | `PublicDirectoryPiiClosureTests`; `restaurants_app.tests_misc_public` |
| I10 | Idempotency keys and daily counters are per-restaurant: the same `client_order_id` across two tenants yields independent orders, a replay returns only the same-tenant order, and counters are independent | `IdempotencyCounterScopingClosureTests` |
| I11 | The subscription write authorizes the target restaurant (`can_manage_restaurant`) and derives the amount server-side (`flat_fee`), never from the body | `SubscriptionTransactionClosureTests` |
| I12 | `get_detail` dynamic dispatch is allowlisted, module-gated, server-resolves the record's restaurant, and returns an indistinguishable 404 for cross-tenant/unknown/unmapped | `GetDetailDispatchClosureTests` |
| I13 | Cross-repo contract constants (header names, salts, routes) match the frontend's | `ContractParityClosureTests` |
| I14 | Menu relationship integrity + deterministic concurrency (the authoritative race proof — invoked, not replaced) | `restaurants_app.tests_menu_relationship_integrity`, `restaurants_app.tests_menu_relationships_concurrency` |

## 6. CI command (the closure gate)

Runs in `scripts/verify.sh` and `.github/workflows/ci.yml` as a named,
fail-fast step **before** the full PostgreSQL suite (which it does not replace):

```
python -m django test \
  dinify_backend.tenancy.tests_tenant_isolation_closure \
  restaurants_app.tests_diner_capability \
  restaurants_app.tests_menu_relationship_integrity \
  restaurants_app.tests_menu_relationships_concurrency \
  restaurants_app.tests_write_surface_tenancy \
  --settings=dinify_backend.test_settings --verbosity=2
```

## 7. Relation baseline

`dinify_backend/tenancy/baseline.txt` holds **61** entries. As of TENANT-ISO-PR5
every one is a **read or archival** serializer relation (`SerializerGet*`,
`SerializerList*`, `SerializerPublicGet*`, `SerArc*`) — **zero production write
serializers remain**. The count is a **classification-coverage** measure (the
ratchet proves every writable relation is consciously classified or baselined),
**not** a vulnerability count, and it may only shrink. See `ASSURANCE.md` for the
exact assurance boundary; `WRITE_ALL_FIELDS_DEBT` is empty and frozen.

## 8. Non-FK inventory disposition

`dinify_backend/tenancy/non_fk_tenant_inventory.py` — 17 entries after this pass:

- **13 remediated** (server-side invariant + adversarial test cited in the note):
  MenuItem.options ids, OrderItem.selected_modifiers, MenuItem.extras_applicable,
  MenuItem tag_ids, menu-create `server_values` binding, DinifyTransaction
  restaurant_id gating, `Order.objects.create`, RestaurantRolePermission writes,
  Table/SectionGroup bulk_create, Secretary dynamic dispatch, `get_detail`
  dispatch, `client_order_id` idempotency, `RestaurantDailyOrderCounter`.
- **4 audited** (examined; tenant-LOCAL data or boundary-at-a-tested-endpoint;
  cannot select/bind another tenant; no dedicated adversarial test in this PR):
  `MenuItem.discount_details`, `Restaurant.socials/preset_tags/cuisine_types/
  branding_configuration`, `OrderItem.*_snapshot`, `DinifyTransaction.objects.create`
  in `tx_subscription` (its boundary is the tested endpoint gate).
- **0 pending-audit.** The pending set is empty because every entry received an
  evidence-backed disposition — **not** because entries were flipped to zero the
  count. The four `audited` entries are the honest residual: safe by
  construction/reasoning (with adjacent test evidence cited), but resting on
  argument rather than a dedicated adversarial test.

## 9. Public-media disposition

Every uploaded file field is a public-by-design `ImageField`:
`Restaurant.logo`, `Restaurant.cover_photo`, `MenuSection.section_banner_image`,
`MenuItem.image`. There is **no** private/KYC/document/identity file field, so no
private asset shares an unauthenticated media path. Predictable public filenames
for branding/menu images are acceptable, intended product behaviour — no
signed-media work is required, and no tenant-isolation claim treats a public
branding image as confidential. If a private document field is added later, it
must not rely on an unguessable URL alone (a re-audit trigger, below).

## 10. Accepted residual risks

- The suite proves the **tested threat model**, not the absence of all defects.
- The four `audited` non-FK entries rest on reasoning + adjacent tests, not
  dedicated adversarial tests.
- Mocked MongoDB behaviour (`archive_record` / `save_action_log` are wrapped and
  best-effort) does not prove live Mongo deployment behaviour; Mongo is currently
  unreachable from EC2 and carries only action logs/archiving.
- `SerializerPublicGetRestaurant` (name notwithstanding) exposes owner email/phone
  but is wired **only** to the authenticated, module-scoped catch-all GET; the
  anonymous directory uses `SerializerMiscPublicRestaurant`, which omits the owner
  block. The separation is correct and tested; the name is a documented footgun.
- Diner payment is unwired (`payment_status` stays `pending`); the PSP order-payment
  write path will be rebuilt authenticated + ownership-gated + server-bounded.

## 11. Out of scope (explicit non-goals)

New auth architecture / OAuth-OIDC; a full penetration test; general OWASP work
unrelated to tenant isolation; resolving every read/archival baseline entry;
converting JSONFields to relational tables; PSP/payment-provider implementation;
customer-account or order/kitchen redesign; bulk QR rotation or
revoke-without-replace; media CDN redesign for intentionally-public images; Mongo
infrastructure deployment; replacing Secretary; a generic authorization DSL.

## 12. Re-audit triggers

Re-run this closure audit when any of these change:

- diner capability / token transport (headers, salts, TTL, header-only rule);
- QR generation or rotation / revocation;
- the canonical menu-publication policy (`menu_publication.py`);
- order creation or idempotency (`create_order.py`, `con_orders.py`);
- restaurant ownership resolution (`_RESTAURANT_RESOLVERS`, resolvers);
- the permission modules / resolver primitives (`permissions_check.py`);
- the Secretary or another generic CRUD layer;
- production write serializers (any `SerializerPut*`/write surface);
- JSON/array tenant references (the non-FK inventory);
- public serializers (`SerializerPublicGet*`, `SerializerMiscPublic*`);
- media privacy classification (any new `FileField`/`ImageField`).

## 13. UAT adversarial smoke (dedicated UAT fixtures — no real customer data)

1. Log in as Restaurant A staff; attempt B list/detail/update/delete by id and
   with foreign nested section/group/table/tag/extra ids → confirm no B data or
   state is exposed.
2. Scan a valid A table QR → confirm a session is issued and the menu loads.
3. Attempt a raw-UUID scan, and query/body credential + session → all denied.
4. With an A session, attempt B order/payment/review ids → denied.
5. Rotate A's QR → old credential fails, old live session fails, new credential
   succeeds, B's table is unaffected.
6. Attempt a rapid double rotation → one effect.
7. Ordinary table edit with `qr_version`/`qr_regenerated_at` in the body →
   ignored.
8. Profile update with `is_staff`/`is_superuser`/`password` → ignored.
9. Attempt to checkout a hidden item / foreign extra / unpublished-via-upsell →
   rejected.
10. Confirm a legitimate order → kitchen preparation → review still works.
11. Confirm portal login and the deploy HTTP-405 health gate remain green.
