# D11 B2-C: OTP collection

This change collects evidence and does nothing else. It adds no rate limit, no shadow
decision and no refusal. D11 stays open: policy, enforcement and operational acceptance
are later decisions, and nothing in these tables feeds any of them yet.

## What is recorded

Two tables, written only by `users_app/otp_accounting.py`. Migration
`users_app/0015_otp_accounting` creates them. It is expand-only: two `CreateModel`s, no
backfill, no `RunPython`, and no existing table is altered.

| table | one row per | columns |
|---|---|---|
| `otp_issuances` | challenge written by `OtpManager.make_otp` | `id` (the `UserOtp` id), `subject_key`, `destination_key`, `origin`, `state`, `created_at`, `finalized_at` |
| `otp_verification_failures` | wrong code actually compared against a live challenge | `failed_at`, `subject_key`, `origin`, `bound_redemption`, `issuance_id` |

- **No foreign keys.** A challenge row is deleted when it is replaced, and a user may be
  deleted. The ledger outlives both.
- **No raw identifiers.** There is no phone, e-mail, code, hash, salt, purpose, message,
  IP or exception text in either table. Database `CHECK` constraints refuse any key that
  is not in the formats below.
  - A subject is `u:<User UUID>` or a phone key.
  - A destination is only a phone key.
  - A phone key is `p1:` followed by the HMAC-SHA256 of the canonical number, under the
    existing OTP pepper, with the fixed label `dinify:otp-accounting:phone:v1` prefixed.
    The way the pepper is derived is unchanged.
- **Unusable numbers are recorded as NULL.** A legacy phone that cannot be
  canonicalised gives a NULL key, and a fixed category is logged. It never gives a raw
  value, and it never causes a refusal.
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

`make_otp` writes three things in one transaction, opened with
`transaction.atomic(durable=True)`:

1. the delete of the challenge being replaced;
2. the new challenge;
3. its `pending` issuance row.

This transaction commits before any SMS or e-mail sender is entered. Rules:

- **User-backed challenges lock the user first.** The transaction takes the user's row
  `FOR KEY SHARE` before the delete. The challenge's foreign key to `users` is deferred,
  so without this step the lock would be taken only at COMMIT, after the old challenge
  was already locked. That is the reverse of redemption's `users` then `user_otps`
  order, and the two would deadlock.
- **A caller's transaction is refused.** `durable=True` raises before any write or send.
- **A ledger write failure sends nothing.** The whole transaction rolls back, and
  `make_otp` returns `False`, which callers already handle as a delivery failure.

After the send, one conditional update moves the row out of `pending`. It runs once,
outside any transaction, and never changes the return value.

| environment | outcome | state |
|---|---|---|
| dev | no e-mail recipient | `not_dispatched` |
| dev | e-mail possible (asynchronous) | `unknown` |
| test or prod | delivery established | `accepted` |
| test or prod | `False`, or a sender raised | `unknown` |

The dev OTP `1234`, the message format and the asynchronous dev notifications are
unchanged.

A wrong code is recorded in a savepoint after the attempt counter is saved.

- **Ordinary failure:** only the observation is rolled back. The invalid answer and the
  counter stand, and a fixed category is logged.
- **Lost connection:** the savepoint cannot contain it, so the error propagates. Nothing
  claims a counter write that cannot commit.

## Retention and cleanup

A row strictly older than seven days is eligible for deletion, in any state, `pending`
included. A row exactly at the boundary is kept. Eligibility is not a deadline: rows go
only when one of these runs.

- **After each finalized issuance:** one bounded, oldest-first batch per table
  (`OPPORTUNISTIC_PRUNE_BATCH`), outside any transaction. Errors are logged under a
  fixed category.
- **`python manage.py prune_otp_accounting [--batch-size N] [--max-batches M]`:** the
  limits are finite, the output is counts only, and there is no retention override.
  Nothing schedules it.

## Deployment note

Merging to `main` auto-deploys the backend to UAT through the existing workflow. That
deploy runs this migration, so collection starts on UAT at merge. No production
scheduler, backfill or live inspection is part of this change.
