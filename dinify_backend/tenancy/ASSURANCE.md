# Tenant-relation ratchet — assurance boundary (TENANT-STRUCT-00)

This document states **precisely** what the tenant-relation tooling in
`dinify_backend/tenancy/` guarantees and — more importantly — what it does **not**.
The mechanism is useful, but it is easy to mistake its green checkmark for "tenant
isolation is proven." It is not. Read this before trusting the ratchet.

## What is GUARANTEED (machine-checked, fail-closed)

1. **Every writable DRF relation on a discovered serializer is consciously
   accounted for.** `tests_relation_classification.py` fails unless each writable
   relational field on a project `ModelSerializer` is either *classified* in
   `Meta.tenant_relations` (`SameTenant` / `ServerDerived` / `GlobalRelation`) or
   listed in `baseline.txt`. A brand-new writable relation cannot be silently
   added to the baseline — it must be classified.
2. **Discovery cannot be evaded by module name.** Serializer-defining modules are
   found by AST (`discovery._serializer_defining_modules`), not by a `"serializer"`
   path substring, so a `ModelSerializer` in `views.py` / `api.py` / a controller is
   still discovered. A serializer module that **cannot be imported** is collected in
   `import_serializer_modules()`'s failure map and **fails the meta-test closed**
   (`KNOWN_UNIMPORTABLE`), because a serializer that cannot be introspected must not
   escape the ratchet.
3. **`fields='__all__'` is policed, not free.** Every serializer exposing `__all__`
   (or `Meta.exclude`) must be an explicit, review-visible exception in
   `all_fields_policy.py`. A **new write** serializer on `__all__` fails — it must use
   an explicit field list (else future model fields, including tenant FKs, become
   client-writable with no review). Read/archival `__all__` is allowed (serialize-out
   only) and enumerated there.
4. **The baseline may only shrink — on PRs and on pushes.** `check_ratchet` compares
   the committed baseline against the base and fails on any addition. The base is
   event-aware (`resolve_base_ref`): a PR compares against its target branch; a
   **push compares against `github.event.before`** (the pre-push commit), not the
   already-advanced branch tip. In CI, an unreadable base **fails closed**.
5. **A classification is well-formed.** `SameTenant` paths must resolve against the
   related model (typo catch); `GlobalRelation` needs a non-empty reason; only the
   three constrained types are permitted in `tenant_relations`.

## What is only CLASSIFIED (a conscious label — NOT a proof)

- A `SameTenant(path)` declares intent and typo-checks the path. **It enforces
  nothing at runtime by itself.** Real enforcement lives in serializer `validate()`
  via `restaurants_app/controllers/tenant_scope.py`, proven only by **two-tenant
  behavioural tests**. To stop a classification from masquerading as a guarantee,
  every *production* `SameTenant` must declare `verified_by` — a resolvable link to
  the behavioural test that exercises it. The meta-test checks the link resolves; it
  does not (and cannot) re-run the proof.
- `GlobalRelation` / `ServerDerived` are review assertions, not verified facts.

## What remains BASELINED (legacy debt)

- `baseline.txt` currently holds **117** writable relations that are *not yet
  classified*. This is **not a count of confirmed vulnerabilities** — most are
  read-only `SerArc*` archival serializers or write serializers already runtime-scoped
  in `validate()`. The count only measures classification progress. Entries are
  removed as domains migrate; the file may only shrink.

## What remains OUTSIDE discovery

- **Non-FK tenant references** — UUIDs in JSON fields, raw UUID arrays, denormalised
  restaurant ids/snapshots, direct `Model.objects.create` writes, dynamic model
  dispatch, and `(restaurant, …)` cache/idempotency keys — are **invisible to the FK
  meta-test**. They are tracked separately in
  [`non_fk_tenant_inventory.py`](./non_fk_tenant_inventory.py), which is an **audit
  list, not a proof** (every entry is `pending-audit`). Do not assume the FK ratchet
  says anything about them.
- A serializer built by a metaclass/dynamically with no `*Serializer` base in source
  can still slip past AST detection; such patterns are exotic and would need a manual
  addition here if introduced.

## One-line summary

The ratchet proves **coverage and conscious classification** of DRF writable
relations and **forbids new unclassified/baselined ones**. It does **not** prove
tenant isolation, does **not** cover non-FK identifiers, and its baseline count is
**not** a vulnerability count. Isolation is proven only by two-tenant behavioural
tests.
