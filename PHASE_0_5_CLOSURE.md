# Dinify Backend — Phase 0.5 Pre-Launch Remediation: Program Closure Record

> **✅ STATUS — PROGRAM CLOSED.** This is the closing record of the Phase 0.5
> pre-launch remediation ladder: four sequential PRs (PR-A → PR-D, #260/#261/#263/
> #264), all merged to `main`. Every defect the ladder targeted is closed and
> test-enforced; everything it deliberately did NOT close is recorded below with
> the reason, so a later reader does not mistake a decision for an oversight. This
> is a historical record: the claims in it are valid at the SHAs cited beside them,
> not necessarily at any later HEAD.

> **Provenance.** The ladder was driven by an external pre-Phase-1 review of the
> admin control plane and the restaurant lifecycle. Each PR was recon-gated — a
> read-only Stage 1 that reported findings before any code was written, then a
> Stage 2 implementation scoped to those findings. Nothing here was discovered by
> this record; it consolidates what the four PRs established. SHAs, PR numbers and
> migration paths are drawn from the git record.

## Context — why this program existed

Four defect classes stood between the backend and a Phase 1 that could safely
onboard restaurants:

1. **A second identity vocabulary.** `User.roles` carried platform authority
   alongside `account_type`: the string `dinify_admin` granted cross-tenant reach
   on the CUSTOMER plane — a write bypass in `check_permission`, an "unrestricted"
   `None` sentinel on both id resolvers, a `can_manage_restaurant` short-circuit.
   Ambient authority is the failure mode a tenant boundary cannot survive.
2. **Two independent failure paths chained through one key.** The admin second
   factor read a bare `code` and tried TOTP first, falling through to recovery.
   But `totp.verify` decrypts the stored secret BEFORE it can reject a wrong code,
   and `crypto` fails closed — so a lost `ADMIN_SECRET_ENCRYPTION_KEY` raised out
   of the TOTP attempt and the recovery branch was never reached. Recovery codes
   existed precisely for the case in which they could not run.
3. **A verification path that was not atomic.** The second factor was consumed
   OUTSIDE the session transaction and the audit written AFTER it committed. A
   recovery code could be burned with no session produced; a live `AdminSession`
   could exist with no success audit row; two concurrent requests could both mint;
   lockout increments could be lost. Separately, the delegated-write audit was
   written by middleware after the view had committed — the one place the
   no-audit-no-action contract did not hold, and its own docstring said so.
4. **A launch boundary that did not exist.** `CAPABILITY_MATRIX` gave `onboarding`
   and `live` byte-identical rows in all six cells, so a restaurant could trade
   for real before it was live. The circularity is the interesting part: the
   matrix justified widening `onboarding` on the grounds that the go-live
   readiness checklist would gate the launch, while `check_go_live_readiness()`
   was a stub that returned ready unconditionally and never read its argument. A
   safety gate that always said yes was load-bearing for a permission that should
   not have been granted.

### Classification key
- **CLOSED** — the defect is removed and an invariant below is enforced by a
  named test running in CI.
- **FAILED CLOSED** — a capability is deliberately REFUSED rather than
  approximated, until Phase 1 builds the thing that would make it safe. A refusal
  is the correct behaviour, not a bug.
- **ACCEPTED GAP** — something is knowingly missing. Recorded with the phase that
  owns it and why nothing is stranded meanwhile.
- **DEFERRED WITH SEAM** — a named single-call function with a real call site
  exists; Phase 1 replaces its BODY, never the seam or the call site.
- **KEPT** — looks dead, wrong or redundant, and is deliberate. Recorded so a
  future sweep does not "fix" it.

### Method
Each PR ran read-only reconnaissance first and reported its findings before
implementing; several Stage-2 designs changed because Stage 1 contradicted the
prompt's assumption. `./scripts/verify.sh` was run against real PostgreSQL for
every PR. Every regression test claimed as proof of a fix was first proved to
FAIL on `main` — by stashing the production files and re-running — so no test in
the table below passes for a reason other than the fix it names.

## What this record claims — and does not

At the four merge commits below, the four defect classes above are closed, and
every invariant in the table has a proving test that runs in CI.

This record does **not** claim that the admin control plane is fully hardened,
that `AdminAuditLog` is tamper-proof against database-level access (its
immutability is application-enforced — see **Deliberately left open**), that
`ADMIN_SECRET_ENCRYPTION_KEY` can be rotated, that the go-live readiness
checklist exists (it does not — the seam refuses instead), that a restaurant can
be onboarded through any API, or that UAT runtime behaviour is verified beyond
what the deploy gates check.

## The ladder — per-PR ledger

| PR | # | Merge commit | Migration | What it closed |
|----|---|--------------|-----------|----------------|
| PR-A | #260 | `946aefe` | `users_app/0013_close_ambient_admin_authority` | Ambient administrator authority on the customer plane |
| PR-B | #261 | `b86e1ee` | *(none)* | Recovery codes unreachable when the Fernet key is lost |
| PR-C | #263 | `8421695` | `platform_admin_app/0007_adminloginchallenge_recovery_only` | Non-atomic admin verification; lockout as a denial of service |
| PR-D | #264 | `79edfbc` | `orders_app/0035_order_is_test` | Pre-live trading; fail-open readiness; the last audit gap |

Two attribution traps, both of which a naive `git log --diff-filter=A` reading
gets wrong. `fba76b2` (#262, the dashboard-v2 bucket vocabulary) merged BETWEEN
PR-C and PR-D and is **not** part of this ladder. And
`restaurants_app/0056_restaurant_lifecycle_states` belongs to PR-5 (the lifecycle
states work that preceded Phase 0.5), **not** to PR-A — PR-D built on it, but did
not introduce it.

### PR-A — ambient administrator authority removed

`User.roles` is no longer an authority vocabulary. `account_type` decides the
plane; `roles` carries restaurant roles only. `is_dinify_admin` /
`is_dinify_superuser` / `dinify_roles` and the `DINIFY_ADMIN` /
`DINIFY_ACCOUNT_MANAGER` constants are DELETED, along with every grant they fed:
the `check_permission` write bypass, the full-access module map, the `None`
"unrestricted" sentinel on both id resolvers (and with it
`build_scoped_instance_queryset`'s `model.objects.all()`), the
`can_manage_restaurant` short-circuit, the login `require_otp` arm, the
`user-lookup` disjunct and the first-time menu self-approval exemption. Four
surfaces then had no principal that could ever reach them and were removed rather
than left authenticated-but-unreachable; they are itemised in
`DEAD_CODE_CLOSURE.md`'s Phase-0.5 addendum.

**The thing to get right:** cross-tenant reach on the customer plane now comes
from a delegation grant or from nowhere. There is no platform short-circuit to
re-add, and TENANT-AUTH-00 (`scripts/check_ambient_authority.py`, empty allowlist)
is PERMANENT — there is no future state in which reintroducing the mechanism is
acceptable, which is why the gate has no escape hatch.

### PR-B — the second factor survives key loss

`auth/verify/` and `auth/elevate/` now require `{"method": "totp"|"recovery",
"code": "..."}`, dispatched by `platform_admin_app/second_factor.py` whose
`check()` returns a total `FactorVerdict` and never raises. The recovery branch
calls only `recovery.consume`; the TOTP branch converts `ImproperlyConfigured`
into an ordinary failure so a broken key is never a 500 or a config leak, while
enrolment still fails LOUDLY.

**The thing to get right:** `method='recovery'` must never touch `totp` or
decrypt anything — that property is what makes a lost key recoverable, and
`platform_admin_app/endpoints/auth.py` no longer imports `totp` at all so it is
visible in the import list rather than buried in a branch. `method` has **no
default**, deliberately: a default reinstates the try-TOTP-first ordering that
caused the defect. Every denial is byte-identical — wrong code, wrong method for
the code, unknown method and unusable key are indistinguishable to the caller; the
cause rides the audit `error_code` and the attempted method the audit `reason`.

### PR-C — atomic, locked, honestly-audited verification

`auth/verify/` and `auth/elevate/` each do ALL their work in ONE
`transaction.atomic()` under `select_for_update`, and the raw session token
reaches the response cookie only after it commits. `challenges.consume` is now
CONDITIONAL and returns bool; `resolve_challenge(..., for_update=True)` locks with
`of=('self',)` so the joined `User` row is not locked as a side effect;
`record_attempt` uses an `F()` update and `lockout.register_failure` a
`select_for_update` re-read, so neither loses an increment.

**Two things to get right.** **LOCK ORDER is `AdminLoginChallenge` then
`PlatformStaffAuth`, never the reverse** — elevate and `lockout.register_failure`
take only the second; reversing it anywhere introduces a deadlock cycle.
*(Extended post-ladder by Closure PR 1, #266: the full order is now `User` →
`AdminLoginChallenge` → `PlatformStaffAuth`. `challenges.create_challenge` takes the
new FIRST level and only that one. It must not take `PlatformStaffAuth` instead —
it goes on to write-lock challenge rows through its consuming `UPDATE`, so holding
the auth row first would give `PlatformStaffAuth` → `AdminLoginChallenge`, the exact
reverse of verification's order. `User` is safe to take first because
`resolve_challenge(for_update=True)` passes `of=('self',)`, so the admin-auth
transactions acquire it in one consistent position.)* *(Corrected by PR-E: this said
"precisely because … so nothing else holds it", which was false at the time of
writing. `delegated_sessions.exchange_code` chained `select_for_update()` with a
multi-table `select_related()` and no `of=`, so it held a `User` row lock and a
`Restaurant` one, closing an ABBA cycle with `transition_restaurant`. Its lock ORDER
was always sound — one statement, so no self-deadlock and no interleaving — but the
BREADTH was not. PR-E added `of=('self',)` there too; redemption now locks the grant
row alone, and `platform_admin_app/tests_delegation_lock_scope.py` fails on the
pre-PR-E code.)* And the
**failure-audit rule**: ordinary denials audit INSIDE the transaction and commit
with their own failure accounting, so either a failure is both counted and
recorded or neither. The ONE path that must roll back — losing the challenge race,
factor already consumed — audits AFTER the block via `_LostChallengeRace`, the
house carry-out pattern. The repo uses NO `transaction.on_commit`, `durable=`,
`set_rollback` or `savepoint=` anywhere; statement order is the whole mechanism.

### PR-D — the launch boundary, and the last audit gap

`CAP_LIVE_TRADING` is the ONE capability cell that differs between `onboarding`
(False) and `live` (True). Two predicates read it: `allows_diner_ordering` refuses
the PUBLIC at a restaurant that has not gone live, and `orders_are_commercial` is
the sole source of `Order.is_test`. `CAP_ORDER_CREATE` deliberately STAYS True at
onboarding so the owner can place the end-to-end rehearsal order the Phase-1
checklist requires. `check_go_live_readiness` now fails closed, and both delegated
tenant writes audit inside their own transaction.

**The thing to get right:** a test order is **operationally real and commercially
invisible**. It occupies its table, reaches the kitchen board and is served or
cancelled normally — that is the rehearsal — but it never appears in money,
history or diner analytics. Every include/exclude in the consumer sweep derives
from that one rule rather than being decided per call site — and the rule cuts
THROUGH functions, not just between them. `_build_kds` sees test orders entirely,
and `_build_tables` is split down the middle: its occupancy queryset deliberately
counts them (a rehearsal order really does occupy its table, and the card must
agree with the kitchen board — see the comment at `dashboard.py:417-420`) while the
median-visit, turns and avg-ticket metrics in the same function exclude them,
because those are history and money.

## Invariants now held (and the test that proves each)

IDs are local to this record and PR-keyed (`A` = PR-A, and so on). They are
**not** the `C1–C5` retirement IDs of `DEAD_CODE_CLOSURE.md`, which are that
record's own numbering.

| # | Invariant | Proving test |
|---|-----------|--------------|
| A1 | No customer-plane principal wields platform authority through `User.roles`; an account carrying the legacy `dinify_admin` string resolves exactly like a stranger — no module, no list scope, no manage level, no write queryset | `dinify_backend.tenancy.tests_ambient_authority` (42 tests); the closure suite's I16 row |
| A2 | Platform staff can neither obtain NOR refresh a customer JWT: `GatedTokenRefreshView` carries the same `account_type` refusal as login, and its denial is byte-identical to SimpleJWT's own `token_not_valid`, so it is not an account-type oracle. Outstanding platform-staff refresh tokens were blacklisted by migration `users_app/0013` | `dinify_backend.tenancy.tests_ambient_authority` (the gating, incl. `test_the_refresh_refusal_matches_an_invalid_token`); rotation/blacklist mechanics in `users_app.tests` |
| A3 | A `restaurant_user` cannot be given a platform role, and `platform_admin_app/services.py` is the only writer of `account_type` — never a serializer, never Secretary | `dinify_backend.tenancy.tests_ambient_authority` |
| A4 | The mechanism cannot regrow by name: no customer-plane production module may reference the retired predicates/constants, hard-code a platform-only role string, or select users by one through a `roles__*` ORM lookup | `scripts/check_ambient_authority.py` (TENANT-AUTH-00, empty allowlist, in `ci.yml` and `verify.sh`) |
| B1 | The second factor is method-explicit; `method` has no default, and an unknown method is an ordinary denial | `platform_admin_app.tests_second_factor` (36 tests) |
| B2 | A recovery-code verification decrypts nothing — it never reaches `totp` or `crypto._fernet` — so a lost or corrupt `ADMIN_SECRET_ENCRYPTION_KEY` leaves recovery codes fully usable | `platform_admin_app.tests_second_factor::BreakGlassSequenceTests` (spies on `platform_admin_app.totp.decrypt_secret` AND `crypto._fernet`) |
| B3 | Every second-factor denial is byte-identical; a broken key is never a 500 or a config leak, while TOTP enrolment still fails loudly | `platform_admin_app.tests_second_factor` |
| C1 | Verification is one transaction: the factor is consumed, the TOTP counter advanced, the session minted and the audit written together, or none of it happens. The raw token reaches the cookie only after commit | `platform_admin_app.tests_admin_auth_atomicity` (7 tests) |
| C2 | A live `AdminSession` cannot exist without its success audit row — a failed audit unwinds the session that would have been minted | `platform_admin_app.tests_admin_auth_atomicity` |
| C3 | Two concurrent verifications of the same challenge mint exactly ONE session | `platform_admin_app.tests_admin_auth_concurrency::AdminVerifyConcurrencyTests` (3 tests, `@tag('concurrency')`, PostgreSQL-only). Scoped to the class post-ladder: Closure PR 1 (#266) added a second class, `AdminLoginChallengeConcurrencyTests`, for the separate challenge-MINTING race, so a bare module count no longer describes what proves this row |
| C4 | Failure accounting is lost-update-free, and an ordinary denial's failure is both counted and recorded, or neither | `platform_admin_app.tests_admin_lockout` (25 tests); `tests_admin_auth_atomicity` |
| C5 | Lockout is a nuisance, not a denial of service: threshold 10, then a window that doubles per further failure from 1 min to a 60 min cap. `failed_attempts` is CUMULATIVE, which is what makes the backoff escalate; the per-challenge attempt cap is its own `ADMIN_CHALLENGE_MAX_ATTEMPTS` (5) so raising the threshold cannot silently widen the guess budget | `platform_admin_app.tests_admin_lockout` |
| C6 | A locked-out administrator recovers WITHOUT operator involvement: with the correct password, `login/` mints a `recovery_only` challenge, `verify/` accepts only `method='recovery'` against it, and success clears the lock and emits `ADMIN_AUTH_LOCKOUT_CLEARED`. An attacker cannot ride this path — it needs the password AND a one-shot recovery code. Responses stay generic; lockout is never an account-existence oracle | `platform_admin_app.tests_admin_lockout`; the shell equivalent is `manage.py unlock_platform_admin` |
| D1 | `onboarding` ≠ `live`: the public cannot create an order at a restaurant that has not gone live, while the owner's authenticated rehearsal order still works and the diner MENU still renders | `orders_app.tests_launch_boundary` (23 tests) |
| D2 | `Order.is_test` is SERVER-DERIVED from `orders_are_commercial(restaurant.status)`; there is no request field for it and it must never gain one | `orders_app.tests_launch_boundary` |
| D3 | A rehearsal order is excluded from every money / history / diner-analytics consumer — the `sale_filters.sale_orders()` chokepoint, both dashboards, `summarize_revenue`, the transactions report, `determine-customers`, and review submission — and deliberately INCLUDED wherever the answer is live floor state: `any_present_ongoing_order` occupancy, `Table.has_unsettled_orders()`, the kitchen active and completed boards, dashboard-v2's `_build_kds` and the occupancy queryset inside `_build_tables`, and the idempotency lookups | `orders_app.tests_launch_boundary` (one assertion per consumer, both directions) |
| D4 | `onboarding → live` is refused on EVERY path — service and endpoint — with `code='not_ready_for_go_live'` and the single machine-readable blocker `readiness_not_configured`. `suspended → live` remains deliberately un-readiness-gated | `restaurants_app.tests_lifecycle`; `platform_admin_app.tests_lifecycle_endpoint::test_go_live_is_refused_while_readiness_is_unconfigured` |
| D5 | Both delegated tenant writes call `audit_delegated_write` inside their own `transaction.atomic()`, so a failed audit rolls the write back. The non-safe allowlisted route set is asserted, so it cannot grow silently. **Wording corrected post-ladder:** this row originally read "the no-audit-no-action contract is universal", which overstated it in two directions — the contract is deliberately ASYMMETRIC (privileged successful state changes and credential issuance are audit-atomic; denials, failure accounting and safety-reducing revocations may be best-effort, because losing a revocation to a failed audit is worse than an unaudited revocation), and one credential-issuing path — admin login-challenge issuance — was still outside a shared transaction until Closure PR 1 (#266) wrapped it. See `platform_admin_app/audit.py` for the contract as stated | `platform_admin_app.tests_delegated_audit` (8 tests) |

## Decisions record

Each records the rejected alternative, because the alternative is what a future
reader will reach for.

- **(a)** Lockout policy — **CHOSEN: threshold 10, then progressive backoff
  1 min → 60 min cap.** The old flat 15-minute `ADMIN_LOCKOUT_DURATION` at
  threshold 5 made a trivial denial of service: five wrong guesses locked a real
  administrator out of the control plane with no self-service way back, and anyone
  who knew a username could do it on demand. Rejected: raising the flat
  threshold alone (which widens the guess budget linearly and still ends in a hard
  wall) and dropping lockout for throttling alone (the DRF throttle's LocMemCache
  counters are per-mod_wsgi-worker, so it is not a real control).
- **(b)** The way back in — **CHOSEN: a `recovery_only` challenge.** `login/` no
  longer refuses a locked account before the password check; with the correct
  password it mints a challenge that only a recovery code can answer. Rejected:
  operator-only unlock via a management command, which makes the platform team a
  single point of failure for their own access. The command
  (`unlock_platform_admin`) was still built, as the narrow shell equivalent — but
  it is no longer the only route.
- **(c)** Separating the launch boundary — **CHOSEN: one new capability key,
  `CAP_LIVE_TRADING`.** Rejected: flipping `CAP_ORDER_CREATE` to False for
  `onboarding`, which reads like the obvious fix and is wrong — the lifecycle gate
  in `ConOrder.initiate_order` is NOT conditioned on `created_by`, so it would
  have blocked the owner's rehearsal order along with the public's, and the
  rehearsal order is exactly what the Phase-1 checklist needs.
- **(d)** The diner menu during onboarding — **CHOSEN: keep it rendering; refuse
  only ordering.** The owner must be able to verify the real QR → menu experience
  before launch, which is itself a readiness concern. Rejected: a 503 for the
  whole menu, which is what `suspended` does and which would have made the
  pre-launch QR untestable. The honest cost — a passer-by could conclude the
  restaurant is open — is carried by an unambiguous refusal message
  (`MESSAGES['NOT_OPEN_YET']`).
- **(e)** Reviews of test orders — **CHOSEN: not reviewable**, refused with the
  same restrained 400 as the other ineligible states. Rejected: filtering
  `is_test` out of review analytics, which cannot work — the six review aggregates
  read the denormalised `Review.restaurant` and never join `Order`, so each would
  need its own join and each would be a place a future query could forget.
  Blocking submission makes the rating average provably unaffected.
- **(f)** A go-live escape hatch — **CHOSEN: none.** See **Deliberately left
  open**. Rejected: an admin-only override flag, which would have recreated the
  fail-open gate under a different name.

## Deliberately left open

Each of these is a decision. None is an oversight.

- **`AdminAuditLog` immutability is APPLICATION-enforced, not database-enforced.**
  The `save`/`delete` overrides and the queryset guard raise
  `AppendOnlyViolation`, which covers every ORM path the application takes — but
  `bulk_create`, raw SQL and direct database access bypass them. The named next
  step is a Postgres `BEFORE UPDATE OR DELETE` trigger; it is deliberately not
  built (PR-D §2.5) and is recorded in the model docstring and in
  `dinify_backend/tenancy/ASSURANCE.md` so the limit travels with the claim.
- **Key ROTATION is not built.** PR-B made key *loss* recoverable; it did not make
  rotation possible. There is no `MultiFernet`, no key-id envelope, and no
  re-encryption path. The substitute is the four-step break-glass sequence in
  `BACKGROUND_TASKS.md` (recovery-code sign-in → install a new key →
  `reset_platform_admin_totp` → re-enrol), covered end to end by
  `tests_second_factor::BreakGlassSequenceTests`. Note the ordering constraint
  that makes it work: `reset_platform_admin_totp` never DECRYPTS, so it survives
  key loss — but it does ENCRYPT, so a valid key must be installed before it runs.
- **No API path creates a restaurant, and none takes one live.** PR-A retired the
  last creation path (its only gate was a role string); PR-D's fail-closed
  readiness refuses every `onboarding → live` transition. Together these mean a
  restaurant can today be created and launched only outside the API. **ACCEPTED
  GAP**, owned by Phase 1, and nothing is stranded: the one production restaurant
  is already `live`, and no onboarding flow exists yet to be blocked. Do not
  re-add a creation path on the customer plane — Phase 1 builds it natively on
  `/api/admin/v1`.
- **The readiness seam has no override.** `check_go_live_readiness` returns
  not-ready with one blocker until Phase 1 wires the real checklist, and there is
  deliberately no bypass flag, no settings escape and no elevated-admin exception.
  A safety gate with an override is the fail-open gate this PR replaced.
- **DRF throttles on the admin auth routes are defence-in-depth only.** Their
  LocMemCache counters are per-mod_wsgi-worker, so they do not bound attempts
  across the process pool. The DB-backed lockout is the real control; the throttles
  ship anyway because they are free and cheap to keep.
- **WebAuthn / hardware second factor is not built.** Out of scope throughout the
  ladder. TOTP plus recovery codes is the current factor set.

## Reported, not fixed

Pre-existing looseness surfaced by the ladder's recon and deliberately left
outside its scope. Recorded here so a later sweep treats it as known rather than
new — and so nobody assumes the ladder blessed it.

- **`reports_app/controllers/restaurant/dashboard.py:64`** — `num_sales =
  orders.count()` counts every order, including cancelled and refunded ones, and
  is then used as the denominator for the paid / cancelled / refunded percentages.
  The name says "sales"; the value is "orders". Unrelated to `is_test` (PR-D added
  the `is_test=False` filter to this queryset, which does not touch the looseness),
  and unfixed because changing a live dashboard denominator is a contract change,
  not a cleanup.
- **`order_source='server_assisted'`** (`orders_app/models.py:55`) is a live,
  migrated, never-written enum slot: the authenticated staff branch persists
  `diner_self_service`. A latent mislabelling — the staff rehearsal path is now
  the one place it would obviously belong — but renaming what an order records is
  a data-semantics change and was outside PR-D's scope.
- **A rehearsal order consumes a real `RestaurantDailyOrderCounter` ticket
  number**, leaving a gap in that day's sequence. This is a write-side effect no
  read filter can undo, and it is harmless: during onboarding there are no real
  orders for the gap to disturb. Accepted and documented rather than engineered
  around.
- **`allows_kitchen`, `allows_support` and `serves_diner_menu` have zero
  production callers** — **KEPT**. `CAPABILITY_MATRIX` is the spec table as data;
  these rows document states whose enforcement is emergent elsewhere (the kitchen
  gates route through the module resolver, support is ungated by design). Deleting
  the predicates would make the matrix an incomplete spec, which is worse than an
  uncalled function.

## Seams Phase 1 inherits

Each is a named single-call function or config table with a real call site.
**Replace the body; do not inline the check at the call site and do not move the
seam.**

| Seam | Location | Today | Phase 1 |
|------|----------|-------|---------|
| `check_go_live_readiness(restaurant)` | `restaurants_app/controllers/lifecycle.py:109`; one call site at `:198` inside `_check_transition_preconditions`, gated on `onboarding → live` | Returns `ReadinessResult(ready=False, blockers=['readiness_not_configured'])` | Replace the body with the real checklist. The refusal machinery around it already works — `LifecycleTransitionError(code='not_ready_for_go_live')` carries `errors['blockers']`, `_deny` audits it, and the endpoint surfaces the code |
| `has_outstanding_receivables(restaurant)` | `restaurants_app/controllers/lifecycle.py:138`; one call site at `:206`, gated on `→ offboarded` | Returns `False` — `SubscriptionInvoice` does not exist, and `DinifyTransaction` is a record-only structure that is never the receivable | Replace the body once a receivables model lands |
| `has_completed_test_order(restaurant)` | `orders_app/controllers/test_orders.py:36` | The queryable rehearsal fact; no production caller yet, by design — it exists for the checklist to import | Call it from `check_go_live_readiness` as one checklist item |
| `CAPABILITY_MATRIX` + `CAP_LIVE_TRADING` | `restaurants_app/controllers/lifecycle_policy.py` | Seven capability keys over four states; `_row()` fails closed on an unknown value | Where a new per-state capability goes. Read it through a named predicate — never a `status == '<literal>'` comparison anywhere in the codebase |
| `audit_delegated_write(request, ...)` | `platform_admin_app/delegated_audit.py:66` | Called from the two delegated tenant writes, inside their transactions | The pattern any NEW delegated tenant write must adopt — or the route stays off `ALLOWED_ROUTES`. A test asserts the non-safe route set, so the allowlist cannot grow silently |
| The admin plane's urlconf | `platform_admin_app/urls.py`, mounted by `dinify_backend/urls_admin.py` | Explicit deny-by-default routes only: health, `auth/*`, `delegations/*`, `restaurants/<id>/transition/` | Where onboarding, support triage and billing get rebuilt. Never a `<str:action>/` catch-all |

## Deploy status at authoring

All four rungs reached UAT. `deploy-uat` completed successfully for `946aefe`
(PR-A, run 242), `b86e1ee` (PR-B, 243), `8421695` (PR-C, 244) and `79edfbc`
(PR-D, 246) — so both migrations the ladder introduced,
`users_app/0013_close_ambient_admin_authority` and `orders_app/0035_order_is_test`,
are applied, and the health-gated restart the deploy performs (the `.env`
permission check, the `www-data` settings probe, `check --deploy`, `apachectl
configtest`, then the HTTP-405 login probe) passed on each.

Worth knowing for the next person watching a deploy: PR-D's run registered about
sixteen minutes after its merge, where the other three appeared within a couple of
minutes. A missing run shortly after a merge is not yet evidence of a failed
deploy. A successful deploy proves the gates passed — it does not exercise the
behaviour; see **What this record claims — and does not**.

## Custodial-invariant re-affirmation

This ladder introduced no money-flow code and removed none. The non-custodial
posture recorded in `REGULATORY_AUDIT.md` is unchanged: the backend holds no PSP
credentials and executes no payments, and the only surviving payment code is the
record-only `DinifyTransaction` model plus the subscription writer that records a
Pending row and stops. `Order.is_test` is a reporting-VISIBILITY flag, not a
financial one — it changes which orders a report counts, never what is charged,
held or settled. PR-D's transactions-report exclusion is deliberately
`Q(order__isnull=True) | Q(order__is_test=False)` rather than a bare `.exclude()`,
precisely so order-less subscription rows survive it.

## Re-verify triggers

Re-read this record, and re-run the suites it cites, when any of these change:

- the capability matrix or any lifecycle predicate
  (`restaurants_app/controllers/lifecycle_policy.py`);
- the readiness or receivables seam (`restaurants_app/controllers/lifecycle.py`);
- the `Order.is_test` derivation (`orders_app/controllers/services/create_order.py`),
  or any new consumer of order data that reports money, history or diners;
- the delegated route allowlist or scope map
  (`platform_admin_app/configs/delegation_scopes.py`), or the set of delegated
  tenant writes;
- the admin verification transaction, its lock order, or the challenge/session
  lifecycle (`platform_admin_app/endpoints/auth.py`,
  `platform_admin_app/challenges.py`, `platform_admin_app/sessions.py`);
- the second-factor dispatcher (`platform_admin_app/second_factor.py`) or anything
  that would make a recovery attempt decrypt;
- `ADMIN_SECRET_ENCRYPTION_KEY` handling (`platform_admin_app/crypto.py`), or any
  attempt to add rotation;
- the lockout policy constants (`platform_admin_app/lockout.py`) or the
  `AdminAuditLog` immutability guards.

## Closure

Authored against branch base `79edfbc` (the merge of PR-D, #264); the closure SHA
is this PR's merge commit. As of this record, no open Phase 0.5 findings remain —
what is not closed is recorded above as a decision, a gap or a seam. Claims are
valid at the SHAs cited beside them, not necessarily at any later HEAD. Sibling
records: `DEAD_CODE_CLOSURE.md` (whose Phase-0.5 addendum itemises PR-A's
retirements), `dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md` and
`ASSURANCE.md` (the tenant boundary and its assurance limits),
`REGULATORY_AUDIT.md` (the non-custodial posture), `BREAKING_CHANGES.md` §7–§9
(the frontend-facing contract changes this ladder made) and `BACKGROUND_TASKS.md`
(the break-glass and unlock runbooks).
