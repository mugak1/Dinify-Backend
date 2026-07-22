# Dinify Backend — Dead-Code / Dead-Endpoint Audit: Program Closure Record

> **✅ STATUS — PROGRAM CLOSED.** This is the closing record of the backend
> dead-code / dead-endpoint audit program. Every surface the audit raised has now
> been either EXECUTED (removed / retired), FIXED FORWARD (real defect patched),
> or consciously KEPT with a recorded rationale. The decision-gated retirements —
> the ones that needed a product/architecture call before removal — landed in the
> final PR recorded below, alongside this record. No open audit findings remain.
> This is a historical record: the finding IDs and claims in it are valid at the
> SHAs cited beside them, not necessarily at any later HEAD.

> **Provenance.** The originating audit was read-only reconnaissance performed
> 2026-07-21 against `90d21b4` (the merge of PR #242). The audit itself changed no
> code; it classified each surface as retire / fix-forward / keep and prescribed
> the remediation that was then sequenced across PRs #244, #245, #247 and this PR.
> Finding IDs and descriptions below are drawn from the git record at the cited
> commits.

## Context — why this program existed

The backend had accumulated dead code and dead / orphaned HTTP surfaces: report
slugs kept only "so the dispatch is unchanged", serializers with no live consumer,
an anonymous public-directory endpoint superseded by the QR-capability flow, and a
manager-OTP profile-update endpoint the frontend never called. Dead surfaces are
not free — an unreferenced `AllowAny` endpoint or an unscoped serializer is attack
surface and audit burden. The program's goal was to shrink that surface to only
what is live, and to close with a durable record so the removals are not silently
re-introduced.

### Classification key
- **RETIRE** — dead (zero live callers) or kept-for-dispatch-only: remove the
  code, route, serializer and tests. A retired route falls through to the generic
  unmapped handling (URL-resolver 404 or a fail-closed rejection), never a 500.
- **FIX-FORWARD** — a real defect found during the audit; patched forward (not
  reverted), keeping the surface live but correct.
- **KEEP** — live, or parked with intent; recorded so a future sweep does not
  mistake it for dead.

### Method
Read-only Explore sweeps over the reports, restaurant-setup, users and tenancy
surfaces, plus first-hand reads of each candidate's definition, its callers
(repo-wide grep), and its test coverage, before any removal. Each retirement was
validated by `makemigrations --check --dry-run` (clean — the removed items are
serializers, not models), the tenant-relation ratchet + tenancy meta-tests (for
the coordinated manifest edits), and the affected app test suites.

## Per-PR executed-finding ledger

| PR | Merge commit | Findings | Summary |
|----|--------------|----------|---------|
| #244 | `dfed018` (work `24299a8`) | DC-BE-014, DC-BE-011, DC-BE-025, DC-BE-004 | Four authz-surface **fix-forward** findings (detail below). |
| #245 | `3e1b74f` (work `cad5868`) | FEU-02 / FEU-03 | Additive restaurant-tags `reorder/` + `usage-count/` endpoints to match the shipped frontend (no model change, no migration). |
| #247 | `7593157` (7-commit sweep) | dead-code sweep | Dead controller files + `startapp` view stubs (`93c8164`), dead functions + orphaned imports (`35119b1`), dead `SerializerEmployeeGetRestaurant` + its `discovery.py` exemption (`d885e8d`), dead constants (`f243dbe`), dead imports / test residue (`4d8962f`, `4bf25cb`), dead deps numpy/pandas/pytz/tzdata (`9da68df`). |
| this PR | (closure) | DC-BE-002, DC-BE-012 (+150/151/152), DC-BE-016, DC-BE-018, DC-BE-020 | The decision-gated retirements C1–C5 (below) + this record. |

### Fix-forward findings — resolved in PR #244 (`24299a8`)
Real defects patched forward; the surface stays live but correct:
- **DC-BE-014** — the ungated `POST restaurant-setup/restaurants/` self-service
  branch ran after only a JWT decode (no `check_permission`, no module gate), so
  any authenticated user could self-create a restaurant and set themselves owner.
  Removed; creation now flows only through the admin-gated
  `admin-register-restaurant` branch.
- **DC-BE-011** — the last-active-owner guard sat on the DEAD DELETE-employees
  branch; moved onto the LIVE `PUT employees {active:'false'}` deactivation path,
  resolving the target through the server-scoped queryset and returning **409**
  (not 403, which force-logs-out the client).
- **DC-BE-025** — removed a stray singular `'restaurant'` serializer key from the
  `delete()` dispatch dict.
- **DC-BE-004** — `DELETE` on `upsell-config/items/reorder/` now returns **405**
  instead of silently deleting an item.

### C1–C5 — decision-gated retirements (this PR)
- **C1 — `dashboard1`** report slug + `get_restaurant_dashboard_1`
  (`reports_app/controllers/restaurant/dashboard.py`) + its now-dead
  `summarize_orders` helper. Zero live callers, zero tests. FENCED and untouched:
  the live `dashboard` slug, `generate_restaurant_dashboard_details`, the
  `dashboard-v2` block, and `summarize_revenue` (kept — it has a live timezone
  test and a `REGULATORY_AUDIT.md` KEEP note).
- **C2 — `sales-summary`** report slug + `generate_restaurant_sales_summary`
  (`reports_app/controllers/restaurant/sales.py`). No frontend consumer; its
  exclusive average / max / min-order-value figures are surfaced nowhere (Reports
  contract audit C4). The four permission canaries, the `reports_app/tests.py`
  default slug + day-cap comment, and the shared-invariant tests re-point onto
  `sales-listing`; the summary-exclusive assertions were deleted; two shared
  invariants (empty-range → 200, restaurant scoping) were migrated onto the
  listing tests so invariant coverage is not lost. `sales-listing` /
  `sales-trends` / `sales-hourly` / the `controllers/common/` foundations (the
  Reports real-data-flip contract) are FENCED.
- **C3 — the setup `orders` vocab** in the `RestaurantSetupEndpoint` catch-all
  (`_RECORD_MODULE`, `LIST_RESTAURANT_PATH`, the orders GET block, the serializer
  / success / error dicts), the `SerializerListGetOrder` serializer
  (`orders_app/serializers.py`), and the `ORDER_FILTERS` filter map
  (`misc_app/controllers/define_filter_params.py`). A `GET
  restaurant-setup/orders/` now falls through to the generic unmapped-resource
  rejection (**403**). Reports-module enforcement over order data lives on the
  reports endpoint and is unaffected.
- **C4 — the anonymous `misc-public` endpoint** (`restaurants_app/endpoints/misc_public.py`),
  its import + route, the `SerializerMiscPublicRestaurant` serializer, and its
  tests. This completes the arc: tables listing retired → directory retired →
  endpoint gone. The narrowing / no-owner-PII guarantees are SUBSUMED by full
  retirement (the route no longer resolves, so nothing is anonymously reachable).
  The one class in `tests_misc_public.py` that actually exercised the separate
  authenticated setup catch-all (`?deleted` behaviour) was relocated to
  `restaurants_app/tests.py` before the file was deleted, so that live-endpoint
  coverage survives.
- **C5 — the manager-OTP `V2UserProfileEndpoint`** (`user-profile/<action>/`,
  `users_app/endpoints/user_profile.py`), its route, the `update_user_profile`
  controller, and the `SerPutUserProfile` write serializer. Zero frontend
  callers. FENCED and untouched: the LIVE self-service `user-profile/` path
  (`self_update_user_profile`, a plain `User.save()`), `UserProfileEndpoint`, and
  `SerGetUserProfile`.

## Decisions record
- **(a)** Self-service restaurant creation — **REMOVED** (DC-BE-014, #244).
- **(b)** Anonymous misc-public directory — **RETIRED** (C4, this PR).
- **(c)** Manager-OTP profile-update endpoint — **RETIRED** (C5, this PR).
- **(d)** Subscription-details `PUT` — **KEPT**, awaiting the admin billing UI
  that will drive it; not dead, parked with intent.
- **(e)** Support issue-detail surface — **KEPT**, parked.
- **(h)** `dashboard1` + `sales-summary` report slugs — **RETIRED** (C1 + C2,
  this PR). Per the Reports contract audit (C4), the summary-exclusive
  average / max / min figures are surfaced nowhere, so their removal loses no live
  contract.

## Protected-suite touch ledger
Prior precedent: PR #247 (`d885e8d`) removed `SerializerEmployeeGetRestaurant`
and, in the SAME PR, emptied its `dinify_backend/tenancy/discovery.py`
`KNOWN_UNINTROSPECTABLE` exemption — the established pattern for retiring a
tenancy-manifest-enrolled serializer.

This PR touches three protected / pinned test files, minimally and itemized:
- `users_app/tests_permission_enforcement.py` — stripped the two `orders`-vocab
  assertions from the staff / owner list-scoping tests (their menu / tables /
  employees assertions kept verbatim; the two methods renamed), removed the now
  orphaned `self.order` fixture, and added `test_orders_vocab_is_retired` (owner
  AND staff `GET restaurant-setup/orders/` → 403). The kitchen probes and other
  Order fixtures are untouched.
- `reports_app/tests.py` — re-pointed the `get_report` default slug and the
  day-cap comment from `sales-summary` to `sales-listing` (semantics identical;
  the 30-day window is deliberately inside the 31-day cap).
- `dinify_backend/tenancy/tests_tenant_isolation_closure.py` — flipped the two
  misc-public restaurants assertions to expect **404** (retired), with a comment
  that the narrowing / PII guarantees are subsumed by full retirement (the
  already-404 tables assertion is unchanged).

Two coordinated tenancy-manifest edits — mechanically required so the tenancy
meta-tests stay green once the two serializers are deleted; an allowed baseline
SHRINK, not new debt:
- `dinify_backend/tenancy/baseline.txt` — regenerated via
  `scripts/gen_tenant_baseline.py`; the `SerializerListGetOrder::customer` /
  `::table` entries dropped (61 → 59), nothing else moved. The ratchet reports
  "no baseline additions".
- `dinify_backend/tenancy/write_surface_policy.py` — removed the
  `SerPutUserProfile` entry from `PRODUCTION_WRITE_SERIALIZERS`, with a breadcrumb
  comment mirroring the existing `SerializerPutOrder was DELETED` note.

## Deferred items (schema / classification debt)
The tenant-relation classification baseline
(`dinify_backend/tenancy/baseline.txt`) still lists **59** not-yet-classified
writable serializer relations across 20 serializers — the temporary legacy debt
tracked by the TENANT-STRUCT-00 ratchet. Most are archival (`SerArc*`) or read
serializers, and the count is explicitly NOT a vulnerability count (see
`dinify_backend/tenancy/ASSURANCE.md`). The baseline may only SHRINK; this PR
shrank it by 2. Classifying the remainder (or migrating the archival serializers
off DRF) is deferred to future domain-migration PRs; when the baseline reaches
zero, the baseline file, the ratchet script, and their `ci.yml` / `verify.sh`
steps self-terminate.

## Custodial-invariant re-affirmation
This program removed no money-flow code and introduced none. The non-custodial
posture recorded in `REGULATORY_AUDIT.md` is unchanged: the backend holds no PSP
credentials and executes no payments; the only surviving payment code is the
record-only `DinifyTransaction` model and the subscription writer that records a
Pending row and stops. None of C1–C5 touches `finance_app` or reintroduces held
balances, disbursement, refunds, wallets, or the retired custodial models.

## Closure
Authored against branch base `7593157` (PR #247); the closure SHA is this PR's
merge commit. As of this record, no open dead-code / dead-endpoint audit findings
remain. Findings are claims valid at the SHAs cited beside them.
