# D06 — remaining consumer ownership, evidence and replay gates

The record for the **E1 / O1 / A1b / V1** pass that follows the C1/C2/C3 + A1
completion. Its siblings are `D06_COMPLETION_CLOSURE.md` (G1–G5) and
`D06_CLOSURE_RECOVERY_CLOSURE.md` (C1/C2/C3 + A1); read those first for what the
closure, the successor primitive and the creation-time authority already
establish.

Reviewed mains: backend `9241d8b` (#324), frontend `99a1a22` (#675).
Branches: `claude/d06-consumer-gates` in both repositories.

**NO MIGRATION.** `orders_app/0041` is untouched, the 30-minute rule is
unchanged, `QUOTE_POLICY_VERSION` stays 1, `quote_protocol` stays 2 and
`checkout_protocol` stays 3. No wire shape changed. No historical data is
repaired and no existing acceptance or closure row is rewritten.

**THE CLIENT RECORD VERSION DOES NOT MOVE.** The predecessor rides INSIDE the
existing `closure` key, for the reason `replaces` records: bumping would make
every record this build writes `unsupported` to the previous one, and an
`unsupported` record BLOCKS — a rollback mid-checkout would strand a diner with
an order in flight. An older build ignores the key and loses nothing it relied
on; the one-successor rule is enforced by comparing the CURRENT record, never by
reading a field an older build could not write.

---

## The four gates

| | what was wrong | what it is now |
|---|---|---|
| **E1** | `ClosureEvidence` had three answers and the consumers collapsed them. A closure this build may not act on, a genuine absence and an unreadable row all reached the same branches, and #675 had quietly widened two rules: every positive policy version was accepted for renewal (`policySupported` demoted to copy) and an ACCEPTED projection returned before a contradictory closure beside it was inspected | FOUR exhaustive answers — `closure` / `unsupported` / `absent` / `malformed` — read through exactly two predicates (`usableClosure`, `closureAsserted`). The agreed supported-policy rule is RESTORED (policy 1 only) and the inconsistent-result path is explicit again. A malformed, unsupported or wrong-reference closure is never permission to treat the quote as open, resend an acceptance, discard evidence or create another intent: the last usable attempt is KEPT and an actionable unresolved state is shown |
| **O1** | A closure was persisted with no record of the attempt it was written against, so `renewAfterClosure` acted on whatever record was current when a caller got round to asking. Three sites auto-renewed on a terminal refusal. The reviewed key was checked only when an expired local deadline happened to trigger the extra enquiry. Two required storage writes ignored their return | The closure carries a `ClosurePredecessor` (`key`, `orderId`, `scope`, `purchase`) in the SAME write. `renewAfterClosure()` takes no argument, reads the persisted evidence and makes a CONDITIONAL transition for THAT predecessor. All three auto-renew paths establish the closure and STOP; `reviewUpdatedOrder()` is the only renewal. The reviewed key is gated before EVERY command is persisted or sent. Both startup/Retry `noteClosure()` returns are checked |
| **A1b** | `test_a_table_taken_out_of_service_still_replays` asserted the OPPOSITE of the rule through a carried diner capability, and the two replay branches disclosed an existing order (and its closure) without asking whether the session that presented it still exists | The oracle is corrected in place, explicitly, with its controls preserved separately. Both replay disclosures ask `session_still_admissible`, and answer the capability channel's own opaque 404 |
| **V1** | The regressions did not drive the actual consumers, and the browser harness had no lost-INITIATION scenario | One new spec file driving the real component through the real coordinator and storage, eight source mutations each failing a named subset, and browser scenario **D06e** |

---

## E1 — the evidence contract, consumer by consumer

`ClosureEvidence` (`_shared/order/quote-transition.ts`):

```ts
| { kind: 'closure';     closure: QuoteClosure }   // usable: act on it
| { kind: 'unsupported'; closure: QuoteClosure }   // a real closure, unusable here
| { kind: 'absent' }                               // the server said nothing
| { kind: 'malformed';   defect: ClosureDefect }   // it said something unreadable
```

- **`unsupported` is a KIND, not a flag.** #675 accepted every positive policy
  version for renewal and kept `policySupported` for copy only — so a closure
  written under a policy this build has never seen drove a real state
  transition. It is a real closure (retirement is a fact the server recorded)
  and it is one this build may not ACT on, which is two different things and
  needs two different answers. A `boolean` beside a usable closure is exactly
  how the distinction gets dropped at the next consumer.
- **`usableClosure(e)` and `closureAsserted(e)` are the only readings.** The
  first answers *may I act on this*, the second *did the server say anything at
  all under `quote_closure`*. The accepted-order contradiction uses the second
  deliberately: a malformed row beside an acceptance is no more coherent than a
  valid one.
- **THE LEVEL IS EVALUATED AT ANSWER TIME AND THE IDENTITY IS NOT.**
  `readPublishedClosure(orderDetails, expected?, demonstrated = 0)` returns
  `malformed('level')` — never `absent` — when a server that has already
  demonstrated `REQUIRED_CLOSURE_PROTOCOL` for THIS attempt then says nothing.
  `demonstratedQuoteProtocol(pending)` takes the max of the frozen snapshot and
  the live record **only for the same key**; a record replaced by a different
  key describes a different operation. The identity half of the captured owner
  stays frozen, which is what stops a held answer being measured against
  whatever storage says now.
- **`quote_protocol` IS NOT `checkout_protocol` AND NEITHER IS INFERRED FROM
  `pricing_version`.** They are separate promises with separate constants
  (`REQUIRED_QUOTE_PROTOCOL` 1, `REQUIRED_CLOSURE_PROTOCOL` 2,
  `checkout_protocol` 3) and separate remembered levels on the record
  (`quoteProtocol`, `protocol`).
- **UNKNOWN IS NOT PERMANENT PARALYSIS.** Nothing is discarded on an unusable
  reading: the record, its key and its settled command all survive, so a later
  valid authorized read resolves it. What is withheld is the ACTION.
- **AN INCONSISTENT PROJECTION IS NOT HALVED.** `RecoveryOutcome.inconsistent`
  carries the order, the correlation and the evidence. The original attempt is
  preserved, no ordinary success is announced, no successor is minted and the
  inconsistency is not erased. No database repair is authorized or performed.

| consumer | parser | what an unusable reading does |
|---|---|---|
| startup GET | `classify` → `closedOr` → `readPublishedClosure` | no `closed`, no renewal, record kept |
| Retry GET | same | same, and no re-send |
| submit refusal | `applyQuoteRefusal` → `readQuoteRefusal` | `unknown`; command NOT settled, key kept |
| resend refusal | `applyQuoteRefusal` (owner-gated) | same |
| initiate replay | `publishedClosure` (level-gated) | nothing renews |
| retire enquiry | `readQuoteAnswer` | `unreadable` (`closure_unsupported_policy`); a Retry, never a submit |
| cached restoration | `closureOf` → `readRecordClosure` → shared `validateClosure` | CTA blocked, manual-recovery state shown |
| explicit review | `currentClosure()` + `renewAfterClosure()` | `unusable` — nothing minted |

---

## O1 — one closure, its actual predecessor, one deliberate successor

`noteClosure(closure)` persists, in ONE verified write:

```ts
stage: 'refused', command: null,
closure: { closedAt, reason, quoteRef, policyVersion,
           predecessor: { key, orderId, scope, purchase } }
```

The order id is read BEFORE `command` is cleared in the same write. Splitting
the two would leave a window where the command is settled and the reason is
gone — a record that looks like an ordinary reprice.

`renewAfterClosure(): RenewalResult` is **zero-argument** and reads that
evidence. Exactly three outcomes for a predecessor, and one of them is a
refusal:

| found | answer |
|---|---|
| predecessor is the current key | create K2 once (`renewed`) |
| current record IS the successor (`replaces === predecessor.key`) | `superseded` — observe it, treat as success |
| same purchase, neither predecessor nor successor | `conflict` — refuse |
| different scope or purchase | `conflict` — refuse |
| no closure / not usable / absent | `none` / `unusable` — mint nothing |
| an acceptance is outstanding | `outstanding` — mint nothing |
| the durable write cannot be verified | `storage-error` — send nothing |

**It can never treat a stale C1 plus a freshly loaded K2 record as permission to
replace K2**: the comparison is against the predecessor the closure names, not
against whatever `record()` returns. Stale UI is therefore not the only
protection, and two deliberately-new meals under different keys are never
globally de-duplicated — the conflict test is scope AND purchase AND key
lineage, not "some closure exists".

**THE REVIEWED KEY IS CHECKED BEFORE EVERY COMMAND.** `reviewedQuote.key`
records the attempt the sheet was priced under, and `reviewedAttemptIsCurrent()`
gates `submitOrder` — before `noteCommand` persists anything and before the
acceptance goes out — not only on the deadline-triggered enquiry path. A NULL
reviewed key is REFUSED, never trusted: a record that unexpectedly disappeared
is the case the gate exists for.

**THE THREE AUTO-RENEW PATHS ARE NOW DELIBERATE.** `handleSubmitFailure`, the
closed-initiation replay in `placeOrder` and the retired answer in `renewQuote`
each establish the closure (verified write) and STOP. The diner then taps
*Review updated order*, which is the only caller of `renewAfterClosure`.
`renewedThisEpisode` is gone — an episode counter was a weaker statement of what
the conditional transition now enforces exactly.

**THE OWNER RULE COVERS BOTH SUBMIT HANDLERS.** `applyQuoteRefusal(error,
issued?)` returns `null` for an answer that no longer settles the captured
operation, and the direct-submit SUCCESS and ERROR handlers both pass it —
including the `quote_ref_stale` reprice refusal, which carries no closure
reference and so has nothing else to stop it matching the current record by
accident.

---

## A1b — the replay disclosure asks whether the session still exists

Two branches disclose an existing order without running any eligibility rule:
`_create_order`'s idempotent replay and `_submit_order`'s accepted-submission
replay. Both now ask `diner_capability.session_still_admissible(capability,
table_row)` and answer the channel's own **opaque 404** on a refusal.

- **THE ORACLE THAT ASSERTED THE OPPOSITE IS CORRECTED, EXPLICITLY.**
  `test_a_table_taken_out_of_service_still_replays` claimed a carried diner
  capability still replays at an unscannable table. It is replaced by
  `test_THE_REGRESSION_an_unscannable_table_does_not_replay_to_a_diner`, with a
  recorded note that the expectation moved. **An internal call with NO
  capability is not an oracle for a public request carrying a revoked one**, and
  that control is kept separately alongside the paused-restaurant, menu-only,
  stock-change and valid-rescan controls.
- **THE ACYCLIC ORDERING IS PRESERVED.** The create-path replay returns BEFORE
  the table lock, so nothing acquires `Table` or the admission advisory lock
  after an `Order` is already held. The row is re-read lock-free and memoised
  (`_once`), so one snapshot answers both the authority question and the session
  question and the pinned replay query budget moves by nothing.
- **THE DECISION POINT IS STATED HONESTLY.** A revocation committed before the
  re-read is respected; one committing after it can still overlap. Under READ
  COMMITTED a plain statement takes its own snapshot, which is the whole
  mechanism — there is no claim to serialize arbitrary revocation, no trusted
  caller flag, no dynamic lock-catalogue probe, no catch-all suppression and no
  live repair.

---

## V1 — the regressions, and what each mutation breaks

`basket-body.evidence-gates.spec.ts` (18) drives the REAL component through the
real `CheckoutCoordinatorService`, the real storage and the real
`quote-transition` boundary. The existing suites were extended where they
already owned the behaviour rather than duplicated.

Eight source mutations, applied one at a time to PRODUCTION modules, each
reverted before the next and the tree verified byte-identical afterwards:

All NINE measured against the SAME final suite (2503), one at a time, each
reverted before the next and the tree verified byte-identical afterwards:

```
M1  unsupported policy accepted as usable      7 FAILED / 2496 SUCCESS
M2  demonstrated level ignored (read `absent`) 2 FAILED / 2501
M3  accepted returned before closure probed    1 FAILED / 2502
M4  closure written without its predecessor    4 FAILED / 2499
M5  reviewed-key gate removed from submit      2 FAILED / 2501
M6  refusal owner ignored                      2 FAILED / 2501
M7  terminal refusal auto-renews again         6 FAILED / 2497
M8  noteClosure() return ignored (both sites)  2 FAILED / 2501
M9  terminal branch holds the app-wide flight  1 FAILED / 2502
```

**M8 FOUND A GAP THE READING DID NOT.** On the first pass it failed **nothing**
in 2497 specs: both recovery consumers already checked the return and a comment
explained why, and the rule was pinned by neither. Three specs were added to
`basket-body.closure-recovery.spec.ts` — the Retry site, the STARTUP site (driven
through a second component over the same persisted record, since the fixture
suppresses its own resume) and the control that must keep offering the review
when the write DOES land. Removing both checks now fails exactly the two
regressions. A rule stated in code and in prose and asserted nowhere is the class
of defect this whole programme is about, so it is recorded rather than quietly
fixed.

**TWO MUTATION FORMS WERE REJECTED AS UNSOUND BEFORE ANY OF THIS WAS MEASURED.**
Neutralising a guard as `if (false && …)` makes its block provably unreachable,
which changes TypeScript's narrowing and produces an unrelated compile error
instead of a test failure — a mutation that "fails the build" proves nothing
about the tests. Every guard is neutralised with an always-false expression the
compiler cannot fold (`Number('x') > 0`), so the block stays reachable and the
only thing that changes is the runtime answer. A mutation whose revert is not a
byte-for-byte inverse was likewise rejected: the first `M4` deleted a block and
reverted by matching the empty string, which silently left the source mutated
into the next run.

Each failing set is the gate's own regressions plus the sibling that shares its
rule; every CONTROL held on every mutation — in particular the supported-policy
control, the genuinely pre-level-2 tolerance and the no-closure `quote_ref_stale`
reprice, which must keep working and do.

**THE BROWSER SCENARIO THE OTHERS DO NOT REACH — D06e.** `recovery.mjs` gains
the lost *initiation* of a successor: price O1 under K1, make the purchase
unacceptable so the server writes a REAL closure, submit and SEE the refusal,
tap *Review updated order* — and destroy the browser's view of the INITIATE
reply after the server has already created O2 (`route.fetch()` then
`route.abort()`). The record then holds K2 with no command and no order, which
is the state both readings get wrong: "nothing happened" mints a third key and a
third draft, "an acceptance may be outstanding" offers a Retry for a command
never issued. Seven steps, asserted: exactly TWO keys across the whole sequence,
exactly ONE order in the kitchen and it is not O1, the successor record survives
the reload, the notice claims no acceptance, and O1 is still a retired
unaccepted draft whose `quote_total` is byte-identical to what it was before.

**THE EXISTING FINAL O1 ASSERTION WAS TIGHTENED RATHER THAN REPLACED.** D06c's
closing check accepted `original.status !== 200` as success and asserted no
saved monetary invariant — so a 404 on the retired order passed. It now
positively requires `status === 200`, `order_status === 'initiated'`,
`accepted !== true`, the closure intact, and `quote_total` / `actual_cost` /
`quote_ref` byte-identical to what was captured before the retirement.

---

## Changed expectations — called out, not silently rewritten

| oracle | asserted | now |
|---|---|---|
| `tests_authority_during_lock_wait.test_a_table_taken_out_of_service_still_replays` | a carried capability still replays at an unscannable table | the replay is refused with the channel's 404; the keyless/staff controls are kept separately |
| `checkout-coordinator.renewal` — unsupported-policy renewal | a renewal is minted under any positive policy version | refused (`unusable`); a SUPPORTED-version control asserts the unchanged path |
| `quote-transition` — `policySupported` on a retired answer | the answer is retired and merely flagged | the answer is `unreadable` (`closure_unsupported_policy`) and renews nothing |
| `closure-recovery` — accepted beside a closure | accepted wins and the closure is ignored | `inconsistent`; the attempt is preserved and nothing is announced |
| `quote-renewal` / `quote-lifetime` — terminal refusal | the client re-prices itself | it establishes the closure and offers the review |
| `renewAfterClosure(closure, replaced?)` call sites | the caller supplies the closure and the record | zero-argument; the persisted evidence decides |

Each carries a comment at the assertion recording what moved and why.

---

## AND THE BROWSER RUN FOUND ONE MORE — the defect this pass introduced

**D06e timed out waiting for *Review updated order* to become enabled.** The O1
change replaced the terminal branch's fall-through to `placeOrder()` with a
`return` — and `placeOrder()` is what OWNED the app-wide checkout flight and
released it on every outcome. Nothing released it on the new path, so
`placingOrder` stayed true, the button that branch renders came up `disabled` /
`aria-busy`, and it stayed that way until the page was reloaded: a dead end
produced by the change that exists to remove one.

**NO UNIT SPEC COULD SEE IT**, and the reason is worth keeping: every spec
asserted on the STATE behind the button (`quoteRetired`, `updatedReviewPrompt`,
the record) rather than on whether the button could be pressed. `releaseCheckout()`
now runs at the top of that branch — the acceptance has RESOLVED, so there is
nothing left to protect, and `releaseFlight` ignores a token that is no longer
current, so a late release from a superseded attempt cannot free a live one. Pinned
by three specs in `basket-body.evidence-gates.spec.ts` (the regression plus a
terminal-reason-without-closure and a transient control, both of which passed
before and after — they are controls, not second regressions), and by **M9**.

## Oracles corrected in the browser harness (V1)

| oracle | asserted | now |
|---|---|---|
| `recovery.mjs` D06b "the client re-priced rather than offering a dead Retry" | `keys.length >= 2` — i.e. an AUTOMATIC renewal inside the failure handler | the refusal alone mints NO key; the review is offered; the diner's deliberate tap mints exactly one successor under a NEW key. **Its SECOND move** — G3b had already inverted it from "every key the same" — and both are recorded at the assertion |
| `recovery.mjs` D06c final money/closure assertions | read `data.order_details.quote_total` / `.quote_closure` on the `?order=` diner read | that read publishes them at the TOP of `data` (D04/U1 + G3a); only the INITIATE response nests them. The nested path inspected `undefined` and compared two absences as equal — an assertion about money that could never fail. `journey.mjs` records the same distinction at its own read |

## Commands and results

```
Backend   ./scripts/verify.sh                    4529 tests, all gates PASS
Frontend  npm run type-check                     clean
          npm run lint                           0 errors, 9 warnings (10 on main;
                                                 one fewer, none new)
          npm run test:tenant-boundary           306 PASS
          npm run test:ci                        2503 SUCCESS
          npm run build:prod                     PASS (pre-existing bundle-budget
                                                 warning only)
Browser   e2e/checkout-journey/journey.mjs       42/42
          e2e/checkout-journey/recovery.mjs      88/88   (was 68; +D06e and the
                                                 corrected D06b/D06c oracles)
          e2e/kitchen-board/kitchen.mjs          55/55
```

All three browser runs are on the **FINAL delivered pair**, re-run after the
component change this pass produced, against a **disposable local PostgreSQL 16**
created in this container (`/var/lib/postgresql/d06`, `127.0.0.1`), a local Django
on `test_settings` at `ENV=dev`, and a **DEVELOPMENT** `ng serve`
(`--configuration development` — NOT optimized assets) on Node 24.15.0 with
Chromium 141. No UAT or production database, host or credential was reached, and
`environment.ts` was restored before commit.

**ONE OPERATIONAL NOTE FOR THE NEXT RUN**, recorded in the harness README:
`recovery.mjs` clears the kitchen board itself and `journey.mjs` does not, so
running the pair in that order leaves an ACCEPTED order holding the table and
`journey.mjs` fails at its first Checkout click on a correctly-disabled button.
Re-seeding is not enough — the seed does not remove orders.

---

## Remaining operational gates — unchanged by this work

* `DELEGATED_QR_TRIAGE.md` — a delegated read-only view receives live table QR
  credentials. **Deliberately NOT bundled here**; the intended separate change
  withholds credential material from delegated readers in both the flat and
  grouped table builders while preserving legitimate owner printing and ordinary
  support table information. No delegation change and no physical QR rotation is
  part of this diff.
* `REGULATORY_AUDIT.md` H2 (PSP credential revocation), H4 (MongoDB Atlas access
  list) and H5 (transactional email provider) are host-side and untouched.
