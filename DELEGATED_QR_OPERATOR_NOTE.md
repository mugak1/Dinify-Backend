# Operator decision note — delegated QR-credential exposure

**Status: DECISION INPUT. Nothing in this note has been done.**

No production or UAT record was inspected, no grant or session was enumerated
against live data, no real table was selected, nothing was rotated and nothing was
revoked. Every fact below is read from this repository's source. The decision this
note exists to inform — *does any already-disclosed QR credential need to be
revoked, and if so which* — requires its own authorization and its own access.

The defect and its containment are recorded in `DELEGATED_QR_TRIAGE.md`. This note
covers only the part containment cannot reach: **what was disclosed before it, what
evidence exists about that, and what the one available remedy actually buys.**

---

## 1. What has to be decided

Containment stops future disclosure through these response paths. It revokes
nothing. A QR credential obtained before it — or a diner table session already
exchanged from one — still works exactly as it did.

So there is a decision, and it is genuinely a decision rather than a formality:

> Is the population of possibly-disclosed table QR credentials small enough, or the
> evidence clear enough, to leave in place — or should some set of tables have their
> QR generation rotated, at the cost of reprinting their physical codes?

**It cannot be answered from this repository.** It needs the grant and session rows
on the deployed database, which nobody has looked at.

---

## 2. What was exposed, in one paragraph

A delegated administrator holding **either** delegation scope (`view` or `support`)
who read `GET /api/v1/restaurant-setup/tables/` — flat or `?grouping=` — received a
live, signed `qr_credential` for **every table in that restaurant**. A QR credential
is the sole anonymous authority for its table: presented to
`orders/journey/table-scan/` it mints a diner table session, and a table session is
what places orders. It is verified **without expiry**, so it does not lapse, and it
**outlives the delegated session and the grant that produced it**.

Four ordinary table-action responses (`seat`, `clear`, `transfer`, `update-status`)
carried the same field, as did `regenerate-qr`.

---

## 3. The evidence that exists

Three places record something. Each answers a narrower question than it first
appears to.

### `delegation_grant` — who was handed authority into which tenant, and why
Columns worth reading: `administrator` (FK, PROTECT — it cannot be erased by
deleting the actor), `restaurant` (FK), `scope`, `reason` (required, ≥10 characters,
enforced at mint), `issued_at`, `redeemed_at`, `revoked_at`, `revoked_reason`,
`session_ttl_seconds`, `issued_ip`, `issued_user_agent`.

Indexed on `(administrator, issued_at)` and `(restaurant, issued_at)`, so both
"where has this administrator been" and "who has been in this tenant" are cheap
queries rather than table scans.

### `delegated_session` — the credential that actually travelled
`grant` (OneToOne — one grant yields at most one session, ever), `issued_at`,
`expires_at`, `ended_at`, `ended_reason`, `issued_ip`, `issued_user_agent`.

**This is the row that bounds the tenant set.** The disclosing read requires a
delegated *session* token, and a session exists only for a grant that was redeemed.
So the distinct `grant.restaurant` over `delegated_session` is the **complete upper
bound on the restaurants whose table credentials could have been disclosed through
this path.** A grant that was minted and never redeemed disclosed nothing.

Each session is also time-bounded: `[issued_at, min(expires_at, ended_at,
grant.revoked_at)]`, with a default TTL of **900 seconds** (`session_ttl_seconds`,
`ADMIN_DELEGATION_MAX_LIVE_GRANTS` caps concurrent live grants at 5).

### `admin_audit_log` — the delegation lifecycle
`admin.delegation.minted` / `mint_denied` / `revoked` / `superseded` /
`session_started` / `session_start_denied` / `session_ended` / `action_performed` /
`action_denied`, each carrying actor, session, restaurant, reason, result and
request id.

---

## 4. The limits of that evidence — read this before drawing a conclusion

**The successful read is the one thing nothing records.** The delegated middleware
writes `admin.delegation.action_performed` only when
`request.method not in SAFE_METHODS`, and `SAFE_METHODS` is `{GET, HEAD, OPTIONS}`.
The disclosing read is a `GET`. So:

> **There is no audit row for a delegated tables read that succeeded.** Absence of
> an `action_performed` entry is *not* evidence that no credential was read. That
> action is only ever written for writes.

The log is worse than merely silent here — it is **skewed in the misleading
direction**. A delegated request that was *refused* is audited (`action_denied`),
including a refused `GET`. So the log records the reads that failed and not the ones
that succeeded, and reading it as an access record inverts the truth.

Four further limits, each real:

- **`delegated_session` carries no `last_seen_at`**, deliberately (a write per
  delegated request would be write amplification on a read path). The row therefore
  cannot say whether the session was used at all after it was minted, let alone what
  it read.
- **No credential value is stored anywhere.** `issue_qr_credential` mints per read
  and persists nothing — the token bytes even vary between two reads of the same
  table. So nothing can be matched against a credential found later, and there is no
  issuance ledger to count.
- **A diner session minted from a disclosed credential has no database row.** It is
  a `django.core.signing` token, stateless by design. There is no session table to
  enumerate and no way to count how many exist or where.
- **Web-server access logs are a host question, not a repository one.** Apache would
  record the request line and status for `GET /api/v1/restaurant-setup/tables/` if
  its logs are retained and survived — but this repository cannot state what is
  retained, for how long, or whether it is intact. `REGULATORY_AUDIT.md`'s APPENDIX
  already records host state diverging from repository assumptions (H2: PSP
  credentials surviving in a backup `.env` long after the teardown that removed them
  from the tree). Treat host log retention as something to establish, never assume.

**And the exposure window cannot be dated from this checkout.** The clone in this
environment is shallow — 142 commits, grafted at `1bec1ed` (2026-07-28) — and both
the delegated read path and the flat serializer's `qr_credential` predate the graft,
so `git log -S` attributes both to the graft commit rather than to their real
changes. The window opens when `tables` joined `SETUP_READABLE_RECORDS` **and the FIRST
disclosing builder existed**, and closes when containment deploys. Establish it
against full history or against the deploy record; do not quote a date from this
checkout.

> **CORRECTED (D07 §14).** This previously dated the window from *the flat table
> serializer's* `qr_credential`. **That is the LATEST of the disclosing builders,
> not the earliest**, so using it as the opening bound UNDERSTATES the exposure.
> Containment had to be applied at **six** sites, and the flat list was added
> LAST — the repository's own record says PR 7A "only covered the other two" and
> the flat field arrived later, because the portal lost every credential on
> reload. The **grouped** (`?grouping`) read and the `regenerate-qr` response
> disclosed before it. Date the window from the earliest disclosing builder that
> a delegated read could reach, and enumerate all of them rather than the one
> whose field name is easiest to grep.

**And "no rows" is not proof.** Any upper bound drawn from retained
`delegation_grant` / `delegated_session` rows is bounded by the completeness and
integrity of that retention, and by the environment and time interval those rows
actually cover. An empty result means *nothing was found in what survived and was
searched* — which, given that a successful delegated read writes no audit row at
all (above), is a much weaker statement than "no historical disclosure occurred"
and must never be recorded as that.

---

## 5. The remedy that exists, and exactly what it buys

`POST /api/v1/restaurant-setup/table-actions/regenerate-qr/` with
`{"table_id": ...}` — gated through the **`tables` module**, **one table per call,
no bulk mode**.

> **CORRECTED (D07 §14).** This read `PUT` until D07's documentation pass.
> `TableActionsEndpoint` defines **only** `post` — there is no `put` handler and no
> `PUT` in any test — so an operator following the previous wording would have been
> answered **405** in the middle of a containment response, and would reasonably
> have concluded the remedy was unavailable.

> **CORRECTED (D07 §14).** This also read *"owner/manager-gated"*. That narrows the
> real policy in prose: `tables` is an ordinary grid module, and
> `role_defaults.DEFAULT_ROLE_MODULES` grants it to **`restaurant_staff`** by
> default as well as to owner and manager — while an owner may widen or withdraw it
> for any non-owner role through the Roles & Access grid. So who may rotate is
> **whatever that restaurant's grid currently says**, which is the thing to read
> before planning a rotation; it is not a fixed two-role list, and it is not
> narrowed by the QR-disclosure containment (which withholds the CREDENTIAL from a
> delegated caller and changes no module policy).

It bumps `qr_version` atomically (an `F()` expression, race-safe under concurrent
taps) and stamps `qr_regenerated_at`.

**What it genuinely does:** every outstanding QR credential for that table — and
every live diner session minted from one — carries the old generation and fails the
verifier's generation re-check. Both tokens are bound to the generation, so this is a
real revocation with no stored secret to revoke. Pinned by
`tests_tenant_isolation_closure`.

**What it costs:** the table's physical sticker must be reprinted. Between the bump
and the reprint that table cannot be scanned at all, so diners at it cannot order.
Per table, and there is no bulk endpoint.

**What it does NOT do, and none of these may be softened:**

- it does not erase any copy of the old credential that was already taken;
- it does not undo orders already accepted under the old generation;
- the generation re-check is a **sequential resolver property**, demonstrated by
  sequential tests. It is not a concurrency guarantee: a request already past its
  check can still complete.

**Ordering matters.** Rotating before containment is deployed is close to
pointless — the next delegated read discloses the fresh credential. **Deploy the
containment first, then rotate.**

---

## 6. The shape of the decision

Three options. Which is proportionate depends entirely on what the bounding query
in §3 returns, and that query has not been run.

| | what it buys | what it costs | when it is defensible |
|---|---|---|---|
| **Leave it** | nothing to do | accepts that any disclosed credential stays live indefinitely | the bounding query returns **no delegated session for any tenant with tables** — the one case where "no exposure" is supported by data rather than by silence |
| **Rotate the named tenants' tables** | revokes the credentials that could have been disclosed | reprinting every table of those tenants, and those tables unscannable until reprinted | the bounding query names a small tenant set — the expected case |
| **Rotate everything** | revokes regardless of evidence | reprinting the entire estate | the grant/session rows are unavailable, incomplete or not trusted |

Two things to hold on to while choosing:

- **the bounding query is an upper bound on tenants, not a measurement of reads.**
  A tenant appearing in it means a delegated session existed there, not that anyone
  opened the tables list. §4 is why the difference cannot be closed;
- **"no audit entry" supports nothing.** The one thing the log cannot record is the
  successful read.

---

## 7. What was not done, and what needs separate authorization

Not done here, deliberately, and each requires its own authorization and access:

- inspecting any production or UAT record, including running the bounding query;
- naming, selecting or rotating any real table;
- revoking any grant or ending any delegated session;
- reprinting anything;
- notifying anyone.

Also deliberately **not** done, and not to be adopted as a substitute for this
decision: mass-rotating QR codes, expiring printed credentials on a timer, rotating
the global signing key, invalidating live diner sessions wholesale, adding an
access-logging platform, or changing delegation audit semantics. Each is a larger
change with its own blast radius, and none of them is containment.

Paired records: `DELEGATED_QR_TRIAGE.md` (the defect, the containment and the
residual risk) and `dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md` (the tenant
boundary this sits inside).
