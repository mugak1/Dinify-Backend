# Dinify — Custodial Payments Regulatory Audit (READ-ONLY INVENTORY)

> **⚠️ STATUS — RETIRED ARCHITECTURE.** This is a point-in-time audit of the
> PRE-REMEDIATION codebase. The custodial payment design it inventories has since
> been REMOVED: the aggregator integration layer was retired in commit `7e15e3f`,
> and the balance-ledger models, disbursement, refund-payout, OVA fee-netting and
> tip wallets were removed across the finance teardown. **As of the current
> codebase the backend implements no payment execution of any kind** — the only
> surviving payment code is the record-only `DinifyTransaction` model and a
> subscription writer that records a Pending row and stops. Every present-tense
> claim below describes the OLD design and is retained (past-tensed where
> practical) purely as the historical remediation record — NOT as current
> behaviour.

> **Original audit note (superseded by the status banner above).** This began as
> read-only reconnaissance; the audit itself changed no code and prescribed the
> remediation that has since landed. Retained for provenance.

## Context — why this audit exists

Dinify must operate as a **software/technology vendor**, NOT a custodial payment
institution, aggregator, gateway/operator, or e-money issuer under Uganda's
National Payment Systems Act 2020. The previous developer ("Falcon") built the
finance layer on the **opposite** model:

- diner payments were collected into a **Dinify-controlled account** (an OVA /
  `dinify_revenue` account),
- Dinify held **per-restaurant balances**, and
- Dinify **disbursed** funds out to restaurants.

**Target (TO-BE), non-custodial:** funds settle DIRECTLY into each restaurant's
own account via a separately-licensed aggregator; Dinify NEVER receives, holds,
controls, settles, or disburses funds. Dinify ONLY (a) transmits a
payment-initiation instruction naming the RESTAURANT as payee, (b) receives
status callbacks, (c) records transactions for reporting/receipts.

This document inventories every structure that assumed or implemented the OLD
custodial model and maps the dependencies along which remediation was sequenced.

### Red-flag rubric (classification key)
- **A** Fund custody (Dinify-controlled bank/momo/OVA/escrow/suspense/settlement/trust)
- **B** Balance/claim ledger (balances restaurants have a claim to)
- **C** Settlement/disbursement control (settle/remit/disburse/release/withhold/schedule)
- **D** Merchant-of-record / collect-on-behalf (Dinify is payee/collector of gross)
- **E** E-money / stored value (wallets, balances, credits, cash-out, top-ups)
- **F** Fee deduction from held funds (Dinify nets its cut from funds it holds)
- **G** Refund/chargeback control (Dinify initiates/controls reversals)
- **H** Payment-product framing / credential control (Dinify-as-processor; aggregator credentials scoped to move funds / change settlement / refund)
- **I** UI/receipt misrepresentation (screen/receipt shows Dinify as payer/collector/holder/processor, or a Dinify-held balance)

### Method
5 parallel read-only Explore sweeps (finance_app; payment_integrations_app +
string_definitions; reports/refunds/fees/receipts; cross-app blast radius;
frontend UI) plus first-hand reads of the highest-severity files
(`finance_app/models.py`, `string_definitions.py`, `tx_subscription.py`,
`tx_disbursement.py`, `initiate_refund.py`, `serializers.py`, `urls.py`,
`seed_dinify_account.py`, root urlconf).

---

## Verdict

At the time of this audit the backend implemented a **fully custodial** payment
model end-to-end (since removed — see the status banner above). The custodial
money-flow loop was:

```
diner pays
  → collected via Yo/DPO/Flutterwave on DINIFY's aggregator credentials (Dinify float)   [A,D,H]
  → recorded as DinifyTransaction; restaurant's DinifyAccount balance CREDITED            [A,B]
  → restaurant requests payout; Dinify checks *_available_balance, pushes funds out (Yo)  [B,C]
Subscriptions via OVA: DEBIT restaurant DinifyAccount, CREDIT dinify_revenue account      [A,B,E,F]
Refunds: Dinify initiates payout to customer from its Flutterwave float                   [A,G]
```

The non-custodial target required retiring or re-scoping the balance ledger
(`DinifyAccount`), the disbursement path, the OVA fee-netting, and Dinify-initiated
refunds; relabelling the merchant-of-record framing in the aggregator integrations;
and hiding/removing the Dinify-held-balance UI. A `DinifyTransaction` row, **stripped
of balance mutation and re-scoped to a pure record**, was KEPT for reporting/receipts.

Severity tally (as-audited): **HIGH ≈ 12 structures**, MEDIUM ≈ 14, LOW/KEEP ≈
several. The two most-entangled structures (`DinifyTransaction` ~29 consumers,
`DinifyAccount` ~19 consumers) were remediated LAST.

---

## FINDINGS — Backend

### 1) finance_app/models.py — the custodial data model (HIGH)

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| M1 | `finance_app/models.py:60` `DinifyAccount` | "the accounts held at Dinify" — one per restaurant + one `dinify_revenue` + waiter (`user`) accounts. **Core custodial ledger.** | A,B,E | HIGH | RE-SCOPE or REMOVE balance ledger; keep at most a non-monetary settlement-config record |
| M2 | `models.py:81-111` balance fields | Per payment-mode (`momo_/card_/cash_`): `*_actual_balance`, `*_available_balance`, `*_cumulative_in/out/in_charges/out_charges/refunds/disbursements` (~30 DecimalFields). These are funds Dinify holds + restaurant claim. | A,B,E,F | HIGH | REMOVE all balance/cumulative fields (Dinify holds no funds) |
| M3 | `models.py:120` `DinifyTransaction` | Transaction record. **Mixed**: neutral fields (type/status/amount/aggregator_reference/timestamps) AND custodial fields. | A,B,C,D,F | HIGH | RE-SCOPE-TO-RECORD-ONLY (strip custodial fields below) |
| M4 | `models.py:139-140,159,168,171` | `amount_in`, `amount_out`, `account_balances` (JSON before/after balance snapshot), `processed`, `revenue_collected` (bool — "revenue collected from the transaction", for "restaurants where Dinify has surcharge"). | B,F | HIGH | REMOVE these fields; they encode balance movement + fee-netting |
| M5 | `models.py:177` `BankAccountRecord` | Restaurant/user bank details + `yo_reference` (Yo-issued id from registering the account **under Dinify's Yo credentials**). Used by Dinify-controlled bank disbursement. | C,H | MED-HIGH | REPLACE-WITH-RESTAURANT-DIRECT (restaurant registers its own settlement destination) |
| M6 | `models.py:206,219,232` post_save archive signals | Archive `DinifyAccount`/`DinifyTransaction`/`BankAccountRecord` to MongoDB (`transformed_amounts=False`). Mirror the custodial schema into archive. | A,B | LOW | Follows the model — revise when models are re-scoped (keep try/except wrappers per MongoDB rule) |

Note: `TRANSACTION_TYPES` (models.py:24) validates against OrderPayment, OrderRefund,
OrderCharge, Disbursement, Subscription — **omits `TransactionType_Tip`** (defined in
string_definitions:36). Tips are still created with that type (Django field validators
don't run on `.create()`), so tip rows exist unvalidated. Minor data-integrity note.

### 2) finance_app/controllers — custodial logic (HIGH)

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| C1 | `controllers/update_wallet_balance.py` `update_wallet_balance()` | Central balance-mutation hub: mutates `*_actual_balance`/`*_available_balance`/cumulatives per mode; returns before/after JSON. Hub for all money movement. | A,B,C,F | HIGH | REMOVE (no held balances to mutate). Blocking dependency for M1/M2. |
| C2 | `controllers/tx_disbursement.py` `DisbursementTransaction.initiate()` | Gates payout on `momo_available_balance`/`card_available_balance` (`:51`), then `YoIntegration().momo_disburse()` (`:76`) pushes funds OUT to restaurant owner's / waiter's MSISDN. Bank path is a `TODO/pass` (`:97-99`). | A,B,C | HIGH | REMOVE (Dinify does not hold or pay out funds) |
| C3 | `controllers/tx_subscription.py` `SubscriptionPaymentTransaction.process()` OVA branch (`:186-222`) | When `payment_mode==ova`: DEBIT restaurant `DinifyAccount` (`amount_out`) then CREDIT `dinify_revenue` account (`amount_in`) + 2nd DinifyTransaction. **Textbook fee-netting from held funds.** | A,B,E,F | HIGH | REMOVE OVA path; bill subscription separately (invoice), never net from held balance |
| C4 | `controllers/tx_subscription.py` momo/card branch (`:92-170`) | Collects subscription via Yo/DPO on Dinify credentials; on confirm CREDITS the account balance. | A,D,F | MED | RE-SCOPE: subscription billing decoupled from any held balance; Dinify-direct billing |
| C5 | `controllers/initiate_refund.py` `initiate_refund()` | Creates `OrderRefund` DinifyTransaction vs restaurant account; for momo fires `Flutterwave(...).send_mobile_money()` to pay the customer **from Dinify's Flutterwave float**. Dinify decides/executes the refund. | A,G | HIGH | REPLACE: relay/record a restaurant/PSP refund decision only; never push from a Dinify float |
| C6 | `controllers/tx_order_payment.py` `OrderPaymentTransaction.initiate()/process()` | Records order payment vs restaurant `DinifyAccount`; calls Yo/DPO on Dinify credentials; on success CREDITS restaurant balance + writes `Order.total_paid`/`balance_payable`/`payment_status`; triggers tip collection. | A,B,D,H | HIGH | REPLACE-WITH-RESTAURANT-DIRECT for collection; KEEP only the Order-state update (re-scoped to reflect aggregator callback, not a balance credit) — **RETIRED:** `tx_order_payment.py` DELETED (order-payment write path removed; no Order-state update remained to keep). Rebuild at PSP integration. |
| C7 | `controllers/tx_tip.py` `TipTransaction.initiate()` | Creates/credits a waiter `DinifyAccount` (`AccountType_User`) and records a `Tip` transaction — tips held in a Dinify wallet. | A,B,E | MED | REPLACE-WITH-RESTAURANT-DIRECT (tip settles to waiter/restaurant directly) |
| C8 | `controllers/process_order_payment.py` `process_order_payment()`/`collect_tip()` | Updates Order paid/balance/payment_status on success; `collect_tip()` credits waiter wallet. Legacy-ish; depends on custodial transaction state. | A,B | MED | KEEP order-state logic, RE-SCOPE to aggregator-callback driven; drop wallet credit |
| C9 | `controllers/process_payment_feedback.py`, `process_yo_feedback.py` | Route aggregator responses to transaction processors; mark confirmed; trigger balance updates. | A,B | MED | RE-SCOPE-TO-RECORD-ONLY (record callback; no balance mutation) |
| C10 | `controllers/initiate_order_payment.py` | Entry wrapper for order-payment initiation (Yo primary; a Flutterwave block is commented out). | A,D | MED | REPLACE-WITH-RESTAURANT-DIRECT |
| C11 | `controllers/initiate_transaction.py` | Generic initiator — imported by transactions endpoint but the call site is **commented out** (effectively dead). | A | LOW | REMOVE (dead code) |

### 3) finance_app endpoints / serializers / urls (HIGH)

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| E1 | root `dinify_backend/urls.py:29` → `finance_app/urls.py` | Mounts at `api/v1/finances/`: `initiate-order-payment/` (`OrderPaymentsEndpoint`), `transactions/` (`TransactionsEndpoint` — routes subscription/disbursement/refund), `bank-accounts/` (`BankAccountRecordsEndpoint`). | A,C,D,G | HIGH | RE-SCOPE: keep payment-initiation (restaurant-direct) + records; REMOVE disbursement/refund routes |
| E2 | `endpoints/transactions.py` `TransactionsEndpoint` | Single endpoint dispatching subscription / **disbursement** / **refund** to the custodial controllers (C2/C3/C5). | A,C,G | HIGH | REMOVE disbursement+refund dispatch; re-scope subscription |
| E3 | `endpoints/order_payments.py` `OrderPaymentsEndpoint` | POST initiates order payment (→ C6). | A,D | HIGH | REPLACE-WITH-RESTAURANT-DIRECT — **RETIRED:** `OrderPaymentsEndpoint` + the `initiate-order-payment/` route DELETED (POST now 404s). |
| E4 | `endpoints/bank_account.py` `BankAccountRecordsEndpoint` | CRUD for `BankAccountRecord` incl. `yo_reference` lifecycle (used for Dinify-controlled disbursement). | C,H | MED-HIGH | REPLACE-WITH-RESTAURANT-DIRECT (settlement-destination config) |
| E5 | `serializers.py` `SerializerGetRestaurantTransactionListing` / `SerializerGetDinifyTransactionListing` | Both expose `account_balances` (Dinify-held balance snapshot) over the API; restaurant listing also derives `amount_in`/`amount_out`. | B,I | MED | REMOVE `account_balances`/`amount_in`/`amount_out` from API; keep neutral record fields |
| E6 | `serializers.py` `SerializerPutAccount` / `SerializerPutDinifyTransaction` | `fields='__all__'` over `DinifyAccount`/`DinifyTransaction` — exposes/writes every balance field. | A,B | MED | REMOVE/restrict once models re-scoped |

> **Update — order-payment write path RETIRED (post-audit):** The anonymous
> `AllowAny` `initiate-order-payment/` route, `OrderPaymentsEndpoint` (E3), and
> `OrderPaymentTransaction` (C6) have been **DELETED** — the endpoint wrote a
> `DinifyTransaction` for any order UUID with no auth, no ownership check, and a
> client-supplied `split` amount (closes BUG-P2-3e / BUG-P2-7). The record-only
> `DinifyTransaction` model, its serializers, the subscription writer
> `tx_subscription.py` (via the surviving `TransactionsEndpoint`), and both
> Transactions reports remain in use and are unchanged. The order-payment
> collection path will be rebuilt at PSP integration: authenticated,
> ownership-gated, server-bounded amounts, non-custodial Pattern A. This note
> annotates the frozen AS-WAS inventory; the rows above are left intact as the
> historical record.

### 4) finance_app management commands (scheduled jobs — HIGH/MED)

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| K1 | `management/commands/seed_dinify_account.py` | Creates the singleton `DinifyAccount(account_type='dinify_revenue')` — **Dinify's revenue vault**. | A,B | HIGH | REMOVE (no Dinify-held revenue account) |
| K2 | `management/commands/createaccountswithyo.py` | For `BankAccountRecord` with null `yo_reference`, calls `YoIntegration().bank_create_verified_account()` and stores Yo id — registers restaurant bank accounts **under Dinify's Yo credentials** for disbursement. | C,H | HIGH | REMOVE (restaurant registers own settlement destination) |
| K3 | `process_transactions.py` | Sweeps `DinifyTransaction` by `processing_status` and dispatches `.process()` (drives balance updates + disbursements + OVA netting). | A,B,C | MED | RE-SCOPE to callback-recording only |
| K4 | `check_yo_transactions.py`, `check_dpo_transactions.py`, `check_transaction_statuses.py` | Poll aggregators for pending transaction status; confirm transactions → balance updates. | A,B | MED | RE-SCOPE-TO-RECORD-ONLY (status reconciliation without balance mutation) |
| K5 | `verify-dpo-tokens.py` | Verifies DPO token/credential health (Dinify's DPO merchant token). | H | LOW | RE-SCOPE/relabel once merchant-of-record removed |

Scheduling: these ran as cron/scheduled jobs (the commands have since been removed).

### 5) payment_integrations_app — aggregator wiring (HIGH)

Settlement destination across **all** integrations was **Dinify's** aggregator
account/credentials, not the restaurant's. Credentials were **Dinify-global** env
vars (`FLUTTERWAVE_SECRET`, `DPO_COMPANY_TOKEN`, Yo username/password,
`PESAPAL_CONSUMER_KEY/SECRET`), scoped to collect AND to move funds out.

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| P1 | `controllers/yo_integrations.py` `momo_collect()` | Yo `acdepositfunds`; narrative `'Dinify Order Payment'`. Collects into Dinify's Yo account. | A,D,H | HIGH | REPLACE-WITH-RESTAURANT-DIRECT (settle to restaurant merchant code) |
| P2 | `yo_integrations.py` `momo_disburse()` | Yo `acwithdrawfunds`; narrative `'Dinify Disbursement'`. Pays OUT from Dinify's Yo float. | A,C | HIGH | REMOVE |
| P3 | `yo_integrations.py` `bank_disburse()` | Yo `acwithdrawfundstobank`; **hardcoded `bank_account_name='ESAU LWANGA'`** — payout from Dinify float to a bank. | A,C,H | HIGH | REMOVE (and note hardcoded beneficiary as a defect/ambiguity) |
| P4 | `yo_integrations.py` `bank_create_verified_account()` | Registers a restaurant bank account under Dinify's Yo credentials; stores `ApiBankIdentifier`→`yo_reference`. | C,H | HIGH | REPLACE-WITH-RESTAURANT-DIRECT |
| P5 | `yo_integrations.py` `process_yo_response()` + `momo_check_transaction()` / `bank_check_disbursement_status()` | Process callbacks; confirm DinifyTransactions; own disbursement status. | A,B,C | MED | RE-SCOPE-TO-RECORD-ONLY |
| P6 | `controllers/dpo.py` `create_token()`/`verify_token()`/`process_response()` | Card payment via **Dinify's** DPO company token; `ServiceDescription='Dinify Order Payment'`; redirect to `dinify-web`. | A,D,H | HIGH | REPLACE-WITH-RESTAURANT-DIRECT |
| P7 | `controllers/flutterwave.py` `collect_mobile_money()` | Momo collect into **Dinify's** Flutterwave account (was commented out at the order-payment call site). | A,D,H | MED | REMOVE or REPLACE-WITH-RESTAURANT-DIRECT |
| P8 | `flutterwave.py` `send_mobile_money()` | Momo payout/refund (`'Dinify Refund'`) from Dinify's Flutterwave float — backs `initiate_refund` (C5). | A,C,G | HIGH | REMOVE |
| P9 | `controllers/pesapal.py` `authenticate()` (+ scaffolding) | Holds Pesapal consumer credentials (Dinify-level); no active flow yet. | H | LOW-MED | REPLACE-WITH-RESTAURANT-DIRECT or REMOVE |
| P10 | `management/commands/process_aggregator_responses.py` | Batch-processes aggregator callbacks from Mongo collections → confirm + balance update. | A,B | MED | RE-SCOPE-TO-RECORD-ONLY |

### 6) string_definitions.py constants (`dinify_backend/configss/string_definitions.py`)

Account types (`:10-13`):
- `AccountType_DinifyRevenue='dinify_revenue'` — **smoking gun**: Dinify's own held-revenue account. **A,B,E. HIGH. REMOVE.**
- `AccountType_Restaurant='restaurant'` — restaurant account **held at Dinify**. A,B. HIGH. RE-SCOPE (only as non-monetary record).
- `AccountType_User='user'` — waiter tip wallet. A,E. MED. RE-SCOPE/REMOVE.

Payment modes (`:16-20`):
- `PaymentMode_Ova='ova'` — **smoking gun**: Dinify internal wallet/stored value. A,B,E. HIGH. REMOVE.
- `PaymentMode_Bank='bank'` — used only by Dinify-controlled bank disbursement. C. MED. REMOVE with disbursement.
- `PaymentMode_MobileMoney='momo'`, `PaymentMode_Card='card'` — neutral channels; collected into Dinify (custodial **because of how used**, not the label). LOW. KEEP (re-scope usage).
- `PaymentMode_Cash='cash'` — neutral. KEEP.

Transaction types (`:31-36`):
- `TransactionType_Disbursement='disbursement'` — Dinify pays out held funds. A,C. HIGH. REMOVE.
- `TransactionType_Subscription='subscription'` — when via OVA, netted from held funds. F. MED. RE-SCOPE.
- `TransactionType_OrderRefund='order_refund'` — Dinify-initiated refund. G. MED. RE-SCOPE/REMOVE.
- `TransactionType_Tip='tip'` — tips held in Dinify wallet (note: not in model's validated list). A,E. MED. RE-SCOPE.
- `TransactionType_OrderCharge='order_charge'` — per-order Dinify fee type; **latent/unused in order pricing today**. F (if activated). MED. AMBIGUOUS — see below.
- `TransactionType_OrderPayment='order_payment'` — neutral if re-scoped to record-only. LOW. KEEP.

Processing statuses (`:84-89`):
- `ProcessingStatus_PendingRevenueAcknowledgement='pending_revenue_acknowledgement'` — names a Dinify revenue-custody workflow; **appears UNUSED** in code. A,B. LOW (cosmetic, but a smoking-gun label). REMOVE.
- Others (`Pending/Done/Confirmed/Failed`) neutral. KEEP.

Statuses `TransactionStatus_*`, aggregator tags (`Aggregator_DPO/Yo`), telecoms,
roles, EOD sys-config: neutral. KEEP.

### 7) reports_app / dashboard

| # | Path / symbol | What it did | Flags | Sev | Revision |
|---|---|---|---|---|---|
| R1 | `reports_app/controllers/dinify/dashboard.py` `summarize_dinify_earnings()` / `generate_dinify_dashboard()` | Aggregates `DinifyTransaction` (subscription + order_charge) into "Dinify earnings" + outstanding-subscription gating. Frames Dinify as revenue holder/collector. | A,B,F,I | MED-HIGH | RENAME-RELABEL → "subscription billings"; remove held-revenue framing |
| R2 | `reports_app/controllers/restaurant/transactions.py` (+ `dinify/transactions.py`) | Lists `DinifyTransaction` incl. `account_balances` for the transaction-listing endpoint (`reports/restaurant/transactions-listing/`) — feeds the frontend balance cards. | B,I | MED | RE-SCOPE-TO-RECORD-ONLY; drop balance snapshot |
| R3 | `reports_app/controllers/restaurant/dashboard.py` `summarize_revenue()` / `revenue` widgets | Sums `Order.actual_cost` where `payment_status=paid` — **reads order/transaction records, NOT a Dinify-held balance.** | — | LOW | KEEP (confirm wording stays "sales/revenue", not "available for disbursement") |
| R4 | `reports_app/controllers/restaurant/sales.py`, `eod/generate_daily_reports.py`, `eod/confirm_daily_orders.py` | EOD/sales reporting reads `DinifyAccount`/`DinifyTransaction`. | B | MED | RE-SCOPE to record-only once balance fields retired |

### 8) Fee / surcharge & receipts

- **Surcharge config**: `Restaurant.order_surcharge_percentage` / `_min_amount` /
  `_cap_amount` exist (editable). `orders_app/controllers/con_orders.py` sets
  `actual_cost = discounted_cost` — **surcharge is NOT currently added to order
  cost / netted from settlement.** Infra is **dormant**. Flags F *if activated*.
  MED. → **AMBIGUOUS** (see below). KEEP-as-dormant; if activated must be a
  separate bill/aggregator split, never netted from held funds.
- **The only fee-netting that existed** was the OVA subscription debit→`dinify_revenue`
  credit (C3) + the `revenue_collected`/`*_cumulative_*_charges` infra (M2/M4).
- **Receipts**: No PDF/HTML receipt generator exists in the backend.
  `Restaurant.receipt_footer` is a stored string only; `notifications_app/.../messenger.py`
  is generic email. **No backend "paid to Dinify" wording found.** I (latent). LOW.
  → **GAP**: a restaurant-as-payee receipt generator must be built before launch.

---

## FINDINGS — Frontend (UI surfaces only; do NOT change)

| # | Path / route | What it shows | Flags | Sev | Revision |
|---|---|---|---|---|---|
| U1 | `restaurant-mgt/payments/payments.component.*` — `/rest-app/payments` | "Actual Balance" + "Available Balance" cards (sum of momo/card/cash); **"Disburse Funds"** button → "Disburse Collections" modal (mode, phone, amount, OTP); ledger w/ running **Balance** column. (`Save()` is empty — disburse submit unwired.) | A,B,C,E,I | HIGH | REMOVE balance cards + disburse modal; RE-SCOPE ledger to records; RELABEL to "Transaction History" |
| U2 | `dinify-mgt/payments/payments.component.*` — `/dinify-mgt/payments` | Same balance cards + disburse modal + balance ledger, admin-wide (no restaurant filter). | A,B,C,E,I | HIGH | REMOVE or RE-SCOPE to admin audit view of records |
| U3 | `restaurant-mgt/report-detail/report-detail.component.html` — `/rest-app/reports/transactions` | "Opening Balance" / "Closing Balance" columns from `account_balances.before/after`; Amount In/Out. | B,I | MED | REMOVE balance columns; keep records |
| U4 | `_models/app.models.ts` `Account` (≈:568-650) | Client model carries `*_actual_balance`, `*_available_balance`, `*_cumulative_disbursements` etc. | B | LOW | KEEP for now; remove from UI; drop when backend fields go |
| U5 | `restaurant-mgt/dashboard/components/revenue-card/*` | "Revenue (UGX)" gross/discounts/refunds/net — sales reporting, not held balance. | — | LOW | KEEP (verify it's restaurant sales, not "Dinify-collected/available") |
| U6 | `diner-app/payment-details/*`, `diner-app/order-complete/*` | Payment outcome + order confirmation; neutral wording; **no Dinify-as-payee.** | — | LOW | KEEP |

No diner-facing receipt misrepresentation found (no "paid to Dinify").

---

## Blast-radius map (ranked least → most entangled)

Sequence remediation from the top (safe) down. Counts = distinct consuming files.

| Rank | Structure | ~Consumers | Note |
|---|---|---|---|
| 1 | `ProcessingStatus_PendingRevenueAcknowledgement` | 1 (def only) | Unused label — safe delete |
| 2 | `initiate_transaction()` | 2 (call site commented out) | Dead code |
| 3 | `process_order_payment()` (legacy) | ~1 | Superseded |
| 4 | `tx_tip` / `TipTransaction` | ~3 | Tip wallet |
| 5 | `tx_disbursement` / `DisbursementTransaction` | ~3 | Disbursement |
| 6 | `initiate_refund()` | ~3 | Refund |
| 7 | `tx_order_payment` / `OrderPaymentTransaction` | ~4 | Collection + order-state |
| 8 | `tx_subscription` / `SubscriptionPaymentTransaction` | ~5 | OVA netting |
| 9 | `update_wallet_balance()` | ~6 | Balance hub (blocks M1/M2) |
| 10 | `PaymentMode_Ova` | ~6 | Wallet mode |
| 11 | `BankAccountRecord` (+`yo_reference`) | ~7 | Disbursement target |
| 12 | management commands (process/check/seed/createaccountswithyo) | ~9 | Scheduled jobs |
| 13 | finance URLs/endpoints | ~5 | API surface |
| 14 | `PaymentMode_*` / `TransactionType_*` / `AccountType_*` constants | ~11-13 | Dispersed string refs |
| 15 | **`DinifyAccount`** | ~19 | restaurants_app create, reports EOD, integrations — **near-last** |
| 16 | **`DinifyTransaction`** | ~29 | **most entangled** — all reporting + integrations + orders read it — **LAST** |

Key blocker chain: integrations + controllers → `update_wallet_balance()` →
`DinifyAccount` balance fields. Reports_app (≥8 files) read `DinifyTransaction`,
so a record-only `DinifyTransaction` had to survive the cut.

Cross-app consumers to watch: `restaurants_app/controllers/create_restaurant.py`
(creates a `DinifyAccount` per restaurant), `reports_app/*`,
`orders_app/controllers/con_orders.py` + `determine-customers` command,
`payment_integrations_app` (yo/dpo), and `Order.total_paid/balance_payable/payment_status`
written by finance controllers (these Order fields are business mirrors, likely KEEP/RE-SCOPE).

---

## Ambiguous / needs-human-decision

1. **`DinifyTransaction` as a whole** — KEEP a stripped, record-only version
   (type/status/amount/aggregator_reference/timestamps/order link) for
   reporting/receipts, OR replace entirely? It is both the custodial ledger and
   the only transaction record. Decision drives the whole reporting layer.
2. **Order-surcharge config (`Restaurant.order_surcharge_*`)** — dormant today
   (not added to `actual_cost`). Is it intended future Dinify revenue? If so, it
   must be a separate bill / aggregator split, never netted from held funds.
3. **`TransactionType_OrderCharge`** — defined and summed in the dinify dashboard
   but not produced by order pricing. Latent per-order fee. Custodial only if
   wired to debit held funds — confirm intent.
4. **Tips (`tx_tip`, `AccountType_User`)** — keep tipping as a product but settle
   waiter-direct, or drop tips entirely from the platform? Custodial as built.
5. **`P3 bank_disburse()` hardcoded beneficiary `'ESAU LWANGA'`** — defect or
   placeholder? Either way it disappears with disbursement, but flag it.
6. **`Order.total_paid` / `balance_payable` / `payment_status`** — keep as
   aggregator-callback-driven records (recommended) vs treat as custodial mirror?
7. **Pesapal** — scaffolding only; remove, or is it the intended licensed
   aggregator for the non-custodial future?

---

## Gaps (the non-custodial target needs these; they DON'T exist today) — flag only

- **Restaurant's own settlement destination** field(s): its mobile-money merchant
  code / bank account / sub-merchant id with the licensed aggregator, that the
  aggregator settles into directly. (`BankAccountRecord` exists but is wired for
  Dinify-controlled disbursement, not direct settlement; no per-restaurant
  aggregator merchant/sub-merchant mapping exists.)
- **Per-restaurant aggregator credential / sub-merchant mapping** (current Yo/DPO/
  Flutterwave credentials are Dinify-global, scoped to move funds).
- **Restaurant-as-payee receipt generator** (none exists; `receipt_footer` is just text).
- **Separate subscription-billing mechanism** (invoice/charge decoupled from the
  OVA debit and the held-balance ledger).
- **Callback-only recording path** that writes a transaction record without mutating
  any Dinify balance (every confirm routed through `update_wallet_balance`).

---

## Safe-to-KEEP (pure non-custodial records / neutral)

- `DinifyTransaction` **record-only fields**: id, timestamps, `transaction_type`,
  `transaction_status`, `transaction_amount`, `payment_mode`, `aggregator`,
  `aggregator_reference`, `order` link, `msisdn` (subject to decision #1).
- `reports_app` restaurant **revenue/sales** widgets that read `Order.actual_cost`
  (R3) — not a Dinify-held balance.
- Diner payment-outcome / order-complete screens (U6).
- Neutral constants: `PaymentMode_Cash`, `TransactionStatus_*`, `Aggregator_*`,
  telecoms, roles, EOD sys-config.

---

## HARD STOP

This was the end of the read-only audit. At the time it was written, no
production code had been modified and no remediation, migrations, or refactors
had been performed; no legal judgment was made — findings were mapped to the
rubric only. The remediation the audit recommended has since landed (see the
status banner at the top); this file is retained as the historical record.

---

## APPENDIX — Post-audit host findings (2026-07-29)

**Scope note.** Everything above this line examined **the repository**, and the
remediation it prescribed was verified **in the repository**. The production host
was never in scope and was never examined. That gap is the subject of this
appendix.

The findings below were discovered during read-only reconnaissance ahead of a
Python 3.12 runtime migration, and are recorded here so the audit record reflects
the **host** as well as the code. They are appended, not merged into the
inventory above — the original findings tables are a dated point-in-time record
and are left intact.

The recurring theme: **removing code from a repository does not remove what the
host is still holding or still running.** Four of the five findings are artefacts
the teardown never reached because the teardown's scope was the tree.

### Summary

| # | Finding | Relates to | Sev | State |
|---|---|---|---|---|
| H1 | Custodial scheduler survived the code teardown — root cron fired a deleted command every minute for ~5 months | K3 | LOW (as executed) | Stopped 2026-07-29 |
| H2 | PSP credentials retained on the host in a world-readable backup `.env` | §5, P1–P9 | HIGH | **Provider revocation outstanding** |
| H3 | OTP broadcast to contractor mailboxes for ~12 months | — | **HIGHEST** | Code path closed; **impact assessment open** |
| H4 | MongoDB Atlas access list open to the internet (`0.0.0.0/0`) | M6 | MED-HIGH | **Outstanding** |
| H5 | `EMAIL_HOST` domain no longer exists — credential trap | — | MED-HIGH | Mitigated 2026-07-29; **provider outstanding** |

### H1 — Custodial scheduler survived the code teardown (relates to K3)

`process_transactions` **the command** was deleted in the teardown.
`process_transactions.sh` **in the host's root crontab** was not, and continued
firing **every minute until 2026-07-29** — roughly five months after the command
it called ceased to exist.

It executed **no payment logic** during that period, because there was nothing
left to execute; every invocation failed (a missing path on one target, `Unknown
command` on the other). The operational residue was 594 daily log files totalling
739 MB.

**Severity: LOW as executed.** Its significance is not what it did but what it
demonstrates: it is the clearest illustration in this record of the scope gap
between a repository audit and a production host. K3 was marked remediated on the
strength of the command's deletion; the thing actually invoking it, on a
one-minute schedule, was never in view. The scheduling side of this finding is
recorded in [`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md).

### H2 — PSP credentials retained on the production host

| | |
|---|---|
| **Path** | `/home/ubuntu/dinify_backend_uat.env.backup` |
| **Owner / mode** | `root:root`, `644` |
| **Written** | 2026-03-17 — one day after the last custodial hardening commit (`fe971d3`), and three months before the teardown |
| **Contents** | `DPO_COMPANY_TOKEN`, `DPO_SERVICE_TYPE`, `DPO_TEST_PRODUCT`, `FLUTTERWAVE_SECRET`, `YO_API_USERNAME`, `YO_API_PASSWORD`, `DEFAULT_PAYMENT_EMAIL` |
| **Live `.env`** | Clean of all of these |

Mode `644` made the file readable by **any local account that knew the path**,
including `www-data` — the account the application itself runs as, and therefore
the account an application-level file-read flaw would have been operating under.

**Exposure checks performed, all negative:** `authorized_keys` held one entry
(`github-actions-deploy`); no contractor accounts existed on the host; and a full
git-history scan found no credential ever committed. **No evidence of external
disclosure was found** in the checks performed. That is a statement about the
checks performed, not a guarantee of non-disclosure.

**Outstanding — record remediation here:** which provider accounts were closed or
revoked, and on what date. The credentials in this file remain live until a
provider-side revocation says otherwise; deleting the file does not revoke them.

### H3 — OTP broadcast to contractor mailboxes (2025-02-04 → 2026-02-19)

**The highest-severity finding in this record.**

| Commit | Date | Effect |
|---|---|---|
| `908f179` "send OTP via email" | 2025-02-04 | Introduced the broadcast |
| `f7c45ac` | — | Briefly removed it |
| `3f03f45` | — | Re-enabled it |
| `cee432a` "removing contact info" | 2026-02-19 | Removed it — recipients narrowed to `[user.email]` |

During that window, **every OTP the restaurant portal generated was emailed to a
fixed list of five mailboxes** — three at the contractor's domain, two personal
accounts — rather than to the requesting user.

The consequence follows directly from how the OTP is used. Because
`reset_password(username, otp)` takes the OTP **as the password-reset
mechanism**, anyone reading those mailboxes could reset the password on any
account whose OTP was generated. The OTP was not a second factor in that flow; it
was the credential.

Mail was relayed via `notifications@greatoaksfinance.com` — **a third party's
mail server** — widening the set of parties able to observe it in transit beyond
the five named mailboxes.

**Current state:** the code path is closed. No contractor address appears
anywhere in the current tree, and the relay domain no longer resolves (see H5).

**Deliberately left open:** the set of affected data subjects, pending the
account-creation query and counsel review. **No legal determination is made in
this document** — consistent with the original audit's method, findings here are
recorded and mapped, not adjudicated.

### H4 — MongoDB Atlas network access list open to the internet

The Atlas project's IP Access List contains **`0.0.0.0/0`**, commented *"for
development purposes,"* alongside the expected `35.177.46.58/32`. The cluster
therefore accepts connections **from any address on the internet**, with the
database password as the only control.

**What bounds the impact:** the `archive_user` `post_save` signal — which had
been shipping full `User` rows **including password hashes** into Atlas — was
removed in PR-0B, and is now held out by the permanent regression guard
`users_app/tests_no_user_archival.py`. The remaining writers are action logs
only, and those have been failing anyway because the cluster is unreachable from
the host.

So the exposure is bounded by what is in the cluster, not by the access list.
That is a fortunate position, not a designed one.

**Outstanding:** remove `0.0.0.0/0`.

### H5 — SMTP host domain no longer exists

`EMAIL_HOST` pointed at `mail.greatoaksfinance.com`. Verified **NXDOMAIN at the
apex** from two independent resolvers on 2026-07-29 — no NS, MX or A record.

Two distinct problems:

1. **Third-party sender identity.** Because `DEFAULT_FROM_EMAIL = EMAIL_HOST_USER`,
   every transactional email Dinify sent carried **a third party's domain in the
   `From` header**.
2. **A credential trap.** An unregistered domain in `EMAIL_HOST` is worse than a
   dead one. Whoever registers it can obtain a valid certificate for the name and
   **receive the SMTP credentials on the next send** — TLS verification would
   legitimately succeed, so nothing in the sending path would object. The failure
   mode is silent credential disclosure to whoever buys a lapsed domain.

**Mitigated 2026-07-29** by setting `EMAIL_HOST = localhost`, which removes the
trap. `EMAIL_HOST` is environment-driven, so this was a host `.env` change with
no repository counterpart.

**Outstanding:** a real transactional email provider, on a Dinify-controlled
domain, before launch.

### Provenance and verification boundary

Recorded honestly, in the spirit of the original audit's method section:

- The **host-side observations** (file paths, modes, crontab contents, log
  volumes, Atlas configuration, DNS results) come from the 2026-07-29
  reconnaissance and are **not independently reproducible from this repository**.
- The **commit SHAs** cited in H2 and H3 — `fe971d3`, `908f179`, `f7c45ac`,
  `3f03f45`, `cee432a` — and the teardown commit `7e15e3f` were **not verified in
  the working clone**, which is shallow. They are recorded as reported.
  (`7e15e3f` is corroborated internally: this document's own status banner cites
  it.)
- Three claims **were** verified against the tree at time of writing: no
  contractor or relay-domain string appears anywhere in it (H3);
  `users_app/tests_no_user_archival.py` is present (H4); and
  `DEFAULT_FROM_EMAIL = EMAIL_HOST_USER` at `dinify_backend/settings.py:300` (H5).

Nothing in this appendix has been remediated by the change that added it. It is a
record, not a resolution — H2, H4 and H5 each carry outstanding host-side work.
