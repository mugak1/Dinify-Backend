# Reports Module — Cross-Repo Contract Reconciliation

**Scope:** `mugak1/Dinify-Frontend` ↔ `mugak1/Dinify-Backend`
**Type:** read-only interface audit (no behavioural change)
**Question:** will the frontend's dormant, mock-gated real-data branches
(`ReportsService.USE_MOCK_DATA = false`) bind cleanly to what the backend
actually returns, so the eventual flip is friction-free?

## Why this exists
The Reports redesign is complete on the frontend but still mock-first
(`ReportsService.USE_MOCK_DATA = true`); every real-data service branch + the
`reports-adapter` parsing layer are dormant. The mock-first strategy is only safe
if the shapes the FE mocks/expects match what the BE returns. This project has
repeatedly hit FE↔BE drift that only a cross-repo read caught (a phantom
`sales-aggregate` slug, the `category` param name, the `payment_mode` vocab gap).
This audit finds the **remaining** seams now, while cheap, rather than at the live
flip in front of a real restaurant.

Verdict markers: ✓ match · ✗ mismatch (flip-risk) · ⚠ gated-known.

**Bottom line:** 2 flip-blockers (both Sales), all GATED items handled correctly,
everything else ✓. The single highest-risk seam is the **sales-trends period
label format** (BE emits `'Mar-24'`, FE `parseISO()` throws) — triggered by the
one-click **"This year"** preset.

> Backend file:line citations refer to `mugak1/Dinify-Backend`; frontend
> citations are paths relative to `src/app/restaurant-mgt/reports/` unless noted.

---

## Per-report contract tables

### 1. sales-trends  (GET · `api.get`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `reports/restaurant/sales-trends/` (services/reports.service.ts:102) | `'sales-trends'` (reports_app/endpoints/restaurant_reports.py:71) | ✓ |
| Params | `restaurant, from, to, category∈{daily,monthly}, result='table'` | reads `restaurant, from(def today), to(def today), category(def 'daily'), result(def 'table')` | ✓ names; ✗ FE never sends `annual` for year bucket (Blocker B2) |
| Envelope | `res.data` → adapter `toArray` (array/records/results/rows) | `{status,message,data:[…]}` bare array | ✓ |
| Fields | `period, orders←count, revenue, discount` (models/reports.models.ts:41-49; services/reports-adapter.ts:53-61) | row `{period, count, revenue, discount}` (controllers/restaurant/sales.py:230-238) | ✓ names; **✗ `period` FORMAT** |
| Enums | n/a | n/a | — |
| Caps | resolveTimeframe mirrors BE (utils/reports-timeframe.ts:42-47) | daily 31 / monthly 731 / quarterly 731 / annual 1850 (sales.py:50-55) | **✗ year→monthly collapse 400s** |

### 2. sales-listing  (GET · `api.loadAllPages`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/sales-listing/` (reports.service.ts:130) | `'sales-listing'` (restaurant_reports.py:65) | ✓ |
| Params | `restaurant, from, to` (+`page` injected by loadAllPages) | reads `restaurant, from, to` (ignores `page`) | ✓ (extra `page` benign) |
| Envelope | loadAllPages: no pagination block → returns `res.data` array (_services/api.service.ts:88-116) | bare array in `data` (sales.py:191-196) | ✓ |
| Fields | `order_number, item_count, gross, discount, revenue, payment_mode, payment_status, time_created` (reports.models.ts:70-83; reports-adapter.ts:63-74) | serializer same keys; money `coerce_to_string=False`→JSON number (reports_app/serializers.py:6-48) | ✓ (FE `num()` robust to number-or-string) |
| Enums | `payment_mode` union `MTN MoMo\|Airtel MoMo\|Cash`; `payment_status` pill | `payment_mode` raw `momo\|cash\|card\|null`; `payment_status` raw `paid\|pending\|failed` | ⚠ payment_mode (GATED); ✓ payment_status data-driven |
| Caps | calls only when `inclusiveDays≤31` ⇒ span≤30 (sales/sales-report.component.ts:171-176) | `(to-from).days>31 → 400` (sales.py:152-156) | ✓ FE strictly within cap |

### 3. sales-hourly  (GET · `api.get`, **no adapter**) — dormant FE branch
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/sales-hourly/` (reports.service.ts:160) | `'sales-hourly'` (restaurant_reports.py:79) | ✓ |
| Params | `restaurant, from, to` | reads `restaurant, from, to` | ✓ |
| Envelope | `res.data` passthrough (identity) | `{…,data:[24]}` | ✓ |
| Fields | `{hour,count,revenue,discount}` (reports.models.ts:56-65) | `{hour,count,revenue,discount}` ×24 zero-filled (sales.py:293-301) | ✓ exact |
| Enums | n/a | n/a | — |
| Caps | none | none | ✓ |

### 4. menu-summary  (GET · `api.get`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/menu-summary/` (reports.service.ts:181) | `'menu-summary'` (restaurant_reports.py:97) | ✓ |
| Params | `restaurant, from, to, grouping∈{sections,groups,items}` | reads `restaurant, from, to, grouping(def 'sections')` | ✓ |
| Envelope | adapter `toArray` finds `.rows` (reports-adapter.ts:76-83) | `data:{grouping, rows:[…]}` (controllers/restaurant/menu.py:104-111) | ✓ |
| Fields | `name, order_count, quantity_sold, revenue` (reports.models.ts:108-115) | rows + `average_rating:null` on items (menu.py:90-102) | ✓ (FE ignores `average_rating`) |
| Enums | n/a | n/a | — |
| Caps | none needed | none (relaxed PR#169) | ✓ |

### 5. transactions-summary  (GET · `api.get`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/transactions-summary/` (reports.service.ts:202) | `'transactions-summary'` (restaurant_reports.py:104) | ✓ |
| Params | `restaurant, from, to` | reads `restaurant, from, to` | ✓ |
| Envelope | adapter reads `by_status / by_type / total_transactions` (reports-adapter.ts:85-101) | `data:{total_transactions, by_status[], by_type[]}` (controllers/restaurant/transactions.py:85-113) | ✓ |
| Fields | byStatus `{status↓,count,amount}`, byType `{type←strip,count,amount}`, totalCount | by_status `{status,count,amount}`, by_type `{type,count,amount}` | ✓ |
| Enums | status `success/failed/pending/initiated`; type strips `order_` | status same; by_type only `order_payment + subscription` | ✓ status/type; ⚠ **no `refund` in by_type** → FE "Refunded" bucket = 0 (GATED) |
| Caps | none | none | ✓ |

### 6. transactions-listing  (GET · `api.loadAllPages`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/transactions-listing/` (reports.service.ts:229) | `'transactions-listing'` (restaurant_reports.py:110) | ✓ |
| Params | `restaurant, from, to, status?, type?` (chip map: paid→`status=success`, pending→`status=pending`, failed→`status=failed`, refunded→`type=refund`; transactions/transactions-view.ts:159-172) | reads `restaurant, from, to, type(None), status(None)` | ✓ |
| Envelope | loadAllPages → bare array (api.service.ts:88-116) | bare array in `data` (transactions.py:154-158) | ✓ |
| Fields | `order_number, transaction_type←strip, transaction_status↓, amount, payment_mode, transaction_platform, time_created` (reports.models.ts:151-161; reports-adapter.ts:103-117) | serializer `{id, transaction_type, transaction_status, order_number, amount(#), payment_mode, transaction_platform, time_created}` (finance_app/serializers.py:6-30) | ✓ (FE ignores `id`) |
| Enums | status data-driven; type strip; payment_mode `methodDisplay` map+fallback; platform unused | status raw; type raw `order_*`; payment_mode raw; platform raw `'web'` | ⚠ payment_mode (GATED); platform `web` vs FE-doc `yo` = cosmetic/unused |
| Caps | `recentWindow` caps span→31 (transactions-view.ts:184-192; transactions-report.component.ts:152) | `>31d → 400` unless `type=subscription` (transactions.py:130-135) | ✓ FE never 400s (over-conservative for subscription) |

### 7. diners-summary  (GET · `api.get`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/diners-summary/` (reports.service.ts:258) | `'diners-summary'` (restaurant_reports.py:85) | ✓ |
| Params | `restaurant, from, to` | reads `restaurant, from, to` | ✓ |
| Envelope | adapter object read (reports-adapter.ts:119-136) | `data:{…}` | ✓ |
| Fields | `identifiedDiners, repeatDiners, guestOrders, avgSpendPerDiner←average_spend_per_identified_diner, mostActive:{name, totalSpend←total_spend}` | `{identified_diners, repeat_diners, guest_orders, average_spend_per_identified_diner, most_active_diner:{name, order_count, total_spend}}` (controllers/restaurant/diners.py:125-131) | ✓ (FE ignores `most_active_diner.order_count`) |
| Enums | n/a | n/a | — |
| Caps | none | none | ✓ |

### 8. diners-listing  (GET · `api.loadAllPages`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/diners-listing/` (reports.service.ts:278) | `'diners-listing'` (restaurant_reports.py:91) | ✓ |
| Params | `restaurant, from, to` (+`page`) | reads `restaurant, from, to` | ✓ |
| Envelope | loadAllPages → bare array | bare array in `data` (diners.py:195-199) | ✓ |
| Fields | `customer_id, name, phone_number, no_orders, total_spend, average_spend, last_order_date` (reports.models.ts:174-185) | same keys; `customer_id` UUID, money `#` (diners.py:179-194) | ✓ (UUID→String) |
| Enums | n/a | n/a | — |
| Caps | `recentWindow` caps span→31 (diners/diners-view.ts:80-88; diners-report.component.ts:120) | `>31d → 400` (diners.py:150-154) | ✓ |

---

## Triaged seam list

### FLIP-BLOCKERS (break/garble at `USE_MOCK_DATA=false`)

**B1 — [HIGH] sales-trends `period` label is non-ISO; FE `parseISO()` throws.**
- BE emits human labels: `month → 'Mar-24'`, `quarter → 'Q1-2024'`, `year → '2024'`; only `day → 'YYYY-MM-DD'` is ISO. → `controllers/restaurant/sales.py:310-326` (`_period_label`, built at `:232`).
- FE declares `period` as ISO (`models/reports.models.ts:42`), passes it through verbatim (`services/reports-adapter.ts:55`, only `String()`), then `parseISO(period)` → `format(...)` in `sales/sales-view.ts:105-108` (and uses `period` as the chart key/sort at `:118-131`). date-fns `format()` **throws `RangeError: Invalid time value`** on `parseISO('Mar-24')`.
- **Trigger:** the **monthly** bucket — reached by the one-click **"This year"** preset and any 32–731-day custom range (`resolveTimeframe` → `month`). (`'2024'` annual parses OK; `'Q1-2024'` quarterly is never auto-selected by the FE ladder.)
- **Masked by mock:** `data/reports-mock-data.ts:98` emits `format(m,'yyyy-MM')` (ISO), so the mock never exercises the real format. Neither contract spec catches it: `services/reports-adapter.spec.ts:25` uses ISO `'2026-06-01'` and only asserts the adapter passthrough; `sales-view.spec` runs on ISO mock periods.
- **Breaks at flip:** monthly Sales trend/breakdown throws → Sales report dead for "This year"/long ranges.
- **TRIAGE DECISION → BACKEND emits ISO period keys** (`yyyy-MM-dd` / `yyyy-MM` / `yyyy-Qn` / `yyyy`), consistent with the rebuild's own "frontend owns display formatting" rule, and keeps `period` sortable-as-text. Touches the pinned BE report tests (`reports_app/tests_sales_report.py`). FE adapter/view stay unchanged.

**B2 — [MED, conditional] sales-trends year bucket is sent as `category=monthly` → BE monthly cap (731) returns 400.**
- `fetchSeries` derives `granularity = bucketUnit==='day' ? 'daily' : 'monthly'` (`sales/sales-report.component.ts:213`) — collapsing the `year` bucket to `monthly` and **ignoring** `tf.category` (which is `'annual'`). Root cause: `getSalesAggregate`'s param type `ReportGranularity = 'daily'|'monthly'` (`reports.models.ts:39`; `reports.service.ts:90-116`) cannot express `annual`. `resolveTimeframe` leaves the range **unclamped** for spans ≤1850 (`utils/reports-timeframe.ts:99-101`).
- BE: `TREND_CAPS['monthly']=731` + cap check → 400 (`sales.py:50-55, 219-224`).
- **Trigger:** a **custom** Sales range of 732–1850 days. All presets are ≤366 days, so latent for presets.
- **Breaks at flip:** main sales-trends call 400s → Sales report error state.
- **TRIAGE DECISION → FRONTEND.** Widen `getSalesAggregate` to accept `SalesTrendsCategory` and pass `tf.category` (so the `year` bucket sends `category=annual`).

### GATED (real but legitimately deferred — confirm handled, do NOT "fix" now)

- **G1 — `payment_mode` vocab gap** (BE `momo/card/cash` vs FE union `MTN MoMo/Airtel MoMo/Cash`). Already documented in CLAUDE.md; arrives properly only with the PSP (Gate 2 — BE cannot distinguish MTN vs Airtel, stores only `momo`). Degrades gracefully: Transactions tab maps via `methodDisplay` (`transactions-view.ts:23-33`, unknown→raw). FE casts `as PaymentMode` at `reports-adapter.ts:70,113` (type-lie, runtime-safe). BE `finance_app/serializers.py:39`. *Cosmetic sub-item:* the **Sales** per-order "Method" column renders the raw token (`format:'text'`, `sales-report.component.ts:61`) — would show `'momo'` literally on flip.
- **G2 — Refunds in Transactions** have no backend source today. BE `SUMMARY_TYPES = [order_payment, subscription]` (`transactions.py:48-51`) excludes `order_refund`, so the summary's `by_type` carries no refund row → FE "Refunded" bucket reads 0 and is flagged `mockOnly` (`transactions-view.ts:79,93,109,125,129`). Correctly slotted as dormant. (Gate 2.)
- **G3 — sales-hourly dormant FE branch** — contract verified clean for the later flip: `{hour 0–23, count, revenue, discount}` × 24 zero-filled, identity-mapped. FE renders an 11:00–22:00 display window (`sales-view.ts:209-229`). Ready.

### COSMETIC (label/format/tidy only — no functional break)

- **C1** `transaction_platform`: BE live `'web'` vs FE-doc `'yo'` (`reports-adapter.spec.ts:145`). Read into the model but **not a rendered column** → no impact.
- **C2** Adapter `as PaymentMode` / `as PaymentStatus` casts (`reports-adapter.ts:70,71,113`) are type-lies; runtime-safe via `num()`/`String()`/fallbacks. Tidy when addressing G1.
- **C3** 400s render a **generic** `ReportStateComponent` error state (e.g. `sales-report.component.ts:158-162`), not a cap-specific guidance banner. Graceful (no crash) but not tailored — optional UX polish.
- **C4** The FE does **not** consume the BE `sales-summary` endpoint at all — Sales hero/KPI totals are computed client-side by summing sales-trends buckets (`sales-view.ts:138-149`). Not a mismatch; just unused BE capability (its `average_order_value/max/min` are never surfaced). The two agree mathematically over `SALE_STATUSES`.

---

## Blind spots (could NOT verify statically — need a running backend + seeded data)

1. **Decimal wire-type for the dict-based reports** (sales-trends/hourly, transactions/diners/menu summaries): these return raw `Decimal`s in a plain dict, rendered by DRF's `JSONEncoder` (→ number, by inference). The two listing serializers are *explicitly* `coerce_to_string=False`. FE `num()` coerces number-or-string either way, so risk is low — but the exact wire type for the dict-Decimals is unconfirmed against a live response.
2. **DRF `DateTimeField` wire format** for `time_created` / `last_order_date` vs the FE `'datetime'` formatter — assumed standard ISO 8601; unverified live.
3. **transactions.py / diners.py listing return shapes** — `sales.py` was line-read directly; the other two were taken from cross-repo exploration (bare array in `data`). High confidence, not independently line-verified here.
4. **Live enum reality** — whether production data ever carries a `transaction_type/status/payment_mode` value outside the documented sets. FE fallbacks humanize unknowns, so low risk.
5. **Empty/absent `data` on a 200** with zero rows — FE treats `[]`/`{}` as truthy (fine); a literal `null` `data` would coerce to an error/empty state. Unverified.

> Per the flip-time gate (CLAUDE.md › Mock Data Pattern › ReportsService flip-time gate),
> all five blind spots are exactly what gate step (2) ("re-verify ALL FOUR reports
> end-to-end against the live backend") must cover before flipping
> `ReportsService.USE_MOCK_DATA` to `false`.

---

## Remediation summary

| ID | Severity | Side | Action |
|---|---|---|---|
| B1 | FLIP-BLOCKER (HIGH) | Backend | `_period_label` emits ISO keys (`yyyy-MM-dd`/`yyyy-MM`/`yyyy-Qn`/`yyyy`) + update `tests_sales_report.py` |
| B2 | FLIP-BLOCKER (MED) | Frontend | `getSalesAggregate` accepts `SalesTrendsCategory`; pass `tf.category` so year→`annual` |
| G1/G2/G3 | GATED | — | Deferred to Gate 1/Gate 2; handled gracefully today |
| C1–C4 | COSMETIC | Frontend | Optional polish |

Each fix lands on its own branch/PR in its repo. This document is the audit
record only — it makes no code changes to either side.
