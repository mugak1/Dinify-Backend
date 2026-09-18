# TRIAGE — a delegated READ hands out live diner ordering authority

**Status: OPEN. Measured, not fixed. No delegation scope is changed by the D06
change that produced this file.**

This is the separate triage item raised alongside the D06 completion work. It is a
**delegation-scope question**, deliberately kept out of the D06 diff: closing it
changes what a delegated administrator may do, which is its own decision with its
own review and its own blast radius on the Admin surfaces that read tables.

Everything below was **measured against unmodified `main`** by
`platform_admin_app/tests_delegated_qr_disclosure.py` (8 tests, all passing, all
asserting today's behaviour and changing none of it). That file is a
CHARACTERIZATION: if one of its assertions starts failing, the exposure has been
closed or has moved, and the file is where to say which.

---

## 1. What it is

`SETUP_READABLE_RECORDS` admits `tables` to the delegated restaurant-setup GET, and
that read MINTS a QR credential per table. A QR credential is the **sole anonymous
authority** for a table: presenting it to `orders/journey/table-scan/` mints a diner
table session, and a table session is what places orders.

So a delegated administrator holding the **`view`** scope — the read-only one —
receives working anonymous ordering authority for every table in the restaurant, as
an ordinary consequence of opening the tables list.

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

## 4. Containment options

Stated with their costs. **None of these is implemented.**

### (a) Withhold the field from a delegated reader — RECOMMENDED
Suppress `qr_credential` in both builders when the request carries a delegation
context, leaving every other table field intact.

- Delegated admins keep the whole tables view (numbers, areas, status, capacity,
  floor plan) and lose only the ability to mint diner authority — which is not
  something support work needs.
- The owner-facing read is byte-identical, so the portal Setup View is untouched.
- **Cost:** it must be applied at BOTH builders, and it needs a real signal to key
  on. The delegation context is already on the request
  (`platform_admin_app.delegated_middleware`), but `SerializerPublicGetTable` is
  constructed by `Secretary` with no serializer context today, so this is a small
  plumbing change rather than a one-line edit — which is precisely why it is not
  being smuggled into a D06 diff.
- **Hazard to avoid:** do not implement it as "blank the field". A key present and
  empty and a key absent are different facts to a client; pick one and pin it.

### (b) Remove `tables` from `SETUP_READABLE_RECORDS`
Simplest and strictest.

- **Cost:** a delegated admin then cannot see the tables at all — not the floor plan,
  not table status, not which table an order belongs to. That is a real loss for the
  support case delegation exists for, and it is a bigger behaviour change than the
  exposure warrants.

### (c) Bind credential minting to the presence of a table-management intent
Mint only on the write paths that actually need to print a code (regenerate-qr),
never on a list read, for ANY principal.

- Strictly the cleanest boundary: a list read stops being a credential factory.
- **Cost:** the portal Setup View currently reloads credentials from the flat list —
  that is exactly why the field was added there — so this needs a frontend change
  first, in the other repository, and a deploy ordering. Largest of the three.

### Interim, requires no code
- A QR regeneration for the affected tables is the existing revocation and remains
  available at any time.
- Grants are already time-boxed, reason-required and elevation-gated to mint, so the
  population who can reach this is small and already recorded at grant time.

## 5. What must NOT be done

- **Do not change delegation scope in the D06 diff.** That was an explicit
  constraint on the work that found this.
- **Do not widen `ALLOWED_ROUTES` or `SETUP_READABLE_RECORDS`** as part of any fix —
  the direction of travel here is narrowing.
- **Do not fix only the serializer.** See §3.
- **Do not make the credential expiring** to "solve" property 3. Verification without
  expiry is deliberate: the credential is what a printed QR code carries, and a
  printed code cannot be reissued on a timer. `qr_version` is the revocation
  mechanism and it should stay the only one.
- **Do not start auditing ordinary delegated reads** to compensate. That contradicts
  the stated audit contract and would bury real decisions under page views.

## 6. Where the evidence is

`platform_admin_app/tests_delegated_qr_disclosure.py` — the route, the permission,
the disclosure on both reads, the exchange through the real scan route, survival past
`delegation/end/`, revocation by `qr_version`, and the cross-tenant control.
