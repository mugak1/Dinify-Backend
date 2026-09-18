# D06 completion — the G1–G5 closure record

The record for the five completion gates raised against the merged D06 pair
(backend PR #322, frontend PR #673). It states, per gate, what was required, what
was actually found, what was changed, how the change was PROVED discriminating,
and what was deliberately left alone.

Read it beside `BREAKING_CHANGES.md` §16/§16a and the D06 sections of both
repositories' `CLAUDE.md`. Where this record and a PR description differ, the
approval and this record govern: **the merged PR descriptions do not supersede
the Stage B decisions.**

**NOTHING HERE WAS MERGED, DEPLOYED OR RUN AGAINST LIVE OR UAT DATA.** Every
measurement below comes from a disposable local PostgreSQL 16.13 and a local
Django on `test_settings`. No migration was added by any of the five gates; no
historical data was repaired; no delegation scope was changed.

---

## The shape of the whole thing, in one example

A synthetic walk-through of one draft, before and after. `K1`/`K2` are
idempotency keys (`client_order_id`), `O1`/`O2` orders, `C1` a closure row.

| step | BEFORE (merged D06) | AFTER (this work) |
|---|---|---|
| diner prices a basket | `initiate(K1)` → draft `O1`, quote `Q1` | same |
| the dish sells out while they read | nothing re-read at acceptance for the staff path; catalogue writers unsynchronised (G1a/G2) | the 86 write takes the admission barrier; acceptance re-reads the catalogue under one `now` in the restaurant's own zone and compares selected MEANING |
| diner confirms | `submit(O1, Q1)` → refused `purchase_needs_review`, closure `C1` written | same — and the answer is CORRELATED (`order`, `quote_ref`, `quote_protocol`) |
| **the refusal response is LOST** | `C1` exists and is announced NOWHERE else | `order_details.quote_closure` publishes `C1` on the diner's own read, under BOTH selectors, and on an `initiate` REPLAY |
| client re-prices | `initiate(K1)` again → D04 REPLAYS `O1`, whose quote is dead. Review sheet renders a quote that can never be paid → refused again → **loop** | the client RENEWS: one new persisted attempt, **new key `K2`**, `replaces: K1` → `initiate(K2)` → **new draft `O2`** |
| diner confirms again | — | `submit(O2, Q2)` accepted |
| end state | `O1` closed by `C1`; `K1` still the client's key; no way out in-app | `O1` closed by `C1`, **untouched and never repriced**; `C1` **never deleted**; `K1 → O1` and `K2 → O2`, **one key, one order, always** |

Three invariants the example exists to make checkable:

* **the old Order is never repriced** — a closure is terminal, and nothing in
  this work writes to a closed draft;
* **the closure is never deleted** — re-closing returns the ORIGINAL row, a read
  never mutates it, and a renewal cannot touch it;
* **one key never names two orders** — a renewal mints a NEW key before it
  initiates, and the durable write is verified before the key is used.

---

## G1a — every eligibility-changing catalogue writer shares the barrier

**Required.** Not only the stock route: every writer of a fact an acceptance
re-reads must synchronise, beginning BEFORE the relevant row/relation locks, with
a deterministic per-tenant strategy for multi-tenant batch writers and a full
updated lock-order map.

**Found.** The merged work enlisted the pause writer (`restaurants` PUT) and left
the catalogue writers out. A menu item could be unpublished, re-priced,
re-configured, soft-deleted or renamed, and a tag renamed or deleted, while an
acceptance was mid-flight — the exact window `catalogue_snapshot` folds its two
statements to close, reopened one layer up.

**Changed.** `restaurants_app/controllers/catalogue_admission.py`
(`lock_catalogue_for_write`) — takes the EXCLUSIVE admission lock per restaurant,
**sorted by id** so a multi-tenant write cannot deadlock against another taking
the same set in a different order, and always as the first statement of the
caller's transaction. Enlisted: the `restaurant-setup` PUT and DELETE branches for
`restaurants` / `menusections` / `sectiongroups` / `menuitems`, the kitchen 86
stock write, and the restaurant-tag rename and delete. The DELETE path now decides
its blocker and performs the soft-delete under one transaction and one barrier.

**Exemptions are enforced by a test of their stated reason**, not recorded in a
comment: a surface is exempt only because nothing an acceptance reads can change
through it, and `restaurants_app/tests_catalogue_admission.py`
(CATALOGUE-ADMISSION-00) asserts that claim per exemption, asserts the enlisted
set, and AST-scans so only named modules may reference the barrier.

**Proved.** `orders_app/tests_catalogue_admission_concurrency.py` — two real
PostgreSQL connections, blocking proved POSITIVELY by `SET lock_timeout` so the
server raises by name, never by watching a thread fail to finish.
`BarrierRemovedTests` inverts the two barrier assertions and reproduces the
interleaving.

**Lock order, updated and complete:**

```
advisory(restaurant) SHARED    -> Table -> Counter -> Order -> OrderItem   order create
advisory(restaurant) SHARED    -> Table -> Order                            order submit
advisory(restaurant) SHARED    -> Table -> Order                            retire-for-review  (G1a)
advisory(restaurant) EXCLUSIVE -> Restaurant -> AdminAuditLog               lifecycle transition
advisory(restaurant) EXCLUSIVE -> Restaurant                                restaurants PUT
advisory(restaurant) EXCLUSIVE -> MenuSection|SectionGroup|MenuItem         menu PUT / DELETE  (G1a)
advisory(restaurant) EXCLUSIVE -> MenuItem                                  kitchen 86 write   (G1a)
advisory(restaurant) EXCLUSIVE -> RestaurantTag                             tag rename/delete  (G1a)
Restaurant -> INSERT Table                                                  table allocation
Restaurant -> RestaurantEmployee                                            membership mutation
```

**Retire-for-review shares the synchronisation without applying new-order
admission policy** — it takes the SHARED lock and never asks `admit`, because
establishing that a held quote is dead must stay possible at a paused,
suspended, offboarded or soft-deleted restaurant. The superseded claim in
`retire_quote_for_review`'s own docstring ("takes NO admission advisory lock")
was corrected in place with the old wording quoted.

---

## G1b — the authority that decides is the one that holds now

**Required.** Preserve CURRENT authority across lock waits: scan-availability at
the protected decision without weakening the public resolver or adding a
retirement-only exception, and carry minimal verified actor authority so
membership and delegation revocation are respected.

**Found.** Scan-availability was ALREADY re-read at the create and submit
decisions (`order_eligibility.facts_from_verdict` reads
`table.is_available_for_scan()` off the LOCKED row). Two real gaps remained:
`retire_quote_for_review` ran no eligibility rule at all, so nothing re-asked
whether the diner's session still existed — and it writes an irreversible
closure; and the staff channel was never re-asked, the endpoint saying so in
terms ("nothing to re-verify... not revoked by a QR regeneration" — true, and
beside the point: it IS revoked by a deactivated membership, a removed role, or
the restaurant leaving `portal_access_states()`).

**Changed.** `orders_app/controllers/services/order_authority.py` —
`StaffAuthority` carries THREE FACTS AND NO CREDENTIAL (the principal, the
SERVER-RESOLVED restaurant read off the order, the module the endpoint gated on)
and re-runs the SAME `can_user_access_module` call, so it can only REFUSE. The
principal travels as the OBJECT because a delegation is an in-memory attribute
the middleware sets on it; re-fetching the row would silently ask a different
question. `diner_capability.session_still_admissible` is the retirement half,
kept a SEPARATE named predicate from `assert_capability_current` so acceptance
keeps answering table liveness with the sentence a diner can read while
retirement keeps its own established 404 — **no retirement-only exception, and
the public resolver is untouched.**

**Proved.** `orders_app/tests_authority_during_lock_wait.py`, 20 tests.
Neutralising the staff re-check fails exactly the 4 staff cases; neutralising the
retirement guard fails exactly the 2 retirement cases while acceptance still
refuses from `order_eligibility`; reverting ONLY the endpoint's wiring fails
exactly the 3 HTTP cases with every control passing.

**The test seam is the part most worth knowing.** The first cut fired at
`assert_capability_current`, which runs once both rows are HELD — and a competing
writer cannot commit against a row this transaction holds. Three tests failed
against correct code on an artefact. The seam fires at the admission advisory
lock now, before either row lock, which is the window a real revocation lands in.

**Stated, not claimed away:** the decision LINEARIZES at the re-check, not at
commit. A revocation committed before that point is respected; one committing
after it can still overlap.

---

## G2 — the protected decision reads the right facts

**(A) The schedule zone.** `schedule_utils` compared a section's opening window
against an instant in whatever zone it arrived in. `timezone.now()` is
UTC-expressed and `settings.TIME_ZONE` is `Africa/Nairobi` (UTC+3), so a window
of 08:00–22:00 was evaluated three hours early — an order accepted before opening
and refused in the last three hours of service. The instant is converted to the
configured zone before comparison.

**The oracle was wrong first, which is why the mutation matters.** The fixtures
pinned LOCAL-expressed instants, so they read the right digits and passed against
the unfixed code: only 2 of 12 failed under mutation. `_as_production_clock`
expresses each fixture instant the way `timezone.now()` expresses it, and the
mutation then produced 14 failures.

**(B) Selected preparation MEANING.** The purchase-integrity check compared
stable IDs, so a group or choice RENAMED under the same id passed — the kitchen
then prepared from a definition nobody agreed to, with the ticket's own
`modifiers_snapshot` contradicting it. `modifier_definition.selection_meaning` is
the one reading, shared by `option_breakdown` and the integrity check, and the
comparison is against the saved `modifiers_snapshot`. It reads NO money: D02
owns pricing and this is a statement about preparation.

**(B) took two corrections in review, both Codex findings on #323 and both
valid.**

*The comparison is a MULTISET, not a sequence* (P1). `OrderItem.selected_
modifiers` is a `JSONField`, which is **jsonb** on PostgreSQL, and jsonb does
not preserve an object's key order — it stores keys sorted by length then
bytewise, measured on the cluster rather than assumed. `selection_meaning`
visits groups in the order the SELECTION names them, so a line saved in
menu-definition order was re-derived in jsonb's order, while `modifiers_
snapshot` is a jsonb ARRAY whose order IS preserved. For a line naming two or
more groups the two agreed only by luck, and a disagreement classifies an
UNCHANGED purchase `purchase_needs_review` — a TERMINAL reason, so the quote is
permanently CLOSED. **Neither iteration order is the fix**: visiting the
DEFINITION's groups instead would break the decision table's "a reorder ->
accept". `meaning_matches` compares as multisets, so a jsonb reorder and a
catalogue reorder are both accepted while a relabel still changes an element.
Every existing test missed it because they compare the two producers IN MEMORY
from one Python dict, or select a single group.

*A label of any type is TEXT, not a crash* (P2). `options` is unvalidated and
`inspect_modifier_definition` never reads a name, so a label set to `null`, a
number or a mapping reached `', '.join(...)` and raised `TypeError` — a 500
where this module's whole contract is a controlled refusal. It stringifies
rather than invalidating: a label is not structural, and failing the line closed
would make a catalogue with a numeric label unsellable rather than oddly
labelled. Both producers call the one `choices_display`, so the fix closes the
checkout path as well as acceptance — the payoff of their being shared rather
than parallel. An ABSENT name still renders `''`, which an explicit null must
not be folded into.

Pinned by nine tests in `tests_quote_preparation_meaning.py`. Under mutation the
order fix fails exactly 1 of 27 (the regression, every control holding) and the
label fix fails 5 of 6, the sixth being the absent-name control that must not
change.

**(C) The staff exemption and non-monetary eligibility.** `item_orderable`
conflated publication with priceability, so the staff parent-publication
exemption could not be expressed without also exempting the price.
`item_published_now` is the non-monetary half and `item_orderable` remains
`item_published_now and item_priceable`; `section_live`/`group_live` name
liveness apart from publication. The exemption is preserved for staff and the
saved price is honoured — what is refused is preparing food from a changed
definition.

**Proved.** `tests_quote_schedule_zone.py` (12), `tests_quote_preparation_meaning.py`
(18), `tests_quote_publication_gates.py` (26), each mutation-tested.

---

## G3a — a retired quote is discoverable, not only announced

**Required.** A bounded, correlated closure/policy projection on authorized read
surfaces.

**Found.** The closure was published on ONE response — the refusal that created
it, which is the single thing a client can lose. The diner's own order read
published NEITHER the level, the deadline NOR the closure: all three had been
added to the INITIATE response only. A quote retired for `purchase_needs_review`
inside its window was invisible on every surface a recovering client could reach,
and its only remaining move was to attempt an acceptance — precisely what
`retire-quote` exists to avoid, since when the quote IS good that attempt
succeeds, claims a table and sends food to a kitchen in order to ask a question.

**Changed.** `GET orders/journey/order-details/` carries `quote_protocol`,
`quote_policy` and `quote_closure` on BOTH selectors, through the SAME constant
and the SAME projections the initiate response uses. `quote_closure` joins the
initiate response too, because a D04 replay returns an order created earlier
whose quote may have been retired since. `QUOTE_PROTOCOL` is **2** — a NEW LEVEL,
never a new meaning for 1 — and the raise was CHECKED against the deployed client
rather than assumed: it gates with `level < REQUIRED_QUOTE_PROTOCOL` (1), so 2
passes and it keeps consulting the deadline unchanged.

**The deadline and the closure are INDEPENDENT FACTS and are labelled apart.** A
quote closed because the purchase changed is finished while its deadline has not
passed, so `quote_policy.status` legitimately reads `live` beside a closure. The
CLOSURE decides acceptability.

**It costs no query, and that is a CORRECTNESS rule before a cost one.** Under
READ COMMITTED each statement takes its own snapshot, so reading the order in one
and the closure in another publishes a correlated answer describing a moment that
never existed. The diner read folds the relation; the initiate path's re-read
replaced a plain `refresh_from_db`.

**Two test oracles were wrong, and both are recorded.** "A closed order costs the
same read as an open one" is BLIND to the fold — an unjoined lookup fires either
way and finds nothing for the open order, so both move together; it passed
against the unfolded code. And a test asserting `quote_ref_stale` for a
retirement naming a foreign reference was asserting against the DOCUMENTED
design: both boundaries deliberately read the committed closure before asking
which quote the caller means.

**Proved.** `orders_app/tests_quote_closure_recovery.py`, 33 tests. 14 of the
first 19 failed on the pre-change tree.

---

## G3b — a closed quote produces ONE new attempt, with a NEW key

**Required.** Recoverable, producing one explicit NEW persisted attempt with a
NEW key; never reprice the old Order, never delete the closure, never map one key
to multiple orders; atomically reserved and verified before initiation, with
replace-in-progress linkage.

**Found.** The shared transition settles the command and KEEPS the key for BOTH
terminal and reprice refusals. Right for `quote_ref_stale`; fatal for a closure,
because the key is bound to the order the closure was written against — so the
re-price replays a retired draft and the diner loops with no in-app escape.

**Changed.** `CheckoutCoordinatorService.renewAfterClosure()` — ONE new persisted
attempt, SAME purchase and scope, NEW key, `replaces` linkage, write verified
before the key is returned. **Exactly one successor**: a caller names the record
it decided about, and a call naming one that is no longer current answers
`superseded`, which the component treats as success. **Refused while an
acceptance is outstanding** — a renewal abandons the key that is the only way to
resolve an unsettled command. Wired at THREE sites: the terminal refusal, an
`initiate` that hands back a retired quote (once per episode), and (via G4) the
retire enquiry's own `quote_closed`.

**The record version deliberately does not move for `replaces`.** Bumping would
make every record this build writes `unsupported` to the previous one, and an
`unsupported` record BLOCKS — a rollback mid-checkout would strand a diner with
an order in flight, to protect a field that carries no guarantee.

**Proved.** `checkout-coordinator.renewal.spec.ts` (11) and
`basket-body.quote-renewal.spec.ts` (13); each wiring site fails exactly 2 when
reverted; ungating the closure reader fails the level control. And in a real
browser: `recovery.mjs`'s sold-out scenario now requires a NEW key across the
re-price and passes.

---

## G4 — one validated, correlated result contract across every consumer

**Required.** One shared contract across direct submit, resend, startup/Retry GET,
cached state, retire request/result, explicit renewal and both mounts; remember
demonstrated D06 support; freeze identity at issuance; bind every callback to its
original operation.

**Found.** D04 had established all of this for ACCEPTANCE answers. The D06
enquiry had joined none of it — and `quote_still_valid` is the answer that leads
to SUBMITTING an order. It read `response.outcome` and acted.

**Changed, in both repositories.**

* **Backend.** The retire answers named NOTHING, so there was nothing to
  correlate against. Every answer stating an `outcome` or a `reason` now carries
  `order`, the caller's `quote_ref` and `quote_protocol`. **The opaque 404 is
  never stamped**, by a STRUCTURAL rule rather than a status list: a body stating
  no outcome and no reason has said nothing about a quote. Pinned here and,
  independently, by the G1b suite.
* **Frontend.** `readQuoteAnswer` is the one reading, and the correlation rule is
  CONTRADICTS, NOT CONFIRMS — an answer naming a different order or reference is
  refused, one naming NEITHER is honoured, because that is an older server
  answering the request it was sent. Identity is frozen at issuance through the
  SAME `CheckoutOwner` every acceptance consumer uses (the old guard was
  `attemptSeq`, a process-local counter that survives nothing), the destroyed
  flag is honoured, a record lost mid-flight is its own explicit answer rather
  than a hang, the retired answer RENEWS, and `CheckoutRecord.quoteProtocol`
  remembers the demonstrated D06 level monotonically — noted by BOTH initiate
  consumers and by the enquiry.

**Proved.** `basket-body.quote-answer.spec.ts` (13) plus 8 backend tests; each of
the four halves fails exactly 2 when reverted, and the backend correlation fails
5 when dropped.

**One pre-existing fixture was COMPLETED rather than the rule relaxed**: three
`quote-lifetime` cases opened the review sheet with no reserved record, a state
production cannot reach.

---

## G5 — verification and evidence

| | |
|---|---|
| backend `./scripts/verify.sh` | **All checks passed** — django check, makemigrations check, money-field guard, ambient-authority gate, tenant-relation ratchet, tenant-isolation gate (398), full suite **4459 OK** |
| frontend `./scripts/verify.sh` | **All checks passed** — type-check, lint (0 errors), tenant-boundary gate (**306**), full suite (**2405**), `build:prod` |
| `e2e/checkout-journey/journey.mjs` | **42/42** |
| `e2e/checkout-journey/recovery.mjs` | **47/47** |
| `e2e/kitchen-board/kitchen.mjs` | **55/55** |
| independent PostgreSQL connections | G1a concurrency suite and the pre-existing admission suites, blocking proved positively by `lock_timeout` |
| query cost | acceptance **14**, replay **7** — pinned and UNCHANGED by all five gates. The create path is unchanged. G3a and G4 add no statement |
| migrations | **none added by G1–G5** |

**Tests asserting a superseded contract were CHANGED EXPLICITLY, never deleted:**
two `quote-lifetime` fixtures (completed with a reservation), and one
`recovery.mjs` assertion inverted from "every key is the same" to "the successor
carries a new key", with the old expectation recorded beside it.

**The browser harnesses mutate their fixture and must be re-seeded between runs.**
The first attempt of this run failed two of its own checks on leftover state — the
dish already at the reprice target and a `pending` order holding the table. A
fixture artefact, recorded in the harness README so the next reader does not
mistake it for a defect.

---

## What this work deliberately did NOT do

* **No merge, no deploy, no live or UAT access, no venue action, no historical
  data repair, no repository-setting change, no workflow dispatch, no payment or
  provider access.** Every run was against disposable local infrastructure.
* **No delegation-scope change.** The `qr_credential` delegated-disclosure item is
  tracked SEPARATELY in `DELEGATED_QR_TRIAGE.md`, measured by
  `platform_admin_app/tests_delegated_qr_disclosure.py`, and is not part of this
  diff. That file is a CHARACTERIZATION of today's behaviour — it asserts the
  exposure rather than closing it, and changes nothing.
* **No migration**, so nothing here changes the rollback posture recorded for
  `orders_app/0041`.
* **The kitchen-draft producer of D04's `evidence_unavailable` is still open** —
  adding an `initiated` guard changes what the kitchen may do to an order, which
  is a D05 transition decision with its own blast radius.
* **`check_go_live_readiness` still fails closed** with `readiness_not_configured`;
  nothing here makes a restaurant ready.
* The two races recorded under "Admin Restaurant Creation" (non-atomic
  `User.email` uniqueness, cross-owner same-name+location duplication) are
  untouched and still open.
