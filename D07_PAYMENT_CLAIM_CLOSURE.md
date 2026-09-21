# D07 — Payment interfaces no longer claim work no provider performed

Delivery record for audit finding **D07**, implemented under the Stage B approval and
controlling amendments dated 21 September 2026. Stage A (reconnaissance, disposable
reproductions, policy decisions D07-A1…A6, plan) is not repeated here; this records
what was BUILT, what was DECIDED during implementation, what was CORRECTED in the
Stage A evidence, and — as importantly — what was deliberately not done.

Sibling records: `REGULATORY_AUDIT.md` (non-custodial posture and its host appendix),
`DEAD_CODE_CLOSURE.md`, `dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md`,
`DELEGATED_QR_OPERATOR_NOTE.md` (three §14 carry-forwards applied by this work).

---

## 1. The delivered pair

| repo | base | branch |
|---|---|---|
| `mugak1/Dinify-Backend` | `8054e5cb28c2ffaa2b88ed489cd98d7aa4153ce9` | `claude/serene-shannon-7iz7bj` |
| `mugak1/Dinify-Frontend` | `2c5bcd49bce9f9ad0974fb16a74c0cf2a94d759d` | `claude/serene-shannon-7iz7bj` |
| `mugak1/Dinify-Admin` | `38df0373e235c3e2952ae1985333731327400b93` | **untouched — no branch, no PR** |

Admin needs no companion change: it calls fifteen routes and none of them is D07's. It
renders no diner payment, no subscription collector and no tender column; its
`commercial` projection is the Admin-plane read, which this work did not touch.

**One migration**, `restaurants_app/0058_table_qr_mode_order_only_default` — model
state only (see §5). No other schema change in either repo.

---

## 2. The finding was seven exposures. What each one's consumer does now

| | exposure (Stage A) | changed consumer |
|---|---|---|
| **E1** | Billing screen renders **"Free trial"** for every restaurant — `subscription_validity` was backfilled `default=True` and has no writer | `settings/billing` is **read-only**. `billing-plans.ts` DELETED (the UGX 150,000 / 1,500,000 figures and the "Save UGX 300,000/yr" badge no server ever sent). The panel now states the SERVER's answer: canonical recorded terms, or an explicit "none recorded", or nothing at all when the response never mentioned them. `termsRecorded` and `termsStated` are separate facts. The Active badge and the "next billing date" are gone with the columns that could not support them |
| **E2** | Every table born promising **"Order & Pay"** | `Table.qr_mode` default `order_pay` → `order_only` (migration 0058); the frontend's create/reset/payload sites follow, the EDIT form keeps `order_pay` labelled "Order (legacy)" for a row that already holds it, and `readQrMode` RETAINS an unknown or absent value (§5) |
| **E3** | The subscription collector's *"payment has been initiated"* | `tx_subscription.initiate` answers **501 `subscription_collection_unavailable`** with zero effects — no transaction, no intent, no OTP, no SMS, no stored MSISDN. Refused at the SERVICE because the endpoint is one caller (§4) |
| **E4** | The billing dialog's OTP went to the operator's **own registered phone**, not the mobile-money number displayed, and the payment path never read it | The dialog is DELETED, with `PayNow()` / `InitPayment()` / `Save()` / `sendOtp()`. No OTP is sent from any billing surface; `users/auth/resend-otp/`, `users/msisdn-lookup/` and every shared verification rule are untouched |
| **E5** | `dashboard-v2` Revenue card: `gross`/`discounts` permanently 0, `net` goes **negative** on a refund, D02/C mixed-pricing notice can never render | `payment_tracking_enabled` is published on v2 and consumed by **all three** governed cards — Revenue, Payment Methods and Total Orders (§6) |
| **E6** | Reports adapter coerced `payment_mode: null` → literal **`'Cash'`** | `reports-adapter`'s `stated()` is the one rule; `PaymentMode` / `PaymentStatus` / `TransactionStatus` became `\| null`; three renderers spell the absence the same way (§7) |
| **E7** | Tax & receipts claimed the rate *"is applied"* and the footer *"prints on receipts"* | Both sentences now describe STORAGE. Approved wording verbatim for VAT; the Receipts section extended by the same rule (§8) |

### D07-A1…A6 → what shipped

- **A1 (supported launch modes)** — no venue's commercial decision was changed, and no
  timing/collection combination was blocked. `offline` remains a first-class mode;
  `pay_first` remains recordable. Nothing was built to enforce payment before
  preparation, which the approval excludes.
- **A2 (unimplemented online collection)** — **501, not 503**, per the amendment. Body
  `{"status": 501, "reason": "subscription_collection_unavailable", "message": "In-app
  subscription payment collection is not available. This request did not create or send
  a payment request."}`. No `Retry-After`, no retry timer, no polling.
- **A3 (subscription truthfulness)** — the billing panel states recorded terms or says
  it has none; no price, payment instruction, account detail, renewal date or support
  address is invented.
- **A4 (existing configuration and historical data)** — **no backfill, no
  reclassification, no repricing, no repair.** Migration 0058 carries no `RunPython`;
  every stored `order_pay` row keeps its value and stays orderable; historical
  `DinifyTransaction` rows are untouched; the two questions Stage A could not settle
  (how many deployed restaurants carry a legacy `monthly`/`yearly`, and what becomes of
  historical pending rows) remain open and unanswered here.
- **A5 (shared capability)** — `finance_app/subscription_capability.py` is one leaf
  module both planes read, and the existing `PAYMENT_TRACKING_ENABLED` pattern was
  reused rather than a new flag framework. It is a DISCLOSURE, never a switch:
  `initiate` deliberately does not branch on it, pinned by a test.
- **A6 (release/rollback)** — §9 below and `BREAKING_CHANGES.md` §17.

---

## 3. The single most important structural decision

**The refusal lives in the service, not the view.** `tx_subscription.initiate` is
reachable in-process by any caller, including one passing `user=None` (which used to
persist `created_by=None`). Disabling a view or hiding a button would have left the
fake collection fully available to the next caller. The insertion branch is **removed**
rather than parked behind an enable switch, because a switch is a working fake
collection one boolean away from returning.

**No privileged internal bypass was introduced and no "already authorized" client flag
is honoured.** The endpoint's existing `can_manage_restaurant` 404 still runs FIRST, and
an unknown or malformed restaurant id still answers that same opaque 404 rather than
500ing or disclosing — only a request that cleared both gates reaches the 501.

**The legacy plan column stopped deciding anything.** `preferred_subscription_method`
used to be the only gate (`== 'per_order'` → refuse). That branch is gone: every
authorized request now gets the same 501 whatever the column says, because branching on
it would have offered *changing a plan* as a way to enable machinery that does not
exist. The post-gate payload strip in `restaurant_setup.py` is UNCHANGED and still
load-bearing for the Phase-1 admin writer.

---

## 4. PR-2 — the restaurant's own billing read (widened by the amendment)

A capability boolean alone was ruled insufficient, so `restaurant-setup/
subscription-details/` carries an **additive restaurant-scoped projection**: the
collection-support fact, and either the actual canonical terms or an explicit
successful "no terms recorded" state, with price / currency / recurrence and the
recorded term-state facts and nothing else.

- It **reuses `commercial_app`'s own open-terms rule** (`commercial_app/reads.py`)
  rather than reconstructing current terms from `flat_fee`,
  `preferred_subscription_method`, a validity default, a transaction count or the UI
  catalogue. No second source of truth, no terms writer, no invoice, no entitlement
  inference, no read-time `get_or_create`.
- It does **not** import or bypass the Admin endpoint to expose its privileged fields.
- The **existing settings-module read gate, the foreign/unknown denials and the
  delegated exclusion are preserved**, resolved through the existing resolvers — read
  permission is not the collector's management permission, and no new hardcoded
  owner/manager check was added.

---

## 5. PR-4 — a new table no longer advertises a pay step

`order_pay` and `order_only` are BOTH in `ORDERING_QR_MODES` and both permit ordering;
`menu_only` is the one that blocks. So the default move changes what a new table
**claims** and nothing about what it **does**.

**"No data migration" means no row rewrite, not no migration.** 0058 is a Django
`AlterField` — model state only, no `RunPython`, no backfill — so a rollback is a TRUE
INVERSE: it restores the old default for FUTURE rows and changes no existing one.

**The eight frontend sites were classified, not swept.** Stage A said six; the
implementation found **eight** production sites plus two that only ever preserve an
existing value. The discrepancy is recorded rather than quietly absorbed:

| site | classification |
|---|---|
| `new-table-modal` form default / reset | → `order_only` |
| `new-table-modal` CREATE picker options | `order_pay` option REMOVED |
| `bulk-add-tables-modal` form default / emitted value | → `order_only` |
| `bulk-add-tables-modal` picker options | never offered `order_pay`; unchanged |
| `tables.service.ts` create payload fallback | → `order_only` |
| `tables.models.ts::readQrMode` | **RETAINS UNKNOWN** — `undefined` for a missing or unrecognised value, and `updateTable` already omits the key, so "retain" reaches the server as "leave it alone" |
| `floor-plan-canvas`, `tables-setup-view` | unrelated edits; existing value preserved |

**Judgement call, recorded because it is the one thing here a reviewer might have
decided differently:** `order_pay` was removed from the CREATE pickers rather than left
selectable-but-discouraged. An operator creating a table cannot choose to advertise a
pay step the platform cannot perform; an operator EDITING a table that already carries
one still sees it, labelled "Order (legacy)", because the stored value is that
venue's own recorded intent and this work does not change it.

---

## 6. PR-5 — option (a) only, with the frontend companion the amendment required

The approval permitted **disclosure of unavailable measurement** and forbade closing it
as a backend-only flag. `payment_tracking_enabled` — the same module constant v1 already
published, asserted EQUAL by identity so the two responses cannot drift — now rides
`dashboard-v2` too, where it governs **three** figures.

**The first implementation disclosed one of the three and that was caught before
delivery.** The flag is a single statement about instrumentation, so disclosing the
Payment Methods card alone would have been the same defect with a smaller blast radius:

- **Revenue** is E5's principal claim and the loudest of the three. `_build_revenue`
  aggregates `gross` and `discounts` over `payment_status='paid'`, a column with no
  writer, so both are zero in every bucket and in `totals`; `net = gross − discounts −
  refunds` is derived from them while `refunds` is NOT paid-gated, so a window holding
  one refund reports a NEGATIVE net against a zero gross — as the Dashboard's headline
  figure, in success green. `pricing_conventions` counts over that same empty set, which
  is why the D02/C mixed-pricing notice has never been able to render on a live payload.
- **Payment Methods** showed an empty list reading as "no one paid today".
- **Total Orders** counts `paid` on the same unwritten column and `open` as everything
  else not cancelled or refunded, so the split reads "nobody has paid" about a
  restaurant that traded all day.

Three things are load-bearing in how it is said. **Nothing is recomputed, suppressed or
repriced** — every server figure is still rendered verbatim beside a statement of what
it is. The Revenue note's below-zero clause is **observed rather than predicted**
(`netIsNegative` reads the number the card is rendering) and is gated on the
disclosure, because a negative net from a server that DOES measure is an ordinary
trading fact. And the Total Orders sentence names **only** Paid and Open/Unpaid, because
Cancelled and Refunded sit on the order-status axis and ARE real measurements.

The flag is read **strictly** (`=== false`) at all three: an older response that never
mentioned it says nothing, and the easy over-correction — showing the caveat whenever
the flag is falsy — is pinned against by a control at each consumer.

---

## 7. PR-6 — a report states the server's tender and status, or none

`stated()` in `reports-adapter` replaced `?? 'Cash'`, `?? 'paid'` and `?? 'pending'`.
Those did not paper over a rare gap: nothing in the platform records a diner payment, so
they MANUFACTURED the entire column — every real Sales row rendered a settled cash sale.

`null` is deliberately **not** a neutral token: `'unknown'` / `'other'` would become
values an operator can filter, sort and total by, and would read as something the server
said.

**A finding of this work, not of Stage A:** the rule reached three consumers and the
third spelled the absence differently. `formatCell(null, 'text')` returns `''`, so the
Sales listing rendered a **blank Method cell beside a `—` Status cell on the same row**
— one absence spelled two ways, which reads as data loss. A new `tender` column format
carries the absence rule; it maps no vocabulary, so the `momo` vs `MTN MoMo` KNOWN GAP
is narrowed (the adapter no longer invents a value) and deliberately **not closed** —
that still needs the product call it always did.

**One derived claim is deliberately KEPT:** `listingDisplayStatus('refund', null)` still
reads `refunded`, because `transaction_type` IS server-stated and the pill restates a
fact rather than inventing one from an absence.

---

## 8. PR-7 — storage, never application

The VAT-rate note carries the approved wording verbatim: *"This value is saved for
reference. Dinify does not currently use it to calculate tax on orders."* — **not**
"will be applied".

**Extension, recorded as a judgement call:** the Receipts section said *"What prints at
the bottom of customer receipts"*, a present-tense claim about something the platform
does not do — `receipt_footer` has NO consumer in either repository (no diner screen, no
kitchen ticket, no print sheet, no backend reader). That is the same class of claim the
approval authorized correcting, so the subtitle and helper were changed to describe
storage and to promise no future receipt either. **No tax was calculated, no quote
altered, no receipt created and no legal compliance inferred.**

---

## 9. Cutover and rollback (the §12 release note)

Full contract in `BREAKING_CHANGES.md` §17. The two halves order **differently** and
carrying one repo's rule across to the other is the mistake to avoid:

- **The 501 is FRONTEND FIRST.** Backend-first makes an older client send a **real OTP**
  for a payment that then fails — the OTP is dispatched before the POST. Frontend-first
  is inert: the new billing panel issues no collection request at all, so it behaves
  identically against a pre-D07 backend.
- **PR-2's projection is additive**, so an old client ignoring it is unaffected in
  either order.
- **PR-4** is safe in either order: both modes are orderable, so a new frontend against
  an old backend creates `order_pay` tables and an old frontend against a new backend
  creates `order_only` ones — and neither changes what a diner can do.
- **PR-5 / PR-6 / PR-7** are additive or client-only.

**Rollback** restores the payment claim. It is schema-free for everything except 0058,
whose reverse is a true inverse (future rows only).

---

## 10. Corrections carried forward into the record

Stage A's §10 evidence, corrected per the approval:

1. **E1 was an unsupported inference in one respect.** That every restaurant reads "Free
   trial" follows from `subscription_validity` defaulting True with no writer; whether a
   given deployed restaurant's screen does is a live-data question that was not asked
   and is not answered here.
2. **E3's conditionality does not rule out an older writer or an import.** "Nothing in
   the tree can write `preferred_subscription_method`" is a statement about the CURRENT
   source, not about how a deployed row got its value.
3. **"No current writer of paid/success" does not prove no historical rows exist.**
4. **A source search supports "no provider operation was found in the inspected current
   paths", not a claim about host state.** `REGULATORY_AUDIT.md`'s appendix already
   records that repo-clean is not host-clean (H2 in particular remains OUTSTANDING).
5. **`ENV=dev` is documentation, not a fresh check** — it was not re-verified.
6. **The orphaned route needed its own deep-link reachability check**, which was done:
   `PUT regenerate-qr` → 405, `POST` → 200.
7. **The NULL-purpose / latest-OTP interaction is source-led** and stays so; no
   discriminating test was executed for it, and none is claimed.

Three **§14 QR documentation carry-forwards** were applied to
`DELEGATED_QR_OPERATOR_NOTE.md` as marked corrections rather than rewrites: the verb is
**POST** not `PUT` (`TableActionsEndpoint` defines only `post`; PUT would 405);
rotation is gated through the **`tables` module** rather than "owner/manager" (
`DEFAULT_ROLE_MODULES` grants `tables` to `RESTAURANT_STAFF` too, and an owner can widen
or withdraw it through the grid); and the exposure window must be dated from the
**earliest** disclosing builder rather than the flat serializer's `qr_credential`, which
is the latest of six sites. A paragraph was added recording that **"no rows" is not
proof**, since a delegated GET that succeeds writes no audit row.

**No real history query, QR extraction, rotation, revocation, reprint or notification was
performed or authorized.**

---

## 11. Verification — and what was NOT run

Both repos are green at the delivered pair.

**Backend** `./scripts/verify.sh` — exit 0. django check PASS · makemigrations check
PASS · money-field guard PASS (11 models.py) · ambient-authority gate PASS (231 modules)
· tenant-relation ratchet PASS (no baseline additions, 57 entries) · tenant-isolation
closure **398 OK** · full suite **4644 OK** in 542s.

**Frontend** `./scripts/verify.sh` — exit 0. type-check PASS · lint PASS (0 errors,
pre-existing warnings only) · tenant-boundary **306/306** · `test:ci` **2736/2736** ·
`build:prod` PASS. The 500 kB initial-bundle warning at 870.34 kB is **pre-existing**
and documented in `CLAUDE.md`; `maximumWarning` exits 0.

**New pins** and their mutation results:

| spec | count | mutations |
|---|---|---|
| `restaurants_app/tests_qr_mode_default.py` | 6 | reverting the default fails exactly its 2 REGRESSIONs; all 4 CONTROLs hold |
| `restaurants_app/tests_subscription_read.py` | 26 | the PR-2 projection, its read gate, its denials and the delegated exclusion |
| `commercial_app/tests_reads.py` | 16 | the shared open-terms rule reused rather than reconstructed |
| `reports_app/tests_dashboard_report.py::DashboardV2PaymentTrackingTests` | 5 | v1 and v2 asserted EQUAL by identity |
| `frontend reports/payment-claim-honesty.spec.ts` | 13 | five mutations fail 2 / 1 / 2 / 2 / 1 named subsets, every control holding |
| `frontend dashboard/payment-tracking-disclosure.spec.ts` | 23 | five mutations fail 3 / 2 / 2 / 3 / 1 named subsets, every control holding |
| `frontend tables/tables-qr-mode.spec.ts` | 15 | classifies all eight sites, including that an unknown stored value is RETAINED |
| `frontend settings/billing/billing.component.spec.ts` | 16 | the read-only panel; `termsRecorded` vs `termsStated` kept apart |

**One shipped oracle was corrected rather than the rule relaxed.** The diner table-scan
snapshot in `restaurants_app/tests.py` asserted `'qr_mode': 'order_pay'` — its fixture
creates a table without naming a mode, so the literal tracked the MODEL DEFAULT. It was
corrected AND a dedicated pin added (`tests_qr_mode_default.py`), so reverting the
default still fails a test that says what it is about.

**NOT RUN, stated rather than implied:**

- **The backend suite was executed LOCALLY on Python 3.11.15, not CI's pinned 3.12.3.**
  The local interpreter is what this container provides; CI is the gate that runs the
  pinned version. Nothing in this change is version-sensitive (no new syntax, no new
  dependency, no interpreter-dependent behaviour), but the local run is not a
  substitute for the pinned leg — so this bullet recorded an OUTSTANDING gap rather
  than a closed one.
  **CI has since supplied it, and this line is kept rather than deleted so the
  provenance of each run stays legible.** `Backend CI` run `35636249160` on `8289ed6`:
  `suite (3.12.3)` green end to end — migrations consistency, the money-field guard,
  the ambient-authority gate, the tenant-relation ratchet, the tenant-isolation closure
  gate and the full suite — with the `test` aggregator green on it. The gap is closed
  by CI, not by the local run.
- **No browser journey was run for this change** — see §12.
- No live-data query, no deployment, no workflow dispatch, no production or UAT access,
  no real OTP/SMS/email, no credential extraction or rotation, no backfill or repair.

---

## 12. Open, and deliberately not done

- **The two Stage A questions the approval did not settle** remain open: how many
  deployed restaurants carry a legacy `monthly`/`yearly`
  `preferred_subscription_method`, and what becomes of historical pending
  `DinifyTransaction` rows. Nothing was backfilled, reclassified or repaired.
- **`e2e/checkout-journey/journey.mjs`, `recovery.mjs` and `e2e/kitchen-board/` were not
  re-run**, and **Playwright is deliberately not a dependency of either repo** — those
  harnesses are manual, expect it to be supplied externally, and additionally need a
  disposable PostgreSQL and two running servers. Installing an unpinned browser driver
  to produce one local run is exactly the "change a pinned dependency to accommodate a
  local runtime" the approval rules out.
  What replaces it, stated so the gap is measurable rather than hand-waved: the ONE
  behaviour change reaching the diner path is the `qr_mode` default, both modes sit
  inside `ORDERING_QR_MODES` and only `menu_only` blocks — so the two are operationally
  identical by construction. That is pinned deterministically by
  `tests_qr_mode_default.py`'s CONTROL pair (a default table and an existing `order_pay`
  table each admit a diner order through the real eligibility rule), and exercised
  incidentally by the **six** order-path fixtures across `orders_app` that create their
  table without naming a mode and therefore now inherit `order_only` — all green in the
  4,644-test run. A browser re-run would still be the stronger evidence and is the right
  thing to do before merge; it is not what this container can honestly produce.
- **A billing browser journey was not built.** The billing surface has unit coverage
  (`billing.component.spec.ts`, 16 specs driven through the real component); a browser
  harness for an operator settings screen has no precedent in this repo and would be a
  new standing asset rather than a verification of this change.
- **An adjacent defect found in passing and NOT fixed** (outside the authorized scope,
  reported rather than absorbed): `dashboard-adapter`'s `adaptRevenueSeries` hardcodes
  `orders: 0` and `aov: 0` on every point, because `dashboard-v2`'s revenue series
  carries no such keys. The Revenue chart tooltip therefore reads "Orders: 0 · AOV:
  UGX 0" on every hover. It is masked today by `USE_MOCK_DATA = true` (the mock supplies
  both) and is a flip-time landmine of the same family as E6 — but it is an ORDERS
  claim, not a payment claim, so closing it belongs with the Dashboard flip-time gate.
- **Not authorized and not built:** an aggregator integration, any real collection, a
  manual settlement acknowledgement, a subscription entitlement/invoice system,
  payment-before-preparation enforcement, a readiness-engine implementation, or a change
  to any venue's commercial decision.
