# CONTAINED — a delegated READ no longer hands out live diner ordering authority

**Status: CONTAINED for FUTURE disclosure through these application paths. It does
NOT revoke anything already disclosed — see §7.**

This was the separate triage item raised alongside the D06 completion work. It was
a **delegation-scope question**, deliberately kept out of the D06 diff because
closing it changes what a delegated administrator may do.

Everything in §§1–3 was **measured against unmodified `main`** by
`platform_admin_app/tests_delegated_qr_disclosure.py` before anything was changed.
That file was a CHARACTERIZATION and is now a REGRESSION SUITE: the three
assertions that asserted the disclosure are INVERTED, the two whose fixtures
depended on the leak are REBUILT against an ordinary authorized read, and the
controls are untouched. The conversion is deliberate and is recorded in that
file's own docstring rather than done by deleting tests.

**The containment is option (a) below.** `restaurants_app/controllers/qr_disclosure.py`
is the one decision; `restaurants_app/tests_qr_disclosure_boundary.py` pins the
rest of the contract — above all the ORDINARY paths, which must keep working.

---

## 1. What it is

`SETUP_READABLE_RECORDS` admits `tables` to the delegated restaurant-setup GET, and
that read MINTS a QR credential per table. A QR credential is the **sole anonymous
authority** for a table: presenting it to `orders/journey/table-scan/` mints a diner
table session, and a table session is what places orders.

So a delegated administrator holding the **`view`** scope — the read-only one —
received working anonymous ordering authority for every table in the restaurant, as
an ordinary consequence of opening the tables list. **`support` received it too**:
`_scope_grid()` is called twice to build `SCOPE_MODULES`, so the two scopes share
one READ grid and differ only in what they may write. Containing only the scope the
triage happened to measure would have contained nothing.

Four properties make it worth triaging rather than noting:

1. **It is not an identifier.** The value exchanges for a session. Asserted through
   the real scan route, not by inspecting the signer.
2. **`view` is enough.** The read-only scope exists so a delegate can look without
   acting; this is the one read that hands over the ability to act.
3. **It OUTLIVES the delegation.** The credential is verified WITHOUT expiry — it is
   bound to `Table.qr_version`, not to a clock and not to the session that disclosed
   it. Ending the delegation, or letting the grant lapse, revokes nothing. Measured:
   after `POST api/v1/delegation/end/` the tables read answers 401 and the credential
   still scans 200.
4. **Only a QR REGENERATION revokes it**, which reprints every physical code on that
   table. The remedy costs the restaurant real work.

A fifth, weaker, property: **the disclosure leaves no audit row.** The delegated
middleware audits only non-safe methods (`request.method in SAFE_METHODS` → return),
so there is no `AdminAuditLog` entry saying credentials were handed out. That is
consistent with the repository's stated audit contract — an audit log is a record of
privileged DECISIONS, not an access log — and it is not a defect on its own. It does
mean that if this is ever exercised, the platform's own records will not show it.

## 2. What it is NOT

- **Not a tenant-boundary break.** The delegate was granted this restaurant and the
  credentials are for that restaurant's tables. Pinned as the control test.
- **Not a cross-tenant privilege escalation.**
- **Not a defect in the capability design.** `issue_qr_credential` is doing exactly
  what the owner-facing read needs it to do: the portal Setup View renders and prints
  QR codes from it, and it must be derivable on every page load.
- **Not evidence anything has happened.** No delegation grant has been observed
  exercising this. This is an exposure, not an incident.

The question is narrowly whether a DELEGATED principal should receive it.

## 3. The surface, exactly

**Three mint sites**, two of them reachable by a delegated session:

| site | read | delegated-reachable |
|---|---|---|
| `restaurants_app/serializers.py::SerializerPublicGetTable.get_qr_credential` | `GET restaurant-setup/tables/` (flat list) | **YES** |
| `restaurants_app/controllers/tables.py::get_tables_by_area` (mints in TWO places — assigned and unassigned tables) | `GET restaurant-setup/tables/?grouping=` | **YES** |
| `restaurants_app/endpoints/table_actions.py` (regenerate-qr response) | `POST restaurant-setup/table-actions/regenerate-qr/` | no — writes are off `ALLOWED_ROUTES` |

**TWO READS, NOT ONE, AND THAT IS THE EASIEST THING TO GET WRONG HERE.** The flat
list and the grouped read are different branches with different builders, but they
share one route (`api/v1/restaurant-setup/<str:config_detail>/`) and one
`config_detail` value, so **one allowlist entry admits both**. A fix applied only to
`SerializerPublicGetTable` would leave the grouped half wide open while looking
closed. Pinned by `test_the_GROUPED_read_hands_them_over_too`.

## 4. What was implemented

**Option (a): withhold the field from a caller whose ordinary, non-delegated
`tables` authority has not been positively established.** Field-level authority
containment — not removal of the table view, and not a change to any route, scope
or module grid.

### The decision, in one place
`restaurants_app/controllers/qr_disclosure.py`. The invariant it enforces:

> QR material is emitted only after ORDINARY, NON-DELEGATED authority for the
> relevant table scope has been positively established; otherwise it is withheld
> BEFORE SIGNING.

Four things about that sentence are load-bearing, and each is a way the obvious
implementation goes wrong:

1. **"Positively established" — absence is never entitlement.** A missing request,
   an anonymous principal, a serializer or helper built with no context, and a
   policy nobody supplied all resolve to `WITHHOLD_ALL`. That polarity is the
   OPPOSITE of this repo's `menu_policy` precedent (`policy = self.context.get(
   'menu_policy'); if policy is not None: <restrict>`), where an absent context
   correctly means the permissive operator path. Copying that polarity here
   produces a containment that reads as applied and discloses anyway — **measured
   against the real delegated read before this module existed**, and now pinned as
   a negative control.
2. **"Non-delegated" is a VETO over the module check, not a refinement of it.**
   `can_user_access_module` / `get_module_restaurant_ids` INTENTIONALLY resolve a
   delegated principal from the stored grant, so a delegate reading the tables list
   is a permitted caller and the resolver says so. Reading a table is not the same
   authority as minting the credential that orders from it, and the resolver cannot
   tell those apart because it was never asked to.
3. **"The relevant table scope" is a SET, not a boolean.** The policy carries the
   restaurant ids the caller holds ordinary `tables` authority over, so each row is
   checked against what its own restaurant authorises — a non-delegated principal is
   not thereby entitled to a foreign table. Resolved ONCE per response and reused for
   every row; never a permission query per row.
4. **"Before signing" is not decoration.** Where the credential is withheld the
   signer is not called at all. Signing and then stripping at an outer layer leaves a
   live bearer capability in memory for a logger, an exception repr, or the next
   person who adds a `to_representation` override above the strip.

It READS the two server-derived delegation signals the platform already establishes
(`delegated_middleware.delegation_context` on the request and
`delegated_auth.PRINCIPAL_DELEGATION_ATTR` on the principal) and the existing module
scope resolver. It resolves nothing itself and accepts NOTHING from a caller — no
query parameter, no body field, no `include_qr` flag, no grouping value, no role name
from a browser.

### The wire contract
**The key is ABSENT.** Not `null`, not `''`, not a table UUID, not a placeholder, and
not a credential-bearing URL or image under another name. A key present and empty and
a key absent are different facts to a client; this one is absent, and the string
`qr_credential` does not appear in a withheld response at all. Every other table
field is untouched — `has_qr`, `qr_mode`, `qr_version`, number, area, capacity,
geometry and status all stay, because blanking ordinary metadata to imitate
containment would be its own defect.

### Every builder and caller
| site | change |
|---|---|
| `serializers.py::SerializerPublicGetTable` | resolves the policy at `__init__`; per-INSTANCE `self.fields.pop('qr_credential')` when it can permit nothing, plus a per-row sentinel that `to_representation` removes. Never `_declared_fields` |
| `controllers/tables.py::get_tables_by_area` | takes `qr_policy`, **defaulting to None, which withholds**. Both branches now build rows through ONE `_grouped_table_row` — the duplication is what made "fix one branch, miss the other" possible |
| `misc_app/controllers/secretary.py::read()` | threads the request it ALREADY HELD (it passed it to `DinifyPaginator` and never to the serializer) into both the paginated and unpaginated branches, preserving any caller-supplied context. Secretary stays generic — it carries the request, it does not decide policy |
| `endpoints/restaurant_setup.py` | resolves the policy ONCE for the grouped read, after its own module gate |
| `endpoints/table_actions.py` | all FIVE ordinary response sites (seat, clear, transfer source, transfer destination, update-status) supply the verified request so fail-closed defaults do not regress responses that were never the exposure; transfer shares one resolved policy across both serializers |
| `endpoints/table_actions.py::_regenerate_qr` | gated on the SAME decision rather than on the route being off the delegated allowlist. It cannot strand an operator: the endpoint already required ordinary `tables` access at that restaurant, and the two resolvers read the same employment, lifecycle and override rows |

### What was NOT done
No migration, no schema change, no route added or removed, no widening or narrowing
of `ALLOWED_ROUTES` or `SETUP_READABLE_RECORDS`, no credential-expiry change, no new
minting endpoint, no delegation redesign, no authorization framework, and no
owner-only policy substituted for the existing role/module behaviour (a MANAGER and a
`restaurant_staff` member still receive the field — pinned).

## 5. What must NOT be done

- **Do not copy the `menu_policy` polarity.** See §4.1. It is the one mistake that
  looks like the fix.
- **Do not widen `ALLOWED_ROUTES` or `SETUP_READABLE_RECORDS`** — the direction of
  travel here is narrowing.
- **Do not "blank the field".** The contract is the key's ABSENCE.
- **Do not sign and then strip.** Withholding must mean the credential was never
  minted.
- **Do not make the credential expiring** to "solve" §1 property 3. Verification
  without expiry is deliberate: the credential is what a printed QR code carries, and
  a printed code cannot be reissued on a timer. `qr_version` is the revocation
  mechanism and it should stay the only one.
- **Do not start auditing ordinary delegated reads** to compensate. That contradicts
  the stated audit contract and would bury real decisions under page views.
- **Do not add a caller-supplied entitlement input** of any kind.

## 6. Where the evidence is

- `platform_admin_app/tests_delegated_qr_disclosure.py` — the converted suite: the
  route and permission controls, the three inverted containment regressions, the
  support-scope case, "the signer is never called", the two rebuilt residual-risk
  tests, the delegated-rotation refusal and the cross-tenant control.
- `restaurants_app/tests_qr_disclosure_boundary.py` — the ordinary paths (owner,
  manager, staff; flat and grouped; scan-correlated, not merely 200), the
  withhold-by-default builders, server-derived entitlement, no alternate disclosure,
  the Secretary plumbing on both branches, cross-request contamination, and the
  header/audit semantics.

**Negative controls, run and reverted.** Each mutation fails the relevant
regressions while the others hold:

| mutation | failures |
|---|---|
| remove the Secretary context propagation | 10, all ORDINARY-path |
| restore the permissive missing-context default | 11 |
| re-enable ONLY the grouped unassigned branch's signer | 3 |
| bypass the delegation veto, keep the module check | 8 |

## 7. RESIDUAL RISK — what containment does not do

**Containment stops FUTURE disclosure through these application response paths. It
revokes nothing.** A credential already obtained, or a diner session already
exchanged from one, is unaffected. Two things follow and neither may be softened:

- **The response headers are not a revocation and not proof of non-retention.** A
  delegated response carried `Cache-Control: no-store, private` and `Vary`, which
  INSTRUCT a compliant cache. RFC 9111 §5.2.2.5 states plainly that `no-store` is not
  a reliable privacy mechanism. It says nothing about what a recipient, a browser
  extension, a log, a tool or a non-compliant intermediary retained, and it must not
  be used to discount a bearer capability that was already delivered.
- **Rollback restores the disclosure.** This change is code-only and migration-free,
  so it is mechanically trivial to revert — and reverting it is not security-safe
  merely because the schemas match. Prefer a forward fix; an operational rollback
  across it needs an explicitly accepted containment/risk decision.

**The remedy for an already-disclosed credential is the existing per-table QR
rotation, and its cost is reprinting that table's physical code.** The measured
property is that a `qr_version` bump makes the old generation fail the verifier's
generation re-check on SUBSEQUENT validation — for the credential AND for a session
already minted from it (both carry the generation; pinned by
`tests_tenant_isolation_closure`). It does not erase copies, undo orders already
accepted, or prove that every request authorized before the rotation is cancelled;
those sequential resolver tests are not a concurrency guarantee.

See `DELEGATED_QR_OPERATOR_NOTE.md` for what grant/session evidence exists, the
limits of that evidence, and the rotation option. **No production or UAT record was
inspected, no real table was rotated and no grant was revoked** — that is a separate
operator decision requiring its own authorization.
