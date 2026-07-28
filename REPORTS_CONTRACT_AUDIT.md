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

> §9 (`dashboard-v2`) was added after this audit and is **not** covered by the verdict
> above: it documents that endpoint's granularity contract only, and carries one open
> seam of its own (no bucket-count cap).

> Backend file:line citations refer to `mugak1/Dinify-Backend`; frontend
> citations are paths relative to `src/app/restaurant-mgt/reports/` unless noted.

---

## Per-report contract tables

### 1. sales-trends  (GET · `api.get`)
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `reports/restaurant/sales-trends/` (services/reports.service.ts:102) | `'sales-trends'` (reports_app/endpoints/restaurant_reports.py:71) | ✓ |
| Params | `restaurant, from, to, category∈{daily,monthly}, result='table'` | reads `restaurant, from(def today), to(def today), category(def 'daily'), result(def 'table')`; accepts `category∈{daily,weekly,monthly,quarterly,annual}` | ✓ names; ✗ FE never sends `annual` for year bucket (Blocker B2) |
| Envelope | `res.data` → adapter `toArray` (array/records/results/rows) | `{status,message,data:[…]}` bare array | ✓ |
| Fields | `period, orders←count, revenue, discount` (models/reports.models.ts:41-49; services/reports-adapter.ts:53-61) | row `{period, count, revenue, discount}` (controllers/restaurant/sales.py:230-238) | ✓ names; **✗ `period` FORMAT** |
| Enums | n/a | n/a | — |
| Caps | resolveTimeframe mirrors BE (utils/reports-timeframe.ts:42-47) | daily 31 / weekly 371 / monthly 731 / quarterly 731 / annual 1850 (sales.py:50-55) | **✗ year→monthly collapse 400s** |

> **`weekly` (TRENDS-WEEKLY-00) — landed DORMANT.** The backend accepts
> `category=weekly` (→ `TruncWeek`), capped at **371 days** (53 weeks exactly ⇒ ≤54
> buckets) with the message `'Date range should not be greater than 1 year.'`. Its
> `period` key is the **Monday boundary of the bucket in EAT**, `'YYYY-MM-DD'` — the
> same format as `daily`, deliberately not an ISO `'YYYY-Wnn'` week string, which
> would sort correctly but break the FE's `parseISO()`. Caveat for any consumer: a
> range starting mid-week yields a first bucket labelled with the **preceding
> Monday**, i.e. a key *before* `from`, holding only the in-range days. No frontend
> sends `weekly` today (the FE column above is unchanged and still accurate); it is
> accepted-but-unrequested exactly as `quarterly` is.

> **CONTRACT — the series is DENSE (BUCKETS-ZEROFILL-00).** `sales-trends` returns
> **one row per period in the requested window**, in ascending order, with periods
> that had no orders emitted as zeros (`count: 0`, `revenue: 0`, `discount: 0`)
> rather than omitted. The axis comes from
> `common/bucketing.py::period_boundaries`, and it holds for every `category`.
> A consumer may rely on this: the row count is a function of the window and the
> granularity alone, never of whether the restaurant traded.
>
> Two consequences worth stating plainly. **The frontend's `normalizeSeries`
> gap-fill is now a no-op** — it finds nothing to fill. It is deliberately being
> left in place as a safety net, not removed. And the **partial-edge week is now
> always emitted**: a window opening mid-week starts at the preceding Monday
> whether or not that week traded, because clipping it would leave the window's
> first days with no bucket to land in. The caveat above stops being a caveat about
> *which keys appear* and becomes one purely about *where the first key falls*.
>
> This also closes, for `sales-trends`, the "graph payload has no series at all on
> an empty window" shape: `make_graph_series_data` derives its series names from
> the rows it is given, so a dense axis means `Revenue` and `Count` lines always
> exist, all-zero rather than absent.

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

### 9. dashboard-v2  (GET · `api.get`) — granularity contract only
| Dim | FE | BE | |
|---|---|---|---|
| Slug | `…/dashboard-v2/` | `'dashboard-v2'` (restaurant_reports.py:109) | ✓ |
| Params | `restaurant, from, to, bucket∈{hour,day,week,month,year}` | reads `restaurant, from(def today), to(def today)`, `bucket` (**no default — REQUIRED**) | ✓ |
| Envelope | `res.data` | `{status, data:{…}}` on 200 — note **no `message` key**, unlike the eight reports above; `{status, message}` on 400 | ⚠ asymmetric, pre-existing, deliberately unchanged |
| Fields | `revenue, payment_methods, orders, popular_items, tables, kds` | same six; `revenue` is `{series, totals}` and `orders` is `{series, breakdown, total}` — **no previous-period fields** (DASH-REMOVE-LEGACY-00) | ✓ |
| Enums | granularity vocabulary (below) | `hour, day, week, month, year` | ✓ |
| Caps | none | **none** — `clean_dates` only parses/orders the dates | ✗ see the note |

> **`bucket` — the granularity vocabulary, and the only one.** The backend accepts
> `bucket ∈ {hour, day, week, month, year}` (→ `TruncHour`/`Day`/`Week`/`Month`/`Year`),
> resolved **fail-CLOSED**: an unrecognised value is a **400** whose message names the
> accepted values, never a silent default.
>
> **`bucket` is REQUIRED (DASH-REMOVE-LEGACY-00).** Absent — omitted, empty, or
> whitespace-only — is the same **400**, distinguishable only by the message's lead
> clause (`Missing bucket` vs `Unsupported bucket 'x'`); the accepted-value tail is
> identical and derived from `BUCKET_TRUNC`. It used to fall back to a legacy `period`
> parameter and *its* fail-OPEN hourly default. With `period` gone there is nothing to
> fall back to and no defensible default to invent — this endpoint bounds neither the
> date range nor the bucket count (see the open seam below), so guessing wrong returns
> an enormous payload rather than an error the caller can see. This is a **contract
> tightening**: no deployed caller omits `bucket`, which makes it safe, not
> non-breaking. Recorded in `BREAKING_CHANGES.md` §10.
>
> There is deliberately **no `quarter`** entry — the dashboard ladder does not emit one.
> This is also *not* `common/bucketing.py::PERIOD_TRUNC`, which has no `hour` and
> carries a `quarter` this endpoint has no caller for; the overlap is wide enough
> to read as an invitation to merge them, and the remaining asymmetry **is** the
> boundary between them. `reports_app/tests_dashboard_report.py` pins that asymmetry
> against a future tidy-up.
>
> **No previous-period comparison (DASH-REMOVE-LEGACY-00).** The response carries ONE
> window. `revenue.previous_totals` / `revenue.previous_series` /
> `orders.previous_total` / `orders.previous_series` — a server-computed
> preceding-equal-length comparison derived from the date range alone — were removed
> once the frontend replaced them with a second call for the basis the user actually
> selected, which the server cannot infer. Each card had been running its **entire**
> aggregation twice, once per window, so this removed **7 queries per dashboard load**
> (revenue 10→5, orders 8→6). `DashboardV2QueryCountTests` pins the counts and
> `DashboardV2ResponseShapeTests` pins the field absence.
>
> **`week` (DASH-WEEK-00) — added for a real caller.** DASH-PERIOD-00 declined `week`
> because the frontend ladder did not emit one. That expired: the ladder jumped `day`
> (≤31 days) straight to `month`, rendering a 60-day range as two points instead of
> about nine, and the fix emits `week` from `resolveTimeframe`. Since `bucket` fails
> CLOSED, the ladder change would otherwise have **400'd every 32–90-day range** — so
> this backend had to merge and deploy first.
>
> **Key format (the thing a shared frontend enumerator turns on).** `dashboard-v2`'s
> series key is **`at`** — *not* `period`, which is the `sales-trends` key — and its
> value is a **full ISO-8601 datetime carrying the `+03:00` EAT offset**, e.g.
> `'2024-09-09T00:00:00+03:00'`. It is produced by `.isoformat()` on the aware
> truncated datetime (`dashboard.py:276` revenue, `:339` orders); no serializer
> intervenes. This is **unchanged by DASH-WEEK-00 and DASH-REMOVE-LEGACY-00** — every
> bucket has always emitted this shape, and `week` simply lands on a Monday midnight.
>
> **Both endpoints anchor weeks to Monday in EAT, and only the format differs.**
>
> | | `dashboard-v2` `bucket=week` | `sales-trends` `category=weekly` |
> |---|---|---|
> | key name | `at` | `period` |
> | value | `'2024-09-09T00:00:00+03:00'` | `'2024-09-09'` |
> | anchor | Monday 00:00 EAT | Monday 00:00 EAT |
> | mechanism | `TruncWeek`, active tz (`Africa/Nairobi`) | `TruncWeek`, explicit `tzinfo=LOCAL_TZ` |
>
> They are separate vocabularies on separate endpoints — `weekly` is not a `bucket`
> value and `week` is not a `category` — but the **shared Monday anchor** is what lets
> one frontend enumerator generate the week axis for both, formatting per endpoint.
> Do not move either boundary independently. Both are `parseISO()`-readable, so
> neither carries Blocker B1's `'Mar-24'` problem. The partial-edge caveat documented
> for `sales-trends` above applies here too: a window starting mid-week yields a first
> bucket labelled with the **preceding Monday**, a key before `from`, holding only the
> in-range days.
>
> Note the enumerator sentence above is now a statement about FORMATTING only. Both
> endpoints emit a dense axis themselves (below), so the frontend no longer has to
> generate one to fill gaps — it reads the keys it is given.

> **CONTRACT — both series are DENSE (BUCKETS-ZEROFILL-00).** `revenue.series` and
> `orders.series` each return **one row per bucket in the requested window**, in
> ascending order, with buckets that had no orders emitted as zeros (`gross`,
> `discounts`, `refunds` = `'0.00'`; `count` = `0`) rather than omitted. Both cards
> share ONE axis, key-for-key, from
> `common/bucketing.py::period_boundaries` — the same helper `sales-trends` uses,
> which spans both vocabularies ('hour' AND 'quarter') precisely so the two
> TRUNCATION maps still do not have to merge.
>
> This is what the Dashboard chart actually needed: it does no client-side filling,
> so an omitted bucket made the line join its neighbours and imply trading that did
> not happen. `adaptRevenueSeries` is a bare `.map`, so dense rows in means a dense
> series out — **no frontend change was required**.
>
> **Watch the basis if you touch `_build_revenue`.** Its series is driven by PAID
> orders with refunds joined in, so a bucket holding only a refund has no paid row.
> The fill INSERTS where that basis has no bucket and never overwrites a real row,
> and it does not widen the axis to `paid ∪ refunded` — the axis is the window. A
> refund-only bucket therefore now surfaces its real refund (it was absent
> entirely before) rather than a `'0.00'` that would contradict `totals.refunds`.
>
> **Open seam — no bucket-count cap. BUCKETS-ZEROFILL-00 made it worse, deliberately.**
> Fail-closing the vocabulary stops the *typo* path, but nothing bounds the bucket
> count of a *valid* request: `bucket=hour` over a 200-day range is ~4,800 buckets,
> and unlike `sales-trends` (`TREND_CAPS`) this endpoint has no per-granularity date
> cap. That figure used to be a *worst case*, reached only if every hour traded; with
> a dense series **it is now the floor** — an empty 200-day hourly window returns
> ~4,800 zero rows where it previously returned few or none. The seam PRE-EXISTS this
> change; the fill did not create it, it removed the sparsity that was accidentally
> masking it.
>
> **Named follow-up — DASH-BUCKET-CAP-00.** Bound the bucket count in
> `_resolve_bucket_trunc`, which already owns the granularity's one failure path:
> compute `len(period_boundaries(date_from, date_to, key))` and return
> `_bucket_error(...)` — the existing 400 envelope, so no new response shape — when it
> exceeds a limit. A cap in the low thousands covers every range the frontend's
> timeframe ladder emits (`hour` is only selected for short windows) while refusing a
> hand-crafted multi-year hourly request. It needs the ladder's real ranges to pick the
> number, which is the only reason it is not done here.
>
> DASH-WEEK-00 left this seam exactly as it found it. `week` is strictly coarser than
> the already-uncapped `day`, so it cannot widen the worst case, and `sales-trends`'
> 371-day `weekly` cap therefore has no counterpart here — that cap bounds *that*
> endpoint's payload, it is not part of the shared Monday-anchor guarantee.
> DASH-REMOVE-LEGACY-00 did not close it either, but it did remove the one path that
> reached hourly bucketing *without the caller asking for it*: an absent or typo'd
> parameter can no longer select a granularity at all.

---

### 10. dashboard  (GET · `api.get`) — the v1 dashboard, metric semantics

Added by DASH-METRICS-00 (PR-H §1). This slug had **no section here** before: §1–§9
pinned every other report while the oldest and most-looked-at one was unpinned,
which is part of why its `num_sales` defect survived so long. Its fields are not
declared in any frontend model file visible from this repo, so the BE column is
authoritative and the FE column records what the payload now promises.

| Dim | FE | BE | |
|---|---|---|---|
| Slug | `reports/restaurant/dashboard/` | `'dashboard'` (reports_app/endpoints/restaurant_reports.py:50) | ✓ |
| Params | `restaurant, from, to` | `restaurant`, `from`(def today), `to`(def today) | ✓ |
| Envelope | `res.data` | `{status, message, data:{…}}` | ✓ |
| Window | — | raw `time_created__gte/lte` against `date` objects — **NOT** `sale_orders()`'s `__date` lookup | **✗ see D1** |
| Serializer | — | none — hand-built dict, rendered directly | ✓ |

**Metric definitions (the contract this section exists to pin).** Each is defined
once in `controllers/restaurant/dashboard.py` and every figure derives from the
definition; none restates a status list inline.

| Key | Definition | Note |
|---|---|---|
| `orders_placed` | `order_status != 'initiated'` | **NEW.** The denominator for all three rates |
| `num_sales` | orders placed ∩ `SALE_STATUSES` (served, paid) | **MEANING CHANGED** — was `orders.count()` |
| `sales_amount` | `revenue_sum()` = `Sum('actual_cost')` over sales | **VALUE CHANGED** — was `Sum('total_cost')` over `payment_status='paid'`, i.e. permanently `null`. `null` when there are no sales |
| `cancelled_orders` | `{number, percentage}` — cancelled ÷ orders placed | denominator changed |
| `refunded_orders` | `{number, percentage}` — refunded ÷ orders placed | denominator changed |
| `paid_orders` | `{number, percentage}` — `payment_status='paid'` ÷ orders placed | **always 0 / 0.0** — see D2 |
| `payment_tracking_enabled` | module constant, `False` | **NEW.** The honest label on `paid_orders` |
| `new_diners`, `repeat_diners`, `most_active_diner`, `most_ordered_item`, `least_ordered_item`, `peak_hour` | unchanged — still read the UNFILTERED queryset | deliberate scope; see D3 |
| `most_liked_item`, `least_liked_item` | hard-coded `None` → `''` | never computed; predates this work |

Sharing ONE denominator is the load-bearing part: it is what makes the cancellation
and refund rates comparable to each other and what stops either from exceeding 100%.

> **The headline number drops on deploy.** `num_sales` was inflated by abandoned
> drafts, cancellations and refunds. Owners will see "Sales" fall, possibly sharply,
> the day this ships. `BREAKING_CHANGES.md` §11 carries the release note.

**Three seams this section opens, all recorded rather than fixed:**

- **D1 — window semantics differ from `sale_orders()`.** This endpoint filters
  `time_created__gte/lte` with `date` objects (Django warns: naive datetime,
  midnight-anchored, so `to` is effectively exclusive of its own day), while
  `sale_filters.sale_orders()` uses the inclusive local-day `__date` lookup its
  own docstring tells callers to prefer. The two therefore agree on counts but can
  disagree at the window edge. Pre-existing, shared with `dashboard-v2`, and pinned
  by the v2 tests; changing it is a separate window-semantics change.
- **D2 — the payment card cannot work until PSP.** Nothing in the codebase writes
  `payment_status='paid'`; creation seeds `'pending'` and the order-payment write
  path was deleted with the custodial teardown. `payment_tracking_enabled` states
  this in the payload. Flip the constant in the PR that lands the PSP write path.
- **D3 — the diner and item figures still count drafts.** `new_diners` also
  collapses every anonymous QR guest into a single phantom customer (`customer` is
  NULL for them), which is exactly what the Diners report was rebuilt to avoid.
  Belongs with that surface, not with a metric-definition change.

**`dashboard-v2` inherited the same draft-counting defect** in `_build_orders`'
base — `orders.total` and `orders.series[].count` counted `initiated` drafts, and
the four `breakdown` rows could never sum to `total` (an initiated order is
excluded from `open` and fails `paid`). Fixed identically, so `orders.total` now
equals v1's `orders_placed` and the breakdown reconciles. `revenue` unchanged.

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
- **G4 — DASH-BUCKET-CAP-00: `dashboard-v2` has no bucket-count cap.** Pre-exists BUCKETS-ZEROFILL-00, which made it sharper rather than creating it: `bucket=hour` over a 200-day range is ~4,800 rows, and with a dense series that is now the **floor** rather than a worst case. Shape of the fix is scoped in §9 above — cap in `_resolve_bucket_trunc` off `len(period_boundaries(...))`, returning the existing `_bucket_error` 400 envelope so no new response shape is introduced. Deferred only because picking the number needs the frontend timeframe ladder's real ranges. Not a flip-blocker: every range the ladder actually emits is small, and `hour` is selected only for short windows.

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
5. **Empty/absent `data` on a 200** with zero rows — FE treats `[]`/`{}` as truthy (fine); a literal `null` `data` would coerce to an error/empty state. Unverified. *Narrowed by BUCKETS-ZEROFILL-00:* `sales-trends` and both `dashboard-v2` series can no longer return an empty array for a valid window — a zero-trading window returns a full axis of zero rows. The blind spot still stands for the listing reports, which remain legitimately `[]` when empty.

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
