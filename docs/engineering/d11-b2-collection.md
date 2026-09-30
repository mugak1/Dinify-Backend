# D11 B2-C: OTP collection

This change collects evidence and does nothing else. It adds no rate limit, no shadow
decision, no admission decision and no refusal. The rows are used only for their own
provenance, finalization and cleanup; nothing reads them to decide anything. D11 stays
open: policy, enforcement and operational acceptance are later decisions.

## What is recorded

Two tables, written only by `users_app/otp_accounting.py`. Migration
`users_app/0015_otp_accounting` (it follows `0014_customer_access_state`) creates them.
It is expand-only: two `CreateModel`s, no backfill, no `RunPython`, and no existing
table is altered.

| table | one row per | columns |
|---|---|---|
| `otp_issuances` | challenge written by `OtpManager.make_otp` | `id` (the `UserOtp` id), `subject_key`, `destination_key`, `origin`, `state`, `created_at`, `finalized_at` |
| `otp_verification_failures` | wrong code actually compared against a live challenge | `failed_at`, `subject_key`, `origin`, `bound_redemption`, `issuance_id` |

- **No foreign keys.** A challenge row is deleted when it is replaced, and a user may be
  deleted. The ledger outlives both.
- **Keys are pseudonymous, not anonymous.** A row holds no phone, e-mail, code, OTP
  hash, salt, purpose, message, IP or exception text, but its keys are stable per
  person, so rows about one person can be grouped and a user key names the account.
  - A subject is `u:<User UUID>` or a phone key.
  - A destination is only a phone key.
  - A phone key is `p1:` followed by the HMAC-SHA256 of the canonical number, under the
    existing OTP pepper, with the fixed label `dinify:otp-accounting:phone:v1` prefixed.
    The way the pepper is derived is unchanged. The OTP HASH (on `user_otps`) is a
    different value and is not stored here; the phone HMAC key is.
  - **A pepper rotation splits phone grouping.** Rotating `OTP_HMAC_PEPPER`, or
    changing `SECRET_KEY` while the pepper is derived from it, changes every phone key
    from then on, so old and new rows for one number no longer match. No rotation or
    reconciliation is implemented.
- **What the database enforces.** `CHECK` constraints enforce the key shapes, the
  closed vocabularies and the `pending` ⇔ no `finalized_at` consistency. They cannot
  check that a producer's claim is true (for example, that a key belongs to the person
  it names).
- **Coverage has gaps, and they are deliberate.** A legacy phone that cannot be
  canonicalised gives a NULL key and a fixed log category; it never gives a raw value
  and never causes a refusal. A wrong-code observation is best-effort: if it fails, the
  verification is unaffected and no row is written.
- **Timestamps come from the database clock** (`STATEMENT_TIMESTAMP()`).

## Origin

The server path that asked for the code sets the origin:

- `password_login` (login)
- `reset_initiation` (starting a password reset)
- `owner_claim_challenge` (owner-claim challenge)
- `login_resend` (a resend admitted under a login anchor)
- `resend_request` (any other resend)
- `unattributed` (any other caller, or an unknown value)

A failure copies the origin of its challenge's issuance. A challenge written before
this migration has no issuance, so its failures record `unrecorded`.
`bound_redemption` is true only when the verifier was bound to the `owner-claim`
purpose, which only redemption does.

## Transactions and locks

Two steps, and only the second is a transaction.

1. **The caller's eligibility preflight takes no lock.** The owner-claim challenge, for
   example, resolves the invitation and checks every claim condition in autocommit,
   holding nothing.
2. **`make_otp` then opens one short `transaction.atomic(durable=True)`** that:
   1. takes the user's row `FOR KEY SHARE` (user-backed challenges only);
   2. deletes the challenge being replaced;
   3. inserts the new challenge;
   4. inserts its `pending` issuance row.

   It commits before any SMS or e-mail sender is entered.

Rules:

- **Why the key share comes first.** The challenge's foreign key to `users` is deferred,
  so without it the `users` row would be reached only at COMMIT, after the old challenge
  was already locked. That is the reverse of redemption's `users` then `user_otps`
  order, and the two would deadlock.
- **Nothing is held across transport.** No `Restaurant` row or ownership barrier, no
  `User` or `UserOtp` lock and no open transaction exists while a sender runs.
- **A caller's transaction is refused.** `durable=True` raises before any write or send.
- **A ledger write failure sends nothing.** The whole transaction rolls back, and
  `make_otp` returns `False`, which callers already handle as a delivery failure.

After the send, one conditional update moves the row out of `pending`. It runs once,
outside any transaction, and never changes the return value.

| environment | outcome | state |
|---|---|---|
| dev | no e-mail recipient | `not_dispatched` |
| dev | e-mail possible (asynchronous) | `unknown` |
| test or prod | the sender reported acceptance | `accepted` |
| test or prod | `False`, or a sender raised | `unknown` |

`accepted` means the existing sender REPORTED acceptance (for SMS, the gateway said
`OK`). It does not mean the code reached a handset. The intentional dev OTP `1234`, the
message format, the asynchronous dev notifications and every return and failure
contract are unchanged.

A wrong code is recorded in a savepoint after the attempt counter is saved.

- **Ordinary failure** (reading the issuance's origin, or writing the row): only the
  observation is rolled back. The invalid answer and the counter stand — for an owner
  claim, the invitation's `claim_failed_attempts` too — and a fixed category is logged.
- **Lost connection:** the savepoint cannot contain it, so the error propagates. Nothing
  claims a counter write that cannot commit.

## Retention and cleanup

A row strictly older than seven days (by its own timestamp) is eligible for deletion,
in any state, `pending` included. A row exactly at the boundary is kept. Seven days is
eligibility, not a guaranteed maximum age or table size: rows go only when one of these
runs.

- **After each issuance's finalization attempt:** one bounded, oldest-first batch per
  table (`OPPORTUNISTIC_PRUNE_BATCH`, 100). A finalization error does not prevent the
  attempt. It runs outside the issuance transaction and holds no OTP or `User` lock.
  Errors are logged under a fixed category and never change what `make_otp` returned.
- **`python manage.py prune_otp_accounting [--batch-size N] [--max-batches M]`:**
  `--batch-size` 1–1000 (default 500), `--max-batches` 1–10000 (default 100), no
  retention override. Nothing schedules it.
  - It prints one line per batch, then a total per table. **Every count printed has
    committed**: it refuses to run inside a caller's transaction (an atomic block, or
    autocommit turned off), and each table's delete commits before the next statement.
  - `complete: no eligible rows found when last checked` only after a short batch AND a
    fresh bounded check found nothing eligible. A short batch alone is not enough (a
    concurrent cleanup can take the rows it selected), and rows keep becoming eligible.
  - `batch limit reached: eligible rows may remain` when `--max-batches` runs out.
  - **On failure it exits 1.** Confirmed counts are printed first, the failed
    statement's outcome is reported as unknown, and the message carries a fixed
    category. No database text, key, id, timestamp or SQL is printed, and the
    database exception is not chained into the error.
  - **If opening the connection for the initial transaction-state check fails,** it
    exits 1 the same way, prints zero deletions and says no cleanup statement was
    attempted.

## Rollback

Rolling back to code without this change keeps the two tables and their rows (there is
no reverse migration, and none is authorized). The old code neither reads nor writes
them, so both collection and automatic cleanup stop. Deleting what was collected would
need an operator to run cleanup under separate authority.

## Deployment

Merging to `main` auto-deploys to UAT through the existing legacy workflow, which runs
migrations. For #352 the deploy log (run 36568672986) records target `ef376b2`,
`Applying users_app.0015_otp_accounting... OK`, the routing probe at HTTP 405 and the
database probe at HTTP 200 / `connected`, around 12:32 UTC on 2026-09-29. The #353
deploy (run 36573040494) records `ef302f3` and `No migrations to apply`.

Those are workflow observations. They are not observed rows, a first-record time or a
verified runtime identity. Collection happens only when the new code runs an OTP path
against the migrated schema; migrating or merging does not by itself start it.
