# D06 — closure recovery, evidence consumption and current authority

The record for the **C1 / C2 / C3 / A1 / V1** pass that follows the G1–G5
completion (backend `d23372a`, frontend `80de76a`). Its sibling is
`D06_COMPLETION_CLOSURE.md`; read that first for what G1–G5 established.

**No new migration.** `orders_app/0041` is untouched, the 30-minute rule is
unchanged, no historical data is repaired and no existing acceptance or closure
record is rewritten. One client-record field was added and its compatibility is
handled explicitly below.

---

## The matrix

| | what was wrong | what it is now | discriminating evidence |
|---|---|---|---|
| **C1** | `CheckoutCoordinator.classify()` was the D04 acceptance-only classifier: its `not-accepted` branch returned `draft` without inspecting `quote_closure`. A committed closure whose refusal was lost therefore read as an ordinary draft, `replayIssuedCommand` re-sent the acceptance the server had permanently refused, `resendIssuedCommand` filed every failure as `unknown`, and Retry returned to the same place. | ONE validated `closed` outcome, reached by every consumer — startup read, Retry read, submit refusal, **resend refusal**, initiate replay, retire enquiry, cached restoration. Acceptance is resolved FIRST, so only a definitive `not-accepted` may become `closed`. The closure is persisted and the review action attempts no acceptance. | `basket-body.closure-recovery.spec.ts` — **4 FAILED / 4 SUCCESS on `80de76a`**, through the real component, `ApiService`, `HttpClient` and `ErrorInterceptor`. 10/10 after. |
| **C2** | `readQuoteAnswer` accepted `quote_still_valid` with no correlation and `quote_closed` with `closure: null`; a mismatched reference, an unknown policy version and a missing moment all read as retired; it took no remembered capability. `applyQuoteRefusal` settled the command for a bare `quote_expired` even at remembered level 2. `renewAfterClosure` accepted no evidence at all. | `readClosureEvidence` → `closure` / `absent` / `malformed`, over a narrow reason vocabulary, a reference, a real moment and a supported version, refusing a closure that names another quote. `validateClosure` is shared with the STORED form. A TERMINAL reason with no readable closure is `unknown`. `renewAfterClosure` REQUIRES the evidence. The enquiry reads the MONOTONIC remembered level at answer time; a still-valid answer continues only a review that is still current. | `quote-transition.spec.ts` 22, `basket-body.quote-answer.spec.ts` 20 — including the compatibility control for a genuinely pre-level-2 server, so narrowing one cannot widen the other. |
| **C3** | Nothing produced a successor from a closure the client had merely *learned about*; the only path was the refusal it may have lost. | `noteClosure` writes the closure and settles the command in ONE verified write; `reviewUpdatedOrder` mints ONE successor under a new key linked by `replaces`, carrying the same purchase. Repeated taps and a second mount share it; a storage failure sends nothing and deletes no closure; a changed cart is preserved; a lost successor replays K2 with the original lines. | `basket-body.closure-successor.spec.ts` 13, plus browser **D06c** end to end. |
| **A1** | `_create_order` carried neither the diner capability nor the staff authority, so a revocation committing during the lock wait still wrote a draft and spent a daily ticket number. The replay branch disclosed an existing order and its closure without re-asking either. `quote_closure.close` claimed the lock half of its precondition was "asserted rather than assumed". | Both channels travel through `initiate_order` to `_create_order` and are re-asked by one shared `_authority_refusal` at every point that discloses or writes. A replay is authorized but stays exempt from every new-order rule. The overclaim is corrected and replaced by a source ratchet. | `tests_authority_during_lock_wait.py` +16 — **9 fail when the check is neutralised, 7 are the controls**. `tests_closure_preconditions.py` 12. |
| **V1** | Four oracles asserted superseded behaviour. | Corrected in place with their discriminating controls, never relaxed. | see below |

---

## The required consumer matrix

| consumer | parser / predicate | owner | durable transition | permitted UI action | driving test |
|---|---|---|---|---|---|
| startup GET | `classify` → `closedOr` → `readPublishedClosure` | `recoveryOwner()` | `noteClosure` | review updated order | `closure-recovery` "a reload that reads the published closure" |
| Retry GET | same | `ownerOf(record)` | `noteClosure` | review updated order | "and it does not re-send the acceptance…" |
| direct submit refusal | `applyQuoteRefusal` → `readQuoteRefusal` | the issuing attempt | `noteClosure` (terminal) / `settleRefusedCommand` (reprice) | re-price under a new key | `quote-renewal` "THE REGRESSION" + its no-closure control |
| **resend refusal** | `applyQuoteRefusal` | `recoveryOwner()` | `noteClosure` | review updated order | `closure-recovery` "a RESEND refused with the closure…" |
| initiated-order replay | `publishedClosure` (level-gated) | current record | renewal (once per episode) | review the successor's quote | `quote-renewal` "an initiate that hands back a retired quote" |
| retirement response | `readQuoteAnswer` (+ remembered level) | `CheckoutOwner` | renewal | re-price | `quote-answer` "a retired quote" |
| cached restoration | `record.closure` via `readStoredClosureEvidence` | n/a — no request | none (already durable) | review updated order | `closure-successor` "the closure is an ESTABLISHED fact" |
| explicit review | `closedQuote()` | current record | `renewAfterClosure` | initiate under K2 | `closure-successor` "THE SEQUENCE" |
| `placeOrder` guard | `closedQuote()` | current record | `renewAfterClosure` | initiate under K2 | `closure-recovery` "nothing is ever priced again under the key…" |

---

## Oracles corrected (V1)

Each was a real statement about the contract, and it is the STATEMENT that moved.

1. `basket-body.quote-renewal.spec.ts` — **"THE REGRESSION: re-prices under a NEW
   key"** and **"and the new attempt LINKS to the retired one"** passed a bare
   `quote_expired` with **no closure** and expected a new key. That asserted the
   superseded behaviour: the word alone settled the command and minted a
   replacement. Both now supply the closure `_TerminalQuoteOutcome` actually
   attaches, and a new control asserts the no-closure case renews nothing.
2. `basket-body.quote-answer.spec.ts` — **"COMPATIBILITY: an answer that names
   NOTHING is still honoured"** labelled an omitted correlation as older-server
   compatibility while ignoring that the fixture's own initiate declared
   `quote_protocol: 2`. It is now refused at demonstrated level 2, with a
   separate control proving the pre-level-2 tolerance survives.
3. `basket-body.quote-lifetime.spec.ts` — three cases asserted that a terminal
   reason with no readable closure still RE-PRICED. The notice was already read
   rather than inferred; C2 extends the same discipline to the action.
4. The browser harness's *"all initiation keys distinct"* shape could not be used
   for a lost successor, because a retry MUST repeat K2. D06c asserts
   `new Set(keys).size === 2` across the whole sequence instead.

Two fixtures were **completed** rather than worked around: the staff replay in
`tests_authority_during_lock_wait` created its original order as a DINER (D04's
intent binding includes provenance, so it was a `checkout_intent_mismatch` long
before authorization was reached), and D06c's prompt assertion matched **two**
mounts under Playwright strict mode — which is now asserted as the point rather
than swallowed.

---

## Commands and results

```
Backend   ./scripts/verify.sh                   4507 tests, all gates PASS
Frontend  npm run type-check / lint             clean
          npm run test:tenant-boundary          PASS
          npm run test:ci                       2450
          npm run build:prod                    PASS
Browser   e2e/checkout-journey/journey.mjs      42/42
          e2e/checkout-journey/recovery.mjs     68/68   (was 47/47)
          e2e/kitchen-board/kitchen.mjs         55/55   (fresh fixture)
```

All against a **disposable local PostgreSQL 16** created in this container
(`/var/lib/postgresql/d06`, `127.0.0.1`), a local Django on `test_settings`, and
a development `ng serve` on Node 24.15.0 with Chromium 141. No UAT or production
database, host or credential was reached.

---

## A genuine old/new example

**Before.** A diner reviews a UGX 35,011.30 quote, presses *Place order*, and
their connection drops as the server records that the dish has gone. The closure
is committed; the browser sees nothing. They reload. The page says *"We have not
been able to confirm your order. Tap retry to send the same order again."* They
tap Retry. The server refuses the acceptance it has already permanently refused.
The page says the same thing. There is no other button.

**After.** The same reload says *"Your order could not be placed at the price you
reviewed, and nothing has been sent to the kitchen"*, and offers **Review updated
order**. One tap mints a new attempt for the same basket, prices it, and shows
the new quote. Exactly one order reaches the kitchen, and the first order is
still an unaccepted draft with its closure intact.

---

## Release

**FRONTEND FIRST, as `BREAKING_CHANGES.md` §16 already requires for D06** — the
client changes here are all consumer-side and are inert against the deployed
backend, which already publishes everything they read. The backend change (§16b)
is additive and introduces one refusal on a request that was already acting on
revoked authority; a client already handles both of its non-disclosing 404s.

The existing hold/drain/update/verify/forward-fix plan is unchanged and is
**prepared, not executed**. Nothing here is merged or deployed.

## Remaining operational gates

* Provider revocation for the PSP credentials in the production backup `.env`
  (`REGULATORY_AUDIT.md` H2) — host-side, untouched by this work.
* `DELEGATED_QR_TRIAGE.md` — a delegated read-only view receives live table QR
  credentials. **Reported synthetic/local evidence, not a production incident**,
  and deliberately NOT bundled into this diff.
